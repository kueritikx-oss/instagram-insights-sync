#!/usr/bin/env python3
"""
Threadsインサイト同期スクリプト (sync_threads_insights.py)

Threads APIから投稿毎のインサイトを取得し、スプレッドシートに書き込む。

取得メトリクス (6種):
  - views, likes, replies, reposts, quotes, shares

計算メトリクス (3種):
  - ER% = (likes + replies + reposts + quotes + shares) / views * 100
  - 会話率% = replies / views * 100
  - リプ/いいね比 = replies / likes

スプレッドシート列マッピング:
  W(22): views
  X(23): likes（いいね）
  Y(24): replies（リプライ数）
  Z(25): reposts（リポスト数）
  AA(26): shares（シェア数）
  AB(27): quotes（引用数）
  AC(28): (フォロー — API未提供)
  AD(29): ER%
  AE(30): 会話率%
  AF(31): リプ/いいね比
  AH(33): 取得日時

Usage:
    # 全投稿のインサイト取得
    python3 sync_threads_insights.py

    # ドライラン（API呼び出しのみ、書き込みなし）
    python3 sync_threads_insights.py --dry-run

    # 特定投稿のみ
    python3 sync_threads_insights.py --post T-001

    # アカウントレベルインサイトも取得
    python3 sync_threads_insights.py --account
"""
from __future__ import annotations

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

# Threads API
THREADS_API_BASE = "https://graph.threads.net/v1.0"
POST_METRICS = "views,likes,replies,reposts,quotes,shares"

# スプレッドシート
THREADS_SPREADSHEET_ID = "1hdBlZBn9s688f1ZwkTiO3suY27tJEHtXMEPkopLdBNI"
THREADS_SHEET_NAME = "Threads投稿毎データ"
DATA_START_ROW = 4

# 列インデックス（0-based）
COL_DATE = 0        # A: 日付
COL_POST_NUM = 2    # C: 番号
COL_URL = 7         # H: URL
COL_VIEWS = 22      # W: views
COL_LIKES = 23      # X: いいね
COL_REPLIES = 24    # Y: リプライ数
COL_REPOSTS = 25    # Z: リポスト数
COL_SHARES = 26     # AA: シェア数
COL_QUOTES = 27     # AB: 引用数
COL_ER = 29         # AD: ER%
COL_CONV_RATE = 30  # AE: 会話率%
COL_REPLY_LIKE = 31 # AF: リプ/いいね比
COL_CAPTURED_AT = 33  # AH: 取得日時
COL_STATUS = 41     # AP: 投稿ステータス
COL_MEDIA_ID = 43   # AR: メディアID

# 読み取り範囲を自動算出
_MAX_COL_IDX = max(COL_DATE, COL_POST_NUM, COL_URL, COL_VIEWS, COL_LIKES,
                   COL_REPLIES, COL_REPOSTS, COL_SHARES, COL_QUOTES,
                   COL_ER, COL_CONV_RATE, COL_REPLY_LIKE, COL_CAPTURED_AT,
                   COL_STATUS, COL_MEDIA_ID)


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


SHEET_READ_END_COL = _col_idx_to_letter(_MAX_COL_IDX)

# タイムゾーン
JST = timezone(timedelta(hours=9))

# レート制限: Threads APIは控えめに
API_CALL_DELAY = 1.0  # 投稿間1秒待機

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"


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
        token=info["token"],
        refresh_token=info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
    )
    return build("sheets", "v4", credentials=creds)


def read_sheet_data(service) -> list:
    """スプレッドシートから全データ行を読み取る"""
    range_str = f"{THREADS_SHEET_NAME}!A{DATA_START_ROW}:{SHEET_READ_END_COL}500"
    result = service.spreadsheets().values().get(
        spreadsheetId=THREADS_SPREADSHEET_ID,
        range=range_str,
    ).execute()
    return result.get("values", [])


def get_col_value(row_data: list, col_idx: int) -> str:
    """行データから列の値を安全に取得"""
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


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


# ── Threads API ──────────────────────────────────────────────────────

def get_threads_token() -> str:
    """Threadsアクセストークンを取得"""
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if token:
        return token
    token_path = GOOGLE_AUTH_DIR / "threads_token.json"
    if token_path.exists():
        with open(token_path) as f:
            data = json.load(f)
        return data.get("access_token", "")
    raise RuntimeError("THREADS_ACCESS_TOKEN not found")


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
    raise RuntimeError("THREADS_USER_ID not found")


