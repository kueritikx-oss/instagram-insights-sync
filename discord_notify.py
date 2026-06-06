#!/usr/bin/env python3
"""
Discord通知共通モジュール

全自動化スクリプトから呼ばれる。通知レベルごとにWebhookを出し分け、
Embed形式・レート制限対応・retry付き。Webhook未設定でも落ちない(graceful degrade)。

環境変数(優先順):
    DISCORD_WEBHOOK            # 単一Webhook運用。下記が未設定なら全レベルのフォールバック
    DISCORD_WEBHOOK_CRITICAL   # 🚨 Critical (投稿失敗/トークン切れ/DB破損)
    DISCORD_WEBHOOK_URL        # ⚠️ Warning (Freshness/同期遅延) ← デフォルト
    DISCORD_WEBHOOK_INFO       # ℹ️ Info    (投稿完了/Healthcheck OK)
    DISCORD_WEBHOOK_REPORTS    # 📊 Reports (週次/月次/日次サマリ)
    DISCORD_WEBHOOK_X_MONITOR  # 🐦 X初動監視専用(既存互換)

Usage:
    from discord_notify import notify, critical, warn, info, report

    notify("タイトル", "メッセージ", level="warn")
    critical("X投稿失敗", "Google Token切れ", fields={"retry": 3, "last": "13:20"})
    warn("Threads Freshness", f"直近24h {n}件", fields={"閾値": 2, "実測": n})
    info("note自動公開完了", url)
    report("週次戦略レポート", summary)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# .env を自動ロード(存在すれば)
# 注: automation_config.env はbashスクリプト用なのでdotenvでは読まない
try:
    from dotenv import load_dotenv  # type: ignore
    env_path = Path("/Users/taiki/Projects/事業/.env")
    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass


JST = timezone(timedelta(hours=9))

# Deduplication: 同じ (title, level) が同一ハッシュで1分以内なら抑制
# ファイルベースのため並列プロセス間でも効く
DEDUPE_STATE_DIR = Path.home() / ".cache" / "tackey_discord_notify"
DEDUPE_STATE_DIR.mkdir(parents=True, exist_ok=True)
DEDUPE_DEFAULT_SEC = 600  # 10分以内の重複は抑制 (critical のみ1分)

# Embed color (Discord RGB int)
COLOR = {
    "critical": 0xE74C3C,  # 赤
    "warn":     0xF39C12,  # 橙
    "info":     0x3498DB,  # 青
    "ok":       0x2ECC71,  # 緑
    "report":   0x9B59B6,  # 紫
}

EMOJI = {
    "critical": "🚨",
    "warn":     "⚠️",
    "info":     "ℹ️",
    "ok":       "✅",
    "report":   "📊",
}


def _webhook_url(level: str) -> str | None:
    """レベル対応のWebhook URL取得。未設定なら既定URLにフォールバック"""
    generic = os.environ.get("DISCORD_WEBHOOK")
    mapping = {
        "critical": os.environ.get("DISCORD_WEBHOOK_CRITICAL") or generic,
        "warn":     os.environ.get("DISCORD_WEBHOOK_URL") or generic,
        "info":     os.environ.get("DISCORD_WEBHOOK_INFO") or generic,
        "ok":       os.environ.get("DISCORD_WEBHOOK_INFO") or generic,
        "report":   os.environ.get("DISCORD_WEBHOOK_REPORTS") or generic,
        "x":        os.environ.get("DISCORD_WEBHOOK_X_MONITOR") or generic,
    }
    url = mapping.get(level)
    if url:
        return url
    # フォールバック: warning用既定URL
    return os.environ.get("DISCORD_WEBHOOK_URL") or generic


def _dedupe_check(title: str, level: str, dedupe_sec: int) -> bool:
    """
    重複抑制チェック。True = 抑制する(通知しない), False = 送信する
    (title, level) をkey、最終送信時刻をファイル保存
    """
    if dedupe_sec <= 0:
        return False
    import hashlib
    key = hashlib.md5(f"{title}|{level}".encode()).hexdigest()[:16]
    state_file = DEDUPE_STATE_DIR / f"{key}.last"
    try:
        if state_file.exists():
            last = float(state_file.read_text().strip())
            if time.time() - last < dedupe_sec:
                return True  # 抑制
        state_file.write_text(str(time.time()))
    except Exception:
        pass
    return False


def notify(
    title: str,
    message: str = "",
    level: str = "warn",
    fields: dict | None = None,
    mention: bool = False,
    url: str | None = None,
    footer: str | None = None,
    silent: bool = False,
    dedupe_sec: int | None = None,
) -> bool:
    """
    Discord通知を送信。

    Args:
        title: Embedタイトル(256文字まで)
        message: Embed本文(4000文字まで)
        level: critical/warn/info/ok/report/x
        fields: {"キー": "値"} の辞書(最大25件、inline表示)
        mention: True かつ level=critical なら @here をつける
        url: (オプション) Embedのclickable title URL
        footer: (オプション) 追加footer text
        silent: True なら送信ログを出さない
        dedupe_sec: 同じtitle+levelをN秒以内に繰り返し送らない(デフォルト level別)

    Returns:
        成功=True, 失敗or未設定or重複抑制=False
    """
    # デフォルトのdedupe閾値 (level別)
    if dedupe_sec is None:
        dedupe_sec = {
            "critical": 60,    # criticalは1分 (すぐ対応したいが連発は抑制)
            "warn":     600,   # 10分
            "info":     1800,  # 30分 (頻度低めでOK)
            "ok":       1800,
            "report":   0,     # レポートは重複OK (週次/月次なので)
            "x":        600,
        }.get(level, 600)

    if _dedupe_check(title, level, dedupe_sec):
        if not silent:
            print(f"  🔕 Discord通知抑制(dedupe) [{level}] {title[:40]}", file=sys.stderr)
        return False

    webhook = _webhook_url(level) if level in ["critical", "warn", "info", "ok", "report", "x"] \
        else _webhook_url("warn")
    if not webhook:
        if not silent:
            print(f"  ⚠️ DISCORD_WEBHOOK_* 未設定 (level={level})。通知スキップ", file=sys.stderr)
        return False

    emoji = EMOJI.get(level, "")
    full_title = f"{emoji} {title}"[:256]

    embed = {
        "title": full_title,
        "description": message[:4000] if message else "",
        "color": COLOR.get(level, COLOR["warn"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": footer or f"タッキー事業 / {level.upper()} / {datetime.now(JST).strftime('%m/%d %H:%M JST')}"},
    }
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [
            {"name": str(k)[:256], "value": str(v)[:1024], "inline": True}
            for k, v in list(fields.items())[:25]
        ]

    payload = {"embeds": [embed]}
    if mention and level == "critical":
        payload["content"] = "@here"

    # 3回まで指数バックオフ retry, 429 は retry_after尊重
    for attempt in range(3):
        try:
            r = requests.post(webhook, json=payload, timeout=10)
            if r.status_code < 300:
                return True
            if r.status_code == 429:
                retry_after = float(r.json().get("retry_after", 2))
                time.sleep(retry_after + 0.5)
                continue
            if not silent:
                print(f"  ⚠️ Discord送信失敗 HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            if not silent:
                print(f"  ⚠️ Discord送信例外(attempt={attempt+1}): {e}", file=sys.stderr)
        time.sleep(2 ** attempt)
    return False


# --- ショートカット関数 -------------------------------------------------------

def critical(title: str, message: str = "", **kwargs) -> bool:
    """🚨 Critical: 即対応。@here mention自動"""
    kwargs.setdefault("mention", True)
    return notify(title, message, level="critical", **kwargs)


def warn(title: str, message: str = "", **kwargs) -> bool:
    """⚠️ Warning: データ欠損・閾値違反など"""
    return notify(title, message, level="warn", **kwargs)


def info(title: str, message: str = "", **kwargs) -> bool:
    """ℹ️ Info: 投稿完了・ヘルスOKなど情報系"""
    return notify(title, message, level="info", **kwargs)


def ok(title: str, message: str = "", **kwargs) -> bool:
    """✅ OK: 処理成功の完了通知"""
    return notify(title, message, level="ok", **kwargs)


def report(title: str, message: str = "", **kwargs) -> bool:
    """📊 Report: 週次/月次/日次サマリ"""
    return notify(title, message, level="report", **kwargs)


# --- 疎通テスト ---------------------------------------------------------------

def test_all_webhooks():
    """設定されている全Webhookに test通知を送る(セットアップ検証用)"""
    tested = []
    for level in ["critical", "warn", "info", "report", "x"]:
        url = _webhook_url(level)
        if url:
            ok_ = notify(
                f"[TEST] {level.upper()} webhook疎通確認",
                f"このメッセージが表示されていればWebhook `{level}` は正常稼働中。\n"
                f"2026-04-16 全自動化インフラ構築",
                level=level if level != "x" else "info",
                fields={"環境": "Local", "用途": level, "Python": f"{sys.version_info.major}.{sys.version_info.minor}"},
            )
            tested.append((level, "✅" if ok_ else "❌"))
        else:
            tested.append((level, "(未設定)"))

    print("\n=== Discord Webhook 疎通テスト結果 ===")
    for lvl, status in tested:
        print(f"  {lvl:10s} {status}")

    has_any = any(s == "✅" for _, s in tested)
    if not has_any:
        print("\n❌ どのレベルもWebhookが設定されていません。")
        print("   → ~/.env または /Users/taiki/Projects/事業/.env に DISCORD_WEBHOOK_URL 等を追記してください")
        print("   → 手順書: _ログ/Discord_セットアップ_2026-04-16.md")
    return has_any


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Discord通知テスト")
    parser.add_argument("--test", action="store_true", help="全Webhookに疎通テスト送信")
    parser.add_argument("--critical", action="store_true", help="critical levelで1通送る")
    parser.add_argument("--warn", action="store_true", help="warn levelで1通送る")
    parser.add_argument("--info", action="store_true", help="info levelで1通送る")
    parser.add_argument("--report", action="store_true", help="report levelで1通送る")
    parser.add_argument("--level", choices=["critical", "warn", "info", "ok", "report", "x"], help="任意levelで1通送る")
    parser.add_argument("--title", default="テスト通知")
    parser.add_argument("--message", default="discord_notify.py からのテストメッセージ")
    args = parser.parse_args()

    if args.test:
        sys.exit(0 if test_all_webhooks() else 1)

    for lvl in ["critical", "warn", "info", "report"]:
        if getattr(args, lvl):
            ok_ = notify(args.title, args.message, level=lvl)
            print(f"{lvl}: {'OK' if ok_ else 'FAILED'}")
            sys.exit(0 if ok_ else 1)
    if args.level:
        ok_ = notify(args.title, args.message, level=args.level)
        print(f"{args.level}: {'OK' if ok_ else 'FAILED'}")
        sys.exit(0 if ok_ else 1)

    # デフォルト: warn で1通
    notify(args.title, args.message, level="warn")
