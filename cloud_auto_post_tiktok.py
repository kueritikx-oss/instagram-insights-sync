#!/usr/bin/env python3
"""
TikTok自動投稿スクリプト (cloud_auto_post_tiktok.py)

Threads自動投稿のアーキテクチャをベースに、
TikTok Content Posting API v2 (open.tiktokapis.com) 用に構築。

対応フォーマット:
  - Photo Post（画像スライド、最大35枚。DIRECT_POST）

認証:
  - TikTok: OAuth 2.0 (access_token + refresh_token、24時間で失効)
  - Google Sheets: OAuth 2.0 (GOOGLE_TOKEN_JSON)

重要な制約:
  - 画像はPULL_FROM_URLのみ（直接アップロード不可）
  - 画像ホスティングドメインの所有権検証が必須
  - WebP/JPEGのみ対応（PNGは不可）
  - 未監査アプリはSELF_ONLYのみ（App Review通過後にPUBLIC_TO_EVERYONE）
  - アクセストークンは24時間で失効（自動リフレッシュ内蔵）

スプレッドシート: TikTok投稿毎データ
  A列: 日付, C列: 番号(TT-001), D列: 時刻, L列: キャプション

Usage:
    python3 cloud_auto_post_tiktok.py --window 45
    python3 cloud_auto_post_tiktok.py --force TT-001
    python3 cloud_auto_post_tiktok.py --dry-run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 定数 ─────────────────────────────────────────────────────────────

# TikTok API
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"

# スプレッドシート — タッキーが作成後にIDを設定
TIKTOK_SPREADSHEET_ID = os.environ.get(
    "TIKTOK_SPREADSHEET_ID",
    "PLACEHOLDER_SPREADSHEET_ID",  # ← 作成後に更新
)
TIKTOK_SHEET_NAME = "TikTok投稿毎データ"
DATA_START_ROW = 4

# 列インデックス（0-based）— IG/Threads/Xと構造を揃えた配置
COL_DATE = 0        # A: 日付
COL_POST_NUM = 2    # C: 番号（TT-001形式）
COL_TIME = 3        # D: 時刻
COL_HOOK = 4        # E: フック/タイトル
COL_CTA_TYPE = 5    # F: 投稿種別
COL_FORMAT = 6      # G: 形式（認知/価値提供/誘導）
COL_URL = 7         # H: TikTok URL
COL_INTENT = 8      # I: 投稿の意図
COL_TYPE = 9        # J: タイプ（フォト/動画）
COL_NOTES = 10      # K: 備考（IG元投稿番号等）
COL_BODY = 11       # L: キャプション（description、最大4000文字）
COL_TITLE = 12      # M: タイトル（最大90文字、Photo Post用）

# 自動投稿列 (N-S)
COL_STATUS = 13     # N: 投稿ステータス (ready/posting/posted/retry/failed)
COL_IMAGE_URLS = 14 # O: 画像URLs (JSON配列、HTTPS、WebP/JPEG)
COL_PUBLISH_ID = 15 # P: TikTok publish_id
COL_ERROR = 16      # Q: エラーメッセージ
COL_RETRY = 17      # R: リトライ回数
COL_LAST_ATTEMPT = 18  # S: 最終投稿試行

# ── 読み取り範囲の自動算出 ──────────────────────────────────────────
def _col_idx_to_letter(idx: int) -> str:
    result = ""
    n = idx
    while True:
        result = chr(ord("A") + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result

_MAX_COL_IDX = max(v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int))
SHEET_READ_END_COL = _col_idx_to_letter(_MAX_COL_IDX)

# 制限・設定
MAX_RETRIES = 3
POST_GAP_SECONDS = 30
DAILY_POST_LIMIT = int(os.environ.get("TIKTOK_DAILY_LIMIT", "5"))
PUBLISH_POLL_MAX_SEC = 300    # 投稿完了待ち最大秒
PUBLISH_POLL_INTERVAL = 10    # ポーリング間隔
MAX_PHOTOS = 35               # 1投稿あたりの画像上限
MAX_TITLE_CHARS = 90          # titleの文字数上限（UTF-16）
MAX_DESC_CHARS = 4000         # descriptionの文字数上限（UTF-16）

# タイムゾーン
JST = timezone(timedelta(hours=9))

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"
TIKTOK_TOKEN_FILE = GOOGLE_AUTH_DIR / "tiktok_token.json"


# ── Google Sheets ─────────────────────────────────────────────────────

def get_sheets_service():
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
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def read_sheet_data(service, range_str: str) -> list:
    result = service.spreadsheets().values().get(
        spreadsheetId=TIKTOK_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    return result.get("values", [])


def update_cell(service, row: int, col: int, value: str):
    col_letter = _col_idx_to_letter(col)
    cell = f"{TIKTOK_SHEET_NAME}!{col_letter}{row}"
    service.spreadsheets().values().update(
        spreadsheetId=TIKTOK_SPREADSHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def batch_update_cells(service, updates: list):
    data = []
    for row, col, value in updates:
        col_letter = _col_idx_to_letter(col)
        data.append({
            "range": f"{TIKTOK_SHEET_NAME}!{col_letter}{row}",
            "values": [[value]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=TIKTOK_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def get_col_value(row_data: list, col_idx: int) -> str:
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


# ── TikTok API 認証 ──────────────────────────────────────────────────

def load_tiktok_token() -> dict:
    """TikTokトークンを取得。環境変数 > ファイル。24時間失効なので自動リフレッシュ。"""
    token_json = os.environ.get("TIKTOK_TOKEN_JSON")
    if token_json:
        data = json.loads(token_json)
    elif TIKTOK_TOKEN_FILE.exists():
        with open(TIKTOK_TOKEN_FILE) as f:
            data = json.load(f)
    else:
        print("ERROR: TikTok token not found")
        print("  Set TIKTOK_TOKEN_JSON env var or create tiktok_token.json")
        sys.exit(1)

    # トークン有効期限チェック（24時間）
    saved_at = data.get("saved_at", 0)
    expires_in = data.get("expires_in", 86400)
    elapsed = time.time() - saved_at

    if elapsed >= expires_in - 300:  # 5分前にリフレッシュ
        print("🔄 TikTokトークンが期限切れ → リフレッシュ中...")
        data = refresh_tiktok_token(data)

    return data


def refresh_tiktok_token(token_data: dict) -> dict:
    """TikTokアクセストークンをリフレッシュ"""
    client_key = token_data.get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = token_data.get("client_secret") or os.environ.get("TIKTOK_CLIENT_SECRET")
    refresh_token = token_data.get("refresh_token")

    if not all([client_key, client_secret, refresh_token]):
        print("ERROR: Cannot refresh TikTok token (missing credentials)")
        sys.exit(1)

    resp = requests.post(
        f"{TIKTOK_API_BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    resp.raise_for_status()
    new_data = resp.json()

    if new_data.get("error", {}).get("code") not in (None, "", "ok"):
        raise RuntimeError(f"Token refresh failed: {new_data}")

    # 元のclient_key/secretを保持
    new_data["client_key"] = client_key
    new_data["client_secret"] = client_secret
    new_data["saved_at"] = time.time()

    # ローカルファイルに保存
    if TIKTOK_TOKEN_FILE.parent.exists():
        with open(TIKTOK_TOKEN_FILE, "w") as f:
            json.dump(new_data, f, indent=2)

    print(f"  ✅ トークンリフレッシュ成功 (expires_in={new_data.get('expires_in')}s)")
    return new_data


def get_tiktok_access_token() -> str:
    """有効なTikTokアクセストークンを取得"""
    data = load_tiktok_token()
    return data.get("access_token", "")


def get_tiktok_open_id() -> str:
    """TikTok open_idを取得"""
    oid = os.environ.get("TIKTOK_OPEN_ID")
    if oid:
        return oid
    data = load_tiktok_token()
    return data.get("open_id", "")


# ── TikTok Photo Post ────────────────────────────────────────────────

def query_creator_info(access_token: str) -> dict:
    """クリエイター情報を取得（利用可能なprivacy_levelを確認）"""
    resp = requests.post(
        f"{TIKTOK_API_BASE}/post/publish/creator_info/query/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "", "ok"):
        raise RuntimeError(f"Creator info query failed: {data}")
    return data.get("data", {})


def post_photo(access_token: str, photo_urls: list,
               title: str = "", description: str = "",
               privacy_level: str = "PUBLIC_TO_EVERYONE") -> str:
    """Photo Postを投稿。publish_idを返す。"""
    # バリデーション
    if not photo_urls:
        raise ValueError("photo_urlsは1枚以上必要")
    if len(photo_urls) > MAX_PHOTOS:
        photo_urls = photo_urls[:MAX_PHOTOS]

    # 画像URLはHTTPS必須
    for url in photo_urls:
        if not url.startswith("https://"):
            raise ValueError(f"画像URLはHTTPS必須: {url}")

    body = {
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
        "post_info": {
            "title": title[:MAX_TITLE_CHARS] if title else "",
            "description": description[:MAX_DESC_CHARS] if description else "",
            "privacy_level": privacy_level,
            "disable_comment": False,
            "auto_add_music": True,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": photo_urls,
        },
    }

    resp = requests.post(
        f"{TIKTOK_API_BASE}/post/publish/content/init/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    error_code = data.get("error", {}).get("code", "")
    if error_code and error_code != "ok":
        raise RuntimeError(f"Photo post failed: {data['error']}")

    return data["data"]["publish_id"]


def wait_for_publish(access_token: str, publish_id: str) -> dict:
    """投稿完了までポーリング"""
    elapsed = 0
    while elapsed < PUBLISH_POLL_MAX_SEC:
        resp = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("data", {}).get("status", "")

        if status == "PUBLISH_COMPLETE":
            return data.get("data", {})
        elif status == "FAILED":
            reason = data.get("data", {}).get("fail_reason", "unknown")
            raise RuntimeError(f"TikTok投稿失敗: {reason}")

        time.sleep(PUBLISH_POLL_INTERVAL)
        elapsed += PUBLISH_POLL_INTERVAL

    raise RuntimeError(f"TikTok投稿タイムアウト ({PUBLISH_POLL_MAX_SEC}s)")


# ── スケジュール判定 ──────────────────────────────────────────────────

def parse_schedule_time(date_str: str, time_str: str) -> datetime | None:
    if not date_str or not time_str:
        return None

    date_str = date_str.strip()
    date_obj = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d"):
        try:
            date_obj = datetime.strptime(date_str, fmt)
            if date_obj.year == 1900:
                date_obj = date_obj.replace(year=datetime.now(JST).year)
            break
        except ValueError:
            continue

    if not date_obj:
        try:
            serial = float(date_str)
            date_obj = datetime(1899, 12, 30) + timedelta(days=serial)
        except (ValueError, TypeError):
            return None

    time_str = time_str.strip()
    hour, minute = 0, 0
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        elif "." in time_str:
            frac = float(time_str)
            total_min = int(frac * 24 * 60)
            hour, minute = total_min // 60, total_min % 60
        else:
            h = int(time_str)
            if h > 100:
                hour, minute = h // 100, h % 100
            else:
                hour = h
    except (ValueError, TypeError):
        return None

    return date_obj.replace(hour=hour, minute=minute, tzinfo=JST)


def is_within_window(scheduled: datetime, now: datetime, window_minutes: int) -> bool:
    diff = (now - scheduled).total_seconds() / 60
    return 0 <= diff <= window_minutes


# ── メイン処理 ────────────────────────────────────────────────────────

def find_ready_posts(service, now: datetime, window_minutes: int) -> list:
    rows = read_sheet_data(service, f"{TIKTOK_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
    ready = []

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        status = get_col_value(row, COL_STATUS)

        if status not in ("ready", "retry"):
            continue

        date_str = get_col_value(row, COL_DATE)
        time_str = get_col_value(row, COL_TIME)
        scheduled = parse_schedule_time(date_str, time_str)

        if not scheduled:
            continue

        if is_within_window(scheduled, now, window_minutes):
            ready.append({
                "row": actual_row,
                "data": row,
                "scheduled": scheduled,
                "post_num": get_col_value(row, COL_POST_NUM),
            })

    ready.sort(key=lambda x: x["scheduled"])
    return ready


def find_post_by_num(service, post_num: str) -> dict | None:
    rows = read_sheet_data(service, f"{TIKTOK_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        if get_col_value(row, COL_POST_NUM) == post_num:
            return {"row": actual_row, "data": row, "post_num": post_num}
    return None


def execute_post(service, access_token: str, post_info: dict,
                 privacy_level: str = "PUBLIC_TO_EVERYONE",
                 dry_run: bool = False) -> bool:
    row = post_info["row"]
    data = post_info["data"]
    post_num = post_info["post_num"]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    body = get_col_value(data, COL_BODY)
    hook = get_col_value(data, COL_HOOK)
    title = get_col_value(data, COL_TITLE)
    image_urls_json = get_col_value(data, COL_IMAGE_URLS)

    description = body if body else hook
    if not title:
        # タイトルが空→hookの先頭90文字をtitleに
        title = (hook or description)[:MAX_TITLE_CHARS]

    if not image_urls_json:
        print(f"  SKIP {post_num}: 画像URLが空（TikTokはPhoto Post必須）")
        return False

    # 画像URLパース
    image_urls = None
    try:
        image_urls = json.loads(image_urls_json)
    except json.JSONDecodeError:
        if image_urls_json.startswith("https://"):
            image_urls = [image_urls_json]

    if not image_urls:
        print(f"  SKIP {post_num}: 画像URLのパース失敗")
        return False

    print(f"  📸 {post_num}: {title[:50]}... ({len(image_urls)}枚)")
    print(f"     desc: {(description or '')[:60]}...")

    if dry_run:
        print(f"  ✅ [DRY RUN] 投稿をスキップ")
        return True

    # ステータス更新
    batch_update_cells(service, [
        (row, COL_STATUS, "posting"),
        (row, COL_LAST_ATTEMPT, now_str),
    ])

    try:
        # Photo Post投稿
        publish_id = post_photo(
            access_token=access_token,
            photo_urls=image_urls,
            title=title,
            description=description or "",
            privacy_level=privacy_level,
        )

        print(f"     publish_id: {publish_id}")

        # 投稿完了待ち
        result = wait_for_publish(access_token, publish_id)
        post_id_list = result.get("publicaly_available_post_id", [])
        post_id = post_id_list[0] if post_id_list else publish_id

        # 成功更新
        batch_update_cells(service, [
            (row, COL_STATUS, "posted"),
            (row, COL_PUBLISH_ID, str(publish_id)),
            (row, COL_LAST_ATTEMPT, now_str),
            (row, COL_ERROR, ""),
        ])

        # TikTok URLをH列に書き込み（post_idがあれば）
        if post_id_list:
            tiktok_url = f"https://www.tiktok.com/@tackey_clear_skincare/photo/{post_id}"
            update_cell(service, row, COL_URL, tiktok_url)
            print(f"  ✅ 投稿成功: {post_num} → {tiktok_url}")
        else:
            print(f"  ✅ 投稿成功: {post_num} (publish_id={publish_id})")

        return True

    except Exception as e:
        error_msg = str(e)[:500]
        retry_count = 0
        try:
            retry_count = int(get_col_value(data, COL_RETRY) or "0")
        except (ValueError, IndexError):
            pass

        retry_count += 1
        new_status = "retry" if retry_count < MAX_RETRIES else "failed"

        batch_update_cells(service, [
            (row, COL_STATUS, new_status),
            (row, COL_ERROR, error_msg),
            (row, COL_RETRY, str(retry_count)),
            (row, COL_LAST_ATTEMPT, now_str),
        ])

        print(f"  ❌ 投稿失敗: {post_num} ({new_status}, retry={retry_count})")
        print(f"     Error: {error_msg[:100]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="TikTok自動投稿")
    parser.add_argument("--window", type=int, default=45)
    parser.add_argument("--force", type=str, default=None,
                        help="強制投稿する投稿番号（TT-001形式）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--privacy", type=str, default="PUBLIC_TO_EVERYONE",
                        choices=["PUBLIC_TO_EVERYONE", "SELF_ONLY",
                                 "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"],
                        help="公開範囲（未監査アプリはSELF_ONLYのみ）")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"🎵 TikTok自動投稿 - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   Window: {args.window}min | DryRun: {args.dry_run} | Privacy: {args.privacy}")
    print(f"   ReadRange: A:{SHEET_READ_END_COL} (max COL idx={_MAX_COL_IDX})")
    print()

    # 起動時バリデーション
    col_constants = {k: v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int)}
    for name, idx in col_constants.items():
        if idx > _MAX_COL_IDX:
            print(f"❌ FATAL: {name}={idx} が読み取り範囲を超えています")
            sys.exit(1)

    if TIKTOK_SPREADSHEET_ID == "PLACEHOLDER_SPREADSHEET_ID":
        print("❌ FATAL: TIKTOK_SPREADSHEET_ID が未設定です")
        sys.exit(1)

    # 初期化
    service = get_sheets_service()
    access_token = get_tiktok_access_token()

    # クリエイター情報確認
    if not args.dry_run:
        try:
            creator = query_creator_info(access_token)
            privacy_options = creator.get("privacy_level_options", [])
            print(f"📊 利用可能なprivacy: {privacy_options}")
            if args.privacy not in privacy_options:
                print(f"⚠️ {args.privacy} が利用不可。SELF_ONLYにフォールバック")
                args.privacy = "SELF_ONLY"
        except Exception as e:
            print(f"⚠️ クリエイター情報取得失敗: {e}")

    # 強制投稿
    if args.force:
        post_info = find_post_by_num(service, args.force)
        if not post_info:
            print(f"❌ 投稿 {args.force} が見つかりません")
            return
        execute_post(service, access_token, post_info,
                     privacy_level=args.privacy, dry_run=args.dry_run)
        return

    # スケジュール投稿
    ready = find_ready_posts(service, now, args.window)
    if not ready:
        print("📭 投稿可能な行なし")
        return

    print(f"📋 {len(ready)}件の投稿を検出")
    posted = 0

    for post_info in ready:
        if posted >= DAILY_POST_LIMIT:
            print(f"⚠️ 日次制限 ({DAILY_POST_LIMIT}) に到達")
            break

        success = execute_post(service, access_token, post_info,
                               privacy_level=args.privacy, dry_run=args.dry_run)
        if success:
            posted += 1
            if posted < len(ready):
                time.sleep(POST_GAP_SECONDS)

    print()
    print(f"🎉 完了: {posted}/{len(ready)}件を投稿")


if __name__ == "__main__":
    main()
