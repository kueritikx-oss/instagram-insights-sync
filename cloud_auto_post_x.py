#!/usr/bin/env python3
"""
X (旧Twitter) 自動投稿スクリプト (cloud_auto_post_x.py)

Threads自動投稿 (cloud_auto_post_threads.py) のアーキテクチャをベースに、
X API v2 (api.x.com) 用に構築。

対応フォーマット:
  - テキストのみ
  - 画像付きテキスト（最大4枚）
  - スレッド（reply chain）

認証:
  - X API v2: OAuth 1.0a (API Key + Secret + Access Token + Secret)
  - Google Sheets: OAuth 2.0 (GOOGLE_TOKEN_JSON)

スプレッドシート: X投稿毎データ
  A列: 日付, C列: 番号(X-001), D列: 時刻, L列: テキスト
  自動投稿列は COL_STATUS 以降

Usage:
    python3 cloud_auto_post_x.py --window 45
    python3 cloud_auto_post_x.py --force X-001
    python3 cloud_auto_post_x.py --force X-001 --thread "X-001,X-002,X-003"
    python3 cloud_auto_post_x.py --dry-run
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests_oauthlib import OAuth1Session
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 定数 ─────────────────────────────────────────────────────────────

# X API
X_API_BASE = "https://api.x.com"
X_TWEET_URL = f"{X_API_BASE}/2/tweets"
X_MEDIA_URL = f"{X_API_BASE}/2/media/upload"

# スプレッドシート
_DEFAULT_X_SPREADSHEET_ID = "1rHnDoMHUK_K0_f7MLxHltiU6Y2ATsz3ztKwdf2Zg8Hc"
_env_id = os.environ.get("X_SPREADSHEET_ID", "")
X_SPREADSHEET_ID = _env_id if (_env_id and "PLACEHOLDER" not in _env_id) else _DEFAULT_X_SPREADSHEET_ID
X_SHEET_NAME = "X投稿毎データ"
DATA_START_ROW = 4  # ヘッダー3行の次

# 列インデックス（0-based）— IG/Threadsと構造を揃えた配置
# 基本情報 (A-L) = 共通
COL_DATE = 0        # A: 日付
COL_POST_NUM = 2    # C: 番号（X-001形式）
COL_TIME = 3        # D: 時刻
COL_HOOK = 4        # E: フック/タイトル
COL_CTA_TYPE = 5    # F: 投稿種別（CTA型）
COL_FORMAT = 6      # G: 形式（認知/価値提供/誘導）
COL_URL = 7         # H: ツイートURL
COL_INTENT = 8      # I: 投稿の意図
COL_TYPE = 9        # J: タイプ（テキスト/画像/スレッド）
COL_NOTES = 10      # K: 備考（IG元投稿番号等）
COL_BODY = 11       # L: ツイート本文

# 自動投稿列 (M-R)
COL_STATUS = 12     # M: 投稿ステータス (ready/posting/posted/retry/failed)
COL_IMAGE_URLS = 13 # N: 画像URLs (JSON配列)
COL_TWEET_ID = 14   # O: ツイートID
COL_ERROR = 15      # P: エラーメッセージ
COL_RETRY = 16      # Q: リトライ回数
COL_LAST_ATTEMPT = 17  # R: 最終投稿試行

# ── 読み取り範囲の自動算出 ──────────────────────────────────────────
def _col_idx_to_letter(idx: int) -> str:
    """0-based列インデックスをExcel列文字に変換"""
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
POST_GAP_SECONDS = 30         # 投稿間のクールダウン
THREAD_REPLY_DELAY = 3        # スレッド内の投稿間隔（秒）
DAILY_POST_LIMIT = int(os.environ.get("X_DAILY_LIMIT", "10"))
MONTHLY_POST_LIMIT = 500      # Free tier月間上限
MAX_TWEET_CHARS = 280         # Xの文字数上限（weighted）
MAX_IMAGES_PER_TWEET = 4      # 1ツイートあたりの画像上限
MEDIA_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB チャンク

# タイムゾーン
JST = timezone(timedelta(hours=9))

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"


# ── X文字数カウント ──────────────────────────────────────────────────

def count_tweet_chars(text: str) -> int:
    """Xのweighted文字数カウント。
    - ASCII英数字・記号: 1
    - 日本語・CJK・絵文字: 2
    - URL: 常に23（t.co短縮後）
    """
    import re
    # URLを仮カウント（23文字として扱う）
    url_pattern = re.compile(r'https?://\S+')
    urls = url_pattern.findall(text)
    text_no_urls = url_pattern.sub('', text)

    count = 0
    for char in text_no_urls:
        cp = ord(char)
        # CJK Unified Ideographs, Hiragana, Katakana, CJK Symbols, etc.
        if (0x3000 <= cp <= 0x9FFF or   # CJK + Japanese
            0xF900 <= cp <= 0xFAFF or   # CJK Compatibility
            0x20000 <= cp <= 0x2FFFF or # CJK Extension B
            0x1F600 <= cp <= 0x1F9FF or # Emoticons & Symbols
            0x1F300 <= cp <= 0x1F5FF or # Misc Symbols
            0xFE00 <= cp <= 0xFE0F or   # Variation Selectors
            0x200D == cp):               # ZWJ
            count += 2
        else:
            count += 1

    # URLは各23文字
    count += len(urls) * 23
    return count


# ── Google Sheets ─────────────────────────────────────────────────────

def get_sheets_service():
    """Google Sheets APIサービスを取得"""
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
    """スプレッドシートからデータを読み取る"""
    result = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    return result.get("values", [])


def update_cell(service, row: int, col: int, value: str):
    """単一セルを更新"""
    col_letter = _col_idx_to_letter(col)
    cell = f"{X_SHEET_NAME}!{col_letter}{row}"
    service.spreadsheets().values().update(
        spreadsheetId=X_SPREADSHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def batch_update_cells(service, updates: list):
    """複数セルを一括更新。updates = [(row, col, value), ...]"""
    data = []
    for row, col, value in updates:
        col_letter = _col_idx_to_letter(col)
        data.append({
            "range": f"{X_SHEET_NAME}!{col_letter}{row}",
            "values": [[value]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=X_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def get_col_value(row_data: list, col_idx: int) -> str:
    """行データから列の値を安全に取得"""
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


# ── X API 認証 ───────────────────────────────────────────────────────

def get_x_oauth_session() -> OAuth1Session:
    """X API用OAuth1Sessionを取得"""
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        # ローカルフォールバック
        token_path = GOOGLE_AUTH_DIR / "x_token.json"
        if token_path.exists():
            with open(token_path) as f:
                data = json.load(f)
                api_key = data.get("api_key", api_key)
                api_secret = data.get("api_secret", api_secret)
                access_token = data.get("access_token", access_token)
                access_token_secret = data.get("access_token_secret", access_token_secret)

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("ERROR: X API credentials not found")
        print("  Required: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET")
        sys.exit(1)

    return OAuth1Session(
        client_key=api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )


# ── X メディアアップロード (v2 Chunked) ──────────────────────────────

def download_image(url: str) -> tuple[bytes, str]:
    """URLから画像をダウンロード。(bytes, content_type)を返す"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


