#!/usr/bin/env python3
"""
Instagram × スプレッドシート 整合性チェック & 自動修正

Instagram Graph APIの実投稿データを正として、
スプレッドシートの内容（ファイル名・意図・キャプション等）を自動修正する。

GitHub Actions で定期実行（毎日1回）。
"""

import os
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── 設定 ────────────────────────────────────────────
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
SHEET_NAME = "Instagram投稿毎データ"
HEADER_ROW = 3       # ヘッダー行（1-indexed）
DATA_START_ROW = 4   # データ開始行（1-indexed）
JST = timezone(timedelta(hours=9))

# カラムインデックス（0-indexed）
COL_DATE = 0       # A: 日付
COL_THUMB = 1      # B: サムネ
COL_NUMBER = 2     # C: 番号
COL_TIME = 3       # D: 時刻
COL_FILENAME = 4   # E: ファイル名
COL_POST_TYPE = 5  # F: 投稿種別
COL_FORMAT = 6     # G: 形式
COL_URL = 7        # H: URL
COL_INTENT = 8     # I: 投稿の意図
COL_CONTENT = 9    # J: 内容
COL_MEMO = 10      # K: 備考
COL_CAPTION = 11   # L: キャプション
COL_LF8 = 12       # M: どんな欲求LF8
COL_EMOTION = 13   # N: 感情トリガー
COL_METRIC = 14    # O: 成果指標
COL_ANALYSIS = 20  # U: 考察・仮説
COL_NEXT = 21      # V: 次の投稿に活かすポイント

# 自動投稿の判定キーワード
AUTO_POST_MARKERS = [
    "@tackey_clear_skincare",
    "【洗顔・保湿をやめて綺麗な肌へ】",
]

# 自動投稿のメタデータ（統一値）
AUTO_META = {
    COL_FILENAME: "自動投稿（@tackey）",
    COL_POST_TYPE: "自動",
    COL_INTENT: "自動投稿（プロフィール誘導）",
    COL_CONTENT: "洗顔・保湿をやめて綺麗な肌へ（定型プロモ）",
    COL_MEMO: "自動投稿（インサイトスケジューラー）",
    COL_LF8: "①生存・健康",
    COL_EMOTION: "共感・希望",
    COL_METRIC: "",
    COL_ANALYSIS: "",
    COL_NEXT: "",
}

DEFAULT_BASE_DIR = Path(
    "/Users/taiki/Library/Mobile Documents/"
    "com~apple~CloudDocs/MacDocuments/01_事業"
)


# ── 認証 ────────────────────────────────────────────
def get_google_credentials():
    auth_dir = Path(
        os.environ.get(
            "INSTAGRAM_INSIGHTS_GOOGLE_AUTH_DIR",
            str(DEFAULT_BASE_DIR / "事業 Cursor/タッキー/02_SNS集客/instagram-auto-post"),
        )
    )
    token_file = auth_dir / "token.json"
    creds_file = auth_dir / "credentials.json"
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    creds = None
    if token_file.exists():
        # token.jsonのexpiry形式を修正（int→文字列変換）
        token_data = json.loads(token_file.read_text())
        if isinstance(token_data.get("expiry"), (int, float)):
            token_data.pop("expiry", None)
            token_file.write_text(json.dumps(token_data))
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        else:
            print("ERROR: Google credentials invalid. Update token.json secret.")
            sys.exit(1)
    return creds


def load_instagram_config():
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_user_id = os.environ.get("INSTAGRAM_IG_USER_ID")
    if access_token and ig_user_id:
        return access_token, ig_user_id

    auth_dir = Path(
        os.environ.get(
            "INSTAGRAM_INSIGHTS_GOOGLE_AUTH_DIR",
            str(DEFAULT_BASE_DIR / "事業 Cursor/タッキー/02_SNS集客/instagram-auto-post"),
        )
    )
    config_file = auth_dir / "instagram_insights_config.json"
    if config_file.exists():
        cfg = json.loads(config_file.read_text())
        return cfg["access_token"], cfg["ig_user_id"]

    print("ERROR: Instagram credentials not found.")
    sys.exit(1)


# ── Instagram API ───────────────────────────────────
def fetch_all_instagram_posts(access_token, ig_user_id, max_pages=10):
    """全投稿を取得（ページネーション付き）"""
    all_posts = []
    url = (
        f"{GRAPH_API_BASE}/{ig_user_id}/media"
        f"?fields=id,caption,timestamp,like_count,comments_count,permalink"
        f"&limit=100&access_token={access_token}"
    )

    for page in range(max_pages):
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"ERROR: Instagram API {resp.status_code}: {resp.text[:200]}")
            break
        data = resp.json()
        posts = data.get("data", [])
        all_posts.extend(posts)
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url = next_url

    print(f"Instagram API: {len(all_posts)}件取得")
    return all_posts


