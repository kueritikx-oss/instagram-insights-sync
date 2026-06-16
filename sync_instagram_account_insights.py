#!/usr/bin/env python3
"""
Instagram アカウントレベルインサイトを API で取得し、既存スプレッドシートに書き込む。

書き込み先（既存シートを最大活用）:
  1. 日ごとデータ（14IUZeZJ...）→ E: 純増, F: 増加, G: 減少
  2. 週ごとInstagramデータ（12fghSF6...）→ S-AF列: リーチ内外・フォロー・タップ等
  3. 月ごとInstagramデータ（14IUZeZJ... 別タブ）→ O-Z列: 同上（月次集計）
  4. IG_account_daily_2026（投稿毎データ内 新タブ）→ 全詳細データ
  5. IG_account_weekly_2026（投稿毎データ内 新タブ）→ デモグラフィック

Usage:
  python3 utils/sync_instagram_account_insights.py              # 日次のみ
  python3 utils/sync_instagram_account_insights.py --weekly     # 日次 + 週次
  python3 utils/sync_instagram_account_insights.py --days 7     # 過去7日分バックフィル
  python3 utils/sync_instagram_account_insights.py --date 2026-03-25
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ========== システムブラウザ強制（Google WebView拒否対策） ==========
import contextlib, webbrowser as _wb

class _SystemBrowser:
    name = "system-default"
    def open(self, url, new=0, autoraise=True):
        import subprocess as _sp
        _sp.Popen(["open", url]) if sys.platform == "darwin" else _sp.Popen(["xdg-open", url])
        return True
    open_new = open_new_tab = open

@contextlib.contextmanager
def _force_system_browser():
    _orig, _env = _wb.get, os.environ.pop("BROWSER", None)
    _wb.get = lambda using=None: _SystemBrowser()
    try:
        yield
    finally:
        _wb.get = _orig
        if _env is not None:
            os.environ["BROWSER"] = _env

# ========== パス・認証 ==========
DEFAULT_BASE_DIR = Path(
    "/Users/taiki/Library/Mobile Documents/com~apple~CloudDocs/MacDocuments/01_事業"
)
BASE_DIR = Path(os.environ.get("INSTAGRAM_INSIGHTS_BASE_DIR", str(DEFAULT_BASE_DIR))).expanduser()
GOOGLE_AUTH_DIR = Path(
    os.environ.get(
        "INSTAGRAM_INSIGHTS_GOOGLE_AUTH_DIR",
        str((BASE_DIR / "事業 Cursor/タッキー/02_SNS集客/instagram-auto-post").resolve()),
    )
).expanduser()
CREDS_FILE = GOOGLE_AUTH_DIR / "credentials.json"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"
INSTAGRAM_CONFIG_FILE = GOOGLE_AUTH_DIR / "instagram_insights_config.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GRAPH_API_BASE = "https://graph.facebook.com/v23.0"
JST = timezone(timedelta(hours=9))

# ========== スプレッドシート ID ==========
# 投稿毎データ（新タブ IG_account_daily/weekly を作る先）
POSTDATA_SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"

# 日ごと・月ごと・購入者データ（既存）
DAILY_SHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
DAILY_TAB_NAME = "日ごとデータ"
MONTHLY_TAB_NAME = "月ごとInstagramデータ"

# 週ごとデータ（既存）
WEEKLY_SHEET_ID = "12fghSF68JkhgqSvPmCa_nGeSXowRizo2MtRz4WyeXyo"
WEEKLY_TAB_NAME = "週ごとInstagramデータ"

# 新タブ名（投稿毎データ内）
DETAIL_DAILY_TAB = "IG_account_daily_2026"
DETAIL_WEEKLY_TAB = "IG_account_weekly_2026"

# ========== 日ごとデータ 列マッピング（0-based）==========
# ヘッダー: フォロー全体(E=4), 増加(F=5), 減少(G=6)
DAILY_COL_FOLLOW_NET = 4    # E: Instagram フォロー全体（純増）
DAILY_COL_FOLLOW_UP = 5     # F: 増加
DAILY_COL_FOLLOW_DOWN = 6   # G: 減少

# ========== 週ごとInstagramデータ 列マッピング（0-based）==========
WEEKLY_COL_VIEWS = 17           # R: インプ（views）
WEEKLY_COL_REACH_TOTAL = 18     # S: ①全体
WEEKLY_COL_REACH_FOLLOWER = 19  # T: ②フォロワー
WEEKLY_COL_REACH_NON_FOL = 20   # U: ③フォロワー以外（＝発見）
WEEKLY_COL_REACH_REELS = 21     # V: リール
WEEKLY_COL_REACH_FEED = 22      # W: 投稿リーチ
WEEKLY_COL_REACH_STORY = 23     # X: ストーリーズ
WEEKLY_COL_PROFILE = 26         # AA: ④プロフアクセス
WEEKLY_COL_FOLLOW_TOTAL = 27    # AB: ⑤フォロー全体
WEEKLY_COL_FOLLOW_UP = 28       # AC: ⑥フォロー増
WEEKLY_COL_FOLLOW_DOWN = 29     # AD: ⑦フォロー減
WEEKLY_COL_WEB_TAP = 30         # AE: ⑧ウェブタップ
WEEKLY_COL_EMAIL_TAP = 31       # AF: Eメールタップ

# ========== 月ごとInstagramデータ 列マッピング（0-based）==========
MONTHLY_COL_VIEWS = 13            # N: インプ
MONTHLY_COL_REACH_TOTAL = 14      # O: ①全体
MONTHLY_COL_REACH_FOLLOWER = 15   # P: ②フォロワー
MONTHLY_COL_REACH_NON_FOL = 16    # Q: ③フォロワー以外
MONTHLY_COL_REACH_FEED = 17       # R: 投稿リーチ
MONTHLY_COL_REACH_STORY = 18      # S: ストーリーズリーチ
MONTHLY_COL_REACH_REELS = 19      # T: リールリーチ
MONTHLY_COL_PROFILE = 20          # U: ④プロフアクセス
MONTHLY_COL_FOLLOW_TOTAL = 21     # V: ⑤フォロー全体
MONTHLY_COL_FOLLOW_UP = 22        # W: ⑥フォロー増
MONTHLY_COL_FOLLOW_DOWN = 23      # X: ⑦フォロー減
MONTHLY_COL_WEB_TAP = 24          # Y: ⑧ウェブタップ
MONTHLY_COL_EMAIL_TAP = 25        # Z: ⑨Eメールタップ

# ========== 詳細日次タブ ヘッダー ==========
DETAIL_DAILY_HEADERS = [
    "date", "reach_total", "reach_follower", "reach_non_follower",
    "views_total", "views_follower", "views_non_follower",
    "follows", "unfollows", "follows_net", "follower_count",
    "accounts_engaged",
    "total_interactions_feed", "total_interactions_reels", "total_interactions_story",
    "likes_feed", "likes_reels", "saves_feed", "saves_reels",
    "shares_feed", "shares_reels", "shares_story",
    "comments_feed", "comments_reels",
    "profile_links_taps_total", "profile_links_taps_email",
    "profile_links_taps_call", "profile_links_taps_bio",
    "reposts",
    "reach_feed", "reach_reels", "reach_story",
    "captured_at",
]

DETAIL_WEEKLY_HEADERS = [
    "week_start", "timeframe",
    "follower_age_13_17", "follower_age_18_24", "follower_age_25_34",
    "follower_age_35_44", "follower_age_45_54", "follower_age_55_64", "follower_age_65_plus",
    "follower_gender_male", "follower_gender_female", "follower_gender_unknown",
    "follower_top_cities", "follower_top_countries",
    "engaged_age_13_17", "engaged_age_18_24", "engaged_age_25_34",
    "engaged_age_35_44", "engaged_age_45_54", "engaged_age_55_64", "engaged_age_65_plus",
    "engaged_gender_male", "engaged_gender_female", "engaged_gender_unknown",
    "engaged_top_cities", "engaged_top_countries",
    "captured_at",
]

# 曜日文字列
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


# ========== 認証 ==========

def get_google_credentials() -> Credentials:
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if service_account_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(service_account_json)
        if info.get("type") == "service_account":
            return service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
    if service_account_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        return service_account.Credentials.from_service_account_file(service_account_file, scopes=GOOGLE_SCOPES)

    GOOGLE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _token_reauth_happened = False
        else:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(f"credentials.json が見つかりません: {CREDS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            with _force_system_browser():
                creds = flow.run_local_server(port=0)
            _token_reauth_happened = True
        with TOKEN_FILE.open("w", encoding="utf-8") as f:
            f.write(creds.to_json())
        if _token_reauth_happened:
            print("⚠️  ブラウザ再認証が実行されました。GitHub Secrets を自動同期します...")
            _sync_script = Path(__file__).resolve().parent / "sync_secrets_to_github.py"
            if _sync_script.exists():
                import subprocess as _sp
                _result = _sp.run(
                    [sys.executable, str(_sync_script)],
                    capture_output=True, text=True,
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
                if _result.returncode == 0:
                    print("✅ GitHub Secrets 同期完了")
                else:
                    print(f"❌ Secret 同期失敗: {_result.stderr[:200]}")
    return creds


def load_instagram_config() -> tuple[str, str]:
    if INSTAGRAM_CONFIG_FILE.exists():
        with open(INSTAGRAM_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("access_token", "").strip()
        ig_id = data.get("ig_user_id", "").strip()
        if token and ig_id:
            return token, ig_id
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    ig_id = os.environ.get("INSTAGRAM_IG_USER_ID", "").strip()
    if token and ig_id:
        return token, ig_id
    raise FileNotFoundError("Instagram 用のトークンと IG User ID がありません。")


# ========== ユーティリティ ==========

def column_letter(col_index: int) -> str:
    out = []
    n = col_index + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        out.append(chr(65 + r))
    return "".join(reversed(out))


def parse_jp_date(date_str: str, default_year: int = 2026) -> Optional[datetime]:
    """' 3/22 月' → datetime(2026, 3, 22) を返す"""
    if not date_str:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})", date_str.strip())
    if not m:
        return None
    month = int(m.group(1))
    day = int(m.group(2))
    try:
        return datetime(default_year, month, day)
    except ValueError:
        return None


def format_daily_date(dt: datetime) -> str:
    """datetime → ' 3/22 月' 形式（日ごとデータのA列フォーマット）"""
    weekday = WEEKDAY_JP[dt.weekday()]
    return f" {dt.month}/{dt.day} {weekday}"


def format_weekly_date(dt: datetime) -> str:
    """datetime → '3/22' 形式（週ごとデータのA列フォーマット）"""
    return f"{dt.month}/{dt.day}"


# ========== シート操作 ==========

def ensure_tab(sheets_service, sheet_id: str, tab_name: str, headers: List[str]) -> None:
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets(properties(title))"
    ).execute()
    titles = {s["properties"]["title"] for s in spreadsheet.get("sheets", [])}
    if tab_name not in titles:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        print(f"  タブ '{tab_name}' を作成")
    col_letter = column_letter(len(headers) - 1)
    header_range = f"'{tab_name}'!A1:{col_letter}1"
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=header_range
    ).execute()
    current = resp.get("values", [[]])[0] if resp.get("values") else []
    if current != headers:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=header_range,
            valueInputOption="USER_ENTERED", body={"values": [headers]},
        ).execute()


def find_row_by_date(
    sheets_service, sheet_id: str, tab_name: str, target_date: datetime,
) -> Optional[int]:
    """A列の日付文字列からtarget_dateに一致する行番号(1-based)を返す。
    逆順検索で最新年を優先。曜日も検証して誤マッチを防ぐ。"""
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A",
    ).execute()
    values = resp.get("values", [])
    target_weekday = WEEKDAY_JP[target_date.weekday()]

    # 逆順検索（最新行 = 最新年 を優先）
    for i in range(len(values) - 1, -1, -1):
        row = values[i]
        if not row:
            continue
        cell = row[0].strip()
        # M/D を抽出
        m = re.search(r"(\d{1,2})/(\d{1,2})", cell)
        if not m:
            continue
        month = int(m.group(1))
        day = int(m.group(2))
        if month == target_date.month and day == target_date.day:
            # 曜日も一致するか確認（" 3/25 水" の "水" 部分）
            weekday_match = re.search(r"[月火水木金土日]", cell)
            if weekday_match and weekday_match.group() == target_weekday:
                return i + 1  # 1-based
            # 曜日がない場合は月日一致だけで返す
            if not weekday_match:
                return i + 1
    return None


def find_row_by_week_start(
    sheets_service, sheet_id: str, tab_name: str, week_start: datetime,
) -> Optional[int]:
    """A列の週開始日（'M/D' or 'M/D'）からマッチする行を返す"""
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A",
    ).execute()
    values = resp.get("values", [])
    target_str = format_weekly_date(week_start)
    for i, row in enumerate(values):
        if not row:
            continue
        cell = row[0].strip()
        if cell == target_str:
            return i + 1
    return None


def update_cells(
    sheets_service, sheet_id: str, tab_name: str,
    row: int, col_value_pairs: List[Tuple[int, Any]],
) -> None:
    """指定行の複数セルを一括更新"""
    data = []
    for col, value in col_value_pairs:
        cell_ref = f"'{tab_name}'!{column_letter(col)}{row}"
        data.append({"range": cell_ref, "values": [[value]]})
    if data:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def append_row(sheets_service, sheet_id: str, tab_name: str, row_data: List[Any]) -> None:
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row_data]},
    ).execute()


def get_existing_dates(sheets_service, sheet_id: str, tab_name: str) -> set:
    try:
        resp = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A",
        ).execute()
        return {row[0] for row in resp.get("values", []) if row}
    except Exception:
        return set()


# ========== Instagram API ==========

def fetch_with_breakdown(
    access_token: str, ig_user_id: str,
    metric: str, breakdown: str, since_ts: int, until_ts: int,
) -> Dict[str, int]:
    url = f"{GRAPH_API_BASE}/{ig_user_id}/insights"
    params = {
        "metric": metric, "period": "day", "metric_type": "total_value",
        "breakdown": breakdown, "since": since_ts, "until": until_ts,
        "access_token": access_token,
    }
    result: Dict[str, int] = {}
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return result
        for item in r.json().get("data", []):
            for bd in item.get("total_value", {}).get("breakdowns", []):
                for entry in bd.get("results", []):
                    dims = entry.get("dimension_values", [])
                    if dims:
                        result[dims[0]] = entry.get("value", 0)
    except requests.RequestException:
        pass
    return result


def fetch_simple(
    access_token: str, ig_user_id: str,
    metric: str, since_ts: int, until_ts: int,
) -> Optional[int]:
    url = f"{GRAPH_API_BASE}/{ig_user_id}/insights"
    params = {
        "metric": metric, "period": "day", "metric_type": "total_value",
        "since": since_ts, "until": until_ts, "access_token": access_token,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        for item in r.json().get("data", []):
            return item.get("total_value", {}).get("value", 0)
    except requests.RequestException:
        pass
    return None


def fetch_follower_count(
    access_token: str, ig_user_id: str,
) -> Optional[int]:
    """フォロワー数を直接フィールドから取得（insights endpoint は不正確なため）"""
    url = f"{GRAPH_API_BASE}/{ig_user_id}"
    params = {"fields": "followers_count", "access_token": access_token}
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        return r.json().get("followers_count", 0)
    except requests.RequestException:
        pass
    return None


def fetch_demographics(
    access_token: str, ig_user_id: str,
    metric: str, breakdown: str, timeframe: str = "this_month",
) -> Dict[str, int]:
    url = f"{GRAPH_API_BASE}/{ig_user_id}/insights"
    params = {
        "metric": metric, "period": "lifetime", "metric_type": "total_value",
        "breakdown": breakdown, "timeframe": timeframe, "access_token": access_token,
    }
    result: Dict[str, int] = {}
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return result
        for item in r.json().get("data", []):
            for bd in item.get("total_value", {}).get("breakdowns", []):
                for entry in bd.get("results", []):
                    dims = entry.get("dimension_values", [])
                    if dims:
                        result[dims[0]] = entry.get("value", 0)
    except requests.RequestException:
        pass
    return result


# ========== データ収集 ==========

def collect_daily_data(
    access_token: str, ig_user_id: str, target_date: datetime,
) -> Dict[str, Any]:
    """1日分のアカウントインサイトを全取得"""
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=JST)
    since_ts = int(day_start.astimezone(timezone.utc).timestamp())
    until_ts = int((day_start + timedelta(days=1)).astimezone(timezone.utc).timestamp())
    date_str = target_date.strftime("%Y-%m-%d")

    print(f"  {date_str}: ", end="", flush=True)

    # reach (follow_type)
    reach_bd = fetch_with_breakdown(access_token, ig_user_id, "reach", "follow_type", since_ts, until_ts)
    reach_follower = reach_bd.get("FOLLOWER", 0)
    reach_non_follower = reach_bd.get("NON_FOLLOWER", 0)
    reach_total = reach_follower + reach_non_follower + reach_bd.get("UNKNOWN", 0)
    print("reach ", end="", flush=True)

    # views (follow_type — ※ follower_type はエラー。follow_type が正しい)
    views_bd = fetch_with_breakdown(access_token, ig_user_id, "views", "follow_type", since_ts, until_ts)
    views_follower = views_bd.get("FOLLOWER", 0)
    views_non_follower = views_bd.get("NON_FOLLOWER", 0)
    views_total = views_follower + views_non_follower + views_bd.get("UNKNOWN", 0)
    print("views ", end="", flush=True)

    # follows_and_unfollows
    follows_bd = fetch_with_breakdown(access_token, ig_user_id, "follows_and_unfollows", "follow_type", since_ts, until_ts)
    follows = follows_bd.get("FOLLOWER", 0)
    unfollows = follows_bd.get("NON_FOLLOWER", 0)
    follows_net = follows - unfollows
    print("follows ", end="", flush=True)

    # follower_count（直接フィールド取得 — insights endpointは不正確）
    follower_count = fetch_follower_count(access_token, ig_user_id) or 0

    # accounts_engaged
    accounts_engaged = fetch_simple(access_token, ig_user_id, "accounts_engaged", since_ts, until_ts) or 0

    # total_interactions, likes, saves, shares, comments (media_product_type)
    interactions_bd = fetch_with_breakdown(access_token, ig_user_id, "total_interactions", "media_product_type", since_ts, until_ts)
    likes_bd = fetch_with_breakdown(access_token, ig_user_id, "likes", "media_product_type", since_ts, until_ts)
    saves_bd = fetch_with_breakdown(access_token, ig_user_id, "saves", "media_product_type", since_ts, until_ts)
    shares_bd = fetch_with_breakdown(access_token, ig_user_id, "shares", "media_product_type", since_ts, until_ts)
    comments_bd = fetch_with_breakdown(access_token, ig_user_id, "comments", "media_product_type", since_ts, until_ts)
    print("engagement ", end="", flush=True)

    # profile_links_taps
    taps_bd = fetch_with_breakdown(access_token, ig_user_id, "profile_links_taps", "contact_button_type", since_ts, until_ts)
    taps_total = sum(taps_bd.values())

    # reposts
    reposts = fetch_simple(access_token, ig_user_id, "reposts", since_ts, until_ts) or 0

    # reach (media_product_type)
    reach_mpt = fetch_with_breakdown(access_token, ig_user_id, "reach", "media_product_type", since_ts, until_ts)
    print("done!", flush=True)

    print(f"    リーチ: {reach_total:,}（内: {reach_follower:,} / 外: {reach_non_follower:,}）"
          f" フォロワー: {follower_count:,}（{follows_net:+d}）")

    return {
        "date_str": date_str,
        "reach_total": reach_total,
        "reach_follower": reach_follower,
        "reach_non_follower": reach_non_follower,
        "views_total": views_total,
        "views_follower": views_follower,
        "views_non_follower": views_non_follower,
        "follows": follows,
        "unfollows": unfollows,
        "follows_net": follows_net,
        "follower_count": follower_count,
        "accounts_engaged": accounts_engaged,
        # ※ APIは media_product_type を POST/CAROUSEL_CONTAINER/REEL/STORY で返す
        #    POST + CAROUSEL_CONTAINER = Feed相当、REEL = Reels相当
        "interactions_feed": interactions_bd.get("POST", 0) + interactions_bd.get("CAROUSEL_CONTAINER", 0),
        "interactions_reels": interactions_bd.get("REEL", 0),
        "interactions_story": interactions_bd.get("STORY", 0),
        "likes_feed": likes_bd.get("POST", 0) + likes_bd.get("CAROUSEL_CONTAINER", 0),
        "likes_reels": likes_bd.get("REEL", 0),
        "saves_feed": saves_bd.get("POST", 0) + saves_bd.get("CAROUSEL_CONTAINER", 0),
        "saves_reels": saves_bd.get("REEL", 0),
        "shares_feed": shares_bd.get("POST", 0) + shares_bd.get("CAROUSEL_CONTAINER", 0),
        "shares_reels": shares_bd.get("REEL", 0),
        "shares_story": shares_bd.get("STORY", 0),
        "comments_feed": comments_bd.get("POST", 0) + comments_bd.get("CAROUSEL_CONTAINER", 0),
        "comments_reels": comments_bd.get("REEL", 0),
        "taps_total": taps_total,
        "taps_email": taps_bd.get("EMAIL", 0),
        "taps_call": taps_bd.get("CALL", 0),
        "taps_bio": taps_bd.get("BOOK_NOW", 0) + taps_bd.get("UNDEFINED", 0),
        "reposts": reposts,
        "reach_feed": reach_mpt.get("POST", 0) + reach_mpt.get("CAROUSEL_CONTAINER", 0),
        "reach_reels": reach_mpt.get("REEL", 0),
        "reach_story": reach_mpt.get("STORY", 0),
    }


# ========== 既存シートへの書き込み ==========

def write_to_daily_sheet(sheets_service, target_date: datetime, data: Dict[str, Any]) -> bool:
    """日ごとデータシートの該当行にフォロー増減を書き込む。
    行が見つからない場合は警告してFalseを返す(append禁止でゴミデータ防止)。"""
    row = find_row_by_date(sheets_service, DAILY_SHEET_ID, DAILY_TAB_NAME, target_date)
    if not row:
        date_str = format_daily_date(target_date)
        print(f"    ⚠️ 日ごとデータ: 行が見つかりません ({date_str}) — A列の日付生成が必要", file=sys.stderr)
        return False

    updates = [
        (DAILY_COL_FOLLOW_NET, data["follows_net"]),
        (DAILY_COL_FOLLOW_UP, data["follows"]),
        (DAILY_COL_FOLLOW_DOWN, data["unfollows"]),
    ]
    update_cells(sheets_service, DAILY_SHEET_ID, DAILY_TAB_NAME, row, updates)
    print(f"    → 日ごとデータ Row {row}: 更新（+{data['follows']}/-{data['unfollows']}={data['follows_net']:+d}）")
    return True


def detect_missing_daily_dates(sheets_service, days: int) -> List[datetime]:
    """日ごとデータの直近days日で、E/F/G列が全て空の日を検出して返す（欠損バックフィル用）"""
    now_jst = datetime.now(JST)
    yesterday = (now_jst - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    missing: List[datetime] = []
    for offset in range(days):
        target = yesterday - timedelta(days=offset)
        row = find_row_by_date(sheets_service, DAILY_SHEET_ID, DAILY_TAB_NAME, target)
        if not row:
            continue  # A列に日付なし(別課題)。無理に埋めない
        rng = f"'{DAILY_TAB_NAME}'!E{row}:G{row}"
        try:
            resp = sheets_service.spreadsheets().values().get(
                spreadsheetId=DAILY_SHEET_ID, range=rng,
            ).execute()
        except Exception as exc:
            print(f"    ⚠️ 欠損チェック失敗 {target.strftime('%Y-%m-%d')}: {exc}", file=sys.stderr)
            continue
        values = resp.get("values", [])
        cells = values[0] if values else []
        is_empty = len(cells) < 3 or all(not (c and str(c).strip()) for c in cells)
        if is_empty:
            missing.append(target)
    return missing


def write_to_weekly_sheet(
    sheets_service, week_start: datetime, weekly_data: Dict[str, Any],
) -> bool:
    """週ごとInstagramデータシートの該当行にリーチ等を書き込む"""
    row = find_row_by_week_start(sheets_service, WEEKLY_SHEET_ID, WEEKLY_TAB_NAME, week_start)
    if not row:
        # 新規行を追加
        week_end = week_start + timedelta(days=6)
        new_row = [""] * 32
        new_row[0] = format_weekly_date(week_start)
        new_row[1] = format_weekly_date(week_end)
        new_row[6] = "7"  # 日数
        new_row[WEEKLY_COL_VIEWS] = weekly_data.get("views_total", 0)
        new_row[WEEKLY_COL_REACH_TOTAL] = weekly_data.get("reach_total", 0)
        new_row[WEEKLY_COL_REACH_FOLLOWER] = weekly_data.get("reach_follower", 0)
        new_row[WEEKLY_COL_REACH_NON_FOL] = weekly_data.get("reach_non_follower", 0)
        new_row[WEEKLY_COL_REACH_REELS] = weekly_data.get("reach_reels", 0)
        new_row[WEEKLY_COL_REACH_FEED] = weekly_data.get("reach_feed", 0)
        new_row[WEEKLY_COL_REACH_STORY] = weekly_data.get("reach_story", 0)
        new_row[WEEKLY_COL_FOLLOW_UP] = weekly_data.get("follows", 0)
        new_row[WEEKLY_COL_FOLLOW_DOWN] = weekly_data.get("unfollows", 0)
        new_row[WEEKLY_COL_FOLLOW_TOTAL] = weekly_data.get("follows_net", 0)
        new_row[WEEKLY_COL_WEB_TAP] = weekly_data.get("taps_bio", 0)
        new_row[WEEKLY_COL_EMAIL_TAP] = weekly_data.get("taps_email", 0)
        append_row(sheets_service, WEEKLY_SHEET_ID, WEEKLY_TAB_NAME, new_row)
        print(f"    → 週ごとデータ: 新規行追加 ({format_weekly_date(week_start)}〜{format_weekly_date(week_end)})")
        return True

    updates = [
        (WEEKLY_COL_VIEWS, weekly_data.get("views_total", 0)),
        (WEEKLY_COL_REACH_TOTAL, weekly_data.get("reach_total", 0)),
        (WEEKLY_COL_REACH_FOLLOWER, weekly_data.get("reach_follower", 0)),
        (WEEKLY_COL_REACH_NON_FOL, weekly_data.get("reach_non_follower", 0)),
        (WEEKLY_COL_REACH_REELS, weekly_data.get("reach_reels", 0)),
        (WEEKLY_COL_REACH_FEED, weekly_data.get("reach_feed", 0)),
        (WEEKLY_COL_REACH_STORY, weekly_data.get("reach_story", 0)),
        (WEEKLY_COL_FOLLOW_UP, weekly_data.get("follows", 0)),
        (WEEKLY_COL_FOLLOW_DOWN, weekly_data.get("unfollows", 0)),
        (WEEKLY_COL_FOLLOW_TOTAL, weekly_data.get("follows_net", 0)),
        (WEEKLY_COL_WEB_TAP, weekly_data.get("taps_bio", 0)),
        (WEEKLY_COL_EMAIL_TAP, weekly_data.get("taps_email", 0)),
    ]
    update_cells(sheets_service, WEEKLY_SHEET_ID, WEEKLY_TAB_NAME, row, updates)
    print(f"    → 週ごとデータ Row {row}: リーチ {weekly_data.get('reach_total', 0):,}"
          f"（外: {weekly_data.get('reach_non_follower', 0):,}）")
    return True


def write_to_detail_daily(sheets_service, data: Dict[str, Any]) -> None:
    """詳細日次タブに書き込み"""
    row_data = [
        data["date_str"],
        data["reach_total"], data["reach_follower"], data["reach_non_follower"],
        data["views_total"], data["views_follower"], data["views_non_follower"],
        data["follows"], data["unfollows"], data["follows_net"], data["follower_count"],
        data["accounts_engaged"],
        data["interactions_feed"], data["interactions_reels"], data["interactions_story"],
        data["likes_feed"], data["likes_reels"],
        data["saves_feed"], data["saves_reels"],
        data["shares_feed"], data["shares_reels"], data["shares_story"],
        data["comments_feed"], data["comments_reels"],
        data["taps_total"], data["taps_email"], data["taps_call"], data["taps_bio"],
        data["reposts"],
        data["reach_feed"], data["reach_reels"], data["reach_story"],
        datetime.now(timezone.utc).isoformat(),
    ]
    append_row(sheets_service, POSTDATA_SHEET_ID, DETAIL_DAILY_TAB, row_data)


# ========== 週次集計 ==========

def aggregate_weekly(daily_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """日次データのリストから週次集計を作成"""
    agg: Dict[str, int] = {}
    sum_keys = [
        "reach_total", "reach_follower", "reach_non_follower",
        "views_total", "views_follower", "views_non_follower",
        "follows", "unfollows", "follows_net",
        "reach_feed", "reach_reels", "reach_story",
        "taps_total", "taps_email", "taps_bio",
    ]
    for key in sum_keys:
        agg[key] = sum(d.get(key, 0) for d in daily_results)
    return agg


# ========== メイン ==========

def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram アカウントインサイト取得（既存シート連携）")
    parser.add_argument("--weekly", action="store_true", help="週次デモグラフィックも取得")
    parser.add_argument("--date", type=str, help="対象日 (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=1, help="過去N日分を取得")
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="日ごとデータ直近N日を走査してE/F/G列欠損日を自動バックフィル")
    args = parser.parse_args()

    print("=== Instagram アカウントインサイト v2.0（既存シート連携）===\n")
    print(f"書き込み先:")
    print(f"  日ごとデータ: {DAILY_SHEET_ID}")
    print(f"  週ごとデータ: {WEEKLY_SHEET_ID}")
    print(f"  詳細タブ: {POSTDATA_SHEET_ID} 内 {DETAIL_DAILY_TAB}")
    print()

    try:
        creds = get_google_credentials()
        access_token, ig_user_id = load_instagram_config()
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        return

    sheets_service = build("sheets", "v4", credentials=creds)

    # タブ確認
    ensure_tab(sheets_service, POSTDATA_SHEET_ID, DETAIL_DAILY_TAB, DETAIL_DAILY_HEADERS)
    if args.weekly:
        ensure_tab(sheets_service, POSTDATA_SHEET_ID, DETAIL_WEEKLY_TAB, DETAIL_WEEKLY_HEADERS)

    # 対象日の決定
    if args.date:
        target_base = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        now_jst = datetime.now(JST)
        target_base = (now_jst - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )

    # 既存詳細タブの日付チェック
    existing_detail = get_existing_dates(sheets_service, POSTDATA_SHEET_ID, DETAIL_DAILY_TAB)

    # ---- 日次データ取得 ----
    print(f"\n--- 日次データ取得（{args.days}日分）---")
    daily_results: List[Dict[str, Any]] = []
    written = 0
    skipped = 0
    daily_write_failed = 0  # 日ごとデータ書き込み失敗数

    for i in range(args.days):
        target = target_base - timedelta(days=i)
        date_str = target.strftime("%Y-%m-%d")

        if date_str in existing_detail:
            print(f"  {date_str}: 詳細タブに既存 — スキップ")
            skipped += 1
            continue

        data = collect_daily_data(access_token, ig_user_id, target)
        daily_results.append(data)

        # 1. 日ごとデータ（既存）に書き込み
        if not write_to_daily_sheet(sheets_service, target, data):
            daily_write_failed += 1

        # 2. 詳細日次タブに書き込み
        write_to_detail_daily(sheets_service, data)
        written += 1

    # ---- バックフィル（欠損自動検知→再取得）----
    backfilled = 0
    if args.backfill_days > 0:
        print(f"\n--- バックフィル: 直近{args.backfill_days}日の欠損検知 ---")
        missing_dates = detect_missing_daily_dates(sheets_service, args.backfill_days)
        if not missing_dates:
            print("  ✅ 欠損なし")
        else:
            print(f"  欠損検出: {len(missing_dates)}日")
            for target in missing_dates:
                date_str = target.strftime("%Y-%m-%d")
                print(f"  バックフィル中: {date_str}")
                try:
                    data = collect_daily_data(access_token, ig_user_id, target)
                    if write_to_daily_sheet(sheets_service, target, data):
                        backfilled += 1
                    else:
                        daily_write_failed += 1
                except Exception as exc:
                    print(f"    ⚠️ {date_str} 失敗: {exc}", file=sys.stderr)
                    daily_write_failed += 1
            print(f"  完了: {backfilled}日補完")

    # ---- 週次集計（7日分以上あれば）----
    if daily_results and args.days >= 7:
        print(f"\n--- 週次集計 ---")
        # 今週の月曜日を特定
        now_jst = datetime.now(JST)
        days_since_monday = now_jst.weekday()
        week_start = (now_jst - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        weekly_agg = aggregate_weekly(daily_results)
        write_to_weekly_sheet(sheets_service, week_start, weekly_agg)

    # ---- 週次デモグラフィック ----
    if args.weekly:
        print(f"\n--- 週次デモグラフィック ---")
        now_jst = datetime.now(JST)
        days_since_monday = now_jst.weekday()
        week_start = (now_jst - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )

        existing_weeks = get_existing_dates(sheets_service, POSTDATA_SHEET_ID, DETAIL_WEEKLY_TAB)
        week_str = week_start.strftime("%Y-%m-%d")

        if week_str in existing_weeks:
            print(f"  {week_str}: 既に取得済み — スキップ")
        else:
            timeframe = "this_month"
            print(f"  {week_str} 週のデモグラフィック取得中...")

            AGE_BUCKETS = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]

            follower_age = fetch_demographics(access_token, ig_user_id, "follower_demographics", "age", timeframe)
            follower_gender = fetch_demographics(access_token, ig_user_id, "follower_demographics", "gender", timeframe)
            follower_city = fetch_demographics(access_token, ig_user_id, "follower_demographics", "city", timeframe)
            follower_country = fetch_demographics(access_token, ig_user_id, "follower_demographics", "country", timeframe)
            engaged_age = fetch_demographics(access_token, ig_user_id, "engaged_audience_demographics", "age", timeframe)
            engaged_gender = fetch_demographics(access_token, ig_user_id, "engaged_audience_demographics", "gender", timeframe)
            engaged_city = fetch_demographics(access_token, ig_user_id, "engaged_audience_demographics", "city", timeframe)
            engaged_country = fetch_demographics(access_token, ig_user_id, "engaged_audience_demographics", "country", timeframe)

            def top_n(d, n=10):
                return json.dumps(dict(sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]), ensure_ascii=False)

            demo_row = [
                week_str, timeframe,
                follower_age.get("13-17", 0), follower_age.get("18-24", 0),
                follower_age.get("25-34", 0), follower_age.get("35-44", 0),
                follower_age.get("45-54", 0), follower_age.get("55-64", 0),
                follower_age.get("65+", 0),
                follower_gender.get("M", 0), follower_gender.get("F", 0),
                follower_gender.get("U", 0),
                top_n(follower_city), top_n(follower_country),
                engaged_age.get("13-17", 0), engaged_age.get("18-24", 0),
                engaged_age.get("25-34", 0), engaged_age.get("35-44", 0),
                engaged_age.get("45-54", 0), engaged_age.get("55-64", 0),
                engaged_age.get("65+", 0),
                engaged_gender.get("M", 0), engaged_gender.get("F", 0),
                engaged_gender.get("U", 0),
                top_n(engaged_city), top_n(engaged_country),
                datetime.now(timezone.utc).isoformat(),
            ]
            append_row(sheets_service, POSTDATA_SHEET_ID, DETAIL_WEEKLY_TAB, demo_row)

            total_f_age = sum(follower_age.values())
            if total_f_age > 0:
                pct_18_24 = follower_age.get("18-24", 0) / total_f_age * 100
                print(f"    フォロワー 18-24歳: {pct_18_24:.1f}%")

    # サマリー
    print(f"\n{'=' * 50}")
    print(f"完了: 日次 {written} 日書き込み（{skipped} 日スキップ, バックフィル {backfilled} 日補完）")
    print(f"書き込み先:")
    print(f"  ✅ 日ごとデータ — フォロー増減（E-G列）")
    print(f"  ✅ 詳細日次 — 全33指標（IG_account_daily_2026）")
    if daily_results and args.days >= 7:
        print(f"  ✅ 週ごとInstagramデータ — リーチ内外・フォロー・タップ（S-AF列）")
    if args.weekly:
        print(f"  ✅ 詳細週次 — デモグラフィック（IG_account_weekly_2026）")

    # 異常検知: 日ごとデータ書き込み失敗が1件以上あれば exit 2
    # ワークフロー側で if: failure() を発動してDiscord通知させるため
    if daily_write_failed > 0:
        print(f"\n🔴 警告: 日ごとデータ書き込み失敗 {daily_write_failed} 件。", file=sys.stderr)
        print(f"     A列の日付生成漏れ、または構造変更の可能性あり。", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
