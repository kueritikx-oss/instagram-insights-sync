#!/usr/bin/env python3
"""
instagram_comment_dm.py — Comment→DM→LINE 自動パイプライン（フォールバック）

Primary: Cloudflare Workers Webhook（1-8秒で即時応答）
Fallback: このスクリプト（GitHub Actions 15分ポーリング、取りこぼし補完）

1. 直近投稿のコメントを取得
2. キーワードマッチしたコメントを検出
3. 未処理コメントに対してDM送信（LINE誘導リンク付き）
4. 処理済みをスプレッドシートに記録

Architecture:
    1. 直近N投稿のコメントをポーリング
    2. キーワード部分一致でトリガー
    3. IGSID取得 → Messaging API でDM送信
    4. 24時間ウィンドウ内チェック
    5. 重複送信防止（スプシで追跡 — Webhook処理済みもスキップ）
    6. レート制限遵守（200 DM/時間）

Usage:
    python3 utils/instagram_comment_dm.py              # 通常実行
    python3 utils/instagram_comment_dm.py --dry-run     # DM送信しない（確認のみ）
    python3 utils/instagram_comment_dm.py --check        # 直近コメント一覧表示
    python3 utils/instagram_comment_dm.py --setup        # スプシにトラッキングタブを作成
    python3 utils/instagram_comment_dm.py --stats        # DM送信統計を表示
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))
GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# スプレッドシート（既存の投稿毎データと同じID）
SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
TRACKING_TAB = "Comment→DM追跡"

# ポーリング設定
LOOKBACK_POSTS = 10          # 直近何投稿をチェックするか
LOOKBACK_HOURS = 24          # コメントの24時間ウィンドウ
MAX_DMS_PER_RUN = 20         # 1実行あたりの最大DM数（レート制限対策）
COMMENT_REPLY_DELAY = 2      # コメント返信間のディレイ（秒）

# LINE誘導URL（UTMパラメータ付き）
# 環境変数 LINE_URL で上書き可能
DEFAULT_LINE_URL = "https://lin.ee/HuELfSJ"

# ---------------------------------------------------------------------------
# キーワード設定
# キーワードを含むコメントにDMを送る。大文字小文字・ひらがなカタカナ区別なし。
# スプシの「Comment→DM設定」タブでも上書き可能。
# ---------------------------------------------------------------------------
DEFAULT_KEYWORDS = [
    "知りたい", "教えて", "欲しい", "ほしい",
    "気になる", "詳しく", "やり方", "方法",
    "LINE", "ライン", "らいん",
    "プレゼント", "資料", "まとめ",
    "DM", "ディーエム",
]

# ---------------------------------------------------------------------------
# DMテンプレート
# {keyword} = マッチしたキーワード
# {line_url} = LINE誘導URL
# {post_title} = 投稿タイトル（取得できれば）
# ---------------------------------------------------------------------------
DM_TEMPLATE = """\
{username}さん、コメントありがとうございます！

詳しい内容をLINEでお送りしています☺️

▼ こちらから受け取れます
{line_url}

お気軽にメッセージくださいね！

※ こちらはコメントへの自動返信です"""

# バリエーション（スパム判定回避: ランダムで選択）
DM_TEMPLATES = [
    """\
{username}さん、コメントありがとうございます！

詳しい内容をLINEでまとめています☺️

▼ こちらから受け取れます
{line_url}

気軽にメッセージくださいね！

※ コメントへの自動返信です""",
    """\
{username}さん！コメントうれしいです✨

もっと詳しい情報はLINEでお届けしてます！

▼ タップして受け取る
{line_url}

何か質問あればLINEで聞いてくださいね☺️

※ こちらは自動返信です""",
    """\
{username}さん、ありがとうございます！

リクエストいただいた内容、LINEで無料でお送りしてます☺️

▼ こちらからどうぞ
{line_url}

お気軽にどうぞ！

