#!/usr/bin/env python3
"""週次SNS PDCAループ(クラウド版) — 毎週月曜 8:00 JST に GitHub Actions で実行。

ローカル版(utils/sync_weekly_media_tabs.py + utils/weekly_sns_strategy_loop.py)の
クラウド統合移植。Macが閉じていても以下が回る:
  1. 週次タブ更新: Threads/X/note の投稿毎データを週(月曜開始)単位に集計し、
     🔴SNS週ごとデータ スプシの各週次タブへ upsert(冪等)。IGタブは空セルのみ部分記入。
  2. 週次レポート生成: 全媒体の先週実績→勝ちパターン→次週施策案をMarkdown 1枚に。
     ※公開リポジトリのためレポートはコミットしない。Discordへ添付ファイルで配信。
  3. Addy壁打ち(2往復): 環境変数 ADDNESS_API_KEY があれば実施。無ければスキップ(degrade)。

ローカル版との併走(月曜 8:30 JST LaunchAgent)前提:
  - タブ upsert は開始日キーの明示 update なので二重実行しても安全(冪等)。
  - Obsidian(AI司令塔)への保存はローカル版の担当。クラウド版は書かない。
  - Discord メッセージはタイトルに ☁️ を付けて区別する。

設計原則:
  - 列番号・行範囲ハードコード禁止(ソース側)。ヘッダー行から列名で動的解決。
  - values().append() は列ズレするので絶対使わない。既存行は開始日で探して update、
    新規行は最終使用行+1 に明示 update。
  - 既存タブ(週ごとInstagramデータ/週ごと施策/週ごとTikTokデータ)のヘッダー・
    手入力列には書かない(IGタブは空セルのみ部分記入)。
  - 書き込み後は読み戻し検証。失敗は exit 非0(沈黙成功禁止)。
  - 機密(SAキー・Webhook URL・Addy APIキー)はログに一切出さない。

環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON  Service Account JSON文字列(GHA secret)
  GOOGLE_SERVICE_ACCOUNT_FILE  もしくはSAキーのファイルパス(既定: auth/service_account.json)
  DISCORD_WEBHOOK              レポート配信先 Webhook(未設定なら送信スキップ)
  ADDNESS_API_KEY              Addy壁打ち用(optional。未設定ならスキップ)

usage:
  python3 scripts/weekly_sns_rollup.py [--dry-run] [--no-notify] [--rebuild]
    --dry-run  : スプシへの書き込みを一切しない(レポート生成・集計は実行)
    --no-notify: Discord送信を抑止(レポートはstdoutにプレビュー)
    --rebuild  : 週次タブを全クリア+ヘッダー書き直してから同期(形式改修時のみ)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

# ---- スプレッドシートID(シート自体はアクセス制限済・ID自体は既存前例どおりコード内OK) ----
WEEKLY_SHEET = "12fghSF68JkhgqSvPmCa_nGeSXowRizo2MtRz4WyeXyo"  # 🔴SNS週ごとデータ
IG_SHEET = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"      # Instagram投稿毎データ
KPI_SHEET = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"     # ファネルKPI日ごとデータ
THREADS_SHEET = "1hdBlZBn9s688f1ZwkTiO3suY27tJEHtXMEPkopLdBNI"
X_SHEET = "1rHnDoMHUK_K0_f7MLxHltiU6Y2ATsz3ztKwdf2Zg8Hc"
NOTE_SHEET = "1gIW_SCigwa5wFPnQVRoFM3EhOaYC3aC_DtkMbqK8gRw"
POLICY_SHEET = WEEKLY_SHEET  # 「週ごと施策」タブ(タッキーPDCA正本)

ADDNESS_API_BASE = "https://vt.api.addness.com/api/v1"

# ================================================================ 認証
def resolve_sa_file() -> Path:
    """SAキーの所在を解決。GHAでは GOOGLE_SERVICE_ACCOUNT_JSON から書き出す。"""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        path = REPO_ROOT / "auth" / "service_account.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            json.loads(raw)  # 妥当性チェック(不正JSONなら早期に落とす)
            path.write_text(raw, encoding="utf-8")
        return path
    env_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if env_file:
        return Path(env_file).expanduser()
    return REPO_ROOT / "auth" / "service_account.json"


def sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_file = resolve_sa_file()
    if not sa_file.exists():
        raise RuntimeError(
            "Service Account キーが見つからない"
            "(GOOGLE_SERVICE_ACCOUNT_JSON か GOOGLE_SERVICE_ACCOUNT_FILE を設定)"
        )
    creds = service_account.Credentials.from_service_account_file(
        str(sa_file), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ================================================================ 共通ヘルパー
def get_values(svc, sheet_id: str, rng: str, retries: int = 3) -> list[list[str]]:
    """values().get() をtransient障害(socket timeout/5xx)に対して自動リトライ。"""
    import time

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id, range=rng)
                .execute()
                .get("values", [])
            )
        except Exception as exc:  # TimeoutError / HttpError 5xx など
            last_exc = exc
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  ⏳ 読み取りリトライ {attempt + 1}/{retries - 1} "
                      f"({type(exc).__name__}) {wait}s待機: {rng}", file=sys.stderr)
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def find_col(header_rows: list[list[str]], candidates: list[str]) -> int | None:
    """ヘッダー行(複数)から候補名に一致する列indexを返す。完全一致優先→部分一致。"""
    for exact in (True, False):
        for row in header_rows:
            for i, cell_v in enumerate(row):
                c = str(cell_v).strip()
                if not c:
                    continue
                for name in candidates:
                    if (exact and c == name) or (not exact and name in c):
                        return i
    return None


def parse_num(v) -> float | None:
    s = str(v).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v) -> datetime | None:
    s = str(v).strip().split(" ")[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            if d.year == 1900:  # %m/%d
                d = d.replace(year=datetime.now(JST).year)
            return d.replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def monday_of(d: datetime) -> datetime:
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def week_key(mon: datetime) -> str:
    return f"{mon:%Y/%m/%d}"


def fmt_num(n: float | None, digits: int = 0) -> str:
    if n is None:
        return ""
    if digits == 0:
        return str(int(round(n)))
    return f"{round(n, digits)}"


def fmt(n: float | None, digits: int = 0) -> str:
    if n is None:
        return "—"
    return f"{n:,.{digits}f}"


def pct_change(cur: float | None, prev: float | None) -> str:
    if not cur or not prev:
        return ""
    ch = (cur - prev) / prev * 100
    arrow = "📈" if ch > 5 else ("📉" if ch < -5 else "→")
    return f" ({arrow}{ch:+.0f}%)"


def cell(row: list, idx: int | None) -> str:
    if idx is None or row is None or len(row) <= idx:
        return ""
    return str(row[idx]).strip()


def sum_or_blank(posts: list[dict], key: str) -> str:
    vals = [p[key] for p in posts if p.get(key) is not None]
    return fmt_num(sum(vals)) if vals else ""


def winning_hook(posts: list[dict]) -> str:
    """週内で views 平均が最大のフック型(2本以上)。"""
    groups: dict[str, list[float]] = {}
    for p in posts:
        if p.get("hook") and p.get("views") is not None:
            groups.setdefault(p["hook"], []).append(p["views"])
    eligible = {k: v for k, v in groups.items() if len(v) >= 2}
    if not eligible:
        return ""
    return max(eligible.items(), key=lambda kv: statistics.mean(kv[1]))[0]


def col_letter(n: int) -> str:
    """1-indexed 列番号 → A1表記(Z超対応)。"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ================================================================
