#!/usr/bin/env python3
"""データ鮮度チェック (Freshness SLO監視)。
重要スプシの最新データが閾値を超えて古い場合、Discordに警告を送る。

Usage:
  python3 scripts/freshness_check.py              # 実行
  python3 scripts/freshness_check.py --dry-run    # 検証のみ（通知なし）

これは sync ワークフローから独立して動く多層防御の一部。
sync 自体が死んでも、こちらが鮮度低下を検知してアラートできる。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

DAILY_SHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"

# Freshness SLOチェック対象
# expected_latest_day_offset: 最新非空データは何日前以内にあるべきか（0=今日, 1=昨日）
CHECKS = [
    {
        "name": "IGフォロー増減 (日ごとデータ E/F/G列)",
        "sheet_id": DAILY_SHEET_ID,
        "tab": "日ごとデータ",
        "cols": ["E", "F", "G"],
        "expected_latest_offset": 1,  # 昨日分までは入ってるべき
        "source": "sync_instagram_account_insights.py",
    },
    {
        "name": "ブログPV (日ごとデータ AM列)",
        "sheet_id": DAILY_SHEET_ID,
        "tab": "日ごとデータ",
        "cols": ["AM"],
        "expected_latest_offset": 1,
        "source": "sync_ga4_daily.py",
    },
    {
        "name": "LPリスト (日ごとデータ AP列)",
        "sheet_id": DAILY_SHEET_ID,
        "tab": "日ごとデータ",
        "cols": ["AP"],
        "expected_latest_offset": 1,
        "source": "sync_lstep_daily.py",
    },
]


def get_sheets_service():
    """GA4_TOKEN_JSON / GOOGLE_TOKEN_JSON の順で認証を試みる"""
    scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    token_dir = Path("auth")
    token_dir.mkdir(exist_ok=True)

    for env_name, filename in [
        ("GA4_TOKEN_JSON", "ga4_token.json"),
        ("GOOGLE_TOKEN_JSON", "token.json"),
    ]:
        token_json = os.environ.get(env_name)
        if token_json:
            path = token_dir / filename
            path.write_text(token_json)
            try:
                creds = Credentials.from_authorized_user_file(str(path), scopes)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                return build("sheets", "v4", credentials=creds)
            except Exception as e:
                print(f"⚠️ {env_name} 認証失敗: {e}", file=sys.stderr)
                continue
    # ローカル fallback
    for path in [
        Path("タッキー/02_SNS集客/instagram-auto-post/ga4_token.json"),
        Path("/Users/taiki/Projects/事業/タッキー/02_SNS集客/instagram-auto-post/ga4_token.json"),
    ]:
        if path.exists():
            creds = Credentials.from_authorized_user_file(str(path), scopes)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return build("sheets", "v4", credentials=creds)
    raise RuntimeError("Google認証情報が見つかりません")


def parse_sheet_date(cell: str, target_year_hint: int) -> datetime | None:
    """'4/13 月' → datetime(2026, 4, 13) のように解釈。年は target_year_hint ベースで曜日一致を検証。"""
    m = re.search(r"(\d{1,2})/(\d{1,2})", cell)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    wd_match = re.search(r"[月火水木金土日]", cell)
    # 年ヒントから前後数年を試して曜日一致する年を返す
    for yr in [target_year_hint, target_year_hint - 1, target_year_hint + 1, target_year_hint - 2]:
        try:
            dt = datetime(yr, month, day)
        except ValueError:
            continue
        if wd_match:
            if WEEKDAY_JP[dt.weekday()] == wd_match.group():
                return dt
        else:
            return dt
    return None


def check_freshness(svc, check: dict) -> dict:
    """1つのチェック対象の鮮度確認"""
    now_jst = datetime.now(JST).replace(tzinfo=None)
    expected_date = (now_jst - timedelta(days=check["expected_latest_offset"])).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # A列 + データ列 を読む
    col_range = f"{check['cols'][0]}:{check['cols'][-1]}"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=check["sheet_id"],
        range=f"'{check['tab']}'!A:{check['cols'][-1]}",
    ).execute()
    rows = resp.get("values", [])

    # 後ろから遡って「非空データがある最新行の日付」を探す
    latest_date: datetime | None = None
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if not row or len(row) < 2:
            continue
        a_cell = row[0] if row else ""
        # データ列に値があるか
        has_data = False
        # 列オフセット（Aからの相対）: E=4, F=5, G=6, AM=38 ...
        for col_letter in check["cols"]:
            col_idx = 0
            for c in col_letter:
                col_idx = col_idx * 26 + (ord(c) - ord('A') + 1)
            col_idx -= 1
            if col_idx < len(row) and row[col_idx] and str(row[col_idx]).strip():
                has_data = True
                break
        if not has_data:
            continue
        # A列から日付取得
        dt = parse_sheet_date(a_cell, now_jst.year)
        if dt:
            latest_date = dt
            break

    result = {
        "name": check["name"],
        "source": check["source"],
        "latest_date": latest_date.strftime("%Y-%m-%d") if latest_date else None,
        "expected_date": expected_date.strftime("%Y-%m-%d"),
        "is_fresh": latest_date is not None and latest_date >= expected_date,
        "days_behind": (expected_date - latest_date).days if latest_date else 999,
    }
    return result


def send_discord_alert(webhook_url: str, stale: list):
    if not webhook_url:
        print("DISCORD_WEBHOOK 未設定 → 通知スキップ")
        return
    fields = []
    for s in stale:
        fields.append({
            "name": s["name"],
            "value": (
                f"最終更新: {s['latest_date'] or '（なし）'}\n"
                f"期待: {s['expected_date']}\n"
                f"遅延: {s['days_behind']}日\n"
                f"原因候補: `{s['source']}` が止まっている"
            ),
            "inline": False,
        })
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "username": "Data Freshness Alert",
        "embeds": [{
            "title": f"🟡 鮮度低下: {len(stale)}指標",
            "description": "いずれかの同期が止まっている可能性があります。",
            "color": 16763904,  # amber
            "fields": fields,
            "footer": {"text": "freshness_check.py"},
            "timestamp": now_iso,
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10).raise_for_status()
        print(f"🟡 Discord通知送信: {len(stale)}件")
    except Exception as e:
        print(f"⚠️ Discord通知失敗: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=== Data Freshness Check ===\n")
    svc = get_sheets_service()
    results = []
    for c in CHECKS:
        r = check_freshness(svc, c)
        results.append(r)
        status = "✅" if r["is_fresh"] else "🟡"
        print(f"  {status} {r['name']}: latest={r['latest_date']} expected={r['expected_date']} 遅延={r['days_behind']}日")

    stale = [r for r in results if not r["is_fresh"]]
    print(f"\n{'=' * 50}")
    print(f"鮮度OK: {len(results) - len(stale)}/{len(results)}  / 鮮度NG: {len(stale)}")

    if stale and not args.dry_run:
        send_discord_alert(os.environ.get("DISCORD_WEBHOOK", ""), stale)

    # 1件でも鮮度NG → exit 2 でワークフロー失敗扱い
    if stale:
        sys.exit(2)


if __name__ == "__main__":
    main()