def upload_media(oauth: OAuth1Session, image_url: str) -> str:
    """画像URLからX APIにアップロードしてmedia_idを返す。
    v2 chunked upload (INIT → APPEND → FINALIZE) を使用。
    """
    # 1. 画像ダウンロード
    image_data, content_type = download_image(image_url)
    total_bytes = len(image_data)

    # MIMEタイプの正規化
    mime_map = {
        "image/jpeg": "image/jpeg",
        "image/jpg": "image/jpeg",
        "image/png": "image/png",
        "image/gif": "image/gif",
        "image/webp": "image/webp",
    }
    media_type = mime_map.get(content_type, "image/jpeg")

    # 2. INIT
    init_data = {
        "command": "INIT",
        "media_type": media_type,
        "total_bytes": str(total_bytes),
        "media_category": "tweet_image",
    }
    resp = oauth.post(X_MEDIA_URL, data=init_data)
    if resp.status_code != 200 and resp.status_code != 202:
        raise RuntimeError(f"Media INIT failed ({resp.status_code}): {resp.text}")
    media_id = resp.json()["id"]

    # 3. APPEND（チャンク分割）
    segment_index = 0
    offset = 0
    while offset < total_bytes:
        chunk = image_data[offset:offset + MEDIA_CHUNK_SIZE]
        files = {"media": ("chunk", chunk, "application/octet-stream")}
        append_data = {
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": str(segment_index),
        }
        resp = oauth.post(X_MEDIA_URL, data=append_data, files=files)
        if resp.status_code not in (200, 202, 204):
            raise RuntimeError(f"Media APPEND failed ({resp.status_code}): {resp.text}")
        offset += MEDIA_CHUNK_SIZE
        segment_index += 1

    # 4. FINALIZE
    finalize_data = {
        "command": "FINALIZE",
        "media_id": media_id,
    }
    resp = oauth.post(X_MEDIA_URL, data=finalize_data)
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"Media FINALIZE failed ({resp.status_code}): {resp.text}")

    result = resp.json()

    # 5. STATUS（非同期処理の場合ポーリング）
    processing = result.get("processing_info")
    poll_count = 0
    while processing and processing.get("state") in ("pending", "in_progress"):
        wait = processing.get("check_after_secs", 5)
        time.sleep(wait)
        status_resp = oauth.get(f"{X_MEDIA_URL}?command=STATUS&media_id={media_id}")
        if status_resp.status_code != 200:
            break
        result = status_resp.json()
        processing = result.get("processing_info")
        poll_count += 1
        if poll_count > 30:  # 最大150秒
            raise RuntimeError(f"Media processing timeout: {media_id}")

    return media_id