# Part 1: 週次タブ upsert (sync_weekly_media_tabs.py 移植)
# ================================================================

# 週ごとInstagramデータ A〜AN と完全同一の40列(実測 2026-07-12)
BASE_HEADERS = [
    "開始日", "終了日", "セールス開始日", "セールス終了日", "施策", "期日", "日数",
    "投稿数", "いいね・保存", "フォロー誘導", "LP誘導", "ストーリーズ", "プロフィール",
    "キャプション", "コメント", "なし", "数", "インプ", "①全体", "②フォロワー",
    "③フォロワー以外", "リール", "投稿リーチ", "ストーリーズ", "動画", "ライブ動画",
    "④プロフアクセス", "⑤フォロー全体", "⑥フォロー増", "⑦フォロー減", "⑧ウェブタップ",
    "Eメールタップ数", "リスト数", "リスト前週比", "プレゼント受取数", "購入者数",
    "ローンチのみ", "メッセージあり購入者数", "電話あり", "zoomあり",
]
assert len(BASE_HEADERS) == 40  # A〜AN

BASE_SECTIONS = [""] * 40
BASE_SECTIONS[0] = "　　"                 # A1(IGタブと同じ全角スペース2つ)
BASE_SECTIONS[7] = "投稿数（CTA別）"      # H1
BASE_SECTIONS[16] = "ストーリーズ"         # Q1
BASE_SECTIONS[18] = "リーチ"              # S1
BASE_SECTIONS[27] = "フォロー"            # AB1

EXTRA_START = 41  # AP列(idx41)。AO(idx40)は空けるスペーサー

TABS = {
    "threads": {
        "title": "週ごとThreadsデータ",
        "extra_section": "Threads固有指標",
        "extra_headers": [
            "平均views", "中央値views", "TOP投稿番号", "TOP views", "リポスト計",
            "平均ER%", "勝ちフック型", "7d_views計", "いいね計", "リプライ計",
        ],  # AP〜AY
    },
    "x": {
        "title": "週ごとXデータ",
        "extra_section": "X固有指標",
        "extra_headers": [
            "平均views", "TOP投稿番号", "TOP views", "RT計", "勝ちフック",
            "failed数", "いいね計", "リプ計",
        ],  # AP〜AW
    },
    "note": {
        "title": "週ごとnoteデータ",
        "extra_section": "note固有指標",
        "extra_headers": ["累計PV", "累計スキ", "PV TOP記事タイトル"],  # AP〜AR
    },
}
PROTECTED_TABS = {"週ごとInstagramデータ", "週ごと施策", "週ごとTikTokデータ"}
DATA_START_ROW = 3  # 行1=セクション, 行2=列名

IDX_DAYS = 6        # G 日数
IDX_POSTS = 7       # H 投稿数
IDX_CTA_START = 8   # I いいね・保存 〜 P なし (8..15)
IDX_IMP = 17        # R インプ
IDX_REACH_ALL = 18  # S ①全体

IG_TAB = "週ごとInstagramデータ"


def total_cols(spec: dict) -> int:
    return EXTRA_START + len(spec["extra_headers"])


def classify_cta(raw: str) -> int:
    """CTA型文字列 → I〜P(idx 8..15)の列index。"""
    s = str(raw).strip()
    if not s or s == "—":
        return 15  # P なし
    if "保存" in s or "いいね" in s:
        return 8   # I
    if "フォロー" in s:
        return 9   # J
    if "LP" in s or "ウェブ" in s or "LINE" in s:
        return 10  # K LP誘導(リスト誘導含む)
    if "プロフ" in s:
        return 12  # M
    if "コメント" in s or "リプ" in s:
        return 14  # O
    return 15      # P その他


def cta_counts_of(posts: list[dict]) -> list[int]:
    counts = [0] * 8  # I..P
    for p in posts:
        counts[classify_cta(p.get("cta", "")) - IDX_CTA_START] += 1
    return counts


def base_row(wk: str, posts_count: int, cta_counts: list[int], views_sum: float | None) -> list[str]:
    """IG形式40列(A〜AN)の週次行を組み立てる。"""
    mon = datetime.strptime(wk, "%Y/%m/%d").replace(tzinfo=JST)
    row = [""] * 40
    row[0] = wk                                     # A 開始日
    row[1] = f"{mon + timedelta(days=6):%Y/%m/%d}"  # B 終了日
    # C〜F: セールス開始/終了・施策・期日 = タッキー手入力枠(空のまま)
    row[IDX_DAYS] = "7"                             # G 日数
    row[IDX_POSTS] = str(posts_count)               # H 投稿数
    for i, c in enumerate(cta_counts):              # I〜P CTA別
        row[IDX_CTA_START + i] = str(c)
    if views_sum is not None:
        row[IDX_IMP] = fmt_num(views_sum)           # R インプ ≒ views合計
        row[IDX_REACH_ALL] = fmt_num(views_sum)     # S ①全体(リーチ近似)
    # T〜AN: 各媒体で取得不能 → 空(0で埋めない)
    return row


def full_row(spec: dict, base: list[str], extras: list[str]) -> list[str]:
    row = base + [""]  # AO スペーサー
    row += extras + [""] * (len(spec["extra_headers"]) - len(extras))
    assert len(row) == total_cols(spec)
    return row


# ---------------------------------------------------------------- collectors
def collect_threads(svc, spec: dict) -> dict[str, list[str]]:
    hdr = get_values(svc, THREADS_SHEET, "'Threads投稿毎データ'!A1:BH3")
    rows = get_values(svc, THREADS_SHEET, "'Threads投稿毎データ'!A4:BH2000")
    col = {
        "date": find_col(hdr, ["日付", "投稿日"]),
        "num": find_col(hdr, ["番号"]),
        "views": find_col(hdr, ["views"]),
        "likes": find_col(hdr, ["いいね"]),
        "replies": find_col(hdr, ["リプライ数", "リプライ"]),
        "reposts": find_col(hdr, ["リポスト数", "リポスト"]),
        "er": find_col(hdr, ["ER%", "ER"]),
        "hook": find_col(hdr, ["フック型"]),
        "views7d": find_col(hdr, ["7d_views"]),
        "status": find_col(hdr, ["投稿ステータス"]),
        "cta": find_col(hdr, ["CTA型"]),
        "kind": find_col(hdr, ["投稿種別"]),
    }
    required = [k for k in ("date", "num", "views", "status") if col[k] is None]
    if required:
        raise RuntimeError(f"Threads 必須列が見つからない: {required}")

    weeks: dict[str, list[dict]] = {}
    for r in rows:
        d = parse_date(cell(r, col["date"]))
        if not d:
            continue
        if cell(r, col["status"]) != "published":  # published のみが実投稿
            continue
        weeks.setdefault(week_key(monday_of(d)), []).append(
            {
                "num": cell(r, col["num"]),
                "views": parse_num(cell(r, col["views"])),
                "likes": parse_num(cell(r, col["likes"])),
                "replies": parse_num(cell(r, col["replies"])),
                "reposts": parse_num(cell(r, col["reposts"])),
                "er": parse_num(cell(r, col["er"])),
                "hook": cell(r, col["hook"]),
                "views7d": parse_num(cell(r, col["views7d"])),
                "cta": cell(r, col["cta"]) or cell(r, col["kind"]),
            }
        )

    out: dict[str, list[str]] = {}
    for wk, posts in weeks.items():
        with_v = [p for p in posts if p["views"] is not None]
        vs = [p["views"] for p in with_v]
        top = max(with_v, key=lambda p: p["views"]) if with_v else None
        ers = [p["er"] for p in posts if p["er"] is not None]
        v7 = [p["views7d"] for p in posts if p["views7d"] is not None]
        base = base_row(wk, len(posts), cta_counts_of(posts), sum(vs) if vs else None)
        extras = [
            fmt_num(statistics.mean(vs)) if vs else "",
            fmt_num(statistics.median(vs)) if vs else "",
            top["num"] if top else "",
            fmt_num(top["views"]) if top else "",
            sum_or_blank(posts, "reposts"),
            fmt_num(statistics.mean(ers), 2) if ers else "",
            winning_hook(posts),
            fmt_num(sum(v7)) if v7 else "",
            sum_or_blank(posts, "likes"),
            sum_or_blank(posts, "replies"),
        ]
        out[wk] = full_row(spec, base, extras)
    return out


