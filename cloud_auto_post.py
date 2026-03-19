#!/usr/bin/env python3
"""
cloud_auto_post.py — GitHub Actions用 Instagram自動投稿スクリプト

スプレッドシートから投稿スケジュールを読み、時間が来た投稿を
Instagram Graph APIでカルーセル投稿する。

Architecture:
    Buffer/Later/Hootsuiteと同等の設計:
    1. Content Publishing Limit事前チェック
    2. コンテナ作成 → ステータスポーリング（指数バックオフ）
    3. 公開 → シート更新
    4. リトライ3回 + Dead Letter
    5. 冪等性保証（media_id既存ならスキップ）

Usage:
    python3 cloud_auto_post.py                    # 通常実行（15分ウィンドウ）
    python3 cloud_auto_post.py --window 30        # 30分ウィンドウ
    python3 cloud_auto_post.py --force 2840       # 特定投稿を強制投稿
    python3 cloud_auto_post.py --dry-run          # 何をするか表示のみ
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config (GitHub Actions: env vars / Local: config file)
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))
GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
SHEET_NAME = "Instagram投稿毎データ"

# Column indices (0-based)
COL_DATE = 0          # A
COL_POST_NUM = 2      # C
COL_TIME = 3          # D
COL_TITLE = 4         # E
COL_URL = 7           # H
COL_CAPTION = 11      # L
COL_STATUS = 92       # CO: 投稿ステータス
COL_IMAGE_URLS = 93   # CP: 画像URLs (JSON array)
COL_MEDIA_ID = 94     # CQ: メディアID
COL_ERROR = 95        # CR: 投稿エラー
COL_RETRY = 96        # CS: リトライ回数
COL_LAST_ATTEMPT = 97 # CT: 最終投稿試行

MAX_RETRIES = 3
CONTAINER_POLL_MAX = 60  # seconds
CONTAINER_POLL_INTERVAL = [1, 2, 4, 8, 8, 8, 8, 8]  # exponential backoff
POST_GAP_SECONDS = 60  # minimum gap between consecutive posts
DAILY_POST_LIMIT = 3   # max posts per day (self-imposed)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_instagram_config():
    """Get Instagram credentials from env vars or config file."""
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_user_id = os.environ.get("INSTAGRAM_IG_USER_ID")

    if not access_token:
        # Local fallback
        config_path = os.environ.get("INSTAGRAM_CONFIG_PATH",
            os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/"
                               "MacDocuments/01_仕事/事業 Cursor/タッキー/"
                               "02_SNS集客/instagram-auto-post/instagram_insights_config.json"))
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
            access_token = config.get("access_token")
            ig_user_id = config.get("ig_user_id")

    if not access_token or not ig_user_id:
        raise RuntimeError("Instagram credentials not found. Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_IG_USER_ID")

    return access_token, ig_user_id


def get_sheets_service():
    """Get Google Sheets API service."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    # Try env vars first (GitHub Actions)
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
    else:
        # Local fallback
        token_path = os.environ.get("GOOGLE_TOKEN_PATH",
            os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/"
                               "MacDocuments/01_仕事/事業 Cursor/タッキー/"
                               "02_SNS集客/instagram-auto-post/token.json"))
        with open(token_path) as f:
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


# ---------------------------------------------------------------------------
# Spreadsheet helpers
# ---------------------------------------------------------------------------
def col_letter(idx):
    result = ""
    n = idx
    while n >= 0:
        result = chr(n % 26 + 65) + result
        n = n // 26 - 1
    return result


def read_all_rows(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_NAME}!A4:CT220",
    ).execute()
    return result.get("values", [])


def write_cells(service, updates):
    """Batch write to multiple cells."""
    if not updates:
        return
    data = [{"range": f"{SHEET_NAME}!{cell}", "values": [[val]]}
            for cell, val in updates]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


