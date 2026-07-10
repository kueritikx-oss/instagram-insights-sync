#!/usr/bin/env python3
"""
Instagram投稿毎データ スプレッドシートの列位置を
ヘッダーから自動検出する共通モジュール。

問題の背景:
  スプシに列が挿入されるとハードコード列番号がズレて、
  データが間違った列に書き込まれる（サイレント破損）。
  2026-04-10に発覚: 2列挿入→insights syncが4日間停止。

使い方:
  from sheet_column_map import load_column_map, validate_columns

  col = load_column_map(sheets_service, SHEET_ID)
  # col["reach_1d"] → 24 (Y列) を自動検出

  validate_columns(col)  # 期待と違えば即エラー
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


# ========== ヘッダー行3（0-based index 2）のキーワード → 列名マッピング ==========
# キー: 内部名, 値: (検索キーワード, セクション制約)
# セクションは行1のヘッダーで判定（"1日後データ", "1週間後データ" 等）

HEADER_ROW = 2  # 0-based（3行目）

# 行1のセクションヘッダー
SECTION_1DAY = "1日後データ"
SECTION_7DAY = "1週間後データ"
SECTION_META = "取得メタ"
SECTION_TAGS = None  # セクション不問
SECTION_AUTO = "自動投稿"

# 検索定義: (内部名, 行3キーワード, セクション, 一致方式)
COLUMN_DEFINITIONS = [
    # --- 基本情報 ---
    ("date",         "日付",         None, "exact"),
    ("thumbnail",    "サムネ",       None, "exact"),
    ("number",       "番号",         None, "exact"),
    ("time",         "時刻",         None, "exact"),
    ("filename",     "ファイル名",   None, "exact"),
    ("post_type",    "投稿種別",     None, "exact"),
    ("format",       ("形式", "投稿目的"), None, "exact"),
    ("url",          "URL",          None, "exact"),
    ("caption",      "キャプション", None, "exact"),

    # --- 1日後データ ---
    ("reach_1d",         "全体",         SECTION_1DAY, "first"),
    ("reach_fw_1d",      "フォロワー",   SECTION_1DAY, "first"),
    ("reach_nfw_1d",     "フォロー外",   SECTION_1DAY, "first"),
    ("views_1d",         "再生数",       SECTION_1DAY, "first"),
    ("initial_plays_1d", "初回の再生",   SECTION_1DAY, "exact"),
    ("play_time_1d",     "再生時間",     SECTION_1DAY, "first"),
    ("avg_play_time_1d", "平均再生時間", SECTION_1DAY, "first"),
    ("engagement_1d",    "全体",         SECTION_1DAY, "second"),  # 2番目の「全体」
    ("likes_1d",         "いいね",       SECTION_1DAY, "first"),
    ("saved_1d",         "保存",         SECTION_1DAY, "first"),
    ("comments_1d",      "コメント",     SECTION_1DAY, "first"),
    ("shares_1d",        "シェア",       SECTION_1DAY, "first"),
    ("profile_visits_1d","プロフアクセス",SECTION_1DAY, "first"),
    ("follows_1d",       "フォロー",     SECTION_1DAY, "first"),
    ("save_rate_1d",     "保存率",       SECTION_1DAY, "first"),

    # --- 1週間後データ ---
    ("reach_7d",         "全体",         SECTION_7DAY, "first"),
    ("views_7d",         "再生回数",     SECTION_7DAY, "first"),
    ("reposts_7d",       "再シェア",     SECTION_7DAY, "first"),
    ("play_time_7d",     "再生時間",     SECTION_7DAY, "first"),
    ("avg_play_time_7d", "平均再生時間", SECTION_7DAY, "first"),
    ("engagement_7d",    "全体",         SECTION_7DAY, "second"),
    ("likes_7d",         "いいね",       SECTION_7DAY, "first"),
    ("saved_7d",         "保存",         SECTION_7DAY, "first"),
    ("comments_7d",      "コメント",     SECTION_7DAY, "first"),
    ("shares_7d",        "シェア",       SECTION_7DAY, "first"),
    ("profile_visits_7d","プロフアクセス",SECTION_7DAY, "first"),
    ("follows_7d",       "フォロー",     SECTION_7DAY, "first"),
    ("save_rate_7d",     "保存率",       SECTION_7DAY, "first"),

    # --- 取得メタ ---
    ("captured_at_1d",   "1日後_取得日時",   None, "exact"),
    ("capture_mode_1d",  "1日後_取得区分",   None, "exact"),
    ("captured_at_7d",   "1週間後_取得日時", None, "exact"),
    ("capture_mode_7d",  "1週間後_取得区分", None, "exact"),
    ("latest_captured",  "最新取得日時",     None, "exact"),

    # --- 分類タグ ---
    ("tag_category",     "大分類",       None, "exact"),
    ("tag_subcategory",  "小分類",       None, "exact"),
    ("tag_hook",         "フック型",     None, "exact"),
    ("tag_cta",          "CTA型",        None, "exact"),
    ("tag_emotion",      "感情トリガー", None, "last"),   # 最後の一致（分類タグ側）
    ("tag_target",       "狙う指標",     None, "exact"),
    ("post_status",      "投稿ステータス", None, "exact"),

    # --- 自動投稿 ---
    ("image_urls",       "画像URLs",     None, "exact"),
    ("media_id",         "メディアID",   None, "exact"),
    ("post_error",       "投稿エラー",   None, "exact"),
    ("post_retry",       "リトライ回数", SECTION_AUTO, "first"),
    ("post_last_attempt", "最終投稿試行", SECTION_AUTO, "first"),
]


def col_letter(idx: int) -> str:
    """0-based列番号 → A, B, ..., Z, AA, AB, ..."""
    result = ""
    n = idx
    while n >= 0:
        result = chr(n % 26 + 65) + result
        n = n // 26 - 1
    return result


def _find_section_range(row1: List[str], section_name: str) -> tuple[int, int]:
    """行1のセクションヘッダーから開始〜次セクションの列範囲を返す"""
    start = None
    for i, val in enumerate(row1):
        if val and section_name in str(val):
            start = i
        elif start is not None and val and str(val).strip():
            return start, i
    if start is not None:
        return start, len(row1)
    return 0, len(row1)


def load_column_map(
    sheets_service,
    spreadsheet_id: str,
    tab_name: str = "Instagram投稿毎データ",
) -> Dict[str, int]:
    """
    ヘッダー行を読んで列名→列番号(0-based)の辞書を返す。

    Returns:
        {"reach_1d": 24, "views_1d": 33, ...}
    """
    r = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!1:3",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    header_rows = r.get("values", [])
    if len(header_rows) < 3:
        raise ValueError(f"ヘッダーが3行未満: {tab_name}")

    row1 = header_rows[0]
    row3 = header_rows[2]

    col_map: Dict[str, int] = {}

    for name, keyword, section, match_mode in COLUMN_DEFINITIONS:
        # セクション制約で検索範囲を絞る
        if section:
            start, end = _find_section_range(row1, section)
        else:
            start, end = 0, len(row3)

        # 行3から検索
        matches = []
        keywords: Sequence[str] = keyword if isinstance(keyword, tuple) else (keyword,)
        for i in range(start, min(end, len(row3))):
            cell = str(row3[i]).strip() if i < len(row3) else ""
            if match_mode == "exact" and cell in keywords:
                matches.append(i)
            elif match_mode in ("first", "second", "last") and cell in keywords:
                matches.append(i)

        if match_mode == "first" and matches:
            col_map[name] = matches[0]
        elif match_mode == "second" and len(matches) >= 2:
            col_map[name] = matches[1]
        elif match_mode == "last" and matches:
            col_map[name] = matches[-1]
        elif match_mode == "exact" and matches:
            col_map[name] = matches[0]

    return col_map


def validate_columns(
    col_map: Dict[str, int],
    required: Optional[List[str]] = None,
    caller: str = "",
) -> None:
    """
    必須列が全て検出されたか検証。欠けていればエラー。

    Args:
        col_map: load_column_map() の戻り値
        required: 検証する列名リスト（Noneなら全定義を検証）
        caller: エラーメッセージ用の呼び出し元スクリプト名
    """
    if required is None:
        required = [name for name, _, _, _ in COLUMN_DEFINITIONS]

    missing = [name for name in required if name not in col_map]
    if missing:
        prefix = f"[{caller}] " if caller else ""
        raise ValueError(
            f"{prefix}以下の列がスプシで見つかりません（列挿入/削除の可能性）:\n"
            f"  {', '.join(missing)}\n"
            f"検出済み: {len(col_map)}列 / 必須: {len(required)}列"
        )


def build_metric_map(
    col_map: Dict[str, int],
    block: str,
) -> Dict[str, int]:
    """
    sync_instagram_insights.py 用: API メトリクス名 → 列番号の辞書を生成。

    Args:
        col_map: load_column_map() の戻り値
        block: "1day" or "7day"
    """
    suffix = "_1d" if block == "1day" else "_7d"
    mapping = {
        "reach":              col_map.get(f"reach{suffix}"),
        "views":              col_map.get(f"views{suffix}"),
        "total_interactions":  col_map.get(f"engagement{suffix}"),
        "likes":              col_map.get(f"likes{suffix}"),
        "saved":              col_map.get(f"saved{suffix}"),
        "comments":           col_map.get(f"comments{suffix}"),
        "shares":             col_map.get(f"shares{suffix}"),
        "profile_visits":     col_map.get(f"profile_visits{suffix}"),
        "follows":            col_map.get(f"follows{suffix}"),
    }
    return {k: v for k, v in mapping.items() if v is not None}


def build_ext_metric_map(
    col_map: Dict[str, int],
    block: str,
) -> Dict[str, int]:
    """Reels拡張メトリクス用の列マッピング"""
    suffix = "_1d" if block == "1day" else "_7d"
    mapping = {
        "ig_reels_avg_watch_time":          col_map.get(f"avg_play_time{suffix}"),
        "ig_reels_video_view_total_time":   col_map.get(f"play_time{suffix}"),
    }
    if block == "7day":
        mapping["reposts"] = col_map.get("reposts_7d")
    return {k: v for k, v in mapping.items() if v is not None}


def print_column_map(col_map: Dict[str, int]) -> None:
    """デバッグ用: 検出された列マップを表示"""
    print(f"列マップ: {len(col_map)}列検出")
    for name, idx in sorted(col_map.items(), key=lambda x: x[1]):
        print(f"  {col_letter(idx):>3}({idx:>3}): {name}")


if __name__ == "__main__":
    """単体実行: 現在のスプシからヘッダーを読んで列マップを表示"""
    import os
    from pathlib import Path
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as build_service

    BASE_DIR = Path.home() / "Projects" / "事業"
    TOKEN_FILE = BASE_DIR / "タッキー/02_SNS集客/instagram-auto-post/token.json"
    SA_FILE = BASE_DIR / "タッキー/02_SNS集客/instagram-dashboard/service_account_key.json"
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"

    # 2026-07-11: Service Account優先(失効しない)。旧OAuth token.jsonはinvalid_grantで引退済み。
    if SA_FILE.exists() and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(str(SA_FILE), scopes=SCOPES)
    else:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
    service = build_service("sheets", "v4", credentials=creds)

    col = load_column_map(service, SHEET_ID)
    print_column_map(col)

    # 検証
    try:
        validate_columns(col, caller="sheet_column_map.py")
        print("\n全列検出OK")
    except ValueError as e:
        print(f"\n{e}")

    # sync_instagram_insights.py 用マッピング表示
    print("\n--- sync_instagram_insights.py 用 ---")
    m1 = build_metric_map(col, "1day")
    print(f"METRIC_TO_COL_1DAY = {m1}")
    m7 = build_metric_map(col, "7day")
    print(f"METRIC_TO_COL_7DAY = {m7}")
    e1 = build_ext_metric_map(col, "1day")
    print(f"EXT_METRIC_TO_COL_1DAY = {e1}")
    e7 = build_ext_metric_map(col, "7day")
    print(f"EXT_METRIC_TO_COL_7DAY = {e7}")
    print(f"COL_1DAY_CAPTURED_AT = {col.get('captured_at_1d')}")
    print(f"COL_1DAY_CAPTURE_MODE = {col.get('capture_mode_1d')}")
    print(f"COL_7DAY_CAPTURED_AT = {col.get('captured_at_7d')}")
    print(f"COL_7DAY_CAPTURE_MODE = {col.get('capture_mode_7d')}")
    print(f"COL_LATEST_CAPTURED_AT = {col.get('latest_captured')}")