def collect_x(svc, spec: dict) -> dict[str, list[str]]:
    hdr = get_values(svc, X_SHEET, "'X投稿毎データ'!A1:AN2")
    rows = get_values(svc, X_SHEET, "'X投稿毎データ'!A3:AN2000")
    col = {
        "date": find_col(hdr, ["日付"]),
        "num": find_col(hdr, ["番号"]),
        "hook": find_col(hdr, ["フック"]),
        "status": find_col(hdr, ["ステータス"]),
        "cta": find_col(hdr, ["CTA型"]),
        "v1": find_col(hdr, ["1d_views"]),
        "v7": find_col(hdr, ["7d_views"]),
        "l1": find_col(hdr, ["1d_likes"]),
        "l7": find_col(hdr, ["7d_likes"]),
        "rt1": find_col(hdr, ["1d_RT"]),
        "rt7": find_col(hdr, ["7d_RT"]),
        "rp1": find_col(hdr, ["1d_リプ"]),
        "rp7": find_col(hdr, ["7d_リプ"]),
    }
    required = [k for k in ("date", "num", "status", "v1") if col[k] is None]
    if required:
        raise RuntimeError(f"X 必須列が見つからない: {required}")

    def best(r, k7, k1):
        v = parse_num(cell(r, col[k7]))
        return v if v is not None else parse_num(cell(r, col[k1]))

    weeks: dict[str, dict] = {}
    for r in rows:
        d = parse_date(cell(r, col["date"]))
        if not d:
            continue  # テンプレ行や空行はここで除外
        st = cell(r, col["status"])
        wk = week_key(monday_of(d))
        bucket = weeks.setdefault(wk, {"posts": [], "failed": 0})
        if st == "failed":
            bucket["failed"] += 1
            continue
        if st != "posted":
            continue
        bucket["posts"].append(
            {
                "num": cell(r, col["num"]),
                "hook": cell(r, col["hook"]),
                "cta": cell(r, col["cta"]),
                "views": best(r, "v7", "v1"),
                "likes": best(r, "l7", "l1"),
                "rt": best(r, "rt7", "rt1"),
                "rp": best(r, "rp7", "rp1"),
            }
        )

    out: dict[str, list[str]] = {}
    for wk, b in weeks.items():
        posts = b["posts"]
        if not posts and not b["failed"]:
            continue
        with_v = [p for p in posts if p["views"] is not None]
        vs = [p["views"] for p in with_v]
        top = max(with_v, key=lambda p: p["views"]) if with_v else None
        base = base_row(wk, len(posts), cta_counts_of(posts), sum(vs) if vs else None)
        extras = [
            fmt_num(statistics.mean(vs)) if vs else "",
            top["num"] if top else "",
            fmt_num(top["views"]) if top else "",
            sum_or_blank(posts, "rt"),
            winning_hook(posts),
            str(b["failed"]),
            sum_or_blank(posts, "likes"),
            sum_or_blank(posts, "rp"),
        ]
        out[wk] = full_row(spec, base, extras)
    return out


def collect_note(svc, spec: dict, now: datetime) -> dict[str, list[str]]:
    """週→行values。当週(now の週)のみ累計PV/スキのスナップショット付き。"""
    hdr = get_values(svc, NOTE_SHEET, "'記事一覧'!A1:AZ1")
    rows = get_values(svc, NOTE_SHEET, "'記事一覧'!A2:AZ500")
    col = {
        "title": find_col(hdr, ["タイトル"]),
        "status": find_col(hdr, ["ステータス"]),
        "date": find_col(hdr, ["公開日"]),
        "pv": find_col(hdr, ["PV"]),
        "like": find_col(hdr, ["スキ"]),
        "cta": find_col(hdr, ["CTA種類", "CTA"]),
    }
    required = [k for k, v in col.items() if v is None and k != "cta"]
    if required:
        raise RuntimeError(f"note 必須列が見つからない: {required}")

    published = []
    for r in rows:
        if "公開" not in cell(r, col["status"]):
            continue
        published.append(
            {
                "title": cell(r, col["title"]),
                "date": parse_date(cell(r, col["date"])),
                "pv": parse_num(cell(r, col["pv"])) or 0,
                "like": parse_num(cell(r, col["like"])) or 0,
                "cta": cell(r, col["cta"]),
            }
        )
    if not published:
        raise RuntimeError("note 公開記事が0件(ステータス列の想定ずれの可能性)")

    by_week: dict[str, list[dict]] = {}
    for a in published:
        if a["date"]:
            by_week.setdefault(week_key(monday_of(a["date"])), []).append(a)

    this_wk = week_key(monday_of(now))
    all_weeks = set(by_week) | {this_wk}
    out: dict[str, list[str]] = {}
    for wk in all_weeks:
        arts = by_week.get(wk, [])
        base = base_row(wk, len(arts), cta_counts_of(arts), None)
        extras = ["", "", ""]
        if wk == this_wk:  # スナップショットは当週行のみ
            top = max(published, key=lambda a: a["pv"])
            extras = [
                fmt_num(sum(a["pv"] for a in published)),
                fmt_num(sum(a["like"] for a in published)),
                top["title"],
            ]
        out[wk] = full_row(spec, base, extras)
    return out


# ---------------------------------------------------------------- writer
def header_rows(spec: dict) -> list[list[str]]:
    n = total_cols(spec)
    sections = BASE_SECTIONS + [""] + [""] * len(spec["extra_headers"])
    sections[EXTRA_START] = spec["extra_section"]
    headers = BASE_HEADERS + [""] + spec["extra_headers"]
    assert len(sections) == len(headers) == n
    return [sections, headers]