def update_post_status(service, row_num, status, media_id="", error="",
                       retry_count=0, url=""):
    """Update all auto-posting columns for a row."""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    updates = [
        (f"{col_letter(COL_STATUS)}{row_num}", status),
        (f"{col_letter(COL_ERROR)}{row_num}", error),
        (f"{col_letter(COL_RETRY)}{row_num}", str(retry_count)),
        (f"{col_letter(COL_LAST_ATTEMPT)}{row_num}", now),
    ]
    if media_id:
        updates.append((f"{col_letter(COL_MEDIA_ID)}{row_num}", media_id))
    if url:
        updates.append((f"{col_letter(COL_URL)}{row_num}", url))
    write_cells(service, updates)


# ---------------------------------------------------------------------------
# Instagram Graph API
# ---------------------------------------------------------------------------
def check_publishing_limit(access_token, ig_user_id):
    """Check content publishing limit before posting. Returns remaining quota."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/content_publishing_limit"
    resp = requests.get(url, params={
        "fields": "config,quota_usage",
        "access_token": access_token,
    }, timeout=15)

    if resp.status_code != 200:
        print(f"  WARNING: Could not check publishing limit: {resp.text[:200]}")
        return 50  # Assume OK if can't check

    data = resp.json().get("data", [{}])[0]
    config = data.get("config", {})
    usage = data.get("quota_usage", 0)
    total = config.get("quota_total", 50)
    remaining = total - usage
    print(f"  Publishing limit: {usage}/{total} used, {remaining} remaining")
    return remaining


def create_child_container(access_token, ig_user_id, image_url):
    """Create a single child container for carousel. Returns container ID."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
    resp = requests.post(url, data={
        "image_url": image_url,
        "is_carousel_item": "true",
        "access_token": access_token,
    }, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"Child container creation failed: {resp.text[:300]}")

    return resp.json()["id"]


def create_carousel_container(access_token, ig_user_id, children_ids, caption):
    """Create the carousel container. Returns container ID."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
    resp = requests.post(url, data={
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "caption": caption,
        "access_token": access_token,
    }, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"Carousel container creation failed: {resp.text[:300]}")

    return resp.json()["id"]


def poll_container_status(access_token, container_id):
    """Poll container status until FINISHED or timeout."""
    for wait in CONTAINER_POLL_INTERVAL:
        time.sleep(wait)
        url = f"{GRAPH_API_BASE}/{container_id}"
        resp = requests.get(url, params={
            "fields": "status_code",
            "access_token": access_token,
        }, timeout=15)

        if resp.status_code != 200:
            continue

        status = resp.json().get("status_code")
        if status == "FINISHED":
            return True
        elif status == "ERROR":
            raise RuntimeError(f"Container {container_id} has ERROR status")
        elif status == "EXPIRED":
            raise RuntimeError(f"Container {container_id} EXPIRED (not published within 24h)")
        # IN_PROGRESS: continue polling

    raise RuntimeError(f"Container {container_id} polling timed out (still IN_PROGRESS)")


def publish_container(access_token, ig_user_id, container_id):
    """Publish the container. Returns media ID."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
    resp = requests.post(url, data={
        "creation_id": container_id,
        "access_token": access_token,
    }, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"Publish failed: {resp.text[:300]}")

    return resp.json()["id"]


def get_media_permalink(access_token, media_id):
    """Get the Instagram URL for a published post."""
    url = f"{GRAPH_API_BASE}/{media_id}"
    resp = requests.get(url, params={
        "fields": "permalink",
        "access_token": access_token,
    }, timeout=15)

    if resp.status_code == 200:
        return resp.json().get("permalink", "")
    return ""


# ---------------------------------------------------------------------------
# Post a single carousel
# ---------------------------------------------------------------------------
def post_carousel(access_token, ig_user_id, image_urls, caption):
    """
    Full carousel posting pipeline:
    1. Create child containers
    2. Create carousel container
    3. Poll status
    4. Publish
    Returns (media_id, permalink)
    """
    print(f"  Creating {len(image_urls)} child containers...")
    children = []
    for i, img_url in enumerate(image_urls):
        child_id = create_child_container(access_token, ig_user_id, img_url)
        children.append(child_id)
        print(f"    [{i+1}/{len(image_urls)}] {child_id}")
        time.sleep(3)  # Rate limit: 1 per 3 seconds

    print(f"  Creating carousel container...")
    carousel_id = create_carousel_container(access_token, ig_user_id, children, caption)
    print(f"    Container: {carousel_id}")

    print(f"  Polling container status...")
    poll_container_status(access_token, carousel_id)
    print(f"    Status: FINISHED")

    print(f"  Publishing...")
    media_id = publish_container(access_token, ig_user_id, carousel_id)
    print(f"    Media ID: {media_id}")

    permalink = get_media_permalink(access_token, media_id)
    print(f"    URL: {permalink}")

    return media_id, permalink