※ 自動返信でお届けしています""",
]

# コメントへの公開返信テンプレート（任意）
COMMENT_REPLY_TEMPLATE = "DMしました！✨"


# ---------------------------------------------------------------------------
# Auth（cloud_auto_post.py と同じパターン）
# ---------------------------------------------------------------------------
def get_instagram_config() -> tuple[str, str]:
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_user_id = os.environ.get("INSTAGRAM_IG_USER_ID")
    if not access_token:
        config_path = os.environ.get(
            "INSTAGRAM_CONFIG_PATH",
            os.path.expanduser(
                "~/Projects/事業/タッキー/02_SNS集客/instagram-auto-post/"
                "instagram_insights_config.json"
            ),
        )
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
            access_token = config.get("access_token")
            ig_user_id = config.get("ig_user_id")
    if not access_token or not ig_user_id:
        raise RuntimeError("Instagram credentials not found")
    return access_token, ig_user_id


def get_sheets_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
    else:
        token_path = os.environ.get(
            "GOOGLE_TOKEN_PATH",
            os.path.expanduser(
                "~/Projects/事業/タッキー/02_SNS集客/instagram-auto-post/token.json"
            ),
        )
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
# Instagram Graph API helpers
# ---------------------------------------------------------------------------
def api_get(endpoint: str, params: dict, token: str) -> dict:
    params["access_token"] = token
    r = requests.get(f"{GRAPH_API_BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(endpoint: str, data: dict, token: str) -> dict:
    data["access_token"] = token
    r = requests.post(f"{GRAPH_API_BASE}/{endpoint}", json=data, timeout=30)
    if r.status_code != 200:
        print(f"  ❌ API POST error: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()


def get_recent_media(token: str, ig_user_id: str, limit: int = 10) -> list[dict]:
    """直近の投稿一覧を取得"""
    data = api_get(
        f"{ig_user_id}/media",
        {"fields": "id,caption,timestamp,permalink,media_type", "limit": limit},
        token,
    )
    return data.get("data", [])


def get_comments(token: str, media_id: str) -> list[dict]:
    """投稿のコメント一覧を取得（from.id = IGSID付き）"""
    comments = []
    url = f"{GRAPH_API_BASE}/{media_id}/comments"
    params = {
        "fields": "id,from,text,timestamp,like_count",
        "limit": 50,
        "access_token": token,
    }
    while url:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        comments.extend(data.get("data", []))
        # ページネーション
        url = data.get("paging", {}).get("next")
        params = {}  # next URLにはパラメータが含まれている
    return comments


def send_dm(token: str, ig_user_id: str, recipient_id: str, message: str) -> dict:
    """Instagram DM を送信"""
    return api_post(
        f"{ig_user_id}/messages",
        {
            "recipient": {"id": recipient_id},
            "message": {"text": message},
        },
        token,
    )


def reply_to_comment(token: str, comment_id: str, message: str) -> dict:
    """コメントに公開返信"""
    return api_post(
        f"{comment_id}/replies",
        {"message": message},
        token,
    )


# ---------------------------------------------------------------------------
# キーワードマッチング
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """ひらがな・カタカナ・大文字小文字を統一"""
    import unicodedata
    text = text.lower()
    text = unicodedata.normalize("NFKC", text)
    # カタカナ→ひらがな変換
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x30A1 <= cp <= 0x30F6:  # カタカナ範囲
            result.append(chr(cp - 0x60))
        else:
            result.append(ch)
    return "".join(result)


def match_keyword(comment_text: str, keywords: list[str]) -> Optional[str]:
    """コメントにキーワードが含まれていればマッチしたキーワードを返す"""
    normalized = normalize_text(comment_text)
    for kw in keywords:
        if normalize_text(kw) in normalized:
            return kw
    return None


# ---------------------------------------------------------------------------
# スプレッドシート追跡
# ---------------------------------------------------------------------------
def get_sent_comment_ids(sheets_service) -> set[str]:
    """既にDM送信済みのコメントIDセットを取得"""
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{TRACKING_TAB}'!D:D",
        ).execute()
        values = result.get("values", [])
        return {row[0] for row in values[1:] if row}  # ヘッダー除外
    except Exception:
        return set()


def append_tracking_row(sheets_service, row: list):
    """追跡タブに1行追加"""
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{TRACKING_TAB}'!A:K",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def setup_tracking_tab(sheets_service):
    """追跡タブを新規作成"""
    # タブが存在するかチェック
    meta = sheets_service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing_tabs = [s["properties"]["title"] for s in meta["sheets"]]

    if TRACKING_TAB in existing_tabs:
        print(f"  ℹ️  タブ「{TRACKING_TAB}」は既に存在します")
        return

    # タブ作成
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": TRACKING_TAB,
                            "gridProperties": {"frozenRowCount": 1},
                        }
                    }
                }
            ]
        },
    ).execute()

    # ヘッダー行
    headers = [
        "日時",              # A: DM送信日時
        "投稿ID",            # B: media_id
        "投稿URL",           # C: permalink
        "コメントID",        # D: comment_id（重複チェック用）
        "IGSID",             # E: コメント主のIGSID
        "ユーザー名",        # F: コメント主のusername
        "コメント本文",      # G: コメントの全文
        "マッチキーワード",  # H: どのキーワードにマッチしたか
        "DMステータス",      # I: sent / failed / dry-run
        "LINE UTM",          # J: 送信したUTM付きURL
        "備考",              # K: エラー詳細等
    ]
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TRACKING_TAB}'!A1:K1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()
    print(f"  ✅ タブ「{TRACKING_TAB}」を作成しました")


# ---------------------------------------------------------------------------
# メインロジック
# ---------------------------------------------------------------------------
def run(dry_run: bool = False, check_only: bool = False, stats_only: bool = False):
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    print(f"{'='*60}")
    print(f"📨 Comment→DM→LINE パイプライン — {now.strftime('%Y-%m-%d %H:%M JST')}")
    if dry_run:
        print("   🔸 DRY-RUN モード（DM送信しない）")
    print(f"{'='*60}")

    # 認証
    token, ig_user_id = get_instagram_config()
    sheets = get_sheets_service()
    line_url = os.environ.get("LINE_URL", DEFAULT_LINE_URL)

    if stats_only:
        show_stats(sheets)
        return

    # 既送信コメントIDセット
    sent_ids = get_sent_comment_ids(sheets)
    print(f"\n📋 既送信コメント: {len(sent_ids)}件")

    # 直近投稿を取得
    media_list = get_recent_media(token, ig_user_id, limit=LOOKBACK_POSTS)
    print(f"📸 チェック対象投稿: {len(media_list)}件（直近{LOOKBACK_POSTS}投稿）")

    dm_count = 0
    new_comments_total = 0
    matched_total = 0

    for media in media_list:
        media_id = media["id"]
        permalink = media.get("permalink", "")
        caption_preview = (media.get("caption") or "")[:50]

        # コメント取得
        comments = get_comments(token, media_id)
        if not comments:
            continue

        # 24時間以内のコメントだけフィルタ
        recent_comments = []
        for c in comments:
            ts = datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00"))
            if ts >= cutoff.astimezone(timezone.utc):
                recent_comments.append(c)

        if not recent_comments:
            continue

        new_comments_total += len(recent_comments)

        if check_only:
            print(f"\n📝 {permalink}")
            print(f"   キャプション: {caption_preview}...")
            for c in recent_comments:
                from_user = c.get("from", {})
                username = from_user.get("username", "???")
                already = "✅送信済" if c["id"] in sent_ids else "🆕未処理"
                kw = match_keyword(c.get("text", ""), DEFAULT_KEYWORDS)
                kw_label = f"🔑{kw}" if kw else "—"
                print(f"   {already} @{username}: {c['text'][:60]} [{kw_label}]")
            continue

        # キーワードマッチ & DM送信
        for c in recent_comments:
            if c["id"] in sent_ids:
                continue

            comment_text = c.get("text", "")
            kw = match_keyword(comment_text, DEFAULT_KEYWORDS)
            if not kw:
                continue

            matched_total += 1
            from_user = c.get("from", {})
            igsid = from_user.get("id")
            username = from_user.get("username", "unknown")

            if not igsid:
                print(f"  ⚠️ @{username}: IGSID取得不可（権限不足の可能性）")
                append_tracking_row(sheets, [
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    media_id, permalink, c["id"],
                    "", username, comment_text, kw,
                    "failed", "", "IGSID取得不可",
                ])
                continue

            # UTMパラメータ付きLINE URL
            utm_url = (
                f"{line_url}?utm_source=instagram&utm_medium=comment_dm"
                f"&utm_campaign=auto&utm_content={kw}"
            )

            # DM本文（テンプレートをランダム選択 → スパム判定回避）
            import random
            template = random.choice(DM_TEMPLATES)
            dm_text = template.format(
                username=username,
                keyword=kw,
                line_url=utm_url,
                post_title=caption_preview,
            )

            if dry_run:
                print(f"  🔸 [DRY-RUN] @{username} にDM送信予定（キーワード: {kw}）")
                append_tracking_row(sheets, [
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    media_id, permalink, c["id"],
                    igsid, username, comment_text, kw,
                    "dry-run", utm_url, "",
                ])
                dm_count += 1
                continue

            # DM送信
            try:
                result = send_dm(token, ig_user_id, igsid, dm_text)
                print(f"  ✅ @{username} にDM送信完了（キーワード: {kw}）")
                status = "sent"
                note = f"message_id: {result.get('message_id', '')}"
            except requests.HTTPError as e:
                print(f"  ❌ @{username} へのDM送信失敗: {e}")
                status = "failed"
                note = str(e)[:200]

            # 追跡記録
            append_tracking_row(sheets, [
                now.strftime("%Y-%m-%d %H:%M:%S"),
                media_id, permalink, c["id"],
                igsid, username, comment_text, kw,
                status, utm_url, note,
            ])

            dm_count += 1
            if dm_count >= MAX_DMS_PER_RUN:
                print(f"\n  ⚠️ 1実行あたりの上限({MAX_DMS_PER_RUN})に到達。残りは次回実行で処理")
                break

            time.sleep(COMMENT_REPLY_DELAY)

        if dm_count >= MAX_DMS_PER_RUN:
            break

    # サマリー
    print(f"\n{'='*60}")
    print(f"📊 実行結果:")
    print(f"   チェック投稿数: {len(media_list)}")
    print(f"   24h以内のコメント: {new_comments_total}件")
    print(f"   キーワードマッチ: {matched_total}件")
    print(f"   DM送信{'予定' if dry_run else '完了'}: {dm_count}件")
    print(f"{'='*60}")


def show_stats(sheets_service):
    """DM送信統計を表示"""
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{TRACKING_TAB}'!A:K",
        ).execute()
        rows = result.get("values", [])
    except Exception:
        print("  ❌ 追跡タブが見つかりません。--setup で作成してください")
        return

    if len(rows) <= 1:
        print("  ℹ️  まだデータがありません")
        return

    data = rows[1:]
    total = len(data)
    sent = sum(1 for r in data if len(r) > 8 and r[8] == "sent")
    failed = sum(1 for r in data if len(r) > 8 and r[8] == "failed")
    dry = sum(1 for r in data if len(r) > 8 and r[8] == "dry-run")

    # ユニークユーザー
    users = {r[5] for r in data if len(r) > 5 and r[5]}

    print(f"\n📊 Comment→DM 統計:")
    print(f"   総処理数: {total}")
    print(f"   DM送信成功: {sent}")
    print(f"   DM送信失敗: {failed}")
    print(f"   Dry-run: {dry}")
    print(f"   ユニークユーザー: {len(users)}")

    # キーワード別集計
    kw_counts: dict[str, int] = {}
    for r in data:
        if len(r) > 7 and r[7]:
            kw_counts[r[7]] = kw_counts.get(r[7], 0) + 1
    if kw_counts:
        print(f"\n   🔑 キーワード別:")
        for kw, count in sorted(kw_counts.items(), key=lambda x: -x[1]):
            print(f"      {kw}: {count}件")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Comment→DM→LINE パイプライン")
    parser.add_argument("--dry-run", action="store_true", help="DM送信しない（確認のみ）")
    parser.add_argument("--check", action="store_true", help="直近コメント一覧表示")
    parser.add_argument("--setup", action="store_true", help="スプシにトラッキングタブを作成")
    parser.add_argument("--stats", action="store_true", help="DM送信統計を表示")
    args = parser.parse_args()

    if args.setup:
        sheets = get_sheets_service()
        setup_tracking_tab(sheets)
        return

    run(dry_run=args.dry_run, check_only=args.check, stats_only=args.stats)


if __name__ == "__main__":
    main()
