#!/usr/bin/env python3
"""
Instagram 投稿インサイトを API で取得し、スプレッドシート「投稿毎データ」の
1日後・1週間後ブロックに書き込む。

前提:
- Phase 1 で Meta のトークン・IG User ID を用意し、
  instagram_insights_config.json または環境変数で設定すること。
- 24時間ごとに実行する想定。通常は投稿から 24h 以降で 1日後、
  7日以降で 1週間後に書き込む。過去の未入力分も後追いで埋める。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ========== パス・スプレッドシート（sync_sheet_thumbnails と共通） ==========
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

SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
# 2026年データのタブ（gid=1787406075）。絵文字入りシート名はAPIでパースエラーになるため sheetId で参照
SHEET_ID_2026 = 1787406075

# Meta 用設定ファイル（Phase 1 で作成。access_token と ig_user_id を書く）
INSTAGRAM_CONFIG_FILE = GOOGLE_AUTH_DIR / "instagram_insights_config.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Instagram Graph API
# ※ Instagram ビジネスアカウントのインサイト取得は Basic Display API ではなく
#   Facebook Graph API 側を使うため、エンドポイントは graph.facebook.com を使う。
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# Raw インサイト保存用シート
RAW_SHEET_NAME = "Instagram_raw_insights_2026"

# ========== シート列（0-based）。シートの見出し行と対応させる ==========
# データ行は 4 行目から。A=0, B=1, ..., H=7
COL_DATE = 0   # A: 日付
COL_TIME = 3   # D: 時刻
COL_URL = 7    # H: 投稿URL

# 1日後ブロック
# 実シート 3 行目見出し:
# - W: 全体（リーチ）
# - AN: いいね
# - AO: 保存
# - AP: コメント
COL_1DAY_REACH = 22     # W: 翌日リーチ（全体）
COL_1DAY_LIKES = 39     # AN: 翌日いいね
COL_1DAY_SAVED = 40     # AO: 翌日保存
COL_1DAY_COMMENTS = 41  # AP: 翌日コメント

# 1週間後ブロック
# 実シート 3 行目見出し:
# - AZ: 全体（リーチ）
# - BR: いいね
# - BS: 保存
# - BT: コメント
COL_7DAY_REACH = 51     # AZ: 1週間後リーチ（全体）
COL_7DAY_LIKES = 69     # BR: 1週間後いいね
COL_7DAY_SAVED = 70     # BS: 1週間後保存
COL_7DAY_COMMENTS = 71  # BT: 1週間後コメント

# 経過時間のしきい値（時間）
HOURS_1DAY_MIN = 24
HOURS_7DAY_MIN = 7 * 24


@dataclass
class SheetRow:
    """シートの1行分（データ行）"""
    row_index: int  # 1-based（Sheets の行番号）
    date_str: str
    time_str: str
    url: str
    # 1日後・1週間後の既存値（必要項目が揃っていればスキップ）
    has_1day: bool
    has_7day: bool
    has_1day_metadata: bool
    has_7day_metadata: bool


@dataclass
class MediaInfo:
    """API から取得したメディア情報"""
    media_id: str
    permalink: str
    timestamp: datetime  # 投稿日時 UTC


SUPPORTED_MEDIA_METRICS = [
    "reach",
    "views",
    "total_interactions",
    "likes",
    "saved",
    "comments",
    "shares",
    "profile_visits",
    "follows",
]

RAW_SHEET_HEADERS = [
    "sheet_row",
    "snapshot_type",
    "capture_mode",
    "snapshot_at_utc",
    "media_id",
    "permalink",
    "reach",
    "views",
    "total_interactions",
    "likes",
    "saved",
    "comments",
    "shares",
    "profile_visits",
    "follows",
]

METRIC_TO_COL_1DAY = {
    "reach": 22,               # W 全体
    "views": 31,               # AF 再生数
    "total_interactions": 36,  # AK 全体
    "likes": 39,               # AN いいね
    "saved": 40,               # AO 保存
    "comments": 41,            # AP コメント
    "shares": 42,              # AQ シェア
    "profile_visits": 43,      # AR プロフアクセス
    "follows": 44,             # AS フォロー
}

METRIC_TO_COL_7DAY = {
    "reach": 51,               # AZ 全体
    "views": 61,               # BJ 再生回数
    "total_interactions": 66,  # BO 全体
    "likes": 69,               # BR いいね
    "saved": 70,               # BS 保存
    "comments": 71,            # BT コメント
    "shares": 72,              # BU シェア
    "profile_visits": 73,      # BV プロフアクセス
    "follows": 74,             # BW フォロー
}
JST = timezone(timedelta(hours=9))

COL_1DAY_CAPTURED_AT = 81
COL_1DAY_CAPTURE_MODE = 82
COL_7DAY_CAPTURED_AT = 83
COL_7DAY_CAPTURE_MODE = 84
COL_LATEST_CAPTURED_AT = 85


def parse_sheet_datetime(date_str: str, time_str: str, default_year: int = 2026) -> Optional[datetime]:
    """
    シートの「日付」「時刻」文字列から投稿日時 (UTC) を推定する。
    日付例: '3/9 月', '3/10 火' → 月/日を抽出。
    時刻例: '21:22' または '2230'（コロンなし4桁）→ 時・分を抽出。
    """
    if not date_str:
        return None
    if not time_str:
        time_str = "12:00"  # 時刻未入力はその日12:00としてマッチさせる

    # 日付: "3/9 月", "3/10 火" など → 月・日
    m = re.search(r"(\d{1,2})/(\d{1,2})", date_str)
    if not m:
        return None
    month = int(m.group(1))
    day = int(m.group(2))

    # 時刻: "21:22" または "2230"（4桁）
    m2 = re.search(r"(\d{1,2}):(\d{2})", time_str)
    if m2:
        hour = int(m2.group(1))
        minute = int(m2.group(2))
    else:
        m3 = re.search(r"(\d{2})(\d{2})", time_str.strip())
        if not m3:
            return None
        hour = int(m3.group(1))
        minute = int(m3.group(2))

    try:
        dt_jst = datetime(default_year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None
    return dt_jst.astimezone(timezone.utc)


def get_google_credentials() -> Credentials:
    """Google 認証（シート読み書き用）"""
    GOOGLE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json が見つかりません: {CREDS_FILE}\n"
                    "Google 用 OAuth クライアントを配置してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        with TOKEN_FILE.open("w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def load_instagram_config() -> tuple[str, str]:
    """Meta の access_token と ig_user_id を config ファイル or 環境変数から取得"""
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
    raise FileNotFoundError(
        f"Instagram 用のトークンと IG User ID がありません。\n"
        f"  - {INSTAGRAM_CONFIG_FILE} に {{ \"access_token\": \"...\", \"ig_user_id\": \"...\" }} を保存するか、\n"
        f"  - 環境変数 INSTAGRAM_ACCESS_TOKEN と INSTAGRAM_IG_USER_ID を設定してください。"
    )


def extract_url_path(url: str) -> Optional[str]:
    """投稿 URL からパス部分 /p/xxx/ または /reel/xxx/ を抽出。照合用。"""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    # https://www.instagram.com/p/ABC123/  or  /reel/ABC123/
    m = re.search(r"(/p/[A-Za-z0-9_-]+/?|/reel/[A-Za-z0-9_-]+/?)", url)
    if m:
        path = m.group(1).rstrip("/")
        return path if path.endswith("/") else path + "/"
    return None


def fetch_all_media(access_token: str, ig_user_id: str) -> List[MediaInfo]:
    """メディア一覧をページネーションで全件取得"""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
    params = {
        "fields": "id,permalink,timestamp",
        "limit": 100,
        "access_token": access_token,
    }
    out: List[MediaInfo] = []
    while url:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            permalink = item.get("permalink") or ""
            ts_str = item.get("timestamp") or ""
            try:
                # ISO 8601 例: 2026-02-02T04:49:00+0000（+0000 は +00:00 に正規化）
                normalized = ts_str.replace("Z", "+00:00").replace("+0000", "+00:00")
                ts = datetime.fromisoformat(normalized)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)
            out.append(MediaInfo(
                media_id=item["id"],
                permalink=permalink,
                timestamp=ts,
            ))
        # 次のページ
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url = next_url
        params = {}
    return out


def fetch_insights(access_token: str, media_id: str) -> Optional[Dict[str, int]]:
    """1メディアのインサイト取得。利用可能な主要メトリクスを個別取得して返す。"""
    result: Dict[str, int] = {}
    for metric in SUPPORTED_MEDIA_METRICS:
        url = f"{GRAPH_API_BASE}/{media_id}/insights"
        params = {
            "metric": metric,
            "access_token": access_token,
        }
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            continue
        data = r.json()
        for item in data.get("data", []):
            name = item.get("name")
            values = item.get("values", [])
            if values and isinstance(values[0].get("value"), (int, float)):
                result[name] = int(values[0]["value"])
    return result if result else None


def has_cell_value(row: List[Any], col_index: int) -> bool:
    if len(row) <= col_index:
        return False
    return str(row[col_index] or "").strip() not in ("", "#DIV/0!", "#REF!")


def block_is_complete(row: List[Any], metric_to_col: Dict[str, int]) -> bool:
    return all(has_cell_value(row, col_index) for col_index in metric_to_col.values())


def parse_sheet_rows(values: List[List[Any]], start_row: int = 4) -> List[SheetRow]:
    """シートの values から行リストを組み立てる。URL がある行だけ。"""
    rows: List[SheetRow] = []
    for offset, row in enumerate(values):
        row_idx = start_row + offset
        if len(row) <= COL_URL:
            continue
        url = (row[COL_URL] or "").strip()
        if not url or "instagram.com" not in url:
            continue
        date_str = (row[COL_DATE] or "").strip() if len(row) > COL_DATE else ""
        time_str = (row[COL_TIME] or "").strip() if len(row) > COL_TIME else ""
        has_1day = block_is_complete(row, METRIC_TO_COL_1DAY)
        has_7day = block_is_complete(row, METRIC_TO_COL_7DAY)
        has_1day_metadata = has_cell_value(row, COL_1DAY_CAPTURED_AT) and has_cell_value(row, COL_1DAY_CAPTURE_MODE)
        has_7day_metadata = has_cell_value(row, COL_7DAY_CAPTURED_AT) and has_cell_value(row, COL_7DAY_CAPTURE_MODE)
        rows.append(SheetRow(
            row_index=row_idx,
            date_str=date_str,
            time_str=time_str,
            url=url,
            has_1day=has_1day,
            has_7day=has_7day,
            has_1day_metadata=has_1day_metadata,
            has_7day_metadata=has_7day_metadata,
        ))
    return rows


def build_media_by_path(media_list: List[MediaInfo]) -> Dict[str, MediaInfo]:
    """permalink のパス部分をキーにした辞書"""
    by_path: Dict[str, MediaInfo] = {}
    for m in media_list:
        path = extract_url_path(m.permalink)
        if path:
            by_path[path] = m
    return by_path


def ensure_raw_sheet(sheets_service) -> None:
    """Raw インサイト保存用シートがなければ作成し、ヘッダー行をセットする。"""
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets(properties(title))"
    ).execute()
    titles = {s["properties"]["title"] for s in spreadsheet.get("sheets", [])}

    if RAW_SHEET_NAME not in titles:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": RAW_SHEET_NAME,
                            }
                        }
                    }
                ]
            },
        ).execute()

    # ヘッダーがなければ書く
    resp = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"{RAW_SHEET_NAME}!A1:O1")
        .execute()
    )
    current_header = resp.get("values", [[]])[0] if resp.get("values") else []
    if current_header != RAW_SHEET_HEADERS:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{RAW_SHEET_NAME}!A1:O1",
            valueInputOption="USER_ENTERED",
            body={"values": [RAW_SHEET_HEADERS]},
        ).execute()


def append_raw_snapshot(
    sheets_service,
    row: int,
    media: MediaInfo,
    snapshot_type: str,
    capture_mode: str,
    insights: Dict[str, int],
) -> None:
    """Raw シートに 1 行追記する。"""
    values = [
        [
            row,
            snapshot_type,
            capture_mode,
            datetime.now(timezone.utc).isoformat(),
            media.media_id,
            media.permalink,
            insights.get("reach"),
            insights.get("views"),
            insights.get("total_interactions"),
            insights.get("likes"),
            insights.get("saved"),
            insights.get("comments"),
            insights.get("shares"),
            insights.get("profile_visits"),
            insights.get("follows"),
        ]
    ]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{RAW_SHEET_NAME}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def fill_missing_urls(
    sheets_service,
    values: List[List[Any]],
    media_list: List[MediaInfo],
    start_row: int = 4,
    debug: bool = False,
) -> None:
    """
    H 列の URL が空の行について、日付(A)・時刻(D)から投稿日時を推定し、
    Instagram のメディア一覧から「同じ年月日・同じ時分」の permalink を埋める。
    同分のメディアがなければ、同日で最も近い時刻のメディアを採用（最大±15分）。
    """
    filled_count = 0
    debug_count = 0
    for offset, row in enumerate(values):
        row_idx = start_row + offset
        # 既に URL が入っていればスキップ
        if len(row) > COL_URL and str(row[COL_URL] or "").strip():
            continue

        date_str = (row[COL_DATE] or "").strip() if len(row) > COL_DATE else ""
        time_str = (row[COL_TIME] or "").strip() if len(row) > COL_TIME else ""
        dt_utc = parse_sheet_datetime(date_str, time_str)
        if not dt_utc:
            if debug and debug_count < 5:
                print(f"  [URL補完] 行{row_idx}: 日付・時刻パース失敗 A={date_str!r} D={time_str!r}")
                debug_count += 1
            continue

        target_jst = dt_utc.astimezone(JST)
        target_key = (
            target_jst.year,
            target_jst.month,
            target_jst.day,
            target_jst.hour,
            target_jst.minute,
        )

        # 1) 完全一致（年月日・時・分）を優先
        best_media: Optional[MediaInfo] = None
        best_diff: Optional[float] = None
        for m in media_list:
            mj = m.timestamp.astimezone(JST)
            media_key = (mj.year, mj.month, mj.day, mj.hour, mj.minute)
            if media_key != target_key:
                continue
            diff = abs((m.timestamp - dt_utc).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_media = m

        # 2) 同分がなければ同日で±15分以内の最も近いメディアを採用
        if best_media is None:
            max_diff_sec = 15 * 60  # 15分
            for m in media_list:
                mj = m.timestamp.astimezone(JST)
                if (mj.year, mj.month, mj.day) != (target_jst.year, target_jst.month, target_jst.day):
                    continue
                diff = abs((m.timestamp - dt_utc).total_seconds())
                if diff <= max_diff_sec and (best_diff is None or diff < best_diff):
                    best_diff = diff
                    best_media = m

        if best_media is None:
            if debug and debug_count < 5:
                # 同日のメディアがあるか確認
                same_day = [m for m in media_list if (m.timestamp.astimezone(JST).year, m.timestamp.astimezone(JST).month, m.timestamp.astimezone(JST).day) == (target_jst.year, target_jst.month, target_jst.day)]
                print(f"  [URL補完] 行{row_idx}: マッチなし key={target_key} A={date_str!r} D={time_str!r} 同日メディア={len(same_day)}件")
                debug_count += 1
            continue

        # values 配列内を更新（後続の parse_sheet_rows が使う）
        if len(row) <= COL_URL:
            row.extend([""] * (COL_URL + 1 - len(row)))
        row[COL_URL] = best_media.permalink

        # 実際のシートにも書き戻す
        update_sheet_cell(sheets_service, row_idx, COL_URL, best_media.permalink)
        filled_count += 1

    if filled_count:
        print(f"URL 自動補完: {filled_count} 行に permalink を設定しました。")


def get_sheet_row_count(sheets_service) -> int:
    """2026 タブの現在の行数を取得する。"""
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,gridProperties(rowCount)))",
    ).execute()
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("sheetId") == SHEET_ID_2026:
            return int(props.get("gridProperties", {}).get("rowCount", 200))
    return 200


def column_letter(col_index: int) -> str:
    """0-based 列番号を A, B, ..., Z, AA, ... に変換"""
    out = []
    n = col_index + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        out.append(chr(65 + r))
    return "".join(reversed(out))


def update_sheet_cell(
    sheets_service, row: int, col_index: int, value: Any
) -> None:
    """1セルを更新（sheetId で指定。絵文字入りシート名のパースエラーを回避）"""
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {
                "dataFilter": {
                    "gridRange": {
                        "sheetId": SHEET_ID_2026,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1,
                    }
                },
                "values": [[value]],
            }
        ],
    }
    sheets_service.spreadsheets().values().batchUpdateByDataFilter(
        spreadsheetId=SHEET_ID, body=body
    ).execute()


def batch_update_cells(sheets_service, updates: List[Dict[str, Any]]) -> None:
    """複数セルを一括更新して write quota を節約する。"""
    if not updates:
        return
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": updates,
    }
    sheets_service.spreadsheets().values().batchUpdateByDataFilter(
        spreadsheetId=SHEET_ID, body=body
    ).execute()


def build_metric_updates(
    row: int,
    insights: Dict[str, int],
    metric_to_col: Dict[str, int],
) -> List[Dict[str, Any]]:
    updates: List[Dict[str, Any]] = []
    for metric, col_index in metric_to_col.items():
        if metric not in insights:
            continue
        updates.append(
            {
                "dataFilter": {
                    "gridRange": {
                        "sheetId": SHEET_ID_2026,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1,
                    }
                },
                "values": [[insights[metric]]],
            }
        )
    return updates


def build_snapshot_metadata_updates(
    row: int,
    snapshot_type: str,
    capture_mode: str,
    captured_at: str,
) -> List[Dict[str, Any]]:
    updates: List[Dict[str, Any]] = []
    if snapshot_type == "1day":
        targets = [
            (COL_1DAY_CAPTURED_AT, captured_at),
            (COL_1DAY_CAPTURE_MODE, capture_mode),
            (COL_LATEST_CAPTURED_AT, captured_at),
        ]
    else:
        targets = [
            (COL_7DAY_CAPTURED_AT, captured_at),
            (COL_7DAY_CAPTURE_MODE, capture_mode),
            (COL_LATEST_CAPTURED_AT, captured_at),
        ]
    for col_index, value in targets:
        updates.append(
            {
                "dataFilter": {
                    "gridRange": {
                        "sheetId": SHEET_ID_2026,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1,
                    }
                },
                "values": [[value]],
            }
        )
    return updates


def write_metric_block(
    sheets_service,
    row: int,
    insights: Dict[str, int],
    metric_to_col: Dict[str, int],
) -> None:
    batch_update_cells(
        sheets_service,
        build_metric_updates(row, insights, metric_to_col),
    )


def append_raw_snapshots(sheets_service, rows: List[List[Any]]) -> None:
    """Raw シートに複数行をまとめて追記する。"""
    if not rows:
        return
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{RAW_SHEET_NAME}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def capture_mode_for_snapshot(snapshot_type: str, hours: float) -> str:
    if snapshot_type == "1day":
        return "scheduled" if HOURS_1DAY_MIN <= hours < 48 else "backfill"
    return "scheduled" if HOURS_7DAY_MIN <= hours < (8 * 24) else "backfill"


def main() -> None:
    print("=== Instagram インサイト連携 ===\n")

    # 1. 認証
    try:
        creds = get_google_credentials()
        access_token, ig_user_id = load_instagram_config()
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        return

    sheets_service = build("sheets", "v4", credentials=creds)
    ensure_raw_sheet(sheets_service)

    # 2. Instagram メディア一覧取得
    try:
        media_list = fetch_all_media(access_token, ig_user_id)
    except requests.RequestException as e:
        print(f"Instagram API エラー: {e}")
        return
    media_by_path = build_media_by_path(media_list)
    print(f"API: メディア {len(media_list)} 件、パス照合用 {len(media_by_path)} 件")

    # 3. シート読み取り（A4:最終行、sheetId で指定。絵文字入りシート名のパースエラーを回避）
    sheet_row_count = get_sheet_row_count(sheets_service)
    body = {
        "dataFilters": [
            {
                "gridRange": {
                    "sheetId": SHEET_ID_2026,
                    "startRowIndex": 3,
                    "endRowIndex": sheet_row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": 93,
                }
            }
        ],
    }
    result = (
        sheets_service.spreadsheets()
        .values()
        .batchGetByDataFilter(spreadsheetId=SHEET_ID, body=body)
        .execute()
    )
    value_ranges = result.get("valueRanges", [])
    values = value_ranges[0].get("valueRange", {}).get("values", []) if value_ranges else []

    # URL が空の行について、日付・時刻から permalink を推定して埋める
    fill_missing_urls(sheets_service, values, media_list, start_row=4)

    sheet_rows = parse_sheet_rows(values, start_row=4)
    print(f"シート: URL が入っている行 {len(sheet_rows)} 件")

    # 4. 照合してインサイト取得・書き込み
    now = datetime.now(timezone.utc)
    written_1day = 0
    written_7day = 0
    repaired_1day_metadata = 0
    repaired_7day_metadata = 0
    pending_updates: List[Dict[str, Any]] = []
    pending_raw_rows: List[List[Any]] = []

    for sheet_row in sheet_rows:
        path = extract_url_path(sheet_row.url)
        if not path:
            continue
        media = media_by_path.get(path)
        if not media:
            continue

        # 経過時間（時間）
        delta = now - media.timestamp
        hours = delta.total_seconds() / 3600

        needs_1day_metrics = hours >= HOURS_1DAY_MIN and not sheet_row.has_1day
        needs_7day_metrics = hours >= HOURS_7DAY_MIN and not sheet_row.has_7day
        needs_1day_metadata = hours >= HOURS_1DAY_MIN and not sheet_row.has_1day_metadata
        needs_7day_metadata = hours >= HOURS_7DAY_MIN and not sheet_row.has_7day_metadata

        if not any([needs_1day_metrics, needs_7day_metrics, needs_1day_metadata, needs_7day_metadata]):
            continue

        # インサイト取得
        insights = fetch_insights(access_token, media.media_id)
        if not insights:
            continue

        row = sheet_row.row_index

        # 1日後ブロック（24h 以上。値未入力の補完と、取得メタデータ欠損の修復を行う）
        if needs_1day_metrics or needs_1day_metadata:
            captured_at = datetime.now(timezone.utc).isoformat()
            capture_mode = capture_mode_for_snapshot("1day", hours)
            if needs_1day_metrics:
                pending_updates.extend(build_metric_updates(row, insights, METRIC_TO_COL_1DAY))
            if needs_1day_metadata:
                pending_updates.extend(build_snapshot_metadata_updates(row, "1day", capture_mode, captured_at))
            pending_raw_rows.append([
                row,
                "1day",
                capture_mode,
                captured_at,
                media.media_id,
                media.permalink,
                insights.get("reach"),
                insights.get("views"),
                insights.get("total_interactions"),
                insights.get("likes"),
                insights.get("saved"),
                insights.get("comments"),
                insights.get("shares"),
                insights.get("profile_visits"),
                insights.get("follows"),
            ])
            if needs_1day_metrics:
                written_1day += 1
                print(
                    f"  行 {row}: 1日後を更新 "
                    f"[{capture_mode}] "
                    f"(reach={insights.get('reach')}, views={insights.get('views')}, likes={insights.get('likes')})"
                )
            else:
                repaired_1day_metadata += 1
                print(f"  行 {row}: 1日後メタデータを補完 [{capture_mode}]")

        # 1週間後ブロック（7日以上。値未入力の補完と、取得メタデータ欠損の修復を行う）
        if needs_7day_metrics or needs_7day_metadata:
            captured_at = datetime.now(timezone.utc).isoformat()
            capture_mode = capture_mode_for_snapshot("7day", hours)
            if needs_7day_metrics:
                pending_updates.extend(build_metric_updates(row, insights, METRIC_TO_COL_7DAY))
            if needs_7day_metadata:
                pending_updates.extend(build_snapshot_metadata_updates(row, "7day", capture_mode, captured_at))
            pending_raw_rows.append([
                row,
                "7day",
                capture_mode,
                captured_at,
                media.media_id,
                media.permalink,
                insights.get("reach"),
                insights.get("views"),
                insights.get("total_interactions"),
                insights.get("likes"),
                insights.get("saved"),
                insights.get("comments"),
                insights.get("shares"),
                insights.get("profile_visits"),
                insights.get("follows"),
            ])
            if needs_7day_metrics:
                written_7day += 1
                print(
                    f"  行 {row}: 1週間後を更新 "
                    f"[{capture_mode}] "
                    f"(reach={insights.get('reach')}, views={insights.get('views')}, likes={insights.get('likes')})"
                )
            else:
                repaired_7day_metadata += 1
                print(f"  行 {row}: 1週間後メタデータを補完 [{capture_mode}]")

    batch_update_cells(sheets_service, pending_updates)
    append_raw_snapshots(sheets_service, pending_raw_rows)

    print(
        f"\n完了: 1日後 {written_1day} 件、1週間後 {written_7day} 件を更新、"
        f"1日後メタデータ {repaired_1day_metadata} 件、"
        f"1週間後メタデータ {repaired_7day_metadata} 件を補完しました。"
    )


if __name__ == "__main__":
    main()