def ensure_tab(svc, spec: dict, rebuild: bool, dry_run: bool) -> None:
    """タブが無ければ作成。ヘッダーが期待形式と違えば --rebuild 時のみ全クリア+書き直し。"""
    title = spec["title"]
    assert title not in PROTECTED_TABS
    ncols = total_cols(spec)
    end = col_letter(ncols)
    meta = svc.spreadsheets().get(spreadsheetId=WEEKLY_SHEET).execute()
    props = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}

    if title not in props:
        if dry_run:
            print(f"  [dry-run] タブ新規作成をスキップ: {title}")
            return
        svc.spreadsheets().batchUpdate(
            spreadsheetId=WEEKLY_SHEET,
            body={"requests": [{"addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": 400, "columnCount": ncols + 5, "frozenRowCount": 2},
            }}}]},
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=WEEKLY_SHEET, range=f"'{title}'!A1:{end}2",
            valueInputOption="USER_ENTERED", body={"values": header_rows(spec)},
        ).execute()
        print(f"  ✨ タブ新規作成: {title}")
        return

    current_hdr = get_values(svc, WEEKLY_SHEET, f"'{title}'!A2:{end}2")
    current = [str(c).strip() for c in (current_hdr[0] if current_hdr else [])]
    expected = header_rows(spec)[1]
    if current == [str(c).strip() for c in expected]:
        return
    if not rebuild:
        raise RuntimeError(
            f"{title} のヘッダーが期待形式(IG40列+固有列)と不一致。--rebuild で作り直すこと"
        )
    if dry_run:
        print(f"  [dry-run] {title}: 全クリア+ヘッダー書き直し(rebuild)をスキップ")
        return
    p = props[title]
    if p["gridProperties"].get("columnCount", 0) < ncols + 5:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=WEEKLY_SHEET,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": p["sheetId"],
                               "gridProperties": {"rowCount": p["gridProperties"]["rowCount"],
                                                  "columnCount": ncols + 5,
                                                  "frozenRowCount": 2}},
                "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
            }}]},
        ).execute()
    svc.spreadsheets().values().clear(
        spreadsheetId=WEEKLY_SHEET, range=f"'{title}'"
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=WEEKLY_SHEET, range=f"'{title}'!A1:{end}2",
        valueInputOption="USER_ENTERED", body={"values": header_rows(spec)},
    ).execute()
    print(f"  🔨 {title}: 全クリア+IG形式ヘッダー書き直し完了")


def upsert_weeks(svc, spec: dict, week_rows: dict[str, list[str]], dry_run: bool) -> tuple[int, int]:
    """開始日キーで upsert。戻り値 (updated, inserted)。append禁止=全て明示rangeのupdate。"""
    title = spec["title"]
    assert title not in PROTECTED_TABS
    ncols = total_cols(spec)
    end = col_letter(ncols)
    existing = get_values(svc, WEEKLY_SHEET, f"'{title}'!A{DATA_START_ROW}:{end}1000")
    key_to_row: dict[str, int] = {}
    last_used = DATA_START_ROW - 1
    for i, r in enumerate(existing):
        rownum = DATA_START_ROW + i
        d = parse_date(cell(r, 0))
        if d:
            key_to_row[week_key(d)] = rownum
        if any(str(c).strip() for c in r):
            last_used = rownum
    existing_by_key = {week_key(parse_date(cell(r, 0))): r for r in existing if parse_date(cell(r, 0))}

    # note: 週間PV増分(R/S) を前週の累計PVスナップ(AP列)との差で埋める
    if title == TABS["note"]["title"]:
        for wk, row in sorted(week_rows.items()):
            if not row[EXTRA_START]:
                continue
            prev_mon = datetime.strptime(wk, "%Y/%m/%d") - timedelta(days=7)
            prev_key = f"{prev_mon:%Y/%m/%d}"
            prev_row = week_rows.get(prev_key) or existing_by_key.get(prev_key)
            prev_pv = parse_num(cell(prev_row, EXTRA_START)) if prev_row else None
            if prev_pv is not None:
                diff = fmt_num(float(row[EXTRA_START]) - prev_pv)
                row[IDX_IMP] = diff
                row[IDX_REACH_ALL] = diff

    data, updated, inserted = [], 0, 0
    next_row = last_used + 1
    for wk in sorted(week_rows):
        row = week_rows[wk]
        if wk in key_to_row:
            rownum = key_to_row[wk]
            updated += 1
        else:
            rownum = next_row
            next_row += 1
            inserted += 1
        data.append({"range": f"'{title}'!A{rownum}:{end}{rownum}", "values": [row]})

    if dry_run:
        print(f"  [dry-run] {title}: update {updated}週 / insert {inserted}週")
        for d in data[:3] + data[-2:]:
            print(f"    {d['range']}: {d['values'][0]}")
        return updated, inserted

    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=WEEKLY_SHEET,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()

    # 読み戻し検証: 全書き込み週の開始日+投稿数(H列)が一致するか
    after = get_values(svc, WEEKLY_SHEET, f"'{title}'!A{DATA_START_ROW}:{end}1000")
    after_by_key = {}
    for r in after:
        d = parse_date(cell(r, 0))
        if d:
            after_by_key[week_key(d)] = r
    for wk, row in week_rows.items():
        got = after_by_key.get(wk)
        if got is None or cell(got, IDX_POSTS) != row[IDX_POSTS]:
            raise RuntimeError(
                f"{title} 読み戻し検証失敗: 週{wk} expected H={row[IDX_POSTS]!r} "
                f"got={cell(got, IDX_POSTS) if got else 'MISSING'!r}"
            )
    print(f"  ✅ {title}: update {updated}週 / insert {inserted}週 / 読み戻し検証OK")
    return updated, inserted


def sync_ig_partial(svc, dry_run: bool) -> None:
    """週ごとInstagramデータタブの「投稿数(CTA別) H〜P」「リスト数 AG」を空セルのみ自動記入。
    既存値(過去の手入力)は一切上書きしない。"""
    hdr = get_values(svc, IG_SHEET, "'Instagram投稿毎データ'!A1:H3")
    c_date = find_col(hdr, ["日付"]) or 0
    c_cta = find_col(hdr, ["投稿種別"])
    rows = get_values(svc, IG_SHEET, "'Instagram投稿毎データ'!A4:H1000")
    weekly_posts: dict[str, list[dict]] = {}
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    for r in rows:
        d = parse_date(cell(r, c_date))
        if not d or d > today:  # 未来のキュー行は数えない
            continue
        weekly_posts.setdefault(week_key(monday_of(d)), []).append({"cta": cell(r, c_cta)})

    khdr = get_values(svc, KPI_SHEET, "A1:BF3")
    k_list = find_col(khdr, ["リスト数"])
    krows = get_values(svc, KPI_SHEET, "A1700:BF2200")
    weekly_list: dict[str, float] = {}
    for r in krows:
        d = parse_date(str(cell(r, 0)).split(" ")[0])
        if not d or d > today:
            continue
        v = parse_num(cell(r, k_list))
        if v is not None:
            wk = week_key(monday_of(d))
            weekly_list[wk] = weekly_list.get(wk, 0) + v

    existing = get_values(svc, WEEKLY_SHEET, f"'{IG_TAB}'!A{DATA_START_ROW}:AG1000")
    updates = []
    filled_h, filled_ag = 0, 0
    for i, r in enumerate(existing):
        rownum = DATA_START_ROW + i
        d = parse_date(cell(r, 0))
        if not d or d > today:
            continue
        wk = week_key(d)
        week_done = d + timedelta(days=7) <= today
        if not cell(r, IDX_POSTS) and wk in weekly_posts and week_done:
            posts = weekly_posts[wk]
            vals = [str(len(posts))] + [str(c) for c in cta_counts_of(posts)]
            updates.append({"range": f"'{IG_TAB}'!H{rownum}:P{rownum}", "values": [vals]})
            filled_h += 1
        if (len(r) <= 32 or not cell(r, 32)) and wk in weekly_list and week_done:
            updates.append({"range": f"'{IG_TAB}'!AG{rownum}", "values": [[fmt_num(weekly_list[wk])]]})
            filled_ag += 1
    if not updates:
        print(f"▶ {IG_TAB}: 空セルなし(自動記入対象0)")
        return
    print(f"▶ {IG_TAB}: 投稿数{filled_h}週 / リスト数{filled_ag}週 を空セルに記入")
    if dry_run:
        for u in updates[:6]:
            print("  [dry-run]", u["range"], u["values"])
        return
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=WEEKLY_SHEET,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    back = get_values(svc, WEEKLY_SHEET, updates[0]["range"])
    if not back or not any(str(c).strip() for c in back[0]):
        raise RuntimeError(f"IGタブ書き込み検証失敗: {updates[0]['range']}")
    print(f"  ✅ {IG_TAB}: 記入+読み戻し検証OK")


