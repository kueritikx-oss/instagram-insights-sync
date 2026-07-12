#!/usr/bin/env python3
"""毎日のAddy考察レビュー — 毎日 JST 22:30 に GitHub Actions で実行。

目的: テンプレ生成の考察(fill_*_analysis.py 系)に加えて、判断OSのAddyが1日1回、
その日にメトリクスが確定した投稿を4媒体横断でレビューし「明日への1手」を出す。

処理フロー:
  1. 収集: 当日(JST)に 1d/7d メトリクスが確定した投稿を IG/Threads/X/note から集める
     (各シートの取得日時/分析更新日時列が当日の行)。1投稿=1行のダイジェストに圧縮。
  2. Addy 1往復: 数字+テンプレ考察を渡して
     ①テンプレ考察の見落とし・誤読の指摘 ②今日一番の学び ③明日への具体的1手
     ④CSP復活モード(IG週2・リスト3-5/日)への影響 を構造化フォーマットで受け取る。
  3. 出力: (a) Discord embed「🤖 Addyデイリー考察 M/D」
           (b) 投稿との対応が識別子で確実に取れた場合のみ、その投稿の考察・仮説セルの
               末尾に「\\n【Addy M/D】一言」を追記(既存テンプレ考察は消さない)。
               対応が曖昧/重複ならシート書き込みはスキップしDiscordのみ(壊すより安全)。

degrade設計(考察本体は別系で生成済みのため、Addy側の障害では落とさない):
  - 当日確定 0件 → Discordに1行だけ送って exit 0(Addyは呼ばない=quota節約)
  - ADDNESS_API_KEY 未設定 / Addy障害 → Discordに接続失敗1行で exit 0
  - Discord送信失敗(--no-notify でない時) → exit 2(沈黙成功禁止)

設計原則(weekly_sns_rollup.py と同一):
  - 列番号ハードコード禁止。ヘッダー行から列名で動的解決。
  - values().append() 禁止。書き込みは対象セルへの明示 update のみ。
  - 書き込み後は読み戻し検証。
  - 機密(SAキー・Webhook URL・Addy APIキー)はログに一切出さない。

環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON  Service Account JSON文字列(GHA secret)
  GOOGLE_SERVICE_ACCOUNT_FILE  もしくはSAキーのファイルパス
  DISCORD_WEBHOOK              配信先 Webhook(未設定なら送信スキップ)
  ADDNESS_API_KEY              Addyレビュー用(未設定ならdegrade)

usage:
  python3 scripts/daily_addy_review.py [--dry-run] [--no-notify] [--date YYYY-MM-DD]
    --dry-run  : スプシへの書き込みをしない(収集・Addy往復・プレビューは実行)
    --no-notify: Discord送信を抑止(内容はstdoutにプレビュー)
    --date     : 対象日を指定(既定=今日JST。直近の確定日でのテスト用)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))     # sheet_column_map (IG列解決)
sys.path.insert(0, str(SCRIPTS_DIR))   # weekly_sns_rollup (共通ヘルパー流用)

from sheet_column_map import load_column_map  # noqa: E402
from weekly_sns_rollup import (  # noqa: E402
    IG_SHEET,
    NOTE_SHEET,
    THREADS_SHEET,
    X_SHEET,
    _addy_chat,
    _addy_create_thread,
    cell,
    col_letter,
    find_col,
    get_values,
    parse_date,
    parse_num,
    sheets_service,
)

JST = timezone(timedelta(hours=9))

IG_TAB = "Instagram投稿毎データ"
THREADS_TAB = "Threads投稿毎データ"
X_TAB = "X投稿毎データ"
NOTE_TAB = "記事一覧"

ANALYSIS_HEADER = "考察・仮説"
MAX_POSTS_TO_ADDY = 20          # 1往復に載せる上限(超過分は件数のみ伝える)
ANALYSIS_EXCERPT = 130          # テンプレ考察をAddyに渡す際の1投稿あたり文字数
ADDY_COMMENT_MAX = 200          # シート追記するAddy一言の上限


# ================================================================ 共通ヘルパー
def fmtn(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}"


def is_target_date(raw: str, target) -> bool:
    """取得日時セル(例 '2026/07/12 22:05')の日付部分が対象日(JST)と一致するか。"""
    d = parse_date(raw)
    return bool(d) and d.date() == target


def find_exact(header_rows: list[list[str]], name: str) -> int | None:
    """ヘッダー行(複数)から完全一致のみで列indexを返す(考察・仮説など一意名用)。"""
    for row in header_rows:
        for i, c in enumerate(row):
            if str(c).strip() == name:
                return i
    return None


def section_ranges(row1: list[str]) -> list[tuple[int, str]]:
    """行1(セクション行)を (開始index, セクション名) のリストに。"""
    return [(i, str(v).strip()) for i, v in enumerate(row1) if str(v).strip()]


def find_in_section(row1: list[str], name_row: list[str], name: str,
                    section: str | None, nth: int = 1) -> int | None:
    """行1のセクション制約つきで、列名行から name 完全一致の nth 番目を返す。"""
    if section:
        starts = section_ranges(row1)
        lo, hi = None, len(name_row)
        for j, (idx, sec) in enumerate(starts):
            if section in sec:
                lo = idx
                hi = starts[j + 1][0] if j + 1 < len(starts) else len(name_row)
                break
        if lo is None:
            return None
    else:
        lo, hi = 0, len(name_row)
    hits = [i for i in range(lo, min(hi, len(name_row)))
            if str(name_row[i]).strip() == name]
    return hits[nth - 1] if len(hits) >= nth else None


def excerpt(s: str, limit: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ================================================================ collectors
# Post dict: media/num/window/hook/cta/metrics/analysis/sheet_id/tab/row/analysis_col
def collect_ig(svc, target) -> list[dict]:
    col = load_column_map(svc, IG_SHEET, IG_TAB)
    for req in ("date", "number", "captured_at_1d", "captured_at_7d"):
        if req not in col:
            raise RuntimeError(f"IG 必須列が見つからない: {req}")
    hdr = get_values(svc, IG_SHEET, f"'{IG_TAB}'!1:3")
    c_analysis = find_exact(hdr, ANALYSIS_HEADER)
    if c_analysis is None:
        raise RuntimeError(f"IG {ANALYSIS_HEADER} 列が見つからない")

    rows = get_values(svc, IG_SHEET, f"'{IG_TAB}'!A4:CT1000")
    out = []
    for i, r in enumerate(rows):
        rownum = 4 + i
        for window, at_key, prefix in (("1d", "captured_at_1d", "_1d"),
                                       ("7d", "captured_at_7d", "_7d")):
            if not is_target_date(cell(r, col.get(at_key)), target):
                continue
            sfx = prefix[1:]  # "1d"/"7d"
            metrics = (
                f"リーチ{fmtn(parse_num(cell(r, col.get(f'reach_{sfx}'))))}"
                f"・保存{fmtn(parse_num(cell(r, col.get(f'saved_{sfx}'))))}"
                f"・いいね{fmtn(parse_num(cell(r, col.get(f'likes_{sfx}'))))}"
                f"・シェア{fmtn(parse_num(cell(r, col.get(f'shares_{sfx}'))))}"
            )
            num = cell(r, col["number"])
            if not num:
                continue
            out.append({
                "media": "IG", "num": num, "window": window,
                "hook": cell(r, col.get("tag_hook")),
                "cta": cell(r, col.get("post_type")) or cell(r, col.get("tag_cta")),
                "metrics": metrics,
                "analysis": cell(r, c_analysis),
                "sheet_id": IG_SHEET, "tab": IG_TAB,
                "row": rownum, "analysis_col": c_analysis,
            })
    return out


def collect_threads(svc, target) -> list[dict]:
    hdr = get_values(svc, THREADS_SHEET, f"'{THREADS_TAB}'!1:3")
    if len(hdr) < 3:
        raise RuntimeError("Threads ヘッダーが3行未満")
    row1, row3 = hdr[0], hdr[2]
    sec_m, sec_t = "Threadsメトリクス", "分類タグ"
    col = {
        "date": find_col(hdr, ["日付"]),
        "num": find_col(hdr, ["番号"]),
        "views": find_in_section(row1, row3, "views", sec_m),
        "likes": find_in_section(row1, row3, "いいね", sec_m),
        "reposts": find_in_section(row1, row3, "リポスト数", sec_m),
        "er": find_in_section(row1, row3, "ER%", sec_m),
        "at_1d": find_in_section(row1, row3, "1d取得日時", sec_m),
        "views_7d": find_exact(hdr, "7d_views"),
        "at_7d": find_exact(hdr, "7d取得日時"),
        "hook": find_in_section(row1, row3, "フック型", sec_t),
        "cta": find_in_section(row1, row3, "CTA型", sec_t),
        "analysis": find_exact(hdr, ANALYSIS_HEADER),
    }
    for req in ("date", "num", "at_1d", "analysis"):
        if col[req] is None:
            raise RuntimeError(f"Threads 必須列が見つからない: {req}")

    rows = get_values(svc, THREADS_SHEET, f"'{THREADS_TAB}'!A4:BH2000")
    out = []
    for i, r in enumerate(rows):
        rownum = 4 + i
        num = cell(r, col["num"])
        if not num:
            continue
        if is_target_date(cell(r, col["at_1d"]), target):
            metrics = (
                f"views{fmtn(parse_num(cell(r, col['views'])))}"
                f"・いいね{fmtn(parse_num(cell(r, col['likes'])))}"
                f"・リポスト{fmtn(parse_num(cell(r, col['reposts'])))}"
                f"・ER{cell(r, col['er']) or '—'}"
            )
            out.append({
                "media": "Threads", "num": num, "window": "1d",
                "hook": cell(r, col["hook"]), "cta": cell(r, col["cta"]),
                "metrics": metrics, "analysis": cell(r, col["analysis"]),
                "sheet_id": THREADS_SHEET, "tab": THREADS_TAB,
                "row": rownum, "analysis_col": col["analysis"],
            })
        if col["at_7d"] is not None and is_target_date(cell(r, col["at_7d"]), target):
            metrics = f"7d_views{fmtn(parse_num(cell(r, col['views_7d'])))}"
            out.append({
                "media": "Threads", "num": num, "window": "7d",
                "hook": cell(r, col["hook"]), "cta": cell(r, col["cta"]),
                "metrics": metrics, "analysis": cell(r, col["analysis"]),
                "sheet_id": THREADS_SHEET, "tab": THREADS_TAB,
                "row": rownum, "analysis_col": col["analysis"],
            })
    return out


def collect_x(svc, target) -> list[dict]:
    hdr = get_values(svc, X_SHEET, f"'{X_TAB}'!1:3")
    col = {
        "date": find_exact(hdr, "日付"),
        "num": find_exact(hdr, "番号"),
        "hook": find_col(hdr, ["フック"]),
        "cta": find_exact(hdr, "CTA型"),
        "v1": find_exact(hdr, "1d_views"),
        "l1": find_exact(hdr, "1d_likes"),
        "rt1": find_exact(hdr, "1d_RT"),
        "rp1": find_exact(hdr, "1d_リプ"),
        "at1": find_exact(hdr, "1d取得日時"),
        "v7": find_exact(hdr, "7d_views"),
        "l7": find_exact(hdr, "7d_likes"),
        "at7": find_exact(hdr, "7d取得日時"),
        "analysis": find_exact(hdr, ANALYSIS_HEADER),
    }
    for req in ("date", "num", "at1", "analysis"):
        if col[req] is None:
            raise RuntimeError(f"X 必須列が見つからない: {req}")

    rows = get_values(svc, X_SHEET, f"'{X_TAB}'!A3:AN2000")
    out = []
    for i, r in enumerate(rows):
        rownum = 3 + i
        num = cell(r, col["num"])
        if not num or not parse_date(cell(r, col["date"])):
            continue  # テンプレ行/空行
        if is_target_date(cell(r, col["at1"]), target):
            metrics = (
                f"views{fmtn(parse_num(cell(r, col['v1'])))}"
                f"・いいね{fmtn(parse_num(cell(r, col['l1'])))}"
                f"・RT{fmtn(parse_num(cell(r, col['rt1'])))}"
                f"・リプ{fmtn(parse_num(cell(r, col['rp1'])))}"
            )
            out.append({
                "media": "X", "num": num, "window": "1d",
                "hook": cell(r, col["hook"]), "cta": cell(r, col["cta"]),
                "metrics": metrics, "analysis": cell(r, col["analysis"]),
                "sheet_id": X_SHEET, "tab": X_TAB,
                "row": rownum, "analysis_col": col["analysis"],
            })
        if col["at7"] is not None and is_target_date(cell(r, col["at7"]), target):
            metrics = (
                f"7d_views{fmtn(parse_num(cell(r, col['v7'])))}"
                f"・7d_いいね{fmtn(parse_num(cell(r, col['l7'])))}"
            )
            out.append({
                "media": "X", "num": num, "window": "7d",
                "hook": cell(r, col["hook"]), "cta": cell(r, col["cta"]),
                "metrics": metrics, "analysis": cell(r, col["analysis"]),
                "sheet_id": X_SHEET, "tab": X_TAB,
                "row": rownum, "analysis_col": col["analysis"],
            })
    return out


def collect_note(svc, target) -> list[dict]:
    hdr = get_values(svc, NOTE_SHEET, f"'{NOTE_TAB}'!A1:AZ1")
    col = {
        "num": find_exact(hdr, "#"),
        "title": find_col(hdr, ["タイトル"]),
        "cat": find_col(hdr, ["カテゴリ"]),
        "pv": find_exact(hdr, "PV") if find_exact(hdr, "PV") is not None else find_col(hdr, ["PV"]),
        "suki": find_col(hdr, ["スキ"]),
        "pv1": find_exact(hdr, "1d_PV"),
        "pv7": find_exact(hdr, "7d_PV"),
        "updated": find_exact(hdr, "分析更新日時"),
        "analysis": find_exact(hdr, ANALYSIS_HEADER),
    }
    for req in ("num", "title", "updated", "analysis"):
        if col[req] is None:
            raise RuntimeError(f"note 必須列が見つからない: {req}")

    rows = get_values(svc, NOTE_SHEET, f"'{NOTE_TAB}'!A2:AZ500")
    out = []
    for i, r in enumerate(rows):
        rownum = 2 + i
        num = cell(r, col["num"])
        if not num or not is_target_date(cell(r, col["updated"]), target):
            continue
        metrics = (
            f"累計PV{fmtn(parse_num(cell(r, col['pv'])))}"
            f"・スキ{fmtn(parse_num(cell(r, col['suki'])))}"
            f"・1dPV{fmtn(parse_num(cell(r, col['pv1'])))}"
            f"・7dPV{fmtn(parse_num(cell(r, col['pv7'])))}"
        )
        out.append({
            "media": "note", "num": num, "window": "分析更新",
            "hook": excerpt(cell(r, col["title"]), 24),
            "cta": cell(r, col["cat"]),
            "metrics": metrics, "analysis": cell(r, col["analysis"]),
            "sheet_id": NOTE_SHEET, "tab": NOTE_TAB,
            "row": rownum, "analysis_col": col["analysis"],
        })
    return out


# ================================================================ Addy 1往復
def post_key(p: dict) -> str:
    return f"{p['media']}#{p['num']}"


def round_robin_by_media(posts: list[dict]) -> list[dict]:
    """媒体間で公平に並べ替え(上限カット時に1媒体が全滅しないように)。"""
    buckets: dict[str, list[dict]] = {}
    for p in posts:
        buckets.setdefault(p["media"], []).append(p)
    out = []
    for i in range(max((len(b) for b in buckets.values()), default=0)):
        for media in ("IG", "Threads", "X", "note"):
            b = buckets.get(media, [])
            if i < len(b):
                out.append(b[i])
    return out


def build_digest(posts: list[dict]) -> str:
    lines = []
    for p in posts[:MAX_POSTS_TO_ADDY]:
        label = "/".join(excerpt(x, 30) for x in (p["hook"], p["cta"]) if x) or "-"
        lines.append(
            f"{post_key(p)} [{p['window']}] {label}\n"
            f"  数字: {p['metrics']}\n"
            f"  テンプレ考察: {excerpt(p['analysis'], ANALYSIS_EXCERPT) or '(未生成)'}"
        )
    if len(posts) > MAX_POSTS_TO_ADDY:
        lines.append(f"(他{len(posts) - MAX_POSTS_TO_ADDY}件は省略)")
    return "\n".join(lines)


def ask_addy(date_label: str, digest: str, n_posts: int) -> tuple[str, str]:
    """Addyに1往復でデイリーレビューを依頼。戻り値 (response, thread_id)。
    キー未設定/失敗は呼び出し元でdegrade処理するため例外をそのまま上げる。"""
    api_key = os.environ.get("ADDNESS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ADDNESS_API_KEY 未設定")
    thread_id = _addy_create_thread(api_key, f"daily_sns_review_{date_label}")
    prompt = (
        f"今日({date_label})の投稿成績デイリーレビュー。本日1d/7dメトリクスが確定した"
        f"{n_posts}投稿の数字とテンプレ自動考察を渡す。判断OSとして横断レビューして。\n\n"
        "必ず次のフォーマットで返答して(見出しはそのまま・他の文章は不要):\n"
        "【学び】今日一番の学びを1つ(2行以内)\n"
        "【明日の1手】明日〜あさっての投稿への具体的な1手を1つ(3行以内)\n"
        "【CSP影響】CSP復活モード(IG週2本・リスト3-5件/日)への影響(2行以内)\n"
        "【投稿別】\n"
        "各投稿につき1行、必ず行頭を識別子から始めて「識別子: 一言」形式で"
        "(識別子は渡したもの(例 X#X-113)をそのままコピー、一言は60字以内、箇条書き記号や装飾は不要)。\n"
        "一言にはテンプレ考察の見落とし・誤読があればその指摘を優先して入れる。\n\n"
        f"--- 本日確定の投稿データ ---\n{digest}"
    )
    resp = _addy_chat(api_key, thread_id, prompt)
    if not resp:
        raise RuntimeError("Addy応答が空")
    return resp, thread_id


def parse_addy_response(resp: str) -> tuple[dict[str, str], dict[str, str]]:
    """Addy応答を (セクションdict, 投稿別一言dict) にパース。
    セクション: learn/next_move/csp。投稿別: {識別子: 一言}。"""
    sections = {"learn": "", "next_move": "", "csp": ""}
    heads = {"【学び】": "learn", "【明日の1手】": "next_move", "【CSP影響】": "csp"}
    marks = sorted(
        [(m.start(), h) for h in list(heads) + ["【投稿別】"]
         for m in re.finditer(re.escape(h), resp)]
    )
    for j, (pos, head) in enumerate(marks):
        end = marks[j + 1][0] if j + 1 < len(marks) else len(resp)
        body = resp[pos + len(head):end].strip()
        if head in heads:
            sections[heads[head]] = body

    per_post: dict[str, str] = {}
    for line in resp.splitlines():
        # 箇条書き記号・markdown装飾・番号つきリストに耐性を持たせる
        m = re.search(
            r"((?:IG|Threads|X|note)\s*#\s*[A-Za-z0-9\-_]+)[\s*_`]*[:：][\s*_]*(.+)$",
            line.strip(),
        )
        if m:
            key = re.sub(r"\s", "", m.group(1))
            comment = m.group(2).strip().strip("*_`").strip()
            if comment:
                per_post[key] = excerpt(comment, ADDY_COMMENT_MAX)
    return sections, per_post


# ================================================================ シート追記
def append_addy_comments(svc, posts: list[dict], per_post: dict[str, str],
                         date_label: str, dry_run: bool) -> tuple[int, int]:
    """考察・仮説セルの末尾に「\\n【Addy M/D】一言」を追記(末尾追記のみ・上書き禁止)。
    識別子→投稿の対応が一意に取れた場合のみ書く。戻り値 (written, skipped)。"""
    by_key: dict[str, list[dict]] = {}
    for p in posts:
        by_key.setdefault(post_key(p), []).append(p)

    marker = f"【Addy {date_label}】"
    written, skipped = 0, 0
    for key, comment in per_post.items():
        matches = by_key.get(key, [])
        # 同一投稿の1d/7d二重ヒットは同じセルなので row で一意化
        rows = {(m["sheet_id"], m["tab"], m["row"], m["analysis_col"]) for m in matches}
        if len(rows) != 1:
            print(f"  ⚠️ 対応が曖昧のためシート追記スキップ: {key} (候補{len(rows)})")
            skipped += 1
            continue
        sheet_id, tab, rownum, cidx = next(iter(rows))
        a1 = f"'{tab}'!{col_letter(cidx + 1)}{rownum}"
        current_rows = get_values(svc, sheet_id, a1)
        current = str(current_rows[0][0]) if current_rows and current_rows[0] else ""
        if marker in current:
            print(f"  ⏭ 追記済みスキップ: {key}")
            skipped += 1
            continue
        new_value = (current + "\n" if current else "") + f"{marker}{comment}"
        if dry_run:
            print(f"  [dry-run] {key} {a1} ← {marker}{excerpt(comment, 60)}")
            written += 1
            continue
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=a1,
            valueInputOption="RAW", body={"values": [[new_value]]},
        ).execute()
        back = get_values(svc, sheet_id, a1)
        back_v = str(back[0][0]) if back and back[0] else ""
        if marker not in back_v or (current and not back_v.startswith(current)):
            raise RuntimeError(f"読み戻し検証失敗: {key} {a1}")
        print(f"  ✅ 追記: {key} {a1}")
        written += 1
    return written, skipped


# ================================================================ Discord
def send_discord(payload: dict, attach: tuple[str, str] | None = None) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        print("⚠️ DISCORD_WEBHOOK 未設定 → 送信スキップ", file=sys.stderr)
        return False
    try:
        if attach:
            fname, content = attach
            r = requests.post(
                webhook,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files={"files[0]": (fname, content.encode("utf-8"), "text/markdown")},
                timeout=30,
            )
        else:
            r = requests.post(webhook, json=payload, timeout=30)
        r.raise_for_status()
        print("✅ Discord配信完了")
        return True
    except Exception as exc:
        # Webhook URLが例外文に含まれ得るため型名のみ
        print(f"🔴 Discord配信失敗: {type(exc).__name__}", file=sys.stderr)
        return False


def notify_or_exit(payload: dict, no_notify: bool,
                   attach: tuple[str, str] | None = None) -> int:
    """Discord送信(or プレビュー)。送信失敗は exit 2 相当を返す。"""
    if no_notify:
        print("----- Discordプレビュー(--no-notify) -----")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2500])
        return 0
    return 0 if send_discord(payload, attach) else 2


def one_line_payload(text: str) -> dict:
    return {"username": "Addyデイリー考察 ☁️", "content": text[:1900]}


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="スプシ書き込みをしない")
    ap.add_argument("--no-notify", action="store_true", help="Discord送信を抑止")
    ap.add_argument("--date", default="", help="対象日 YYYY-MM-DD (既定=今日JST)")
    args = ap.parse_args()

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = datetime.now(JST).date()
    date_label = f"{target.month}/{target.day}"
    print(f"===== Addyデイリー考察レビュー 対象日={target} (dry_run={args.dry_run}) =====")

    try:
        svc = sheets_service()
    except Exception as exc:
        print(f"❌ Sheets認証失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # ---- Phase 1: 当日確定の投稿を4媒体から収集 ----
    posts: list[dict] = []
    failures: list[str] = []
    for name, fn in (("IG", collect_ig), ("Threads", collect_threads),
                     ("X", collect_x), ("note", collect_note)):
        try:
            got = fn(svc, target)
            print(f"▶ {name}: 当日確定 {len(got)}件")
            posts.extend(got)
        except Exception as exc:
            print(f"🔴 {name} 収集失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append(name)
    if len(failures) == 4:
        print("❌ 全媒体の収集に失敗", file=sys.stderr)
        return 2

    fail_note = f" (⚠️ 収集失敗: {'/'.join(failures)})" if failures else ""

    if not posts:
        print("当日確定の投稿なし → Addy呼び出しスキップ")
        rc = notify_or_exit(
            one_line_payload(
                f"🤖 Addyデイリー考察 {date_label}: 今日は1d/7d確定の投稿なし"
                f"(Addy呼び出しスキップ){fail_note}"
            ),
            args.no_notify,
        )
        return rc

    posts = round_robin_by_media(posts)
    digest = build_digest(posts)
    print("----- ダイジェスト -----")
    print(digest)

    # ---- Phase 2: Addy 1往復 ----
    try:
        resp, thread_id = ask_addy(f"{target}", digest, len(posts))
    except Exception as exc:
        print(f"⚠️ Addyレビュー接続失敗: {type(exc).__name__}", file=sys.stderr)
        rc = notify_or_exit(
            one_line_payload(
                f"🤖 Addyデイリー考察 {date_label}: Addyレビュー接続失敗"
                f"(テンプレ考察は通常どおり生成済み・確定{len(posts)}件){fail_note}"
            ),
            args.no_notify,
        )
        # 考察本体は別系で生成済みのため degrade は正常終了(ただしDiscord不達は失敗)
        return rc

    sections, per_post = parse_addy_response(resp)
    print(f"Addy応答 {len(resp)}字 / 投稿別一言 {len(per_post)}件 / thread:{thread_id[:8]}...")

    # ---- Phase 3: 考察セルへ末尾追記(対応が確実な投稿のみ) ----
    written, skipped = 0, 0
    if per_post:
        try:
            written, skipped = append_addy_comments(
                svc, posts, per_post, date_label, args.dry_run)
        except Exception as exc:
            print(f"🔴 シート追記失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped = len(per_post) - written
            failures.append("シート追記")
    else:
        print("⚠️ 投稿別一言をパースできず → シート追記なし(Discordのみ)")
        print(f"--- Addy応答冒頭(パース診断用) ---\n{resp[:600]}\n---")

    # ---- Phase 4: Discord embed配信 ----
    per_post_text = "\n".join(f"{k}: {v}" for k, v in list(per_post.items())[:12]) or "—"
    write_note = f"✅ {written}件追記 / ⏭ {skipped}件スキップ" + (" (dry-run)" if args.dry_run else "")
    payload = {
        "username": "Addyデイリー考察 ☁️",
        "embeds": [{
            "title": f"🤖 Addyデイリー考察 {date_label}(確定{len(posts)}件)",
            "description": (
                f"**📚 学び**\n{sections['learn'] or '—'}\n\n"
                f"**👉 明日の1手**\n{sections['next_move'] or '—'}\n\n"
                f"**🎯 CSP影響**\n{sections['csp'] or '—'}"
            )[:3500],
            "color": 0x2ECC71,
            "fields": [
                {"name": "投稿別一言", "value": per_post_text[:1000], "inline": False},
                {"name": "考察セル追記", "value": write_note, "inline": False},
            ],
            "footer": {"text": f"daily_addy_review.py (GitHub Actions){fail_note}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    full_md = (
        f"# 🤖 Addyデイリー考察 {target}\n\n## 本日確定の投稿\n\n{digest}\n\n"
        f"## Addyレビュー全文\n\n{resp}\n"
    )
    rc = notify_or_exit(payload, args.no_notify,
                        attach=(f"{target}-addy-daily-review.md", full_md))
    if rc != 0:
        return rc
    print("✅ Addyデイリー考察レビュー 完了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