# ---------------------------------------------------------------------------
# Schedule logic
# ---------------------------------------------------------------------------
def parse_schedule_time(date_str, time_str):
    """Parse date + time from spreadsheet into JST datetime.

    Handles multiple date formats:
    - YYYY/MM/DD, YYYY-MM-DD (full)
    - M/D, MM/DD (no year → assume current year)
    - M/D/YYYY (US-style)

    Handles multiple time formats:
    - HH:MM, H:MM
    - HHMM (no colon)
    - HH：MM (full-width colon)
    """
    if not date_str or not time_str:
        return None

    date_str = date_str.strip()
    time_str = time_str.strip().replace("：", ":")

    # Normalize time
    if ":" not in time_str and len(time_str) >= 3:
        time_str = time_str[:-2] + ":" + time_str[-2:]

    # Parse time
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None

    # Parse date
    date_str = date_str.replace("-", "/")
    parts = date_str.split("/")

    try:
        if len(parts) == 3 and len(parts[0]) == 4:
            # YYYY/MM/DD
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 3 and len(parts[2]) == 4:
            # MM/DD/YYYY
            month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            # M/D (no year) → assume current year
            month, day = int(parts[0]), int(parts[1])
            year = datetime.now(JST).year
        else:
            return None

        dt = datetime(year, month, day, hour, minute, tzinfo=JST)
        return dt
    except (ValueError, IndexError):
        return None


def find_due_posts(rows, window_minutes=20, force_post_num=None):
    """Find posts that are due for posting within the time window."""
    now = datetime.now(JST)
    due = []

    for i, row in enumerate(rows):
        row_num = i + 4
        post_num = row[COL_POST_NUM].strip() if len(row) > COL_POST_NUM else ""
        if not post_num:
            continue

        # Force mode
        if force_post_num and post_num != str(force_post_num):
            continue

        status = row[COL_STATUS] if len(row) > COL_STATUS else ""
        image_urls_str = row[COL_IMAGE_URLS] if len(row) > COL_IMAGE_URLS else ""
        media_id = row[COL_MEDIA_ID] if len(row) > COL_MEDIA_ID else ""
        caption = row[COL_CAPTION] if len(row) > COL_CAPTION else ""
        retry_count = int(row[COL_RETRY]) if len(row) > COL_RETRY and row[COL_RETRY].strip() else 0

        # Skip: already posted or permanently failed
        if status == "posted" or media_id:
            continue
        if status == "failed" and retry_count >= MAX_RETRIES:
            continue

        # Must have image URLs and caption
        if not image_urls_str or not caption:
            continue

        # Must be status "ready" or "retry"
        if status not in ("ready", "retry") and not force_post_num:
            continue

        # Parse schedule time
        date_str = row[COL_DATE] if len(row) > COL_DATE else ""
        time_str = row[COL_TIME] if len(row) > COL_TIME else ""
        scheduled = parse_schedule_time(date_str, time_str)

        if force_post_num:
            # Force: ignore schedule
            pass
        elif scheduled is None:
            continue
        elif not (scheduled <= now <= scheduled + timedelta(minutes=window_minutes)):
            # Not within the posting window
            # But also catch overdue posts (up to 2 hours late)
            if not (now - timedelta(hours=2) <= scheduled <= now):
                continue

        try:
            image_urls = json.loads(image_urls_str)
        except json.JSONDecodeError:
            continue

        due.append({
            "row_num": row_num,
            "post_num": post_num,
            "caption": caption,
            "image_urls": image_urls,
            "status": status,
            "retry_count": retry_count,
            "scheduled": scheduled,
        })

    return due


