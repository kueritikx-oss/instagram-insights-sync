"""note記事の 1日後/7日後スナップショット + 考察を自動生成する。

対象: 「🔴note記事コンテンツ管理 2026」の「記事一覧」タブ。
IG fill_post_analysis.py / Threads fill_threads_analysis.py と同じ設計思想:
  - 列番号ハードコード禁止。行1ヘッダーから resolve_columns() で動的解決
  - PVパーセンタイル順位 × カテゴリ平均比 × ファネル位置 × CTA種類で考察を個別化
  - 書き込みは新設8列(1d_PV〜分析更新日時)への update のみ。append/列追加禁止
  - 既存の PV/スキ/コメント列はローカル sync_note_stats.py の担当。読み取りのみ・絶対に書かない

note固有の観点:
  - PV/スキはローカル同期(毎日20:00 JST)が累計値を上書き更新する前提。
    本スクリプトは「その累計値のスナップショット」を1d/7d時点で切り取る。
    同期が止まっていた日は多少ズレる(許容)。記録時刻は分析更新日時に残す。
  - 1dスナップショット: 公開から20〜52時間 かつ 1d_PV空 → 現在のPV/スキを記録
  - 7dスナップショット: 公開から6.5〜8.5日 かつ 7d_PV空 → 現在のPV/スキを記録
  - 窓を過ぎても空のままの過去記事は「-」を入れて対象外化し、
    現在の累計PVベースの考察だけ生成する(バックフィル)
  - noteはストック型メディア。7d/1d の伸び率でエバーグリーン度を判定

Usage:
    python3 scripts/fill_note_analysis.py             # スナップショット+空欄のみ考察生成
    python3 scripts/fill_note_analysis.py --dry-run   # 書き込みせず内容だけ表示
    python3 scripts/fill_note_analysis.py --force     # 既存の考察も再生成
    python3 scripts/fill_note_analysis.py --rows 2-10 # 対象行を絞る
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SHEET_ID = "1gIW_SCigwa5wFPnQVRoFM3EhOaYC3aC_DtkMbqK8gRw"
SHEET_NAME = "記事一覧"
HEADER_ROW = 1
DATA_START_ROW = 2

JST = timezone(timedelta(hours=9))

GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"
SA_KEY_FILE = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-dashboard/service_account_key.json"

# スナップショット窓(公開からの経過時間)
SNAP_1D_MIN_H, SNAP_1D_MAX_H = 20.0, 52.0
SNAP_7D_MIN_H, SNAP_7D_MAX_H = 6.5 * 24, 8.5 * 24

BACKFILL_MARK = "-"  # 窓を逃した記事の対象外化マーク

# ── Column indices (0-based)。resolve_columns() が行1ヘッダーから動的解決 ──
COL_NUM = None        # '#'
COL_TITLE = None      # タイトル
COL_CATEGORY = None   # カテゴリ
COL_STATUS = None     # ステータス
COL_PUB_DATE = None   # 公開日
COL_PUB_TIME = None   # 時間
COL_FUNNEL = None     # ファネル位置
COL_CTA = None        # CTA種類
COL_URL = None        # note URL
COL_PV = None         # PV(累計・読み取りのみ)
COL_SUKI = None       # スキ(累計・読み取りのみ)
COL_COMMENT = None    # コメント(読み取りのみ)
# 書き込み先8列(2026-07-12 新設)
COL_1D_PV = None
COL_1D_SUKI = None
COL_7D_PV = None
COL_7D_SUKI = None
COL_RESULT = None     # 結果
COL_ANALYSIS = None   # 考察・仮説
COL_NEXT = None       # 次に活かすポイント
COL_UPDATED = None    # 分析更新日時

# (変数名, 行1の列名候補, 必須か)
_COLUMN_SPECS = [
    ("COL_NUM",      ("#",),                True),
    ("COL_TITLE",    ("タイトル",),         True),
    ("COL_CATEGORY", ("カテゴリ",),         True),
    ("COL_STATUS",   ("ステータス",),       True),
    ("COL_PUB_DATE", ("公開日",),           True),
    ("COL_PUB_TIME", ("時間",),             False),
    ("COL_FUNNEL",   ("ファネル位置",),     False),
    ("COL_CTA",      ("CTA種類",),          False),
    ("COL_URL",      ("note URL",),         False),
    ("COL_PV",       ("PV",),               True),
    ("COL_SUKI",     ("スキ",),             True),
    ("COL_COMMENT",  ("コメント",),         False),
    ("COL_1D_PV",    ("1d_PV",),            True),
    ("COL_1D_SUKI",  ("1d_スキ",),          True),
    ("COL_7D_PV",    ("7d_PV",),            True),
    ("COL_7D_SUKI",  ("7d_スキ",),          True),
    ("COL_RESULT",   ("結果",),             True),
    ("COL_ANALYSIS", ("考察・仮説",),       True),
    ("COL_NEXT",     ("次に活かすポイント",), True),
    ("COL_UPDATED",  ("分析更新日時",),     True),
]


def col_letter(idx: int) -> str:
    """0-based index → A1形式の列文字"""
    letters = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def get_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if service_account_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(service_account_json)
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
            return build("sheets", "v4", credentials=creds)
    if service_account_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
        return build("sheets", "v4", credentials=creds)
    if SA_KEY_FILE.exists() and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(str(SA_KEY_FILE), scopes=scopes)
        return build("sheets", "v4", credentials=creds)

    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
    else:
        with open(TOKEN_FILE) as f:
            info = json.load(f)
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=scopes,
    )
    return build("sheets", "v4", credentials=creds)


def resolve_columns(service) -> None:
    """行1ヘッダーを読み、COL_* を名前で動的解決する。

    必須列が見つからない場合は即エラー終了(ズレたまま書き込むより安全)。
    完全一致で解決するため PV / 1d_PV / 7d_PV は混同しない。
    """
    r = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!{HEADER_ROW}:{HEADER_ROW}",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = r.get("values", [])
    if not rows:
        raise SystemExit(f"❌ ヘッダー行が空: {SHEET_NAME}")
    header = [str(v).strip() for v in rows[0]]

    resolved: Dict[str, Optional[int]] = {}
    missing: List[str] = []
    for var, names, required in _COLUMN_SPECS:
        matches = [i for i, h in enumerate(header) if h in names]
        if len(matches) > 1:
            raise SystemExit(f"❌ 列名が重複しています: {names} → {matches}")
        if matches:
            resolved[var] = matches[0]
        elif required:
            missing.append(f"{var}({'/'.join(names)})")
        else:
            resolved[var] = None

    if missing:
        raise SystemExit(
            "❌ 必須列がヘッダーから見つかりません(列挿入/改名の可能性):\n  "
            + "\n  ".join(missing)
        )

    globals().update(resolved)

    # 書き込み先の連続性検証(範囲書き込みの前提)
    if [COL_1D_PV, COL_1D_SUKI] != list(range(COL_1D_PV, COL_1D_PV + 2)):
        raise SystemExit(f"❌ 1d_PV/1d_スキ が連続していません: {[COL_1D_PV, COL_1D_SUKI]}")
    if [COL_7D_PV, COL_7D_SUKI] != list(range(COL_7D_PV, COL_7D_PV + 2)):
        raise SystemExit(f"❌ 7d_PV/7d_スキ が連続していません: {[COL_7D_PV, COL_7D_SUKI]}")
    write4 = [COL_RESULT, COL_ANALYSIS, COL_NEXT, COL_UPDATED]
    if write4 != list(range(COL_RESULT, COL_RESULT + 4)):
        raise SystemExit(f"❌ 結果〜分析更新日時の4列が連続していません: {write4}")
    # 既存PV/スキ列(読み取り専用)に書き込み先が被っていないことを保証
    write_cols = set(range(COL_1D_PV, COL_1D_SUKI + 1)) | set(range(COL_7D_PV, COL_7D_SUKI + 1)) | set(write4)
    if {COL_PV, COL_SUKI} & write_cols:
        raise SystemExit("❌ 書き込み先が既存PV/スキ列と衝突しています")
    print(f"🧭 列マップ解決: PV={col_letter(COL_PV)} スキ={col_letter(COL_SUKI)} "
          f"1d={col_letter(COL_1D_PV)}:{col_letter(COL_1D_SUKI)} "
          f"7d={col_letter(COL_7D_PV)}:{col_letter(COL_7D_SUKI)} "
          f"結果〜更新日時={col_letter(COL_RESULT)}:{col_letter(COL_UPDATED)}")


def safe_int(val: str) -> int:
    if not val or not str(val).strip():
        return 0
    try:
        return int(float(str(val).strip().replace(",", "")))
    except ValueError:
        return 0


def cell(row: list, idx: Optional[int]) -> str:
    if idx is not None and idx < len(row):
        return row[idx].strip() if isinstance(row[idx], str) else str(row[idx])
    return ""


def parse_published_at(date_str: str, time_str: str) -> Optional[datetime]:
    """公開日+時間 → JST datetime。パース不能なら None"""
    ds = date_str.strip().replace("/", "-")
    if not ds:
        return None
    try:
        d = datetime.strptime(ds, "%Y-%m-%d")
    except ValueError:
        return None
    hh, mm = 12, 0
    ts = time_str.strip()
    if ts:
        try:
            parts = ts.split(":")
            hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            pass
    return d.replace(hour=hh, minute=mm, tzinfo=JST)


def percentile_rank(value: float, sorted_values: List[float]) -> int:
    """値のパーセンタイル順位を返す(上位X%)"""
    if not sorted_values:
        return 50
    count_below = sum(1 for v in sorted_values if v < value)
    rank = (count_below / len(sorted_values)) * 100
    return 100 - int(rank)


def compute_benchmarks(published: List[Dict]) -> Dict[str, Any]:
    """全公開記事からベンチマーク統計を算出(累計PVベース)"""
    bm: Dict[str, Any] = {}
    if not published:
        return bm
    pvs = sorted(p["pv"] for p in published)
    sukis = sorted(p["suki"] for p in published)
    bm["all"] = {
        "pv": pvs,
        "suki": sukis,
        "mean_pv": statistics.mean(pvs),
        "median_pv": statistics.median(pvs),
        "mean_suki": statistics.mean(sukis),
        "count": len(published),
    }

    for key in ("category", "funnel", "cta"):
        groups = defaultdict(list)
        for p in published:
            if p[key]:
                groups[p[key]].append(p)
        bm[key] = {}
        for name, posts in groups.items():
            if len(posts) >= 2:
                bm[key][name] = {
                    "mean_pv": statistics.mean(pp["pv"] for pp in posts),
                    "mean_suki": statistics.mean(pp["suki"] for pp in posts),
                    "count": len(posts),
                }

    bm["top_pv"] = sorted(published, key=lambda p: p["pv"], reverse=True)[:10]
    return bm


def generate_result(p: Dict, bm: Dict) -> str:
    """結果: 累計PVパーセンタイル + カテゴリ平均比"""
    pv_pct = percentile_rank(p["pv"], bm["all"]["pv"])
    n = bm["all"]["count"]
    parts = [f"PV {p['pv']:,}（全{n}記事中 上位{pv_pct}%）"]

    cb = bm.get("category", {}).get(p["category"])
    if cb and cb["mean_pv"] > 0:
        ratio = (p["pv"] - cb["mean_pv"]) / cb["mean_pv"] * 100
        sign = "+" if ratio >= 0 else ""
        parts.append(f"カテゴリ「{p['category']}」平均{cb['mean_pv']:.1f}比{sign}{ratio:.0f}%（{cb['count']}本）")

    engagement = f"スキ{p['suki']}"
    if p["comment"] > 0:
        engagement += f" コメント{p['comment']}"
    parts.append(engagement)
    return " | ".join(parts)


def generate_analysis(p: Dict, bm: Dict) -> str:
    """考察・仮説: カテゴリ×ファネル位置×CTA種類×数値根拠で個別化"""
    insights = []
    pv_pct = percentile_rank(p["pv"], bm["all"]["pv"])
    cat = p["category"]
    funnel = p["funnel"]
    cta = p["cta"]
    cb = bm.get("category", {}).get(cat)
    fb = bm.get("funnel", {}).get(funnel)
    mean_pv = bm["all"]["mean_pv"]

    # ── 1. なぜ読まれた/読まれなかった(PV軸) ──
    if pv_pct <= 15:
        top_cats = [t["category"] for t in bm.get("top_pv", [])[:5]]
        if cb and p["pv"] >= cb["mean_pv"]:
            insights.append(
                f"PV {p['pv']:,}は全体上位{pv_pct}%かつ「{cat}」平均{cb['mean_pv']:.1f}超え。"
                f"タイトルの具体性（数字・断言）が検索/タイムラインで刺さったと考えられる")
        else:
            insights.append(
                f"PV {p['pv']:,}は全体上位{pv_pct}%。「{cat}」テーマ自体の検索需要が読まれた主因と推測")
        if cat and top_cats.count(cat) >= 2:
            insights.append(f"「{cat}」はPV TOP5のうち{top_cats.count(cat)}本を占める勝ちテーマ")
    elif pv_pct <= 35:
        insights.append(f"PV {p['pv']:,}（上位{pv_pct}%）で平均{mean_pv:.1f}を上回る安定圏")
    elif pv_pct >= 70:
        if cb and cb["mean_pv"] > mean_pv and p["pv"] < cb["mean_pv"] * 0.7:
            insights.append(
                f"PV {p['pv']:,}は下位{100 - pv_pct}%。「{cat}」自体は平均{cb['mean_pv']:.1f}と需要がある"
                f"テーマなので、タイトルの引き（検索キーワード・数字）が原因の可能性が高い")
        else:
            insights.append(
                f"PV {p['pv']:,}は下位{100 - pv_pct}%。noteは検索とSNS流入が入口のため、"
                f"タイトルの検索性か外部導線（IG/Threadsからの誘導）不足が主因と考えられる")
    else:
        insights.append(f"PV {p['pv']:,}（上位{pv_pct}%）は中位圏")

    # ── 2. ファネル位置との整合 ──
    if funnel and fb and fb["count"] >= 3:
        if p["pv"] > fb["mean_pv"] * 1.5:
            insights.append(f"ファネル「{funnel}」記事の平均PV {fb['mean_pv']:.1f}を大きく超える当たり回")
        elif "認知" in funnel and p["pv"] < fb["mean_pv"] * 0.6:
            insights.append(
                f"認知目的なのにPVが「{funnel}」平均{fb['mean_pv']:.1f}を下回り、入口の役割を果たせていない")

    # ── 3. 反応の質(スキ率) ──
    if p["pv"] > 0:
        suki_rate = p["suki"] / p["pv"] * 100
        if p["suki"] >= 1 and suki_rate >= 5:
            insights.append(
                f"スキ率{suki_rate:.1f}%（スキ{p['suki']}/PV{p['pv']}）と反応密度が濃く、"
                f"届けば刺さる内容。露出を増やす価値がある")
        elif p["suki"] == 0 and p["pv"] >= mean_pv:
            insights.append(
                "露出量に対してスキ0。読了前の離脱か、共感で締める段落（実体験・Before/After）の不足が考えられる")

    # ── 4. CTAとの整合 ──
    if cta and "LINE" in cta and p["pv"] < bm["all"]["median_pv"]:
        insights.append(
            f"CTA「{cta}」はPVが母数。まず記事への流入導線（IG/Threadsプロフィール・関連記事リンク）を太くする段階")

    # ── 5. 1d/7d 伸び率(エバーグリーン度) ──
    if p["pv_1d"] > 0 and p["pv_7d"] > 0:
        growth = (p["pv_7d"] - p["pv_1d"]) / p["pv_1d"] * 100
        if growth >= 50:
            insights.append(
                f"1d→7d PV {p['pv_1d']:,}→{p['pv_7d']:,}（+{growth:.0f}%）と後伸びしており、"
                f"検索経由で読まれ続けるエバーグリーン素材。内部リンクのハブにする価値が高い")
        elif growth >= 15:
            insights.append(f"1d→7d PV {p['pv_1d']:,}→{p['pv_7d']:,}（+{growth:.0f}%）で初速後も継続流入あり")
        else:
            insights.append(
                f"1d→7d PV {p['pv_1d']:,}→{p['pv_7d']:,}（+{growth:.0f}%）で初速型。SNS流入依存のため、"
                f"公開直後の拡散設計が数字を決める")

    return "。".join(insights[:4])


def generate_next_action(p: Dict, bm: Dict) -> str:
    """次に活かすポイント: 具体的で再現可能なアクション"""
    actions = []
    pv_pct = percentile_rank(p["pv"], bm["all"]["pv"])
    cat = p["category"]
    cb = bm.get("category", {}).get(cat)

    if pv_pct <= 15:
        actions.append(f"「{cat}」×このタイトル構造は勝ちパターン。同カテゴリで切り口を変えてシリーズ化する")
        if p["suki"] >= 1:
            actions.append("スキが付いた記事はIGストーリーズ/Threadsで「反響あった記事」として再拡散する")
    if pv_pct >= 60:
        top = bm.get("top_pv", [])
        if top:
            t = top[0]
            if t["num"] != p["num"]:
                actions.append(
                    f"TOP記事#{t['num']}「{t['title'][:20]}…」（PV {t['pv']:,}）のタイトル型"
                    f"（数字+具体ベネフィット）に寄せてリライトを検討")
        actions.append("IG/Threadsの関連投稿から本記事へのUTM付き導線を1本追加して外部流入を作る")
    if p["suki"] == 0 and p["pv"] >= bm["all"]["median_pv"]:
        actions.append("記事末尾を実体験の一言+問いかけで締めてスキを押す理由を作る")
    if p["pv_1d"] > 0 and p["pv_7d"] > 0 and p["pv_7d"] >= p["pv_1d"] * 1.5:
        actions.append("後伸び記事なので、新記事から本記事への内部リンクを張って検索流入を回遊させる")

    if not actions:
        if cb and cb["count"] >= 3 and p["pv"] < cb["mean_pv"]:
            actions.append(f"「{cat}」平均PV {cb['mean_pv']:.1f}未達。タイトル先頭に数字か「保存版」を入れて再テスト")
        else:
            actions.append("中位圏。タイトルに検索キーワード+数字を入れ、公開直後にIGストーリーズで初速を作る")

    return "。".join(actions[:3])


def main():
    parser = argparse.ArgumentParser(description="note記事の1d/7dスナップショット+考察を自動生成")
    parser.add_argument("--force", action="store_true", help="既存の考察も再生成")
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず内容だけ表示")
    parser.add_argument("--rows", type=str, default=None, help="対象行範囲（例: 2-30）")
    args = parser.parse_args()

    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    print(f"🕐 実行時刻: {now_str} JST")

    service = get_service()
    resolve_columns(service)

    last_col = col_letter(max(COL_1D_PV, COL_1D_SUKI, COL_7D_PV, COL_7D_SUKI,
                              COL_RESULT, COL_ANALYSIS, COL_NEXT, COL_UPDATED))
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!A{DATA_START_ROW}:{last_col}1000",
    ).execute()
    raw_rows = result.get("values", [])
    print(f"📊 {len(raw_rows)}行を読み込み")

    published: List[Dict] = []
    for offset, row in enumerate(raw_rows):
        num = cell(row, COL_NUM)
        status = cell(row, COL_STATUS)
        if not num or status != "公開済み":
            continue
        p = {
            "row_idx": DATA_START_ROW + offset,
            "num": num,
            "title": cell(row, COL_TITLE),
            "category": cell(row, COL_CATEGORY),
            "funnel": cell(row, COL_FUNNEL),
            "cta": cell(row, COL_CTA),
            "url": cell(row, COL_URL),
            "pv": safe_int(cell(row, COL_PV)),
            "suki": safe_int(cell(row, COL_SUKI)),
            "comment": safe_int(cell(row, COL_COMMENT)),
            "published_at": parse_published_at(cell(row, COL_PUB_DATE), cell(row, COL_PUB_TIME)),
            "raw_1d_pv": cell(row, COL_1D_PV),
            "raw_7d_pv": cell(row, COL_7D_PV),
            "pv_1d": safe_int(cell(row, COL_1D_PV)),
            "suki_1d": safe_int(cell(row, COL_1D_SUKI)),
            "pv_7d": safe_int(cell(row, COL_7D_PV)),
            "suki_7d": safe_int(cell(row, COL_7D_SUKI)),
            "existing_result": cell(row, COL_RESULT),
            "existing_analysis": cell(row, COL_ANALYSIS),
        }
        published.append(p)

    print(f"📋 公開済み記事: {len(published)}件")
    if not published:
        raise SystemExit("❌ 公開済み記事が0件。ステータス列の値かシート構造を確認してください")

    if args.rows:
        start, end = args.rows.split("-")
        row_start, row_end = int(start), int(end)
    else:
        row_start, row_end = DATA_START_ROW, 10**6

    # ── Phase 1: スナップショット判定 ──
    snap_updates: List[Dict] = []
    snap_1d = snap_7d = backfilled = 0
    for p in published:
        if p["row_idx"] < row_start or p["row_idx"] > row_end:
            continue
        pub = p["published_at"]
        if pub is None:
            print(f"⚠️  行{p['row_idx']} #{p['num']}: 公開日をパースできずスナップショット対象外")
            continue
        age_h = (now - pub).total_seconds() / 3600
        snapped = False

        if not p["raw_1d_pv"]:
            if SNAP_1D_MIN_H <= age_h <= SNAP_1D_MAX_H:
                snap_updates.append({
                    "range": f"'{SHEET_NAME}'!{col_letter(COL_1D_PV)}{p['row_idx']}:{col_letter(COL_1D_SUKI)}{p['row_idx']}",
                    "values": [[p["pv"], p["suki"]]],
                })
                p["pv_1d"], p["suki_1d"] = p["pv"], p["suki"]
                p["raw_1d_pv"] = str(p["pv"])
                snap_1d += 1
                snapped = True
                print(f"📸 1d snapshot 行{p['row_idx']} #{p['num']}: PV={p['pv']} スキ={p['suki']}（公開から{age_h:.0f}h）")
            elif age_h > SNAP_1D_MAX_H:
                snap_updates.append({
                    "range": f"'{SHEET_NAME}'!{col_letter(COL_1D_PV)}{p['row_idx']}:{col_letter(COL_1D_SUKI)}{p['row_idx']}",
                    "values": [[BACKFILL_MARK, BACKFILL_MARK]],
                })
                p["raw_1d_pv"] = BACKFILL_MARK
                backfilled += 1
                print(f"⏭️  行{p['row_idx']} #{p['num']}: 1d窓を超過（{age_h / 24:.0f}日前公開）→「{BACKFILL_MARK}」で対象外化")

        if not p["raw_7d_pv"]:
            if SNAP_7D_MIN_H <= age_h <= SNAP_7D_MAX_H:
                snap_updates.append({
                    "range": f"'{SHEET_NAME}'!{col_letter(COL_7D_PV)}{p['row_idx']}:{col_letter(COL_7D_SUKI)}{p['row_idx']}",
                    "values": [[p["pv"], p["suki"]]],
                })
                p["pv_7d"], p["suki_7d"] = p["pv"], p["suki"]
                p["raw_7d_pv"] = str(p["pv"])
                p["snapped_7d"] = True
                snap_7d += 1
                snapped = True
                print(f"📸 7d snapshot 行{p['row_idx']} #{p['num']}: PV={p['pv']} スキ={p['suki']}（公開から{age_h / 24:.1f}日）")
            elif age_h > SNAP_7D_MAX_H:
                snap_updates.append({
                    "range": f"'{SHEET_NAME}'!{col_letter(COL_7D_PV)}{p['row_idx']}:{col_letter(COL_7D_SUKI)}{p['row_idx']}",
                    "values": [[BACKFILL_MARK, BACKFILL_MARK]],
                })
                p["raw_7d_pv"] = BACKFILL_MARK

        p["snapped_now"] = snapped

    # ── Phase 2: 考察生成 ──
    bm = compute_benchmarks(published)
    print(f"📈 ベンチマーク算出（公開{bm['all']['count']}記事）: "
          f"PV平均{bm['all']['mean_pv']:.1f} 中央値{bm['all']['median_pv']:.1f} スキ平均{bm['all']['mean_suki']:.2f}")

    analysis_updates: List[Dict] = []
    generated = skipped = 0
    for p in published:
        if p["row_idx"] < row_start or p["row_idx"] > row_end:
            continue
        # 考察対象: 1d_PV に値がある(数値 or 「-」=バックフィル)記事
        if not p["raw_1d_pv"]:
            skipped += 1
            continue
        regen = args.force or p.get("snapped_7d") or p.get("snapped_now")
        if not regen and p["existing_result"] and p["existing_analysis"]:
            skipped += 1
            continue

        res = generate_result(p, bm)
        analysis = generate_analysis(p, bm)
        next_action = generate_next_action(p, bm)

        if args.dry_run:
            print(f"\n{'=' * 60}")
            print(f"Row{p['row_idx']} #{p['num']} [{p['category']}/{p['funnel']}/{p['cta']}] {p['title'][:36]!r}")
            print(f"  結果: {res}")
            print(f"  考察: {analysis}")
            print(f"  次: {next_action}")

        analysis_updates.append({
            "range": f"'{SHEET_NAME}'!{col_letter(COL_RESULT)}{p['row_idx']}:{col_letter(COL_UPDATED)}{p['row_idx']}",
            "values": [[res, analysis, next_action, now_str]],
        })
        generated += 1

    print(f"\n📝 スナップショット: 1d={snap_1d}件 7d={snap_7d}件 バックフィル対象外化={backfilled}件")
    print(f"📝 考察生成: {generated}件 | スキップ: {skipped}件")

    if args.dry_run:
        print("\n🔍 dry-runモード: 書き込みはしません")
        return

    all_updates = snap_updates + analysis_updates
    if not all_updates:
        print("✅ 更新対象なし（全記事が処理済み）")
        return

    print(f"\n⬆️  {len(all_updates)}レンジをスプレッドシートに書き込み中...")
    batch_size = 50
    for i in range(0, len(all_updates), batch_size):
        batch = all_updates[i:i + batch_size]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW",
                  "data": [{"range": u["range"], "majorDimension": "ROWS", "values": u["values"]} for u in batch]},
        ).execute()
        print(f"  バッチ {i // batch_size + 1}: {len(batch)}レンジ更新完了")
        if i + batch_size < len(all_updates):
            time.sleep(1)

    # ── Phase 3: 読み戻し検証 ──
    print("\n🔎 読み戻し検証中...")
    verify_ranges = [u["range"] for u in all_updates]
    mismatches = []
    for i in range(0, len(verify_ranges), batch_size):
        chunk = verify_ranges[i:i + batch_size]
        r = service.spreadsheets().values().batchGet(
            spreadsheetId=SHEET_ID, ranges=chunk,
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        for u, vr in zip(all_updates[i:i + batch_size], r.get("valueRanges", [])):
            got = [str(v).strip().replace(",", "") for v in (vr.get("values") or [[]])[0]]
            want = [str(v).strip().replace(",", "") for v in u["values"][0]]
            got += [""] * (len(want) - len(got))
            if got[:len(want)] != want:
                mismatches.append(f"{u['range']}: 期待{want} 実際{got[:len(want)]}")
    if mismatches:
        print("❌ 読み戻し検証に失敗:")
        for m in mismatches[:10]:
            print("  " + m)
        sys.exit(2)
    print(f"✅ 読み戻し検証OK（{len(verify_ranges)}レンジ一致）")
    print(f"\n✅ 完了: スナップショット{snap_1d + snap_7d}件 / バックフィル{backfilled}件 / 考察{generated}件")


if __name__ == "__main__":
    main()
