#!/usr/bin/env python3
"""
Threadsインサイト同期スクリプト (sync_threads_insights.py)

Instagram版と同じ1日後/7日後スナップショット方式。
投稿からの経過時間に応じて、適切なブロックに1回だけ記録する。

取得メトリクス (6種):
  - views, likes, replies, reposts, quotes, shares

計算メトリクス (3種):
  - ER% = (likes + replies + reposts + quotes + shares) / views * 100
  - 会話率% = replies / views * 100
  - リプ/いいね比 = replies / likes

スプレッドシート列マッピング:
  ── 1日後ブロック (24-48h) ──
  W(22): views          X(23): likes       Y(24): replies
  Z(25): reposts        AA(26): shares     AB(27): quotes
  AD(29): ER%           AE(30): 会話率%    AF(31): リプ/いいね比
  AG(32): 1d取得日時    AH(33): 1d_mode

  ── 7日後ブロック (168-192h) ──
  AV(47): views         AW(48): likes      AX(49): replies
  AY(50): reposts       AZ(51): shares     BA(52): quotes
  BB(53): ER%           BC(54): 会話率%    BD(55): リプ/いいね比
  BE(56): 7d取得日時    BF(57): 7d_mode

Usage:
    python3 sync_threads_insights.py              # 通常実行（1d/7dスナップショット）
    python3 sync_threads_insights.py --dry-run     # 書き込みなし確認
    python3 sync_threads_insights.py --post T-001  # 特定投稿のみ
    python3 sync_threads_insights.py --account     # アカウントレベルも取得
    python3 sync_threads_insights.py --force        # 取得済みでも上書き
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
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
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── 列インデックス（0-based）────────────────────────────────────────

# 基本情報
COL_DATE = 0        # A: 日付
COL_POST_NUM = 2    # C: 番号
COL_TIME = 3        # D: 時刻
COL_URL = 7         # H: URL

# 1日後ブロック (24-48h)
COL_1D_VIEWS = 22      # W
COL_1D_LIKES = 23      # X
COL_1D_REPLIES = 24    # Y
COL_1D_REPOSTS = 25    # Z
COL_1D_SHARES = 26     # AA
COL_1D_QUOTES = 27     # AB
COL_1D_ER = 29         # AD
COL_1D_CONV_RATE = 30  # AE
COL_1D_REPLY_LIKE = 31 # AF
COL_1D_CAPTURED_AT = 32  # AG
COL_1D_MODE = 33       # AH

# 7日後ブロック (168-192h)
COL_7D_VIEWS = 47      # AV
COL_7D_LIKES = 48      # AW
COL_7D_REPLIES = 49    # AX
COL_7D_REPOSTS = 50    # AY
COL_7D_SHARES = 51     # AZ
COL_7D_QUOTES = 52     # BA
COL_7D_ER = 53         # BB
COL_7D_CONV_RATE = 54  # BC
COL_7D_REPLY_LIKE = 55 # BD
COL_7D_CAPTURED_AT = 56  # BE
COL_7D_MODE = 57       # BF

# 自動投稿列（読み取り用）
COL_STATUS = 41     # AP: 投稿ステータス
COL_MEDIA_ID = 43   # AR: メディアID

# ── 読み取り範囲の自動算出 ──────────────────────────────────────────
_ALL_COLS = {k: v for k, v in globals().items() if k.startswith("COL_") and isinstance(v, int)}
_MAX_COL_IDX = max(_ALL_COLS.values())


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


def sanitize_error(error: Exception | str) -> str:
    """Avoid leaking access tokens in API error URLs."""
    text = str(error)
    text = re.sub(r"(access_token=)[^&\s]+", r"\1***", text)
    return text


# スナップショット判定閾値
SNAPSHOT_1D_MIN_HOURS = 24
SNAPSHOT_1D_MAX_HOURS = 72   # 3日以内なら「scheduled」、超えたら「backfill」
SNAPSHOT_7D_MIN_HOURS = 168
SNAPSHOT_7D_MAX_HOURS = 240  # 10日以内なら「scheduled」、超えたら「backfill」

# タイムゾーン
JST = timezone(timedelta(hours=9))

# レート制限
API_CALL_DELAY = 1.0

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"


# ── Google Sheets ─────────────────────────────────────────────────────

def get_sheets_service():
    """Google Sheets APIサービスを取得"""
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if service_account_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(service_account_json)
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            return build("sheets", "v4", credentials=creds)
    if service_account_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        return build("sheets", "v4", credentials=creds)

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
    params = {"metric": POST_METRICS, "access_token": token}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    metrics = {}
    for item in result.get("data", []):
        name = item["name"]
        values = item.get("values", [])
        if values:
            metrics[name] = values[0].get("value", 0)
        else:
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
        "since": since, "until": until, "access_token": token,
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

    # フォロワー数
    try:
        params2 = {"metric": "followers_count", "access_token": token}
        resp2 = requests.get(url, params=params2, timeout=30)
        resp2.raise_for_status()
        for item in resp2.json().get("data", []):
            if item["name"] == "followers_count":
                vals = item.get("values", [])
                metrics["followers_count"] = vals[0].get("value", 0) if vals else 0
    except Exception as e:
        print(f"  ⚠ フォロワー数取得失敗: {sanitize_error(e)}")

    return metrics


# ── スナップショット判定 ──────────────────────────────────────────────

def parse_post_datetime(date_str: str, time_str: str) -> datetime | None:
    """日付+時刻文字列からdatetimeを生成"""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
        if time_str:
            parts = time_str.replace("：", ":").split(":")
            dt = dt.replace(hour=int(parts[0]), minute=int(parts[1]) if len(parts) > 1 else 0)
        return dt
    except (ValueError, TypeError):
        return None


def calc_metrics(metrics: dict) -> tuple:
    """APIメトリクスから計算メトリクスを算出"""
    views = metrics.get("views", 0)
    likes = metrics.get("likes", 0)
    replies = metrics.get("replies", 0)
    reposts = metrics.get("reposts", 0)
    shares = metrics.get("shares", 0)
    quotes = metrics.get("quotes", 0)

    total_eng = likes + replies + reposts + quotes + shares
    er = round(total_eng / views * 100, 2) if views > 0 else 0
    conv_rate = round(replies / views * 100, 2) if views > 0 else 0
    reply_like = round(replies / likes, 2) if likes > 0 else 0

    return er, conv_rate, reply_like


def build_snapshot_updates(actual_row: int, metrics: dict, now_str: str,
                           mode: str, block: str) -> list:
    """スナップショットブロックの書き込みデータを構築"""
    views = metrics.get("views", 0)
    likes = metrics.get("likes", 0)
    replies = metrics.get("replies", 0)
    reposts = metrics.get("reposts", 0)
    shares = metrics.get("shares", 0)
    quotes = metrics.get("quotes", 0)
    er, conv_rate, reply_like = calc_metrics(metrics)

    if block == "1d":
        return [
            (actual_row, COL_1D_VIEWS, str(views)),
            (actual_row, COL_1D_LIKES, str(likes)),
            (actual_row, COL_1D_REPLIES, str(replies)),
            (actual_row, COL_1D_REPOSTS, str(reposts)),
            (actual_row, COL_1D_SHARES, str(shares)),
            (actual_row, COL_1D_QUOTES, str(quotes)),
            (actual_row, COL_1D_ER, f"{er}%"),
            (actual_row, COL_1D_CONV_RATE, f"{conv_rate}%"),
            (actual_row, COL_1D_REPLY_LIKE, str(reply_like)),
            (actual_row, COL_1D_CAPTURED_AT, now_str),
            (actual_row, COL_1D_MODE, mode),
        ]
    else:  # 7d
        return [
            (actual_row, COL_7D_VIEWS, str(views)),
            (actual_row, COL_7D_LIKES, str(likes)),
            (actual_row, COL_7D_REPLIES, str(replies)),
            (actual_row, COL_7D_REPOSTS, str(reposts)),
            (actual_row, COL_7D_SHARES, str(shares)),
            (actual_row, COL_7D_QUOTES, str(quotes)),
            (actual_row, COL_7D_ER, f"{er}%"),
            (actual_row, COL_7D_CONV_RATE, f"{conv_rate}%"),
            (actual_row, COL_7D_REPLY_LIKE, str(reply_like)),
            (actual_row, COL_7D_CAPTURED_AT, now_str),
            (actual_row, COL_7D_MODE, mode),
        ]


# ── メイン処理 ────────────────────────────────────────────────────────

def sync_insights(service, token: str, dry_run: bool = False,
                  target_post: str | None = None,
                  force: bool = False) -> dict:
    """全投稿のインサイトを1日後/7日後スナップショット方式で同期"""
    rows = read_sheet_data(service)
    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    stats = {"total": 0, "1d_new": 0, "1d_skip": 0, "7d_new": 0, "7d_skip": 0, "errors": 0}

    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        post_num = get_col_value(row, COL_POST_NUM)
        status = get_col_value(row, COL_STATUS)
        media_id = get_col_value(row, COL_MEDIA_ID)

        if target_post and post_num != target_post:
            continue
        if status != "published" or not media_id:
            continue

        stats["total"] += 1

        # 投稿日時を取得して経過時間を計算
        date_str = get_col_value(row, COL_DATE)
        time_str = get_col_value(row, COL_TIME)
        post_dt = parse_post_datetime(date_str, time_str)
        if not post_dt:
            continue

        hours_elapsed = (now - post_dt).total_seconds() / 3600
        hook = get_col_value(row, 4)[:30]

        # 1日後ブロック: 既に取得済みかチェック
        has_1d = bool(get_col_value(row, COL_1D_CAPTURED_AT))
        needs_1d = (not has_1d or force) and hours_elapsed >= SNAPSHOT_1D_MIN_HOURS

        # 7日後ブロック: 既に取得済みかチェック
        has_7d = bool(get_col_value(row, COL_7D_CAPTURED_AT))
        needs_7d = (not has_7d or force) and hours_elapsed >= SNAPSHOT_7D_MIN_HOURS

        if not needs_1d and not needs_7d:
            if has_1d:
                stats["1d_skip"] += 1
            if has_7d:
                stats["7d_skip"] += 1
            continue

        # API呼び出し（1d/7d両方必要でも1回で済む — lifetimeメトリクスだから）
        try:
            metrics = fetch_post_insights(token, media_id)
            views = metrics.get("views", 0)
            likes = metrics.get("likes", 0)
            replies = metrics.get("replies", 0)
            er, _, _ = calc_metrics(metrics)

            print(f"  {post_num}: {hook}... ({hours_elapsed:.0f}h経過)")
            print(f"    views={views} likes={likes} replies={replies} ER={er}%")

            if needs_1d:
                mode_1d = "scheduled" if hours_elapsed <= SNAPSHOT_1D_MAX_HOURS else "backfill"
                print(f"    → 1日後ブロック書き込み (mode={mode_1d})")
                if not dry_run:
                    updates = build_snapshot_updates(actual_row, metrics, now_str, mode_1d, "1d")
                    batch_update_cells(service, updates)
                stats["1d_new"] += 1

            if needs_7d:
                mode_7d = "scheduled" if hours_elapsed <= SNAPSHOT_7D_MAX_HOURS else "backfill"
                print(f"    → 7日後ブロック書き込み (mode={mode_7d})")
                if not dry_run:
                    updates = build_snapshot_updates(actual_row, metrics, now_str, mode_7d, "7d")
                    batch_update_cells(service, updates)
                stats["7d_new"] += 1

            time.sleep(API_CALL_DELAY)

        except Exception as e:
            print(f"  ❌ {post_num}: {sanitize_error(e)}")
            stats["errors"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="Threadsインサイト同期")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなし確認")
    parser.add_argument("--post", type=str, default=None, help="特定投稿のみ（T-001形式）")
    parser.add_argument("--account", action="store_true", help="アカウントレベルも取得")
    parser.add_argument("--force", action="store_true", help="取得済みでも上書き")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"📊 Threadsインサイト同期 v2.0 - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   DryRun: {args.dry_run} | Force: {args.force} | ReadRange: A:{SHEET_READ_END_COL}")
    print()

    # 起動時バリデーション
    for name, idx in _ALL_COLS.items():
        if idx > _MAX_COL_IDX:
            print(f"❌ FATAL: {name}={idx} > max={_MAX_COL_IDX}")
            sys.exit(1)

    service = get_sheets_service()
    token = get_threads_token()
    user_id = get_threads_user_id()

    # 投稿インサイト
    print("── 投稿インサイト（1d/7d スナップショット）──")
    stats = sync_insights(service, token, dry_run=args.dry_run,
                          target_post=args.post, force=args.force)
    print()
    print(f"📋 結果: 対象{stats['total']}件")
    print(f"   1日後: {stats['1d_new']}件新規 / {stats['1d_skip']}件スキップ")
    print(f"   7日後: {stats['7d_new']}件新規 / {stats['7d_skip']}件スキップ")
    if stats["errors"]:
        print(f"   エラー: {stats['errors']}件")

    # アカウントインサイト
    if args.account:
        print()
        print("── アカウントインサイト（7日間） ──")
        try:
            acct = fetch_account_insights(token, user_id, days=7)
            for k, v in sorted(acct.items()):
                print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")
        except Exception as e:
            print(f"  ❌ {sanitize_error(e)}")

    print()
    print("✅ 完了")


if __name__ == "__main__":
    main()