# ── X ツイート投稿 ───────────────────────────────────────────────────

def post_tweet(oauth: OAuth1Session, text: str,
               media_ids: list = None,
               reply_to_id: str = None) -> dict:
    """ツイートを投稿。{id, text} を返す。"""
    payload = {"text": text}

    if media_ids:
        payload["media"] = {"media_ids": media_ids}

    if reply_to_id:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to_id}

    resp = oauth.post(X_TWEET_URL, json=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Tweet post failed ({resp.status_code}): {resp.text}")

    return resp.json().get("data", {})


def post_text_only(oauth: OAuth1Session, text: str) -> dict:
    """テキストのみ投稿"""
    return post_tweet(oauth, text)


def post_with_images(oauth: OAuth1Session, text: str,
                     image_urls: list) -> dict:
    """画像付き投稿（最大4枚）"""
    media_ids = []
    for url in image_urls[:MAX_IMAGES_PER_TWEET]:
        mid = upload_media(oauth, url)
        media_ids.append(mid)
        time.sleep(1)  # アップロード間のスロットル

    return post_tweet(oauth, text, media_ids=media_ids)


def post_thread(oauth: OAuth1Session,
                tweets: list) -> list:
    """スレッド（連投）。
    tweets = [{"text": str, "image_urls": list|None}, ...]
    戻り値: [{"id": str, "text": str}, ...]
    """
    results = []
    prev_id = None

    for i, tw in enumerate(tweets):
        text = tw["text"]
        image_urls = tw.get("image_urls")
        media_ids = None

        if image_urls:
            media_ids = []
            for url in image_urls[:MAX_IMAGES_PER_TWEET]:
                mid = upload_media(oauth, url)
                media_ids.append(mid)
                time.sleep(1)

        result = post_tweet(oauth, text,
                           media_ids=media_ids,
                           reply_to_id=prev_id)
        results.append(result)
        prev_id = result.get("id")

        if i < len(tweets) - 1:
            time.sleep(THREAD_REPLY_DELAY)

    return results


# ── スケジュール判定 ──────────────────────────────────────────────────

def parse_schedule_time(date_str: str, time_str: str) -> datetime | None:
    """日付+時刻文字列をdatetimeに変換"""
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
    """スケジュール時刻がウィンドウ内か判定"""
    diff = (now - scheduled).total_seconds() / 60
    return 0 <= diff <= window_minutes


# ── メイン処理 ────────────────────────────────────────────────────────

def find_ready_posts(service, now: datetime, window_minutes: int) -> list:
    """投稿可能な行を検索"""
    rows = read_sheet_data(service, f"{X_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
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
    """投稿番号で行を検索"""
    rows = read_sheet_data(service, f"{X_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        if get_col_value(row, COL_POST_NUM) == post_num:
            return {
                "row": actual_row,
                "data": row,
                "post_num": post_num,
            }
    return None


def count_posts_today(service, now: datetime) -> int:
    """本日の投稿数をカウント"""
    rows = read_sheet_data(service, f"{X_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
    today_str = now.strftime("%Y-%m-%d")
    count = 0

    for row in rows:
        status = get_col_value(row, COL_STATUS)
        last_attempt = get_col_value(row, COL_LAST_ATTEMPT)
        if status == "posted" and last_attempt.startswith(today_str):
            count += 1

    return count


def execute_post(service, oauth: OAuth1Session, post_info: dict,
                 dry_run: bool = False, thread_nums: list = None) -> bool:
    """1投稿を実行"""
    row = post_info["row"]
    data = post_info["data"]
    post_num = post_info["post_num"]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    post_type = get_col_value(data, COL_TYPE)
    body = get_col_value(data, COL_BODY)
    hook = get_col_value(data, COL_HOOK)
    image_urls_json = get_col_value(data, COL_IMAGE_URLS)

    text = body if body else hook
    if not text:
        print(f"  SKIP {post_num}: テキストが空")
        return False

    # 文字数チェック
    char_count = count_tweet_chars(text)
    if char_count > MAX_TWEET_CHARS:
        print(f"  ⚠ {post_num}: {char_count}文字 > {MAX_TWEET_CHARS}文字上限 → 切り詰め")
        # 簡易切り詰め（末尾に...を付ける）
        while count_tweet_chars(text) > MAX_TWEET_CHARS - 3:
            text = text[:-1]
        text = text.rstrip() + "..."

    print(f"  📝 {post_num}: {text[:60]}... ({char_count}chars)")
    print(f"     タイプ: {post_type}")

    if dry_run:
        print(f"  ✅ [DRY RUN] 投稿をスキップ")
        return True

    # ステータスを「posting」に更新
    batch_update_cells(service, [
        (row, COL_STATUS, "posting"),
        (row, COL_LAST_ATTEMPT, now_str),
    ])

    try:
        tweet_id = None
        tweet_url = ""

        # スレッド（連投）
        if thread_nums:
            tweets_data = []
            for tn in thread_nums:
                tp = find_post_by_num(service, tn)
                if tp:
                    tp_body = get_col_value(tp["data"], COL_BODY)
                    tp_hook = get_col_value(tp["data"], COL_HOOK)
                    tp_imgs = get_col_value(tp["data"], COL_IMAGE_URLS)
                    img_list = None
                    if tp_imgs:
                        try:
                            img_list = json.loads(tp_imgs)
                        except json.JSONDecodeError:
                            img_list = [tp_imgs] if tp_imgs.startswith("http") else None

                    tweets_data.append({
                        "text": tp_body or tp_hook,
                        "image_urls": img_list,
                    })

            if tweets_data:
                results = post_thread(oauth, tweets_data)
                if results:
                    tweet_id = results[0].get("id")
                    tweet_url = f"https://x.com/tackey_clear/status/{tweet_id}"
                    # 各スレッド投稿のステータス更新
                    for j, tn in enumerate(thread_nums):
                        tp = find_post_by_num(service, tn)
                        if tp and j < len(results):
                            tid = results[j].get("id")
                            batch_update_cells(service, [
                                (tp["row"], COL_STATUS, "posted"),
                                (tp["row"], COL_TWEET_ID, str(tid)),
                                (tp["row"], COL_URL, f"https://x.com/tackey_clear/status/{tid}"),
                            ])

        # 画像付き
        elif image_urls_json:
            image_urls = None
            try:
                image_urls = json.loads(image_urls_json)
            except json.JSONDecodeError:
                if image_urls_json.startswith("http"):
                    image_urls = [image_urls_json]

            if image_urls:
                result = post_with_images(oauth, text, image_urls)
            else:
                result = post_text_only(oauth, text)
            tweet_id = result.get("id")
            tweet_url = f"https://x.com/tackey_clear/status/{tweet_id}"

        # テキストのみ
        else:
            result = post_text_only(oauth, text)
            tweet_id = result.get("id")
            tweet_url = f"https://x.com/tackey_clear/status/{tweet_id}"

        # 成功更新
        batch_update_cells(service, [
            (row, COL_STATUS, "posted"),
            (row, COL_TWEET_ID, str(tweet_id)),
            (row, COL_LAST_ATTEMPT, now_str),
            (row, COL_ERROR, ""),
        ])

        if tweet_url:
            update_cell(service, row, COL_URL, tweet_url)

        print(f"  ✅ 投稿成功: {post_num} → {tweet_url}")
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
    parser = argparse.ArgumentParser(description="X自動投稿")
    parser.add_argument("--window", type=int, default=45,
                        help="スケジュールウィンドウ（分）")
    parser.add_argument("--force", type=str, default=None,
                        help="強制投稿する投稿番号（X-001形式）")
    parser.add_argument("--thread", type=str, default=None,
                        help="スレッドの投稿番号（カンマ区切り）")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せずに確認のみ")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"🐦 X自動投稿 - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   Window: {args.window}min | DryRun: {args.dry_run}")
    print(f"   ReadRange: A:{SHEET_READ_END_COL} (max COL idx={_MAX_COL_IDX})")
    print()

    # ── 起動時バリデーション ──
    col_constants = {k: v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int)}
    for name, idx in col_constants.items():
        if idx > _MAX_COL_IDX:
            print(f"❌ FATAL: {name}={idx} が読み取り範囲 (max={_MAX_COL_IDX}) を超えています")
            sys.exit(1)

    if not X_SPREADSHEET_ID or "PLACEHOLDER" in X_SPREADSHEET_ID:
        print("❌ FATAL: X_SPREADSHEET_ID が未設定です")
        print("   スプレッドシートを作成し、IDを環境変数またはスクリプトに設定してください")
        sys.exit(1)

    # サービス初期化
    service = get_sheets_service()
    oauth = get_x_oauth_session()

    # 日次投稿数チェック
    if not args.dry_run and not args.force:
        today_count = count_posts_today(service, now)
        print(f"📊 本日の投稿数: {today_count}/{DAILY_POST_LIMIT}")
        if today_count >= DAILY_POST_LIMIT:
            print("❌ 日次投稿上限に到達")
            return

    # 強制投稿
    if args.force:
        post_info = find_post_by_num(service, args.force)
        if not post_info:
            print(f"❌ 投稿 {args.force} が見つかりません")
            return

        thread_nums = None
        if args.thread:
            thread_nums = [n.strip() for n in args.thread.split(",")]

        execute_post(service, oauth, post_info,
                     dry_run=args.dry_run, thread_nums=thread_nums)
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

        success = execute_post(service, oauth, post_info,
                               dry_run=args.dry_run)
        if success:
            posted += 1
            if posted < len(ready):
                time.sleep(POST_GAP_SECONDS)

    print()
    print(f"🎉 完了: {posted}/{len(ready)}件を投稿")


if __name__ == "__main__":
    main()