def is_auto_post(caption):
    """自動投稿かどうか判定"""
    if not caption:
        return False
    for marker in AUTO_POST_MARKERS:
        if marker in caption[:50]:
            return True
    return False


def parse_instagram_posts(posts):
    """投稿データをpermalink→詳細の辞書に変換"""
    result = {}
    for p in posts:
        permalink = p.get("permalink", "")
        caption = p.get("caption", "")
        ts_str = p.get("timestamp", "")

        # JST変換
        jst_dt = None
        if ts_str:
            dt = datetime.fromisoformat(ts_str.replace("+0000", "+00:00"))
            jst_dt = dt.astimezone(JST)

        # permalink正規化（?igsh=... 等を除去）
        clean_link = permalink.split("?")[0].rstrip("/") + "/"

        result[clean_link] = {
            "caption": caption,
            "timestamp": ts_str,
            "jst": jst_dt,
            "jst_date": jst_dt.strftime("%-m/%-d") if jst_dt else "",
            "jst_time": jst_dt.strftime("%-H:%M") if jst_dt else "",
            "likes": p.get("like_count", 0),
            "comments": p.get("comments_count", 0),
            "is_auto": is_auto_post(caption),
            "permalink": permalink,
        }
    return result


# ── スプレッドシート ────────────────────────────────
def read_sheet_data(sheets_service):
    """スプレッドシートの全データ行を取得"""
    range_str = f"{SHEET_NAME}!A{DATA_START_ROW}:V1000"
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=range_str)
        .execute()
    )
    rows = result.get("values", [])
    print(f"スプレッドシート: {len(rows)}行取得")
    return rows