def fetch_post_insights(token: str, media_id: str) -> dict:
    """投稿のインサイトを取得"""
    url = f"{THREADS_API_BASE}/{media_id}/insights"
    params = {
        "metric": POST_METRICS,
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    metrics = {}
    for item in result.get("data", []):
        name = item["name"]
        # lifetime metrics have a single value
        values = item.get("values", [])
        if values:
            metrics[name] = values[0].get("value", 0)
        else:
            # total_value fallback
            metrics[name] = item.get("total_value", {}).get("value", 0)

    return metrics


def fetch_account_insights(token: str, user_id: str, days: int = 7) -> dict:
    """アカウントレベルのインサイトを取得"""
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(days=days)).timestamp())
    until = int(now.timestamp())

    url = f"{THREADS_API_BASE}/{user_id}/threads_insights"
    params = {
        "metric": "views,likes,replies,reposts,quotes",
        "since": since,
        "until": until,
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    metrics = {}
    for item in result.get("data", []):
        name = item["name"]
        values = item.get("values", [])
        total = item.get("total_value", {}).get("value", 0)
        metrics[name] = total if total else sum(v.get("value", 0) for v in values)

    # フォロワー数（別リクエスト、since/untilを無視する）
    try:
        url2 = f"{THREADS_API_BASE}/{user_id}/threads_insights"
        params2 = {
            "metric": "followers_count",
            "access_token": token,
        }
        resp2 = requests.get(url2, params=params2, timeout=30)
        resp2.raise_for_status()
        result2 = resp2.json()
        for item in result2.get("data", []):
            if item["name"] == "followers_count":
                vals = item.get("values", [])
                metrics["followers_count"] = vals[0].get("value", 0) if vals else 0
    except Exception as e:
        print(f"  ⚠ フォロワー数取得失敗: {e}")

    return metrics


# ── メイン処理 ────────────────────────────────────────────────────────

def sync_insights(service, token: str, dry_run: bool = False,
                  target_post: str | None = None) -> dict:
    """全投稿のインサイトを同期"""
    rows = read_sheet_data(service)
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    stats = {"total": 0, "synced": 0, "skipped": 0, "errors": 0}

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        post_num = get_col_value(row, COL_POST_NUM)
        status = get_col_value(row, COL_STATUS)
        media_id = get_col_value(row, COL_MEDIA_ID)

        # 特定投稿のみモード
        if target_post and post_num != target_post:
            continue

        # published + media_id がある投稿のみ対象
        if status != "published" or not media_id:
            continue

        stats["total"] += 1
        hook = get_col_value(row, 4)[:30]  # E列: タイトル

        try:
            metrics = fetch_post_insights(token, media_id)

            views = metrics.get("views", 0)
            likes = metrics.get("likes", 0)
            replies = metrics.get("replies", 0)
            reposts = metrics.get("reposts", 0)
            shares = metrics.get("shares", 0)
            quotes = metrics.get("quotes", 0)

            # 計算メトリクス
            total_engagement = likes + replies + reposts + quotes + shares
            er = round(total_engagement / views * 100, 2) if views > 0 else 0
            conv_rate = round(replies / views * 100, 2) if views > 0 else 0
            reply_like = round(replies / likes, 2) if likes > 0 else 0

            print(f"  {post_num}: {hook}...")
            print(f"    views={views} likes={likes} replies={replies} "
                  f"reposts={reposts} shares={shares} quotes={quotes} ER={er}%")

            if not dry_run:
                batch_update_cells(service, [
                    (actual_row, COL_VIEWS, str(views)),
                    (actual_row, COL_LIKES, str(likes)),
                    (actual_row, COL_REPLIES, str(replies)),
                    (actual_row, COL_REPOSTS, str(reposts)),
                    (actual_row, COL_SHARES, str(shares)),
                    (actual_row, COL_QUOTES, str(quotes)),
                    (actual_row, COL_ER, f"{er}%"),
                    (actual_row, COL_CONV_RATE, f"{conv_rate}%"),
                    (actual_row, COL_REPLY_LIKE, str(reply_like)),
                    (actual_row, COL_CAPTURED_AT, now_str),
                ])

            stats["synced"] += 1
            time.sleep(API_CALL_DELAY)

        except Exception as e:
            print(f"  ❌ {post_num}: {e}")
            stats["errors"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="Threadsインサイト同期")
    parser.add_argument("--dry-run", action="store_true",
                        help="書き込みせずに確認のみ")
    parser.add_argument("--post", type=str, default=None,
                        help="特定投稿のみ取得（T-001形式）")
    parser.add_argument("--account", action="store_true",
                        help="アカウントレベルインサイトも取得")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"📊 Threadsインサイト同期 - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   DryRun: {args.dry_run} | ReadRange: A:{SHEET_READ_END_COL}")
    print()

    service = get_sheets_service()
    token = get_threads_token()
    user_id = get_threads_user_id()

    # 投稿インサイト
    print("── 投稿インサイト ──")
    stats = sync_insights(service, token, dry_run=args.dry_run,
                          target_post=args.post)
    print()
    print(f"📋 結果: {stats['synced']}/{stats['total']}件同期 "
          f"(skip={stats['skipped']}, err={stats['errors']})")

    # アカウントインサイト
    if args.account:
        print()
        print("── アカウントインサイト（7日間） ──")
        try:
            acct = fetch_account_insights(token, user_id, days=7)
            for k, v in sorted(acct.items()):
                print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")
        except Exception as e:
            print(f"  ❌ アカウントインサイト取得失敗: {e}")

    print()
    print("✅ 完了")


if __name__ == "__main__":
    main()
