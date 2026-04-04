#!/usr/bin/env python3
"""
Threads自動投稿スクリプト (cloud_auto_post_threads.py)

Instagram自動投稿 (cloud_auto_post.py) のアーキテクチャをベースに、
Threads API (graph.threads.net) 用に構築。

対応フォーマット:
  - テキストのみ（auto_publish_text=true で即時投稿可能）
  - 画像付きテキスト
  - カルーセル（2-20アイテム）
  - スレッドチェーン（reply_to_id で自己リプライ連鎖）

スプレッドシート: Threads投稿毎データ_2026
  AE列(30): 投稿ステータス (draft/scheduled/ready/posting/published/retry/failed)
  AF列(31): スケジュール日時
  AG列(32): Threads Media ID
  AH列(33): 画像URLs (JSON array)
  AI列(34): エラー
  AJ列(35): 最終投稿試行

Usage:
    # スケジュール通りに投稿（GitHub Actions / cron用）
    python3 utils/cloud_auto_post_threads.py --window 45

    # 特定投稿を強制投稿
    python3 utils/cloud_auto_post_threads.py --force T-001

    # ドライラン（投稿せずに確認）
    python3 utils/cloud_auto_post_threads.py --dry-run

    # スレッドチェーンを投稿
    python3 utils/cloud_auto_post_threads.py --force T-010 --chain "T-010,T-011,T-012"
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 定数 ─────────────────────────────────────────────────────────────

# Threads API
THREADS_API_BASE = "https://graph.threads.net/v1.0"

# スプレッドシート
THREADS_SPREADSHEET_ID = "1hdBlZBn9s688f1ZwkTiO3suY27tJEHtXMEPkopLdBNI"
THREADS_SHEET_NAME = "Threads投稿毎データ"
DATA_START_ROW = 4  # ヘッダー3行の次

# 列インデックス（0-based）— IG投稿毎データと構造を揃えた配置
# 基本情報 (A-H) = IGと完全一致
COL_DATE = 0        # A: 日付
COL_POST_NUM = 2    # C: 番号（T-001形式）※IGと同じ位置
COL_TIME = 3        # D: 時刻
COL_HOOK = 4        # E: ファイル名/タイトル/フック
COL_CTA_TYPE = 5    # F: 投稿種別（CTA型）
COL_FORMAT = 6      # G: 形式（認知/価値提供/誘導）
COL_URL = 7         # H: URL（Threads permalink）
# 企画・設計 (I-P) = IGと完全一致
COL_INTENT = 8      # I: 投稿の意図
COL_TYPE = 9        # J: 内容（投稿タイプ: テキスト/画像/カルーセル/チェーン）
COL_NOTES = 10      # K: 備考（IG元投稿番号/変換方法）
COL_BODY = 11       # L: キャプション（本文, 500文字以内）
# Threadsメトリクス (W-AH)
COL_VIEWS = 22      # W: views
# 分類タグ (AI-AO)
COL_TOPIC_TAG = 40  # AO: トピックタグ
# 自動投稿 (AP-AU) = IGのCO-CTに対応
COL_STATUS = 41     # AP: 投稿ステータス
COL_IMAGE_URL = 42  # AQ: 画像URL（単一画像 or JSON配列兼用）
COL_IMAGE_URLS = 42 # AQ: 画像URLs (JSON) — COL_IMAGE_URLと同一列
COL_MEDIA_ID = 43   # AR: メディアID
COL_ERROR = 44      # AS: 投稿エラー
COL_RETRY = 45      # AT: リトライ回数
COL_LAST_ATTEMPT = 46  # AU: 最終投稿試行

# ── 読み取り範囲の自動算出（列定数から導出、ハードコード禁止） ──────────
# COL_* の最大値から必要な列範囲を自動計算。
# 新しい列を追加しても読み取り範囲不足が起きない。
def _col_idx_to_letter(idx: int) -> str:
    """0-based列インデックスをExcel列文字に変換（0=A, 25=Z, 26=AA, ...）"""
    result = ""
    n = idx
    while True:
        result = chr(ord("A") + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result

_MAX_COL_IDX = max(v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int))
SHEET_READ_END_COL = _col_idx_to_letter(_MAX_COL_IDX)  # 現在: AU（46）

# 制限・設定
MAX_RETRIES = 3
CONTAINER_POLL_MAX_SEC = 120  # コンテナ処理待ち最大秒
CONTAINER_POLL_INTERVAL = 5   # ポーリング間隔（秒）
POST_GAP_SECONDS = 30         # 投稿間のクールダウン
DAILY_POST_LIMIT = int(os.environ.get("THREADS_DAILY_LIMIT", "5"))
CHAIN_REPLY_DELAY = 8         # スレッドチェーン間の遅延（秒）

# タイムゾーン
JST = timezone(timedelta(hours=9))

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"

# ── 最適投稿タイミング（リサーチベース） ───────────────────────────────
# Buffer 2.5M投稿分析 + 日本市場調整
# ゴールデンタイム: 平日 7-9時, 12-13時, 20-22時 JST
# ベスト曜日: 火〜木
OPTIMAL_SCHEDULE = {
    "weekday_slots": ["07:30", "12:00", "20:00"],
    "weekend_slots": ["10:00", "20:00"],
    "best_days": ["火", "水", "木"],
    "good_days": ["月", "金"],
    "rest_days": ["土", "日"],  # 投稿少なめ or 会話参加のみ
    "recommended_frequency": {
        "launch_phase": "3-4/week",     # 最初の2週間
        "growth_phase": "5-7/week",     # 3-8週目
        "cruise_phase": "5/week",       # 9週目以降
        "max_per_day": 2,               # 1日2投稿まで
        "min_spacing_hours": 4,         # 投稿間最低4時間
    },
}


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
        spreadsheetId=THREADS_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    return result.get("values", [])


def update_cell(service, row: int, col: int, value: str):
    """単一セルを更新"""
    col_letter = _col_idx_to_letter(col)
    cell = f"{THREADS_SHEET_NAME}!{col_letter}{row}"
    service.spreadsheets().values().update(
        spreadsheetId=THREADS_SPREADSHEET_ID,
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
            "range": f"{THREADS_SHEET_NAME}!{col_letter}{row}",
            "values": [[value]],
        })

    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=THREADS_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def get_col_value(row_data: list, col_idx: int) -> str:
    """行データから列の値を安全に取得"""
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


# ── Threads API ──────────────────────────────────────────────────────

def get_threads_token() -> str:
    """Threadsアクセストークンを取得"""
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if token:
        return token
    # ローカルフォールバック
    token_path = GOOGLE_AUTH_DIR / "threads_token.json"
    if token_path.exists():
        with open(token_path) as f:
            data = json.load(f)
            return data.get("access_token", "")
    print("ERROR: THREADS_ACCESS_TOKEN not found")
    sys.exit(1)


def get_threads_user_id() -> str:
    """ThreadsユーザーIDを取得"""
    uid = os.environ.get("THREADS_USER_ID")
    if uid:
        return uid
    token_path = GOOGLE_AUTH_DIR / "threads_token.json"
    if token_path.exists():
        with open(token_path) as f:
            data = json.load(f)
            return data.get("user_id", "")
    print("ERROR: THREADS_USER_ID not found")
    sys.exit(1)


def check_publishing_limit(token: str, user_id: str) -> dict:
    """投稿レート制限を確認"""
    resp = requests.get(
        f"{THREADS_API_BASE}/{user_id}/threads_publishing_limit",
        params={
            "fields": "quota_usage,config",
            "access_token": token,
        },
    )
    resp.raise_for_status()
    data = resp.json().get("data", [{}])[0]
    return {
        "used": data.get("quota_usage", 0),
        "total": data.get("config", {}).get("quota_total", 250),
    }


def create_text_container(token: str, user_id: str, text: str,
                          topic_tag: str = None, reply_to_id: str = None) -> str:
    """テキストのみのコンテナを作成"""
    params = {
        "media_type": "TEXT",
        "text": text[:500],  # 500文字上限
        "access_token": token,
    }
    if topic_tag:
        params["topic_tag"] = topic_tag
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    # テキストのみ+非リプライ→auto_publish
    if not reply_to_id:
        params["auto_publish_text"] = "true"

    resp = requests.post(f"{THREADS_API_BASE}/{user_id}/threads", data=params)
    resp.raise_for_status()
    return resp.json()["id"]


def create_image_container(token: str, user_id: str, image_url: str,
                           text: str = None, topic_tag: str = None,
                           reply_to_id: str = None,
                           is_carousel_item: bool = False) -> str:
    """画像付きコンテナを作成"""
    params = {
        "media_type": "IMAGE",
        "image_url": image_url,
        "access_token": token,
    }
    if text:
        params["text"] = text[:500]
    if topic_tag:
        params["topic_tag"] = topic_tag
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    if is_carousel_item:
        params["is_carousel_item"] = "true"

    resp = requests.post(f"{THREADS_API_BASE}/{user_id}/threads", data=params)
    resp.raise_for_status()
    return resp.json()["id"]


def create_carousel_container(token: str, user_id: str,
                              children_ids: list, text: str = None,
                              topic_tag: str = None) -> str:
    """カルーセルコンテナを作成"""
    params = {
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "access_token": token,
    }
    if text:
        params["text"] = text[:500]
    if topic_tag:
        params["topic_tag"] = topic_tag

    resp = requests.post(f"{THREADS_API_BASE}/{user_id}/threads", data=params)
    resp.raise_for_status()
    return resp.json()["id"]


def poll_container_status(token: str, container_id: str) -> str:
    """コンテナのステータスをポーリング"""
    elapsed = 0
    while elapsed < CONTAINER_POLL_MAX_SEC:
        resp = requests.get(
            f"{THREADS_API_BASE}/{container_id}",
            params={
                "fields": "id,status,error_message",
                "access_token": token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")

        if status == "FINISHED":
            return "FINISHED"
        elif status == "PUBLISHED":
            return "PUBLISHED"  # auto_publish_text の場合
        elif status == "ERROR":
            error_msg = data.get("error_message", "Unknown error")
            raise RuntimeError(f"Container {container_id} ERROR: {error_msg}")
        elif status == "EXPIRED":
            raise RuntimeError(f"Container {container_id} EXPIRED")

        # IN_PROGRESS → 待機
        time.sleep(CONTAINER_POLL_INTERVAL)
        elapsed += CONTAINER_POLL_INTERVAL

    raise RuntimeError(f"Container {container_id} poll timeout ({CONTAINER_POLL_MAX_SEC}s)")


def publish_container(token: str, user_id: str, container_id: str) -> str:
    """コンテナを公開"""
    resp = requests.post(
        f"{THREADS_API_BASE}/{user_id}/threads_publish",
        data={
            "creation_id": container_id,
            "access_token": token,
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def get_thread_permalink(token: str, media_id: str) -> str:
    """投稿のパーマリンクを取得"""
    resp = requests.get(
        f"{THREADS_API_BASE}/{media_id}",
        params={
            "fields": "permalink",
            "access_token": token,
        },
    )
    resp.raise_for_status()
    return resp.json().get("permalink", "")


# ── 投稿ロジック ─────────────────────────────────────────────────────

def post_text(token: str, user_id: str, text: str,
              topic_tag: str = None) -> str:
    """テキスト投稿（auto_publish_text=trueで即時公開）"""
    container_id = create_text_container(token, user_id, text, topic_tag)
    # auto_publish_text=true なのでコンテナ作成=公開
    # ステータス確認
    status = poll_container_status(token, container_id)
    if status == "PUBLISHED":
        return container_id  # container_id = media_id
    elif status == "FINISHED":
        return publish_container(token, user_id, container_id)
    return container_id


def post_image(token: str, user_id: str, image_url: str,
               text: str = None, topic_tag: str = None) -> str:
    """画像付き投稿"""
    container_id = create_image_container(token, user_id, image_url, text, topic_tag)
    poll_container_status(token, container_id)
    return publish_container(token, user_id, container_id)


def post_carousel(token: str, user_id: str, image_urls: list,
                  text: str = None, topic_tag: str = None) -> str:
    """カルーセル投稿"""
    children_ids = []
    for url in image_urls:
        child_id = create_image_container(
            token, user_id, url, is_carousel_item=True
        )
        children_ids.append(child_id)
        time.sleep(3)  # 画像間のスロットル

    container_id = create_carousel_container(
        token, user_id, children_ids, text, topic_tag
    )
    poll_container_status(token, container_id)
    return publish_container(token, user_id, container_id)


def post_thread_chain(token: str, user_id: str,
                      posts: list, topic_tag: str = None) -> list:
    """スレッドチェーン投稿。posts = [(text, image_url_or_none), ...]
    戻り値: [(media_id, permalink), ...]"""
    results = []
    prev_media_id = None

    for i, (text, image_url) in enumerate(posts):
        is_root = (i == 0)
        tag = topic_tag if is_root else None  # タグはルート投稿のみ

        if image_url:
            # 画像付きリプライ
            container_id = create_image_container(
                token, user_id, image_url, text, tag,
                reply_to_id=prev_media_id,
            )
            poll_container_status(token, container_id)
            media_id = publish_container(token, user_id, container_id)
        else:
            # テキストリプライ
            if prev_media_id:
                # リプライ（auto_publish不可）
                container_id = create_text_container(
                    token, user_id, text, tag, reply_to_id=prev_media_id
                )
                # reply_to_id指定時はauto_publish_textが無効なのでpoll+publish
                poll_container_status(token, container_id)
                media_id = publish_container(token, user_id, container_id)
            else:
                # ルート投稿
                media_id = post_text(token, user_id, text, tag)

        permalink = get_thread_permalink(token, media_id)
        results.append((media_id, permalink))
        prev_media_id = media_id

        if i < len(posts) - 1:
            time.sleep(CHAIN_REPLY_DELAY)

    return results


# ── スケジュール判定 ──────────────────────────────────────────────────

def parse_schedule_time(date_str: str, time_str: str) -> datetime | None:
    """日付+時刻文字列をdatetimeに変換"""
    if not date_str or not time_str:
        return None

    # 日付パース
    date_str = date_str.strip()
    date_obj = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d", "%-m/%-d"):
        try:
            date_obj = datetime.strptime(date_str, fmt)
            if date_obj.year == 1900:
                date_obj = date_obj.replace(year=datetime.now(JST).year)
            break
        except ValueError:
            continue

    if not date_obj:
        # Excel serial date
        try:
            serial = float(date_str)
            date_obj = datetime(1899, 12, 30) + timedelta(days=serial)
        except (ValueError, TypeError):
            return None

    # 時刻パース
    time_str = time_str.strip()
    hour, minute = 0, 0
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        elif "." in time_str:
            # Excelの小数時刻（0.375 = 9:00）
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
    rows = read_sheet_data(service, f"{THREADS_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")
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
            # スケジュール日時がない場合はスキップ
            continue

        if is_within_window(scheduled, now, window_minutes):
            ready.append({
                "row": actual_row,
                "data": row,
                "scheduled": scheduled,
                "post_num": get_col_value(row, COL_POST_NUM),
            })

    # スケジュール順にソート
    ready.sort(key=lambda x: x["scheduled"])
    return ready


def find_post_by_num(service, post_num: str) -> dict | None:
    """投稿番号で行を検索"""
    rows = read_sheet_data(service, f"{THREADS_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500")

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        if get_col_value(row, COL_POST_NUM) == post_num:
            return {
                "row": actual_row,
                "data": row,
                "post_num": post_num,
            }
    return None


def execute_post(service, token: str, user_id: str, post_info: dict,
                 dry_run: bool = False, chain_nums: list = None) -> bool:
    """1投稿を実行"""
    row = post_info["row"]
    data = post_info["data"]
    post_num = post_info["post_num"]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    post_type = get_col_value(data, COL_TYPE)
    body = get_col_value(data, COL_BODY)
    hook = get_col_value(data, COL_HOOK)
    topic_tag = get_col_value(data, COL_TOPIC_TAG)
    image_url = get_col_value(data, COL_IMAGE_URL)
    image_urls_json = get_col_value(data, COL_IMAGE_URLS)

    # 本文の組み立て（フック + 本文）
    text = body if body else hook
    if not text:
        print(f"  SKIP {post_num}: 本文が空")
        return False

    print(f"  📝 {post_num}: {text[:50]}...")
    print(f"     タイプ: {post_type}, タグ: {topic_tag or 'なし'}")

    if dry_run:
        print(f"  ✅ [DRY RUN] 投稿をスキップ")
        return True

    # ステータスを「posting」に更新
    batch_update_cells(service, [
        (row, COL_STATUS, "posting"),
        (row, COL_LAST_ATTEMPT, now_str),
    ])

    try:
        media_id = None
        permalink = ""

        # スレッドチェーン
        if chain_nums:
            chain_posts = []
            for cn in chain_nums:
                cp = find_post_by_num(service, cn)
                if cp:
                    cp_body = get_col_value(cp["data"], COL_BODY)
                    cp_hook = get_col_value(cp["data"], COL_HOOK)
                    cp_img = get_col_value(cp["data"], COL_IMAGE_URL)
                    chain_posts.append((cp_body or cp_hook, cp_img or None))
            if chain_posts:
                results = post_thread_chain(token, user_id, chain_posts, topic_tag)
                # ルート投稿のIDとpermalink
                media_id, permalink = results[0]
                # 各チェーン投稿のステータスも更新
                for j, cn in enumerate(chain_nums):
                    cp = find_post_by_num(service, cn)
                    if cp and j < len(results):
                        mid, plink = results[j]
                        batch_update_cells(service, [
                            (cp["row"], COL_STATUS, "published"),
                            (cp["row"], COL_MEDIA_ID, str(mid)),
                        ])

        # カルーセル（JSON配列の画像URL必須）
        elif post_type == "カルーセル" and image_urls_json:
            try:
                urls = json.loads(image_urls_json)
                media_id = post_carousel(token, user_id, urls, text, topic_tag)
            except json.JSONDecodeError:
                print(f"  ⚠ カルーセルURLのJSON不正 → テキスト投稿にフォールバック")
                media_id = post_text(token, user_id, text, topic_tag)

        # 画像付き（「画像」「テキスト+画像」等、image_urlがあれば画像投稿）
        elif image_url and not image_url.startswith("["):
            media_id = post_image(token, user_id, image_url, text, topic_tag)

        # テキストのみ（画像URLなし or デフォルト）
        else:
            media_id = post_text(token, user_id, text, topic_tag)

        # パーマリンク取得
        if media_id and not permalink:
            try:
                permalink = get_thread_permalink(token, media_id)
            except Exception:
                permalink = ""

        # 成功更新
        batch_update_cells(service, [
            (row, COL_STATUS, "published"),
            (row, COL_MEDIA_ID, str(media_id)),
            (row, COL_LAST_ATTEMPT, now_str),
            (row, COL_ERROR, ""),
        ])

        # Threads URLをH列（COL_URL）に書き込み
        if permalink:
            update_cell(service, row, COL_URL, permalink)

        print(f"  ✅ 投稿成功: {post_num} → {permalink}")
        return True

    except Exception as e:
        error_msg = str(e)[:500]
        retry_count = 0
        try:
            retry_count = int(get_col_value(data, COL_ERROR - 1) or "0")
        except (ValueError, IndexError):
            pass

        retry_count += 1
        new_status = "retry" if retry_count < MAX_RETRIES else "failed"

        batch_update_cells(service, [
            (row, COL_STATUS, new_status),
            (row, COL_ERROR, error_msg),
            (row, COL_LAST_ATTEMPT, now_str),
        ])

        print(f"  ❌ 投稿失敗: {post_num} ({new_status}, retry={retry_count})")
        print(f"     Error: {error_msg[:100]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Threads自動投稿")
    parser.add_argument("--window", type=int, default=45,
                        help="スケジュールウィンドウ（分）")
    parser.add_argument("--force", type=str, default=None,
                        help="強制投稿する投稿番号（T-001形式）")
    parser.add_argument("--chain", type=str, default=None,
                        help="スレッドチェーンの投稿番号（カンマ区切り）")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せずに確認のみ")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"🧵 Threads自動投稿 - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   Window: {args.window}min | DryRun: {args.dry_run}")
    print(f"   ReadRange: A:{SHEET_READ_END_COL} (max COL idx={_MAX_COL_IDX})")
    print()

    # ── 起動時バリデーション ──
    # COL_* 定数が読み取り範囲内か検証（列追加時の範囲不足を防止）
    col_constants = {k: v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int)}
    for name, idx in col_constants.items():
        if idx > _MAX_COL_IDX:
            print(f"❌ FATAL: {name}={idx} が読み取り範囲 (max={_MAX_COL_IDX}) を超えています")
            sys.exit(1)

    # サービス初期化
    service = get_sheets_service()
    token = get_threads_token()
    user_id = get_threads_user_id()

    # レート制限チェック
    if not args.dry_run:
        try:
            limits = check_publishing_limit(token, user_id)
            remaining = limits["total"] - limits["used"]
            print(f"📊 レート制限: {limits['used']}/{limits['total']} (残り{remaining})")
            if remaining <= 0:
                print("❌ レート制限到達。投稿不可。")
                return
        except Exception as e:
            print(f"⚠️ レート制限確認失敗: {e}")

    # 強制投稿
    if args.force:
        post_info = find_post_by_num(service, args.force)
        if not post_info:
            print(f"❌ 投稿 {args.force} が見つかりません")
            return

        chain_nums = None
        if args.chain:
            chain_nums = [n.strip() for n in args.chain.split(",")]

        execute_post(service, token, user_id, post_info,
                     dry_run=args.dry_run, chain_nums=chain_nums)
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

        success = execute_post(service, token, user_id, post_info,
                               dry_run=args.dry_run)
        if success:
            posted += 1
            if posted < len(ready):
                time.sleep(POST_GAP_SECONDS)

    print()
    print(f"🎉 完了: {posted}/{len(ready)}件を投稿")


if __name__ == "__main__":
    main()
