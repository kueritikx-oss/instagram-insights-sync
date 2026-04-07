#!/usr/bin/env python3
"""
IG投稿 → TikTok/X マルチプラットフォーム変換スクリプト

Instagram投稿毎データから上位投稿を選定し、TikTok/X用にフォーマット変換して
各プラットフォームのスプレッドシートに書き込む。

処理フロー:
  1. IG投稿毎データから投稿をスコアリング（保存+リーチ+シェア）
  2. TOP N投稿を選定
  3. TikTok用に変換（キャプション→description、画像URLs→WebP/JPEG検証）
  4. X用に変換（キャプション→280文字以内テキスト、画像は最大4枚）
  5. 各プラットフォームのスプシに書き込み

Usage:
    # TOP30をTikTok/X用に変換（ドライラン）
    python3 convert_ig_to_multiplatform.py --top 30 --dry-run

    # TikTokのみに変換して書き込み
    python3 convert_ig_to_multiplatform.py --top 30 --platform tiktok

    # X用に変換して書き込み
    python3 convert_ig_to_multiplatform.py --top 30 --platform x

    # 両方
    python3 convert_ig_to_multiplatform.py --top 30 --platform both
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pathlib import Path

# ── 定数 ──────────────────────────────────────────────────────────

JST = timezone(timedelta(hours=9))

# Instagram スプレッドシート
IG_SPREADSHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
IG_SHEET_NAME = "Instagram投稿毎データ"
IG_DATA_START_ROW = 4

# IG列インデックス（cloud_auto_post.py準拠）
IG_COL_DATE = 0       # A: 日付
IG_COL_POST_NUM = 2   # C: 投稿番号
IG_COL_TIME = 3       # D: 時刻
IG_COL_TITLE = 4      # E: タイトル
IG_COL_CTA = 5        # F: CTA型
IG_COL_FORMAT = 6     # G: 形式（認知/誘導）
IG_COL_URL = 7        # H: IG URL
IG_COL_CAPTION = 11   # L: キャプション
IG_COL_IMAGE_URLS = 93  # CP: 画像URLs (JSON)
IG_COL_STATUS = 92    # CO: ステータス

# メトリクス列（7日後）
IG_COL_REACH_7D = 51     # AZ: 7日後リーチ
IG_COL_SAVES_7D = 53     # BB: 7日後保存
IG_COL_SHARES_7D = 54    # BC: 7日後シェア
IG_COL_PROFILE_7D = 57   # BF: 7日後プロフアクセス

# TikTok/X スプレッドシート（環境変数で設定）
TIKTOK_SPREADSHEET_ID = os.environ.get("TIKTOK_SPREADSHEET_ID", "PLACEHOLDER")
X_SPREADSHEET_ID = os.environ.get("X_SPREADSHEET_ID", "1rHnDoMHUK_K0_f7MLxHltiU6Y2ATsz3ztKwdf2Zg8Hc")
TIKTOK_SHEET_NAME = "TikTok投稿毎データ"
X_SHEET_NAME = "X投稿毎データ"

# Google Auth
GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"


# ── Google Sheets ────────────────────────────────────────────────────

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


def get_col_value(row_data: list, col_idx: int) -> str:
    if col_idx < len(row_data):
        return str(row_data[col_idx]).strip()
    return ""


def safe_int(val: str) -> int:
    try:
        return int(float(val.replace(",", "")))
    except (ValueError, TypeError):
        return 0


# ── IG投稿スコアリング ──────────────────────────────────────────────

def score_post(row: list) -> float:
    """IG投稿をスコアリング。高いほど良い。
    配点: シェア40% + 保存30% + プロフアクセス20% + リーチ10%
    """
    reach = safe_int(get_col_value(row, IG_COL_REACH_7D))
    saves = safe_int(get_col_value(row, IG_COL_SAVES_7D))
    shares = safe_int(get_col_value(row, IG_COL_SHARES_7D))
    profile = safe_int(get_col_value(row, IG_COL_PROFILE_7D))

    if reach == 0:
        return 0

    # 正規化スコア（リーチ当たり）
    save_rate = saves / reach * 100
    share_rate = shares / reach * 100
    profile_rate = profile / reach * 100

    score = (share_rate * 40 + save_rate * 30 + profile_rate * 20 + (reach / 1000) * 10)
    return round(score, 2)


def select_top_posts(service, top_n: int = 30) -> list:
    """IG投稿毎データからTOP N投稿を選定"""
    rows = service.spreadsheets().values().get(
        spreadsheetId=IG_SPREADSHEET_ID,
        range=f"{IG_SHEET_NAME}!A{IG_DATA_START_ROW}:CT500",
    ).execute().get("values", [])

    scored = []
    for i, row in enumerate(rows):
        status = get_col_value(row, IG_COL_STATUS)
        if status != "posted":
            continue

        caption = get_col_value(row, IG_COL_CAPTION)
        image_urls_json = get_col_value(row, IG_COL_IMAGE_URLS)
        if not caption or not image_urls_json:
            continue

        try:
            image_urls = json.loads(image_urls_json)
            if not image_urls:
                continue
        except json.JSONDecodeError:
            continue

        score = score_post(row)
        if score == 0:
            continue

        scored.append({
            "ig_row": IG_DATA_START_ROW + i,
            "post_num": get_col_value(row, IG_COL_POST_NUM),
            "title": get_col_value(row, IG_COL_TITLE),
            "cta_type": get_col_value(row, IG_COL_CTA),
            "format": get_col_value(row, IG_COL_FORMAT),
            "caption": caption,
            "image_urls": image_urls,
            "reach": safe_int(get_col_value(row, IG_COL_REACH_7D)),
            "saves": safe_int(get_col_value(row, IG_COL_SAVES_7D)),
            "shares": safe_int(get_col_value(row, IG_COL_SHARES_7D)),
            "score": score,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


# ── TikTok変換 ──────────────────────────────────────────────────────

def convert_to_tiktok(post: dict, index: int) -> dict:
    """IG投稿をTikTok用に変換"""
    caption = post["caption"]

    # IGのハッシュタグをTikTok用に調整
    # TikTok: #スキンケア #fyp #tiktok を追加
    tiktok_tags = "#スキンケア #肌荒れ改善 #ニキビケア #美肌 #クリア肌 #fyp"

    # キャプションからIGハッシュタグ部分を抽出して置換
    # IG固有のハッシュタグを削除
    caption_clean = re.sub(r'#(instagram|instagood|insta\w+)', '', caption, flags=re.IGNORECASE)

    # LP URLをTikTok用に差し替え
    caption_clean = re.sub(
        r'https?://\S+',
        'https://cellosupport.xsrv.jp/clearahadaprogress-members/lp.php?utm_source=tiktok&utm_medium=social',
        caption_clean,
    )

    # 末尾にTikTokタグ追加
    if tiktok_tags not in caption_clean:
        caption_clean = caption_clean.rstrip() + "\n\n" + tiktok_tags

    # description上限4000文字
    description = caption_clean[:4000]

    # title: フック部分（最初の1行、90文字以内）
    first_line = caption.split("\n")[0].strip()
    # ハッシュタグや絵文字を除去してクリーンに
    title = re.sub(r'#\S+', '', first_line).strip()[:90]

    return {
        "post_num": f"TT-{index + 1:03d}",
        "title": title,
        "description": description,
        "image_urls": post["image_urls"],  # HTTPS + WebP/JPEGが必要
        "hook": post["title"],
        "cta_type": post["cta_type"],
        "format": post["format"],
        "ig_source": post["post_num"],
        "score": post["score"],
    }


# ── X変換 ────────────────────────────────────────────────────────────

def count_x_chars(text: str) -> int:
    """X (Twitter) のweighted文字数カウント"""
    url_pattern = re.compile(r'https?://\S+')
    urls = url_pattern.findall(text)
    text_no_urls = url_pattern.sub('', text)

    count = 0
    for char in text_no_urls:
        cp = ord(char)
        if (0x3000 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or
            0x20000 <= cp <= 0x2FFFF or 0x1F600 <= cp <= 0x1F9FF or
            0x1F300 <= cp <= 0x1F5FF):
            count += 2
        else:
            count += 1

    count += len(urls) * 23
    return count


def convert_to_x(post: dict, index: int) -> dict:
    """IG投稿をX用に変換"""
    caption = post["caption"]

    # 1. フック（最初の1-2行）を抽出
    lines = [l.strip() for l in caption.split("\n") if l.strip()]
    hook = lines[0] if lines else ""

    # 2. IG固有のハッシュタグ除去
    hook = re.sub(r'#(instagram|instagood|insta\w+)', '', hook, flags=re.IGNORECASE).strip()

    # 3. LP URLを追加
    lp_url = "https://cellosupport.xsrv.jp/clearahadaprogress-members/lp.php?utm_source=x&utm_medium=social"

    # 4. Xハッシュタグ（少なめ、3個以内）
    x_tags = "#スキンケア #ニキビ改善"

    # 5. 280文字以内に収める
    # URL=23カウント、タグ約30カウント → テキスト用に227カウント
    tweet_body = hook
    tweet_with_url = f"{tweet_body}\n\n{lp_url}\n\n{x_tags}"

    # 文字数チェック＆調整
    while count_x_chars(tweet_with_url) > 280 and len(tweet_body) > 10:
        tweet_body = tweet_body[:-1]
        tweet_with_url = f"{tweet_body}...\n\n{lp_url}\n\n{x_tags}"

    # 画像は最大4枚
    image_urls = post["image_urls"][:4]

    return {
        "post_num": f"X-{index + 1:03d}",
        "text": tweet_with_url,
        "image_urls": image_urls,
        "hook": post["title"],
        "cta_type": post["cta_type"],
        "format": post["format"],
        "ig_source": post["post_num"],
        "score": post["score"],
        "char_count": count_x_chars(tweet_with_url),
    }


# ── スプシ書き込み ─────────────────────────────────────────────────

def _col_idx_to_letter(idx: int) -> str:
    result = ""
    n = idx
    while True:
        result = chr(ord("A") + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def write_tiktok_posts(service, posts: list, start_date: str = None):
    """TikTokスプシに書き込み"""
    if TIKTOK_SPREADSHEET_ID == "PLACEHOLDER":
        print("⚠️ TIKTOK_SPREADSHEET_ID未設定 → スキップ")
        return

    now = datetime.now(JST)
    if not start_date:
        start_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    values = []
    date = datetime.strptime(start_date, "%Y-%m-%d")

    for i, post in enumerate(posts):
        time_slot = "20:00"  # TikTokベスト時間帯
        row = [
            date.strftime("%Y-%m-%d"),  # A: 日付
            "",                          # B: 空
            post["post_num"],            # C: 番号
            time_slot,                   # D: 時刻
            post["hook"],                # E: フック
            post["cta_type"],            # F: CTA型
            post["format"],              # G: 形式
            "",                          # H: URL (自動)
            "",                          # I: 意図
            "フォト",                    # J: タイプ
            f"IG#{post['ig_source']}",   # K: 備考
            post["description"],         # L: キャプション
            post["title"],               # M: タイトル
            "ready",                     # N: ステータス
            json.dumps(post["image_urls"]),  # O: 画像URLs
        ]
        values.append(row)
        date += timedelta(days=1)  # 1日1投稿

    service.spreadsheets().values().update(
        spreadsheetId=TIKTOK_SPREADSHEET_ID,
        range=f"{TIKTOK_SHEET_NAME}!A4",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    print(f"✅ TikTokスプシに{len(values)}件書き込み完了")


def write_x_posts(service, posts: list, start_date: str = None):
    """Xスプシに書き込み"""
    if X_SPREADSHEET_ID == "PLACEHOLDER":
        print("⚠️ X_SPREADSHEET_ID未設定 → スキップ")
        return

    now = datetime.now(JST)
    if not start_date:
        start_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    values = []
    date = datetime.strptime(start_date, "%Y-%m-%d")

    for i, post in enumerate(posts):
        time_slot = "12:00"  # X昼帯がベスト
        row = [
            date.strftime("%Y-%m-%d"),  # A: 日付
            "",                          # B: 空
            post["post_num"],            # C: 番号
            time_slot,                   # D: 時刻
            post["hook"],                # E: フック
            post["cta_type"],            # F: CTA型
            post["format"],              # G: 形式
            "",                          # H: URL (自動)
            "",                          # I: 意図
            "画像",                      # J: タイプ
            f"IG#{post['ig_source']}",   # K: 備考
            post["text"],                # L: テキスト
            "ready",                     # M: ステータス
            json.dumps(post["image_urls"]),  # N: 画像URLs
        ]
        values.append(row)
        date += timedelta(days=1)

    service.spreadsheets().values().update(
        spreadsheetId=X_SPREADSHEET_ID,
        range=f"{X_SHEET_NAME}!A4",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    print(f"✅ Xスプシに{len(values)}件書き込み完了")


# ── メイン ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IG→TikTok/X変換")
    parser.add_argument("--top", type=int, default=30, help="選定するTOP投稿数")
    parser.add_argument("--platform", choices=["tiktok", "x", "both"], default="both")
    parser.add_argument("--start-date", type=str, default=None,
                        help="投稿開始日(YYYY-MM-DD)。デフォルト=明日")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"🔄 IG→マルチPF変換 (TOP {args.top})")
    service = get_sheets_service()

    # 1. TOP投稿選定
    print(f"\n📊 IG投稿をスコアリング中...")
    top_posts = select_top_posts(service, args.top)
    print(f"   {len(top_posts)}件を選定")

    if not top_posts:
        print("❌ 対象投稿なし")
        return

    # TOP5を表示
    print(f"\n🏆 TOP 5:")
    for i, p in enumerate(top_posts[:5]):
        print(f"   {i+1}. #{p['post_num']} Score={p['score']} "
              f"R={p['reach']} S={p['saves']} Sh={p['shares']}")
        print(f"      {p['title'][:50]}...")

    # 2. TikTok変換
    if args.platform in ("tiktok", "both"):
        print(f"\n🎵 TikTok用に変換中...")
        tiktok_posts = [convert_to_tiktok(p, i) for i, p in enumerate(top_posts)]
        for tp in tiktok_posts[:3]:
            print(f"   {tp['post_num']}: {tp['title'][:40]}... ({len(tp['image_urls'])}枚)")

        if not args.dry_run:
            write_tiktok_posts(service, tiktok_posts, args.start_date)
        else:
            print(f"   [DRY RUN] {len(tiktok_posts)}件をスキップ")

    # 3. X変換
    if args.platform in ("x", "both"):
        print(f"\n🐦 X用に変換中...")
        x_posts = [convert_to_x(p, i) for i, p in enumerate(top_posts)]
        for xp in x_posts[:3]:
            print(f"   {xp['post_num']}: {xp['text'][:40]}... ({xp['char_count']}chars, {len(xp['image_urls'])}枚)")

        if not args.dry_run:
            write_x_posts(service, x_posts, args.start_date)
        else:
            print(f"   [DRY RUN] {len(x_posts)}件をスキップ")

    print(f"\n🎉 完了")


if __name__ == "__main__":
    main()
