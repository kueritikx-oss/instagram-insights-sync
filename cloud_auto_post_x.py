#!/usr/bin/env python3
"""
X (旧Twitter) 自動投稿スクリプト — twikit版 (cloud_auto_post_x.py)

公式API（従量課金）を使わず、twikit（内部GraphQL API）で無料投稿。
スプレッドシート連携・スケジュール判定・リトライロジックは旧版を維持。

認証:
  - X: twikit (セッションCookie)。TWITTER_COOKIES env var (JSON)
  - Google Sheets: OAuth 2.0 (GOOGLE_TOKEN_JSON)

Usage:
    python3 cloud_auto_post_x.py --window 45
    python3 cloud_auto_post_x.py --force X-001
    python3 cloud_auto_post_x.py --dry-run
"""

# ── twikit v2.3.3 モンキーパッチ (Issue #408) ──────────────────────
# ClientTransaction の正規表現が壊れているため、import前にパッチ適用
import re as _re
_tx_mod = __import__('twikit.x_client_transaction.transaction', fromlist=['ClientTransaction'])
_tx_mod.ON_DEMAND_FILE_REGEX = _re.compile(
    r""",(\d+):["']ondemand\.s["']""", flags=(_re.VERBOSE | _re.MULTILINE))
_tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
_tx_mod.INDICES_REGEX = _re.compile(r"\[(\d+)\],\s*16")

async def _patched_get_indices(self, home_page_response, session, headers):
    key_byte_indices = []
    response = self.validate_response(home_page_response) or self.home_page_response
    response_str = str(response)
    on_demand_file = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_str)
    if on_demand_file:
        idx = on_demand_file.group(1)
        hash_regex = _re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(idx))
        hash_match = hash_regex.search(response_str)
        if hash_match:
            url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_match.group(1)}a.js"
            resp = await session.request(method="GET", url=url, headers=headers)
            for item in _tx_mod.INDICES_REGEX.finditer(str(resp.text)):
                key_byte_indices.append(item.group(1))
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]

_tx_mod.ClientTransaction.get_indices = _patched_get_indices
# ── パッチ終了 ──────────────────────────────────────────────────────

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from twikit import Client as TwikitClient
from twikit.errors import (
    TooManyRequests, Unauthorized, Forbidden,
    TwitterException, DuplicateTweet,
)
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 定数 ─────────────────────────────────────────────────────────────

# スプレッドシート
_DEFAULT_X_SPREADSHEET_ID = "1rHnDoMHUK_K0_f7MLxHltiU6Y2ATsz3ztKwdf2Zg8Hc"
_env_id = os.environ.get("X_SPREADSHEET_ID", "")
X_SPREADSHEET_ID = _env_id if (_env_id and "PLACEHOLDER" not in _env_id) else _DEFAULT_X_SPREADSHEET_ID
X_SHEET_NAME = "X投稿毎データ"
DATA_START_ROW = 4

# 列インデックス（0-based）
COL_DATE = 0
COL_POST_NUM = 2
COL_TIME = 3
COL_HOOK = 4
COL_CTA_TYPE = 5
COL_FORMAT = 6
COL_URL = 7
COL_INTENT = 8
COL_TYPE = 9
COL_NOTES = 10
COL_BODY = 11
COL_STATUS = 12
COL_IMAGE_URLS = 13
COL_TWEET_ID = 14
COL_ERROR = 15
COL_RETRY = 16
COL_LAST_ATTEMPT = 17

# 読み取り範囲の自動算出
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
DAILY_POST_LIMIT = int(os.environ.get("X_DAILY_LIMIT", "10"))
MAX_TWEET_CHARS = 280
MAX_IMAGES_PER_TWEET = 4
JST = timezone(timedelta(hours=9))

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"

# twikit Cookie
LOCAL_COOKIES_FILE = GOOGLE_AUTH_DIR / "x_twikit_cookies.json"


# ── X文字数カウント ──────────────────────────────────────────────────

def count_tweet_chars(text: str) -> int:
    url_pattern = re.compile(r'https?://\S+')
    urls = url_pattern.findall(text)
    text_no_urls = url_pattern.sub('', text)
    count = 0
    for char in text_no_urls:
        cp = ord(char)
        if (0x3000 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or
            0x20000 <= cp <= 0x2FFFF or 0x1F600 <= cp <= 0x1F9FF or
            0x1F300 <= cp <= 0x1F5FF or 0xFE00 <= cp <= 0xFE0F or
            0x200D == cp):
            count += 2
        else:
            count += 1
    count += len(urls) * 23
    return count


# ── Google Sheets ─────────────────────────────────────────────────────

def get_sheets_service():
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


def read_sheet_data(service, range_str):
    return service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID, range=range_str,
    ).execute().get("values", [])


