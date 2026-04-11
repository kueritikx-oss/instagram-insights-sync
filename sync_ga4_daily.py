#!/usr/bin/env python3
"""
GA4 日次PVデータをスプレッドシート「日ごとデータ」のAM列に書き込む。

Usage:
  python3 utils/sync_ga4_daily.py --setup        # 初回: OAuth認証 + プロパティID取得
  python3 utils/sync_ga4_daily.py                 # 日次: 昨日分のPVを取得して書き込み
  python3 utils/sync_ga4_daily.py --days 7        # 過去7日分バックフィル
  python3 utils/sync_ga4_daily.py --date 2026-04-10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as build_service


# ========== パス ==========
BASE_DIR = Path(os.environ.get(
    "GA4_BASE_DIR",
    str(Path.home() / "Projects" / "事業")
)).expanduser()

GOOGLE_AUTH_DIR = Path(os.environ.get(
    "GA4_GOOGLE_AUTH_DIR",
    str(BASE_DIR / "タッキー/02_SNS集客/instagram-auto-post")
)).expanduser()

CREDS_FILE = GOOGLE_AUTH_DIR / "credentials.json"
GA4_TOKEN_FILE = GOOGLE_AUTH_DIR / "ga4_token.json"
GA4_CONFIG_FILE = GOOGLE_AUTH_DIR / "ga4_config.json"

# GA4 + Sheets の両方のスコープ
GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ========== スプレッドシート ==========
DAILY_SHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
DAILY_TAB = "日ごとデータ"

# ブログのPV数セクション
COL_PV_TOTAL = 38   # AM: 全体
COL_PV_IG = 39      # AN: Instagram
COL_PV_TIKTOK = 40  # AO: TikTok

# リスト数セクション
COL_LIST_TOTAL = 41  # AP: 全体
COL_LIST_IG = 42     # AQ: Instagram
COL_LIST_TIKTOK = 43 # AR: TikTok

# CVRセクション
COL_CVR_TOTAL = 44   # AS: 全体
COL_CVR_IG = 45      # AT: Instagram
COL_CVR_TIKTOK = 46  # AU: TikTok

# ========== 曜日 ==========
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]
JST = timezone(timedelta(hours=9))


def get_ga4_credentials() -> Credentials:
    """GA4用OAuth認証"""
    creds = None
    if GA4_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GA4_TOKEN_FILE), GA4_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(f"credentials.json が見つかりません: {CREDS_FILE}")
            from google_oauth_helper import force_system_browser
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GA4_SCOPES)
            with force_system_browser():
                creds = flow.run_local_server(port=8098, prompt="consent")

        GA4_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with GA4_TOKEN_FILE.open("w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def load_ga4_config() -> Dict[str, Any]:
    """GA4設定ファイルを読む"""
    if GA4_CONFIG_FILE.exists():
        with open(GA4_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    # 環境変数フォールバック
    prop_id = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if prop_id:
        return {"property_id": prop_id}
    return {}


# 旧プロパティ（2025-01 〜 2026-03-25）
OLD_PROPERTY_ID = "402601716"
# 新プロパティに切り替わった日（この日以降は新を使う）
NEW_PROPERTY_CUTOVER = datetime(2026, 3, 26)


def save_ga4_config(config: Dict[str, str]) -> None:
    with open(GA4_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def setup_ga4(creds: Credentials) -> str:
    """GA4プロパティを自動検出して設定ファイルに保存"""
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient

    print("\n--- GA4 セットアップ ---")
    client = AnalyticsAdminServiceClient(credentials=creds, transport="rest")

    # アカウント一覧
    accounts = list(client.list_accounts())
    if not accounts:
        print("GA4アカウントが見つかりません。https://analytics.google.com で作成してください。")
        sys.exit(1)

    print(f"アカウント数: {len(accounts)}")

    # 全プロパティを探索
    all_properties = []
    for acc in accounts:
        props = list(client.list_properties(
            request={"filter": f"parent:{acc.name}"}
        ))
        for p in props:
            # データストリームからmeasurement IDを取得
            streams = list(client.list_data_streams(parent=p.name))
            measurement_ids = []
            for s in streams:
                if hasattr(s, 'web_stream_data') and s.web_stream_data:
                    measurement_ids.append(s.web_stream_data.measurement_id)
            all_properties.append({
                "name": p.name,
                "display_name": p.display_name,
                "property_id": p.name.split("/")[-1],
                "measurement_ids": measurement_ids,
            })
            mid_str = ", ".join(measurement_ids) if measurement_ids else "なし"
            print(f"  [{len(all_properties)}] {p.display_name} (ID: {p.name.split('/')[-1]}) MID: {mid_str}")

    if not all_properties:
        print("プロパティが見つかりません。")
        sys.exit(1)

    # G-VVS0MLR0ZD に一致するプロパティを自動選択
    target_mid = "G-VVS0MLR0ZD"
    matched = [p for p in all_properties if target_mid in p["measurement_ids"]]

    if matched:
        prop = matched[0]
        print(f"\n自動選択: {prop['display_name']} (MID: {target_mid})")
    elif len(all_properties) == 1:
        prop = all_properties[0]
        print(f"\n自動選択（1件のみ）: {prop['display_name']}")
    else:
        while True:
            try:
                choice = int(input("\nプロパティ番号を選択: ").strip()) - 1
                if 0 <= choice < len(all_properties):
                    prop = all_properties[choice]
                    break
            except (ValueError, EOFError):
                pass

    config = {
        "property_id": prop["property_id"],
        "display_name": prop["display_name"],
        "measurement_ids": prop["measurement_ids"],
    }
    save_ga4_config(config)
    print(f"\n設定保存: {GA4_CONFIG_FILE}")
    print(f"  property_id: {config['property_id']}")
    return config["property_id"]


def fetch_daily_pv(
    creds: Credentials,
    property_id: str,
    target_date: datetime,
) -> Dict[str, Any]:
    """GA4 Data APIで1日分のPVデータを取得"""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    client = BetaAnalyticsDataClient(credentials=creds, transport="rest")
    date_str = target_date.strftime("%Y-%m-%d")

    # 1. 全体PV
    total_request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="activeUsers"),
            Metric(name="sessions"),
        ],
    )
    total_resp = client.run_report(total_request)
    total_pv = 0
    total_users = 0
    total_sessions = 0
    if total_resp.rows:
        row = total_resp.rows[0]
        total_pv = int(row.metric_values[0].value)
        total_users = int(row.metric_values[1].value)
        total_sessions = int(row.metric_values[2].value)

    # 2. 媒体別PV（source/medium）
    source_request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="activeUsers"),
        ],
    )
    source_resp = client.run_report(source_request)
    by_channel = {}
    if source_resp.rows:
        for row in source_resp.rows:
            channel = row.dimension_values[0].value
            pv = int(row.metric_values[0].value)
            users = int(row.metric_values[1].value)
            by_channel[channel] = {"pv": pv, "users": users}

    # 3. ページ別PV（LP vs 会員サイト）
    page_request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
    )
    page_resp = client.run_report(page_request)
    lp_pv = 0
    member_pv = 0
    if page_resp.rows:
        for row in page_resp.rows:
            path = row.dimension_values[0].value
            pv = int(row.metric_values[0].value)
            if "lp" in path.lower() or path == "/" or "landing" in path.lower():
                lp_pv += pv
            else:
                member_pv += pv

    # 媒体別PV集計
    ig_pv = by_channel.get("Organic Social", {}).get("pv", 0)
    tiktok_pv = by_channel.get("Organic Video", {}).get("pv", 0)
    direct_pv = by_channel.get("Direct", {}).get("pv", 0)
    referral_pv = by_channel.get("Referral", {}).get("pv", 0)

    result = {
        "date": date_str,
        "total_pv": total_pv,
        "total_users": total_users,
        "total_sessions": total_sessions,
        "lp_pv": lp_pv,
        "member_pv": member_pv,
        "ig_pv": ig_pv,
        "tiktok_pv": tiktok_pv,
        "direct_pv": direct_pv,
        "referral_pv": referral_pv,
        "by_channel": by_channel,
    }

    return result


def format_daily_date(dt: datetime) -> str:
    """datetime → ' 3/22 月' 形式"""
    weekday = WEEKDAY_JP[dt.weekday()]
    return f" {dt.month}/{dt.day} {weekday}"


def find_row_by_date(sheets_service, target_date: datetime) -> Optional[int]:
    """日ごとデータのA列から該当日の行番号(1-based)を探す"""
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=DAILY_SHEET_ID,
        range=f"'{DAILY_TAB}'!A:A",
    ).execute()
    values = resp.get("values", [])
    target_weekday = WEEKDAY_JP[target_date.weekday()]

    for i in range(len(values) - 1, -1, -1):
        row = values[i]
        if not row:
            continue
        cell = row[0].strip()
        m = re.search(r"(\d{1,2})/(\d{1,2})", cell)
        if not m:
            continue
        month = int(m.group(1))
        day = int(m.group(2))
        if month == target_date.month and day == target_date.day:
            weekday_match = re.search(r"[月火水木金土日]", cell)
            if weekday_match and weekday_match.group() == target_weekday:
                return i + 1
            if not weekday_match:
                return i + 1
    return None


def col_letter(idx: int) -> str:
    result = ""
    n = idx
    while n >= 0:
        result = chr(n % 26 + 65) + result
        n = n // 26 - 1
    return result


def write_pv_to_sheet(
    sheets_service,
    target_date: datetime,
    data: Dict[str, Any],
) -> None:
    """PVデータをスプシに書き込み"""
    row_num = find_row_by_date(sheets_service, target_date)
    if not row_num:
        print(f"  {data['date']}: 行が見つかりません。スキップ。")
        return

    updates = []

    def add(col, val):
        cell = f"'{DAILY_TAB}'!{col_letter(col)}{row_num}"
        updates.append({"range": cell, "values": [[val]]})

    # PV
    add(COL_PV_TOTAL, data["total_pv"])
    add(COL_PV_IG, data["ig_pv"])
    add(COL_PV_TIKTOK, data["tiktok_pv"])

    if updates:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=DAILY_SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    print(f"  {data['date']}: PV={data['total_pv']} (IG:{data['ig_pv']} TikTok:{data['tiktok_pv']} "
          f"Direct:{data['direct_pv']} Ref:{data['referral_pv']}) "
          f"LP:{data['lp_pv']} 会員:{data['member_pv']} → Row {row_num}")


def main():
    parser = argparse.ArgumentParser(description="GA4 日次PV同期")
    parser.add_argument("--setup", action="store_true", help="初回セットアップ（OAuth + プロパティ選択）")
    parser.add_argument("--days", type=int, default=1, help="過去N日分を取得")
    parser.add_argument("--date", type=str, help="特定日を取得（YYYY-MM-DD）")
    parser.add_argument("--dry-run", action="store_true", help="データ取得のみ（書き込みしない）")
    args = parser.parse_args()

    print("=== GA4 日次PV同期 ===\n")

    # 認証
    try:
        creds = get_ga4_credentials()
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        sys.exit(1)

    # セットアップモード
    if args.setup:
        property_id = setup_ga4(creds)
        print(f"\nセットアップ完了。次回から以下で実行:")
        print(f"  python3 utils/sync_ga4_daily.py")
        print(f"  python3 utils/sync_ga4_daily.py --days 30  # 過去30日バックフィル")
        return

    # 設定読み込み
    config = load_ga4_config()
    property_id = config.get("property_id")
    if not property_id:
        print("GA4プロパティIDが設定されていません。")
        print("先に --setup を実行してください:")
        print(f"  python3 utils/sync_ga4_daily.py --setup")
        sys.exit(1)

    print(f"新プロパティ: {config.get('display_name', '')} (ID: {property_id})")
    print(f"旧プロパティ: tackey_skincare (ID: {OLD_PROPERTY_ID}) ← 2026-03-25以前")

    # Sheets API
    sheets_service = build_service("sheets", "v4", credentials=creds) if not args.dry_run else None

    # 日付範囲
    now_jst = datetime.now(JST)
    if args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d")]
    else:
        dates = []
        for i in range(args.days):
            d = now_jst - timedelta(days=i + 1)  # 昨日から
            dates.append(d)
        dates.reverse()

    print(f"取得期間: {len(dates)}日分\n")

    for dt in dates:
        # 日付で旧/新プロパティを自動選択
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        use_prop = property_id if dt_naive >= NEW_PROPERTY_CUTOVER else OLD_PROPERTY_ID
        prop_label = "新" if dt_naive >= NEW_PROPERTY_CUTOVER else "旧"
        try:
            data = fetch_daily_pv(creds, use_prop, dt)
            if args.dry_run:
                channels = data.get("by_channel", {})
                ch_str = ", ".join(f"{k}:{v['pv']}" for k, v in sorted(channels.items(), key=lambda x: -x[1]['pv']))
                print(f"  [{prop_label}] {data['date']}: PV={data['total_pv']} UU={data['total_users']} "
                      f"(IG:{data['ig_pv']} Direct:{data['direct_pv']} Ref:{data['referral_pv']})")
            else:
                write_pv_to_sheet(sheets_service, dt, data)
        except Exception as e:
            print(f"  {dt.strftime('%Y-%m-%d')}: エラー: {e}")

    print("\n完了")


if __name__ == "__main__":
    main()