def cell_ref(col_idx, row_1based):
    """カラムインデックス(0-indexed)と行番号(1-indexed)からA1表記を生成"""
    col_letter = chr(65 + col_idx) if col_idx < 26 else chr(64 + col_idx // 26) + chr(65 + col_idx % 26)
    return f"{SHEET_NAME}!{col_letter}{row_1based}"


def get_cell(row, col_idx):
    """行データからセル値を安全に取得"""
    if col_idx < len(row):
        return row[col_idx]
    return ""


# ── 整合性チェック & 修正 ───────────────────────────
def check_and_fix(sheet_rows, ig_posts):
    """
    スプレッドシートとInstagram投稿を突合し、修正データを生成。
    戻り値: (updates_list, summary_dict)
    """
    updates = []
    summary = {
        "checked": 0,
        "auto_fixed": 0,
        "url_added": 0,
        "date_fixed": 0,
        "unposted_cleared": 0,
        "caption_fixed": 0,
        "details": [],
    }

    for i, row in enumerate(sheet_rows):
        sheet_row = DATA_START_ROW + i  # 1-indexed行番号
        post_num = get_cell(row, COL_NUMBER)
        if not post_num:
            continue

        summary["checked"] += 1
        url_raw = get_cell(row, COL_URL)
        date_val = get_cell(row, COL_DATE)
        filename = get_cell(row, COL_FILENAME)

        # URLを正規化
        url_clean = ""
        if url_raw:
            url_clean = url_raw.split("?")[0].rstrip("/") + "/"

        # ── Case 1: URLあり → Instagram実データと照合 ──
        if url_clean and url_clean in ig_posts:
            ig = ig_posts[url_clean]

            # 1a: 自動投稿なのに個別コンテンツ名が入っている
            if ig["is_auto"] and filename and "自動投稿" not in filename:
                detail = f"#{post_num} (Row{sheet_row}): 自動投稿に修正 (旧: {filename[:30]})"
                summary["details"].append(detail)
                summary["auto_fixed"] += 1

                for col_idx, val in AUTO_META.items():
                    updates.append({
                        "range": cell_ref(col_idx, sheet_row),
                        "values": [[val]],
                    })
                # キャプションを実際のものに
                updates.append({
                    "range": cell_ref(COL_CAPTION, sheet_row),
                    "values": [[ig["caption"]]],
                })

            # 1b: 日付・時刻がInstagramと異なる
            if ig["jst_date"] and date_val and ig["jst_date"] != date_val:
                summary["date_fixed"] += 1
                summary["details"].append(
                    f"#{post_num} (Row{sheet_row}): 日付修正 {date_val} → {ig['jst_date']}"
                )
                updates.append({
                    "range": cell_ref(COL_DATE, sheet_row),
                    "values": [[ig["jst_date"]]],
                })
            if ig["jst_time"]:
                time_val = get_cell(row, COL_TIME)
                if time_val and ig["jst_time"] != time_val:
                    summary["date_fixed"] += 1
                    updates.append({
                        "range": cell_ref(COL_TIME, sheet_row),
                        "values": [[ig["jst_time"]]],
                    })

        # ── Case 2: URLなし + 日付あり → 未投稿の可能性チェック ──
        elif not url_raw and date_val:
            # キャプションで照合を試みる
            caption_in_sheet = get_cell(row, COL_CAPTION)
            matched_ig = None

            if caption_in_sheet:
                # キャプション先頭30文字で一致を探す
                cap_prefix = caption_in_sheet[:30].replace("\n", " ")
                for link, ig in ig_posts.items():
                    ig_cap_prefix = ig["caption"][:30].replace("\n", " ")
                    if cap_prefix and cap_prefix == ig_cap_prefix:
                        matched_ig = ig
                        matched_link = link
                        break

            if matched_ig:
                # 投稿済みだった → URL・日付・時刻を追加
                summary["url_added"] += 1
                summary["details"].append(
                    f"#{post_num} (Row{sheet_row}): URL追加 {matched_ig['permalink']}"
                )
                updates.append({
                    "range": cell_ref(COL_URL, sheet_row),
                    "values": [[matched_ig["permalink"]]],
                })
                if matched_ig["jst_date"]:
                    updates.append({
                        "range": cell_ref(COL_DATE, sheet_row),
                        "values": [[matched_ig["jst_date"]]],
                    })
                if matched_ig["jst_time"]:
                    updates.append({
                        "range": cell_ref(COL_TIME, sheet_row),
                        "values": [[matched_ig["jst_time"]]],
                    })
            else:
                # 本当に未投稿 → 日付が過去なら日付をクリア
                # ただし当日の投稿は絶対に消さない（時刻がまだ来ていない可能性）
                # 翌日 00:00 JST を過ぎて初めて「未投稿」と判定する
                try:
                    # 日付パース（M/D形式）
                    month, day = date_val.split("/")
                    post_date = datetime(2026, int(month), int(day), tzinfo=JST)
                    now = datetime.now(JST)
                    # 投稿予定日の翌日 00:00 JST を過ぎたら未投稿と判定
                    deadline = post_date + timedelta(days=1)
                    if now >= deadline:
                        summary["unposted_cleared"] += 1
                        summary["details"].append(
                            f"#{post_num} (Row{sheet_row}): 未投稿 → 日付クリア (予定: {date_val})"
                        )
                        updates.append({
                            "range": cell_ref(COL_DATE, sheet_row),
                            "values": [[""]],
                        })
                        updates.append({
                            "range": cell_ref(COL_TIME, sheet_row),
                            "values": [[""]],
                        })
                except (ValueError, IndexError):
                    pass

        # ── Case 3: URLあり + Instagramに存在しない → 警告のみ ──
        elif url_clean and url_clean not in ig_posts:
            # APIの取得範囲外の古い投稿の可能性がある → 警告だけ
            pass

    return updates, summary


# ── メイン ──────────────────────────────────────────
def main():
    print("=" * 60)
    print("Instagram × スプレッドシート 整合性チェック")
    print(f"実行時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    print("=" * 60)

    # 認証
    access_token, ig_user_id = load_instagram_config()
    creds = get_google_credentials()
    sheets_service = build("sheets", "v4", credentials=creds)

    # データ取得
    ig_raw = fetch_all_instagram_posts(access_token, ig_user_id)
    ig_posts = parse_instagram_posts(ig_raw)
    sheet_rows = read_sheet_data(sheets_service)

    # 整合性チェック
    updates, summary = check_and_fix(sheet_rows, ig_posts)

    # 結果表示
    print()
    print(f"チェック対象: {summary['checked']}行")
    print(f"  自動投稿修正: {summary['auto_fixed']}件")
    print(f"  URL追加: {summary['url_added']}件")
    print(f"  日付修正: {summary['date_fixed']}件")
    print(f"  未投稿クリア: {summary['unposted_cleared']}件")

    if summary["details"]:
        print()
        print("--- 修正詳細 ---")
        for d in summary["details"]:
            print(f"  {d}")

    # 修正実行
    if updates:
        print()
        print(f"スプレッドシート更新中... ({len(updates)}セル)")
        batch_size = 50
        for start in range(0, len(updates), batch_size):
            batch = updates[start : start + batch_size]
            body = {"valueInputOption": "USER_ENTERED", "data": batch}
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID, body=body
            ).execute()
        print("更新完了 ✅")
    else:
        print()
        print("修正不要 ✅ スプレッドシートはInstagramと一致しています")

    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