def update_cell(service, row, col, value):
    col_letter = _col_idx_to_letter(col)
    service.spreadsheets().values().update(
        spreadsheetId=X_SPREADSHEET_ID,
        range=f"{X_SHEET_NAME}!{col_letter}{row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def batch_update_cells(service, updates):
    data = []
    for row, col, value in updates:
        data.append({
            "range": f"{X_SHEET_NAME}!{_col_idx_to_letter(col)}{row}",
            "values": [[value]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=X_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def get_col_value(row_data, col_idx):
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


# ── twikit 認証 ──────────────────────────────────────────────────────

async def get_twikit_client() -> TwikitClient:
    """Cookie認証でtwikit Clientを初期化。
    CI: TWITTER_COOKIES env var (JSON string)
    ローカル: x_twikit_cookies.json
    """
    client = TwikitClient('ja')

    # 1. 環境変数（GitHub Actions）
    cookies_env = os.environ.get("TWITTER_COOKIES")
    if cookies_env:
        cookies = json.loads(cookies_env)
        client.set_cookies(cookies)
        print("   認証: TWITTER_COOKIES env var")
        return client

    # 2. ローカルファイル
    if LOCAL_COOKIES_FILE.exists():
        client.load_cookies(str(LOCAL_COOKIES_FILE))
        print(f"   認証: {LOCAL_COOKIES_FILE.name}")
        return client

    print("❌ FATAL: Twitter Cookieが見つかりません")
    print("   ローカルで setup_x_twikit_cookies.py を実行してCookieを生成してください")
    sys.exit(1)


def save_cookies(client: TwikitClient):
    """Cookie更新を保存（ct0はリクエストごとに変わる）"""
    cookies = client.get_cookies()

    # ローカル保存
    with open(LOCAL_COOKIES_FILE, 'w') as f:
        json.dump(cookies, f)

    # GitHub Actions向け: 環境変数が設定されていれば更新後のCookieを出力
    if os.environ.get("TWITTER_COOKIES"):
        # GitHub Actionsのstep outputsでは使えないが、ログで確認可能
        print(f"   Cookie更新済み (ct0={cookies.get('ct0', '?')[:8]}...)")


# ── twikit 投稿 ──────────────────────────────────────────────────────

async def download_image_bytes(url: str) -> bytes:
    """URLから画像をダウンロード"""
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.content


async def twikit_upload_media(client: TwikitClient, image_url: str) -> str:
    """画像URLからtwikit経由でアップロード。media_idを返す"""
    image_bytes = await download_image_bytes(image_url)
    media_id = await client.upload_media(image_bytes)
    return media_id


async def twikit_post_tweet(client: TwikitClient, text: str,
                            media_ids=None, reply_to=None):
    """ツイートを投稿。226エラーは最大3回リトライ（2-5秒間隔）"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            tweet = await client.create_tweet(
                text=text,
                media_ids=media_ids,
                reply_to=reply_to,
            )
            return tweet
        except Exception as e:
            if "226" in str(e) and attempt < max_retries - 1:
                delay = 3 + attempt * 2  # 3s, 5s
                print(f"  ⚠️ 226検知, リトライ {attempt+1}/{max_retries} ({delay}s後)...")
                await asyncio.sleep(delay)
                continue
            raise


# ── スケジュール判定 ──────────────────────────────────────────────────

def parse_schedule_time(date_str, time_str):
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


def is_within_window(scheduled, now, window_minutes):
    diff = (now - scheduled).total_seconds() / 60
    return 0 <= diff <= window_minutes


# ── メイン処理 ────────────────────────────────────────────────────────

def find_ready_posts(service, now, window_minutes):
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
                "row": actual_row, "data": row,
                "scheduled": scheduled,
                "post_num": get_col_value(row, COL_POST_NUM),
            })
    ready.sort(key=lambda x: x["scheduled"])
    return ready


def find_post_by_num(service, post_num):
    rows = read_sheet_data(service, f"{X_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
    for i, row in enumerate(rows):
        if get_col_value(row, COL_POST_NUM) == post_num:
            return {"row": DATA_START_ROW + i, "data": row, "post_num": post_num}
    return None


def count_posts_today(service, now):
    rows = read_sheet_data(service, f"{X_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
    today_str = now.strftime("%Y-%m-%d")
    return sum(1 for r in rows if get_col_value(r, COL_STATUS) == "posted"
               and get_col_value(r, COL_LAST_ATTEMPT).startswith(today_str))


async def execute_post(service, client: TwikitClient, post_info,
                       dry_run=False) -> bool:
    """1投稿を実行"""
    row = post_info["row"]
    data = post_info["data"]
    post_num = post_info["post_num"]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

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
        while count_tweet_chars(text) > MAX_TWEET_CHARS - 3:
            text = text[:-1]
        text = text.rstrip() + "..."
        char_count = count_tweet_chars(text)

    print(f"  📝 {post_num}: {text[:60]}... ({char_count}字)")

    if dry_run:
        print(f"  ✅ [DRY RUN] スキップ")
        return True

    # ステータス → posting
    batch_update_cells(service, [
        (row, COL_STATUS, "posting"),
        (row, COL_LAST_ATTEMPT, now_str),
    ])

    try:
        # 画像アップロード
        media_ids = None
        if image_urls_json:
            try:
                image_urls = json.loads(image_urls_json)
            except json.JSONDecodeError:
                image_urls = [image_urls_json] if image_urls_json.startswith("http") else None

            if image_urls:
                media_ids = []
                for url in image_urls[:MAX_IMAGES_PER_TWEET]:
                    mid = await twikit_upload_media(client, url)
                    media_ids.append(mid)
                    await asyncio.sleep(1)

        # ツイート投稿
        tweet = await twikit_post_tweet(client, text, media_ids=media_ids)
        tweet_id = tweet.id
        tweet_url = f"https://x.com/tackey_clear/status/{tweet_id}"

        # 成功更新
        batch_update_cells(service, [
            (row, COL_STATUS, "posted"),
            (row, COL_TWEET_ID, str(tweet_id)),
            (row, COL_LAST_ATTEMPT, now_str),
            (row, COL_ERROR, ""),
        ])
        update_cell(service, row, COL_URL, tweet_url)

        print(f"  ✅ 投稿成功: {post_num} → {tweet_url}")
        return True

    except (TooManyRequests, Forbidden) as e:
        # レート制限 / Cloudflare → transient、readyに戻す
        error_msg = str(e)[:300]
        batch_update_cells(service, [
            (row, COL_STATUS, "ready"),
            (row, COL_ERROR, f"[transient] {error_msg[:200]}"),
            (row, COL_LAST_ATTEMPT, now_str),
        ])
        print(f"  ⚠️ transient: {post_num} → ready")
        print(f"     {error_msg[:100]}")
        return False

    except Unauthorized:
        # Cookie期限切れ → 全停止（再ログイン必要）
        batch_update_cells(service, [
            (row, COL_STATUS, "ready"),
            (row, COL_ERROR, "[auth] Cookie expired. Re-login required."),
            (row, COL_LAST_ATTEMPT, now_str),
        ])
        print(f"  ❌ Cookie期限切れ: 再ログインが必要")
        sys.exit(2)  # 特別exit codeで通知

    except DuplicateTweet:
        # 重複 → skip
        batch_update_cells(service, [
            (row, COL_STATUS, "skip"),
            (row, COL_ERROR, "Duplicate tweet"),
            (row, COL_LAST_ATTEMPT, now_str),
        ])
        print(f"  ⚠️ 重複ツイート: {post_num} → skip")
        return False

    except Exception as e:
        error_msg = str(e)[:500]

        # 汎用transientチェック
        is_transient = any(kw in error_msg for kw in (
            "Rate limit", "Too Many", "429", "Connection", "Timeout",
            "226", "looks like it might be automated",
        ))
        if is_transient:
            batch_update_cells(service, [
                (row, COL_STATUS, "ready"),
                (row, COL_ERROR, f"[transient] {error_msg[:200]}"),
                (row, COL_LAST_ATTEMPT, now_str),
            ])
            print(f"  ⚠️ transient: {post_num} → ready")
            return False

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
        print(f"  ❌ 失敗: {post_num} ({new_status})")
        print(f"     {error_msg[:100]}")
        return False


async def async_main(args):
    now = datetime.now(JST)
    print(f"🐦 X自動投稿 (twikit) - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   Window: {args.window}min | DryRun: {args.dry_run}")
    print()

    # バリデーション
    if not X_SPREADSHEET_ID or "PLACEHOLDER" in X_SPREADSHEET_ID:
        print("❌ FATAL: X_SPREADSHEET_ID 未設定")
        sys.exit(1)

    # サービス初期化（Sheetsは常に必要。X認証は投稿対象がある時だけ行う）
    service = get_sheets_service()

    # 日次投稿数チェック
    if not args.dry_run and not args.force:
        today_count = count_posts_today(service, now)
        print(f"📊 本日の投稿数: {today_count}/{DAILY_POST_LIMIT}")
        if today_count >= DAILY_POST_LIMIT:
            print("❌ 日次上限到達")
            return

    # 強制投稿
    if args.force:
        post_info = find_post_by_num(service, args.force)
        if not post_info:
            print(f"❌ {args.force} が見つかりません")
            return
        client = None if args.dry_run else await get_twikit_client()
        await execute_post(service, client, post_info, dry_run=args.dry_run)
        if client:
            save_cookies(client)
        return

    # スケジュール投稿
    ready = find_ready_posts(service, now, args.window)
    if not ready:
        print("📭 投稿可能な行なし")
        return

    print(f"📋 {len(ready)}件の投稿を検出")
    client = None if args.dry_run else await get_twikit_client()
    posted = 0

    for post_info in ready:
        if posted >= DAILY_POST_LIMIT:
            print(f"⚠️ 日次制限 ({DAILY_POST_LIMIT}) に到達")
            break
        success = await execute_post(service, client, post_info,
                                     dry_run=args.dry_run)
        if success:
            posted += 1
            if posted < len(ready):
                await asyncio.sleep(POST_GAP_SECONDS)

    # Cookie保存（ct0ローテーション対応）
    if client:
        save_cookies(client)

    print()
    print(f"🎉 完了: {posted}/{len(ready)}件を投稿")


def main():
    parser = argparse.ArgumentParser(description="X自動投稿 (twikit)")
    parser.add_argument("--window", type=int, default=45)
    parser.add_argument("--force", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