def run_tab_sync(svc, rebuild: bool, dry_run: bool) -> list[str]:
    """週次タブ同期を全媒体実行。失敗タブ名のリストを返す(空=全成功)。"""
    now = datetime.now(JST)
    failures: list[str] = []
    collectors = {
        "threads": lambda spec: collect_threads(svc, spec),
        "x": lambda spec: collect_x(svc, spec),
        "note": lambda spec: collect_note(svc, spec, now),
    }
    for key, fn in collectors.items():
        spec = TABS[key]
        try:
            week_rows = fn(spec)
            if not week_rows:
                raise RuntimeError("集計結果が0週(ソースシート構造の想定ずれの可能性)")
            print(f"▶ {spec['title']}: {len(week_rows)}週分を集計")
            ensure_tab(svc, spec, rebuild, dry_run)
            if dry_run:
                meta = svc.spreadsheets().get(spreadsheetId=WEEKLY_SHEET).execute()
                titles = {s["properties"]["title"] for s in meta["sheets"]}
                if spec["title"] not in titles:
                    print("  [dry-run] タブ未作成のため書き込みプレビューのみ:")
                    for wk in sorted(week_rows)[:3]:
                        print(f"    {wk}: {week_rows[wk]}")
                    continue
            upsert_weeks(svc, spec, week_rows, dry_run)
        except Exception as exc:
            print(f"🔴 {spec['title']} 失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append(spec["title"])

    try:
        sync_ig_partial(svc, dry_run)
    except Exception as exc:
        print(f"🔴 {IG_TAB} 部分記入 失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        failures.append(IG_TAB)
    return failures


# ================================================================
# Part 2: 週次レポート (weekly_sns_strategy_loop.py 移植・読み取り専用)
# ================================================================
def week_range(now: datetime) -> tuple[datetime, datetime, datetime, datetime]:
    """先週(月〜日)と先々週の範囲を返す。"""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    last_mon = this_monday - timedelta(days=7)
    prev_mon = this_monday - timedelta(days=14)
    return last_mon, this_monday, prev_mon, last_mon


def analyze_ig(svc, last_mon, this_mon, prev_mon) -> tuple[str, dict]:
    hdr = get_values(svc, IG_SHEET, "'Instagram投稿毎データ'!A1:CT3")
    rows = get_values(svc, IG_SHEET, "'Instagram投稿毎データ'!A4:CT1000")
    col = {
        "date": find_col(hdr, ["投稿日", "日付"]) or 0,
        "num": find_col(hdr, ["番号", "No"]),
        "cat": find_col(hdr, ["投稿目的", "カテゴリ"]),
        "hook": find_col(hdr, ["フック型", "フック"]),
        "reach": find_col(hdr, ["全体"]),
        "saves": find_col(hdr, ["保存"]),
        "shares": find_col(hdr, ["シェア"]),
        "likes": find_col(hdr, ["いいね"]),
        "prof": find_col(hdr, ["プロフィール"]),
    }
    missing = [k for k, v in col.items() if v is None]

    def bucket(start, end):
        out = []
        for r in rows:
            d = parse_date(r[col["date"]]) if len(r) > col["date"] else None
            if not d or not (start <= d < end):
                continue
            met = {
                k: parse_num(r[col[k]]) if col[k] is not None and len(r) > col[k] else None
                for k in ("reach", "saves", "shares", "likes", "prof")
            }
            if met["reach"] is None:
                continue  # インサイト未着(投稿1日以内)は除外
            score = (
                (met["shares"] or 0) * 0.4
                + (met["saves"] or 0) * 0.3
                + (met["prof"] or 0) * 0.2
                + (met["likes"] or 0) * 0.1
            )
            out.append(
                {
                    "num": r[col["num"]] if col["num"] is not None and len(r) > col["num"] else "?",
                    "date": d,
                    "cat": r[col["cat"]] if col["cat"] is not None and len(r) > col["cat"] else "",
                    "hook": r[col["hook"]] if col["hook"] is not None and len(r) > col["hook"] else "",
                    "score": score,
                    **met,
                }
            )
        return out

    cur, prev = bucket(last_mon, this_mon), bucket(prev_mon, last_mon)
    if not cur:
        return "### Instagram\n- ⚠️ 先週のインサイト到着済み投稿なし\n", {}

    avg_reach = statistics.mean(p["reach"] for p in cur)
    prev_reach = statistics.mean(p["reach"] for p in prev) if prev else None
    top = sorted(cur, key=lambda p: -p["score"])[:3]
    worst = sorted(cur, key=lambda p: p["score"])[:1]

    def group_best(key: str) -> str | None:
        groups: dict[str, list[float]] = {}
        for p in cur + prev:
            if p[key]:
                groups.setdefault(p[key], []).append(p["reach"])
        eligible = {k: v for k, v in groups.items() if len(v) >= 2}
        if not eligible:
            return None
        return max(eligible.items(), key=lambda kv: statistics.mean(kv[1]))[0]

    best_hook = group_best("hook")
    best_cat = group_best("cat")

    def label(p) -> str:
        return "/".join(x for x in (p["cat"], p["hook"]) if x) or "-"

    lines = ["### Instagram"]
    lines.append(
        f"- 投稿 **{len(cur)}本** / 平均リーチ **{fmt(avg_reach)}**{pct_change(avg_reach, prev_reach)}"
    )
    for i, p in enumerate(top, 1):
        lines.append(
            f"- TOP{i}: #{p['num']} ({p['date']:%m/%d} {label(p)}) "
            f"リーチ{fmt(p['reach'])}・保存{fmt(p['saves'])}・シェア{fmt(p['shares'])}"
        )
    if worst and len(cur) > 3:
        p = worst[0]
        lines.append(f"- WORST: #{p['num']} ({label(p)}) リーチ{fmt(p['reach'])}")
    if best_hook:
        lines.append(f"- 直近2週の勝ちフック: **{best_hook}**")
    if best_cat:
        lines.append(f"- 直近2週の勝ち目的: **{best_cat}**")
    if missing:
        lines.append(f"- ⚠️ 列未検出: {missing}")
    return "\n".join(lines) + "\n", {
        "best_hook": best_hook,
        "best_cat": best_cat,
        "top": top,
        "n": len(cur),
    }


def analyze_threads(svc, last_mon, this_mon, prev_mon) -> tuple[str, dict]:
    hdr = get_values(svc, THREADS_SHEET, "'Threads投稿毎データ'!A1:BH3")
    rows = get_values(svc, THREADS_SHEET, "'Threads投稿毎データ'!A4:BH1000")
    col = {
        "date": find_col(hdr, ["投稿日", "日付"]) or 0,
        "num": find_col(hdr, ["番号", "ID"]),
        "hook": find_col(hdr, ["フック型", "フック"]),
        "views": find_col(hdr, ["views", "ビュー", "閲覧"]),
        "likes": find_col(hdr, ["likes", "いいね"]),
        "status": find_col(hdr, ["ステータス", "status"]),
    }

    def bucket(start, end):
        out = []
        for r in rows:
            d = parse_date(r[col["date"]]) if len(r) > col["date"] else None
            if not d or not (start <= d < end):
                continue
            views = parse_num(r[col["views"]]) if col["views"] is not None and len(r) > col["views"] else None
            out.append(
                {
                    "num": r[col["num"]] if col["num"] is not None and len(r) > col["num"] else "?",
                    "hook": r[col["hook"]] if col["hook"] is not None and len(r) > col["hook"] else "",
                    "views": views,
                    "date": d,
                }
            )
        return out

    cur, prev = bucket(last_mon, this_mon), bucket(prev_mon, last_mon)
    if not cur:
        return "### Threads\n- ⚠️ 先週の投稿なし\n", {}
    with_v = [p for p in cur if p["views"] is not None]
    avg_v = statistics.mean(p["views"] for p in with_v) if with_v else None
    prev_v = [p["views"] for p in prev if p["views"] is not None]
    top = sorted(with_v, key=lambda p: -p["views"])[:3]

    lines = ["### Threads"]
    lines.append(
        f"- 投稿 **{len(cur)}本** (メトリクス到着 {len(with_v)}本) / 平均views **{fmt(avg_v)}**"
        f"{pct_change(avg_v, statistics.mean(prev_v) if prev_v else None)}"
    )
    for i, p in enumerate(top, 1):
        lines.append(f"- TOP{i}: {p['num']} ({p['date']:%m/%d} {p['hook']}) views {fmt(p['views'])}")
    unfetched = len(cur) - len(with_v)
    if unfetched > 2:
        lines.append(f"- ⚠️ メトリクス未取得 {unfetched}本(取りこぼし監視)")
    return "\n".join(lines) + "\n", {"top": top, "n": len(cur)}


def analyze_x(svc, last_mon, this_mon, prev_mon) -> tuple[str, dict]:
    hdr = get_values(svc, X_SHEET, "'X投稿毎データ'!A1:AN3")
    rows = get_values(svc, X_SHEET, "'X投稿毎データ'!A4:AN1000")
    col = {
        "date": find_col(hdr, ["日付"]) or 0,
        "num": find_col(hdr, ["番号"]),
        "hook": find_col(hdr, ["フック"]),
        "status": find_col(hdr, ["ステータス"]),
        "tid": find_col(hdr, ["ツイートID"]),
        "views": find_col(hdr, ["1d_views"]),
        "likes": find_col(hdr, ["1d_likes"]),
    }

    def bucket(start, end):
        out, failed, no_tid = [], 0, 0
        for r in rows:
            d = parse_date(r[col["date"]]) if len(r) > col["date"] else None
            if not d or not (start <= d < end):
                continue
            st = str(r[col["status"]]).strip() if col["status"] is not None and len(r) > col["status"] else ""
            if st == "failed":
                failed += 1
                continue
            if st != "posted":
                continue
            tid = r[col["tid"]] if col["tid"] is not None and len(r) > col["tid"] else ""
            if not str(tid).strip():
                no_tid += 1
            views = parse_num(r[col["views"]]) if col["views"] is not None and len(r) > col["views"] else None
            out.append(
                {
                    "num": r[col["num"]] if col["num"] is not None and len(r) > col["num"] else "?",
                    "hook": r[col["hook"]] if col["hook"] is not None and len(r) > col["hook"] else "",
                    "views": views,
                    "date": d,
                }
            )
        return out, failed, no_tid

    posted, failed, no_tid = bucket(last_mon, this_mon)
    prev_posted, _, _ = bucket(prev_mon, last_mon)
    lines = ["### X"]
    with_v = [p for p in posted if p["views"] is not None]
    avg_v = statistics.mean(p["views"] for p in with_v) if with_v else None
    prev_v = [p["views"] for p in prev_posted if p["views"] is not None]
    lines.append(
        f"- 投稿 **{len(posted)}本** (views到着 {len(with_v)}本) / failed {failed}本"
        + (f" / 平均views **{fmt(avg_v)}**{pct_change(avg_v, statistics.mean(prev_v) if prev_v else None)}" if avg_v is not None else "")
    )
    for i, p in enumerate(sorted(with_v, key=lambda p: -(p["views"] or 0))[:3], 1):
        lines.append(f"- TOP{i}: {p['num']} ({p['date']:%m/%d} {p['hook']}) views {fmt(p['views'])}")
    if no_tid:
        lines.append(f"- ⚠️ tweet_id未記録 {no_tid}本")
    return "\n".join(lines) + "\n", {"n": len(posted), "top": sorted(with_v, key=lambda p: -(p['views'] or 0))[:3]}


def analyze_note(svc, last_mon, this_mon, prev_mon) -> tuple[str, dict]:
    try:
        hdr = get_values(svc, NOTE_SHEET, "'記事一覧'!A1:AZ1")
        rows = get_values(svc, NOTE_SHEET, "'記事一覧'!A2:AZ500")
    except Exception as exc:
        return f"### note\n- ⚠️ 読み取り不可: {type(exc).__name__}\n", {}
    col = {
        "title": find_col(hdr, ["タイトル"]),
        "cat": find_col(hdr, ["カテゴリ"]),
        "date": find_col(hdr, ["公開日"]),
        "status": find_col(hdr, ["ステータス"]),
        "pv": find_col(hdr, ["PV"]),
        "like": find_col(hdr, ["スキ"]),
    }
    arts = []
    for r in rows:
        if "公開" not in cell(r, col["status"]):
            continue
        arts.append(
            {
                "title": cell(r, col["title"]),
                "cat": cell(r, col["cat"]),
                "pv": parse_num(cell(r, col["pv"])) or 0,
                "like": parse_num(cell(r, col["like"])) or 0,
                "date": parse_date(cell(r, col["date"])),
            }
        )
    if not arts:
        return "### note\n- ⚠️ 公開記事なし\n", {}
    total_pv = sum(a["pv"] for a in arts)
    recent = [a for a in arts if a["date"] and last_mon <= a["date"] < this_mon]
    top = sorted(arts, key=lambda a: -a["pv"])[:3]
    lines = ["### note"]
    lines.append(f"- 公開 **{len(arts)}本** / 累計PV **{fmt(total_pv)}** / スキ計 {fmt(sum(a['like'] for a in arts))}")
    lines.append(f"- 先週の新規公開 {len(recent)}本")
    for i, a in enumerate(top, 1):
        lines.append(f"- PV TOP{i}: {a['title'][:28]}({a['cat']}) {fmt(a['pv'])}PV")
    return "\n".join(lines) + "\n", {"n": len(recent), "total_pv": total_pv}


def analyze_funnel(svc, last_mon, this_mon, prev_mon) -> tuple[str, dict]:
    """KPI日ごとデータからリスト数/プレゼント受取の週計を出す。"""
    hdr = get_values(svc, KPI_SHEET, "A1:BF3")
    col_list = find_col(hdr, ["リスト数"])
    col_gift = find_col(hdr, ["プレゼント受取数"])
    rows = get_values(svc, KPI_SHEET, "A1700:BF2200")

    def wsum(start, end, ci):
        total, days = 0, 0
        for r in rows:
            d = parse_date(str(r[0]).split(" ")[0]) if r else None
            if d and start <= d < end:
                v = parse_num(r[ci]) if ci is not None and len(r) > ci else None
                if v is not None:
                    total += v
                    days += 1
        return (total, days)

    cur_l, cur_days = wsum(last_mon, this_mon, col_list)
    prev_l, _ = wsum(prev_mon, last_mon, col_list)
    cur_g, _ = wsum(last_mon, this_mon, col_gift)

    lines = ["### ファネル(LINE登録)"]
    if cur_days == 0:
        lines.append("- 🔴 リスト数データなし(Lステップ同期停止の可能性→再ログイン)")
        return "\n".join(lines) + "\n", {}
    lines.append(f"- リスト **{cur_l:.0f}件/週**{pct_change(cur_l, prev_l or None)} / プレゼント受取 {cur_g:.0f}件")
    lines.append(f"- CSP復活モード目標=3-5件/日(週21-35)に対し **{cur_l/7:.1f}件/日**")
    return "\n".join(lines) + "\n", {"list": cur_l, "gift": cur_g}


def read_policy(svc) -> str:
    """「週ごと施策」タブの最新行を読み、先週施策の結果チェックをレポートに載せる(読み取りのみ)。"""
    try:
        vals = get_values(svc, POLICY_SHEET, "'週ごと施策'!A1:L1000")
    except Exception as exc:
        return f"## 📋 施策タブ\n- ⚠️ 読み取り不可: {type(exc).__name__}\n"
    latest = None
    for i, row in enumerate(vals[1:], 2):
        if row and str(row[0]).strip():
            latest = (i, row)
    if not latest:
        return ""
    i, row = latest

    def pcell(idx):
        return str(row[idx]).strip() if len(row) > idx else ""

    lines = ["## 📋 施策タブとの対応(週ごと施策タブ 行%d)" % i]
    lines.append(f"- 直近施策: **{pcell(0)}** 期日 {pcell(7) or '—'}")
    if pcell(6):
        lines.append(f"- 成果指標: {pcell(6)[:120]}")
    if pcell(8):
        lines.append(f"- 結果: {pcell(8)[:120]}")
    else:
        lines.append("- 結果: **未記入** ← このレポートの実績を使って記入する(Claudeに「結果書いて」でOK)")
    return "\n".join(lines) + "\n"


def build_actions(ig: dict, th: dict, x: dict) -> str:
    lines = ["## 🎯 次週の施策案(データ根拠つき・承認したらClaudeに「来週分作って」)"]
    lines.append("")
    winner = ig.get("best_hook") or ig.get("best_cat")
    if winner:
        lines.append(f"1. **IG 週2本(火・木 20:00)は勝ち筋「{winner}」を最低1本**。")
    else:
        lines.append("1. **IG 週2本(火・木 20:00)**: 先週サンプル不足→フック実験を継続。")
    if ig.get("top"):
        best = ig["top"][0]
        best_label = "/".join(x_ for x_ in (best["cat"], best["hook"]) if x_) or "-"
        lines.append(
            f"2. IG TOP1 (#{best['num']} {best_label}) の切り口を"
            f" Threads/X にリパーパス(multi-platform-flagship)。"
        )
    th_top = th.get("top") or []
    if th_top:
        lines.append(f"3. Threads: TOP1 ({th_top[0]['num']}) の型で新規2-3本 + 30分リプ戦略。")
    lines.append("4. X: キュー自動投稿を継続。failed/tweet_id欠落があれば先に修理。")
    lines.append("")
    lines.append("> 承認の仕方: このレポートを見て Claude に「来週分作って」→ 前週施策の結果を「週ごと施策」タブに記入")
    lines.append("> +来週行を同タブに追記(A〜L列に明示書き込み・appendは列ズレするので禁止)+各媒体スキルで投稿生成→キュー反映。")
    return "\n".join(lines) + "\n"


# ================================================================
# Part 3: Addy壁打ち (ADDNESS_API_KEY 直接POST・最小クライアント)
# ================================================================
ADDY_CHAT_TIMEOUT = 180  # SSE全体タイムアウト(秒)


def _addy_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _addy_create_thread(api_key: str, title: str) -> str:
    r = requests.post(
        f"{ADDNESS_API_BASE}/team/ai/threads",
        headers=_addy_headers(api_key),
        json={"title": title},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["id"]


def _addy_chat(api_key: str, thread_id: str, message: str) -> str:
    """SSEストリームを集約して本文テキストを返す(ask_addy.chat_stream の最小移植)。"""
    payload = {
        "message": message,
        "mode": "hearing_mode",
        "model": "gpt-5.4",
        "mentionedObjectiveIds": [],
        "mentionedMemberIds": [],
        "mentionedSkillIds": [],
        "userLocalTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timezone": "Asia/Tokyo",
    }
    url = f"{ADDNESS_API_BASE}/team/ai/threads/{thread_id}/chat"
    full = ""
    with requests.post(url, headers=_addy_headers(api_key), json=payload,
                       stream=True, timeout=ADDY_CHAT_TIMEOUT) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            body = line[6:]
            if body == "[DONE]":
                break
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            t = evt.get("type", "")
            if t in ("assistant-message-delta", "text-delta", "delta"):
                full += evt.get("delta") or evt.get("text") or evt.get("content") or ""
            elif t == "assistant-message-completed" and not full:
                full = (evt.get("message") or {}).get("content") or evt.get("content") or ""
    return full.strip()


def addy_sparring(week_label: str, data_summary: str) -> str:
    """週次データをAddy(判断OS)に投げて施策を2往復で壁打ちする。
    ADDNESS_API_KEY 未設定/失敗時は1行のみでdegrade(レポートは継続)。"""
    api_key = os.environ.get("ADDNESS_API_KEY", "").strip()
    if not api_key:
        return "## 🤖 Addy壁打ち\n- (Addy壁打ちはキー未設定でスキップ)\n"
    try:
        thread_id = _addy_create_thread(api_key, f"weekly_sns_cloud_{week_label}")
        r1 = _addy_chat(
            api_key, thread_id,
            f"週次SNS施策の壁打ち({week_label})。以下の実測データを踏まえ、来週の施策案を批評・具体化して。"
            f"CSP復活モード=IG週2本(火木20:00)+リスト3-5/日が主KPI。Threads/X/noteは自動投稿の補助動線。"
            f"回答は簡潔なMarkdown(箇条書き中心・800字以内)で。\n\n{data_summary}",
        )
        if not r1:
            raise RuntimeError("R1応答が空")
        r2 = _addy_chat(
            api_key, thread_id,
            "(壁打ち2巡目・同スレッド) いま挙がった施策案のうち最大リスクへの対策を1手に具体化して。"
            "さらに来週『やらないこと』を1つ追加し、施策を最大3つに絞り込んで最終形にして。"
            "回答は簡潔なMarkdown(箇条書き中心・600字以内)で。",
        )
        lines = ["## 🤖 Addy壁打ち(2往復・☁️自動)"]
        lines.append(f"- thread: {thread_id[:8]}... (本物Addy・後で差分確認可)")
        lines.append("")
        lines.append("### R1: 施策批評")
        lines.append(r1[:2500])
        if r2:
            lines.append("")
            lines.append("### R2: リスク具体化+絞り込み(最終形)")
            lines.append(r2[:2500])
        lines.append("")
        lines.append("> この壁打ち結果を叩き台に「来週分作って」でClaudeが施策タブ+投稿に落とす。")
        return "\n".join(lines) + "\n"
    except Exception as exc:
        # 例外メッセージにURLやキーが混ざる可能性があるため型名のみ出す
        print(f"⚠️ Addy壁打ち失敗(レポートは継続): {type(exc).__name__}", file=sys.stderr)
        return "## 🤖 Addy壁打ち\n- ⚠️ 今週は接続失敗(「来週分作って」の時にClaudeが手動でAddy相談する)\n"


# ================================================================
# Part 4: Discord配信 (embed + レポートmd添付)
# ================================================================
def send_discord_report(summary: str, tab_result: str, filename: str, report_md: str) -> bool:
    """Webhookへ「サマリembed + レポート全文md添付」をmultipartで送る。"""
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        print("⚠️ DISCORD_WEBHOOK 未設定 → 送信スキップ", file=sys.stderr)
        return False
    payload = {
        "username": "週次SNSループ ☁️",
        "embeds": [{
            "title": f"☁️📊 {summary.splitlines()[0][:230]}",
            "description": "\n".join(summary.splitlines()[1:])[:3500],
            "color": 0x9B59B6,  # 紫(reports)
            "fields": [
                {"name": "週次タブ更新", "value": tab_result[:1000] or "—", "inline": False},
                {"name": "レポート全文", "value": f"添付の `{filename}` を開く", "inline": False},
            ],
            "footer": {"text": "weekly_sns_rollup.py (GitHub Actions・クラウド版)"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    try:
        r = requests.post(
            webhook,
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files={"files[0]": (filename, report_md.encode("utf-8"), "text/markdown")},
            timeout=30,
        )
        r.raise_for_status()
        print("✅ Discord配信完了(embed+md添付)")
        return True
    except Exception as exc:
        # Webhook URLが例外文に含まれ得るため型名のみ
        print(f"🔴 Discord配信失敗: {type(exc).__name__}", file=sys.stderr)
        return False


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="スプシ書き込みを一切しない")
    ap.add_argument("--no-notify", action="store_true", help="Discord送信を抑止")
    ap.add_argument("--rebuild", action="store_true",
                    help="週次タブ全クリア+ヘッダー書き直し(形式改修時のみ)")
    args = ap.parse_args()
    now = datetime.now(JST)
    last_mon, this_mon, prev_mon, _ = week_range(now)
    iso_year, iso_week, _ = last_mon.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"
    report_filename = f"{week_label}-sns施策_cloud.md"

    try:
        svc = sheets_service()
    except Exception as exc:
        print(f"❌ Sheets認証失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # ---- Phase 1: 週次タブ upsert ----
    print(f"===== Phase 1: 週次タブ同期 (dry_run={args.dry_run}) =====")
    tab_failures = run_tab_sync(svc, args.rebuild, args.dry_run)
    tab_result = (
        f"🔴 失敗: {', '.join(tab_failures)}" if tab_failures
        else ("✅ 全タブOK" + (" (dry-run)" if args.dry_run else ""))
    )

    # ---- Phase 2: 週次レポート生成(読み取りのみ) ----
    print(f"===== Phase 2: 週次レポート生成 ({week_label}) =====")
    sections, ok = [], 0
    results: dict[str, dict] = {}
    for name, fn in (("ig", analyze_ig), ("threads", analyze_threads), ("x", analyze_x),
                     ("note", analyze_note), ("funnel", analyze_funnel)):
        try:
            text, data = fn(svc, last_mon, this_mon, prev_mon)
            sections.append(text)
            results[name] = data
            ok += 1
        except Exception as exc:
            sections.append(f"### {name}\n- 🔴 集計失敗: {type(exc).__name__}: {str(exc)[:80]}\n")
            results[name] = {}
    if ok == 0:
        print("❌ 全媒体の集計に失敗", file=sys.stderr)
        return 2

    # ---- Phase 3: Addy壁打ち(キーがあれば) ----
    print("===== Phase 3: Addy壁打ち =====")
    addy_section = addy_sparring(week_label, "\n".join(sections))

    report_md = "\n".join(
        [
            "---",
            "type: weekly-sns-strategy",
            f"week: {week_label}",
            f"range: {last_mon:%Y-%m-%d} 〜 {(this_mon - timedelta(days=1)):%Y-%m-%d}",
            f"generated: {now:%Y-%m-%d %H:%M} JST by weekly_sns_rollup.py (☁️クラウド版)",
            "---",
            "",
            f"# ☁️ 週次SNS施策ループ {week_label}",
            "",
            "## 📊 先週の実績",
            "",
            *sections,
            read_policy(svc),
            build_actions(results.get("ig", {}), results.get("threads", {}), results.get("x", {})),
            addy_section,
        ]
    )
    # 公開リポのためレポートはコミットしない(ファイル出力もしない)。配信はDiscord添付のみ。

    summary = (
        f"週次SNSループ {week_label}\n"
        f"IG **{results.get('ig', {}).get('n', 0)}本**"
        f" / Threads **{results.get('threads', {}).get('n', 0)}本**"
        f" / X **{results.get('x', {}).get('n', 0)}本**"
        f" / note新規 {results.get('note', {}).get('n', 0)}本\n"
        f"勝ち筋 = **{results.get('ig', {}).get('best_hook') or results.get('ig', {}).get('best_cat') or '?'}**\n"
        f"Addy壁打ち: {'✅ 2往復済' if '### R1' in addy_section else ('スキップ(キー未設定)' if 'キー未設定' in addy_section else '⚠️ 失敗')}"
    )
    print("----- サマリ -----")
    print(summary)

    # ---- Phase 4: Discord配信 ----
    if args.no_notify:
        print("===== Phase 4: Discord送信抑止(--no-notify) =====")
        print(f"[preview] 添付ファイル名: {report_filename} / 本文 {len(report_md)} 文字")
        print(report_md[:1500])
        print("... (以下略)")
    else:
        print("===== Phase 4: Discord配信 =====")
        sent = send_discord_report(summary, tab_result, report_filename, report_md)
        if not sent:
            return 2  # レポートが誰にも届かないのは失敗(沈黙成功禁止)

    if tab_failures:
        print(f"❌ 週次タブ同期に失敗あり: {tab_failures}", file=sys.stderr)
        return 2
    print("✅ 週次SNSループ(クラウド版) 完了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