def count_posts_today(rows):
    """Count how many posts were already made today."""
    now = datetime.now(JST)
    today_md = f"{now.month}/{now.day}"         # "3/19"
    today_full = now.strftime("%Y-%m-%d")        # "2026-03-19"
    today_slash = now.strftime("%Y/%m/%d")       # "2026/03/19"
    count = 0
    for row in rows:
        status = row[COL_STATUS] if len(row) > COL_STATUS else ""
        if status != "posted":
            continue
        date = (row[COL_DATE] if len(row) > COL_DATE else "").strip()
        if date in (today_md, today_full, today_slash):
            count += 1
        elif f"/{now.month}/{now.day}" in date or f"-{now.month:02d}-{now.day:02d}" in date:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Cloud Instagram Auto-Poster")
    parser.add_argument("--window", type=int, default=20,
                        help="Posting window in minutes (default: 20)")
    parser.add_argument("--force", type=int,
                        help="Force post a specific post number (ignore schedule)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be posted without actually posting")
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"Instagram Cloud Auto-Poster")
    print(f"Time: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}")
    print(f"Window: {args.window} minutes")
    print(f"{'='*60}")

    # Initialize
    access_token, ig_user_id = get_instagram_config()
    service = get_sheets_service()

    # Read all rows
    rows = read_all_rows(service)
    print(f"Read {len(rows)} rows from sheet")

    # Check daily limit
    today_count = count_posts_today(rows)
    print(f"Posts today: {today_count}/{DAILY_POST_LIMIT}")
    if today_count >= DAILY_POST_LIMIT and not args.force:
        print("Daily post limit reached. Exiting.")
        return

    # Find due posts
    due = find_due_posts(rows, args.window, args.force)
    if not due:
        print("No posts due. Exiting.")
        return

    print(f"\nFound {len(due)} post(s) due:")
    for p in due:
        sched = p['scheduled'].strftime('%Y-%m-%d %H:%M') if p['scheduled'] else 'N/A'
        print(f"  Post {p['post_num']} | Scheduled: {sched} | "
              f"Images: {len(p['image_urls'])} | Retry: {p['retry_count']}")

    if args.dry_run:
        print("\n[DRY RUN] Would post the above. Exiting.")
        return

    # Check publishing limit
    remaining = check_publishing_limit(access_token, ig_user_id)
    if remaining <= 0:
        print("Instagram publishing limit reached. Exiting.")
        return

    # Post each due item
    for p in due:
        if today_count >= DAILY_POST_LIMIT:
            print(f"\nDaily limit ({DAILY_POST_LIMIT}) reached. Stopping.")
            break

        print(f"\n{'─'*50}")
        print(f"Posting: {p['post_num']}")
        print(f"{'─'*50}")

        # Mark as posting
        update_post_status(service, p["row_num"], "posting",
                           retry_count=p["retry_count"])

        try:
            media_id, permalink = post_carousel(
                access_token, ig_user_id,
                p["image_urls"], p["caption"]
            )

            # Success
            update_post_status(
                service, p["row_num"], "posted",
                media_id=media_id, url=permalink,
                retry_count=p["retry_count"]
            )
            today_count += 1
            print(f"  ✓ Posted successfully!")

        except Exception as e:
            error_msg = str(e)[:500]
            new_retry = p["retry_count"] + 1
            new_status = "retry" if new_retry < MAX_RETRIES else "failed"

            update_post_status(
                service, p["row_num"], new_status,
                error=error_msg, retry_count=new_retry
            )
            print(f"  ✗ Failed: {error_msg}")
            print(f"  Retry {new_retry}/{MAX_RETRIES}. Status: {new_status}")

        # Gap between posts
        if len(due) > 1:
            print(f"  Waiting {POST_GAP_SECONDS}s before next post...")
            time.sleep(POST_GAP_SECONDS)

    print(f"\n{'='*60}")
    print(f"Done. {today_count} total posts today.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
