#!/usr/bin/env python3
"""note記事別 LP流入数を GA4から取得 → 記事一覧タブの「LP流入」列に書き込む(クラウド版)。

ローカル utils/sync_note_lp_inflow.py (LaunchAgent com.tackey.note-lp-inflow) のクラウド移植。
UTM_campaign別セッション数を GA4 Data API から取得し、
記事一覧タブの「UTM_campaigns」列のマッピングと突合して記事別に合算する。

設計 (fill_note_analysis.py と同思想):
  - 列番号ハードコード禁止。行1ヘッダーから「LP流入」「UTM_campaigns」列を動的解決
  - 書き込みは既存行の「LP流入」セル update のみ。append/列追加は一切しない
  - 認証: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → GA4はOAuth token fallback可
  - GA4 property_id: GA4_PROPERTY_ID env → auth/ga4_config.json の順で解決

Usage:
    python3 scripts/sync_note_lp_inflow.py             # 過去30日分合算
    python3 scripts/sync_note_lp_inflow.py --days 7    # 過去7日
    python3 scripts/sync_note_lp_inflow.py --dry-run   # 書き込みなし
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))

SPREADSHEET_ID = "1gIW_SCigwa5wFPnQVRoFM3EhOaYC3aC_DtkMbqK8gRw"
SHEET_NAME = "記事一覧"
HEADER_ROW = 1
DATA_START_ROW = 2

HEADER_LP_INFLOW = "LP流入"
HEADER_UTM = "UTM_campaigns"

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

AUTH_DIR = Path(os.environ.get("GA4_GOOGLE_AUTH_DIR", "auth"))
GA4_TOKEN_FILE = AUTH_DIR / "ga4_token.json"
GA4_CONFIG_FILE = AUTH_DIR / "ga4_config.json"


def col_letter(idx: int) -> str:
    """0-based column index → A1記法の列文字"""
    letters = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _sa_credentials(scopes):
    """GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_FILE から SA credsを作る"""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(sa_json)
        if info.get("type") == "service_account":
            return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    if sa_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        return service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
    return None


def get_sheets_service():
    creds = _sa_credentials(SHEETS_SCOPES)
    if creds is None:
        token_json = os.environ.get("GOOGLE_TOKEN_JSON")
        if not token_json:
            raise SystemExit("❌ Sheets認証情報なし (GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_TOKEN_JSON)")
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_ga4_client():
    """GA4 Data APIクライアント。SA優先 → OAuth token fallback (ローカル版と同順)"""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    creds = _sa_credentials(GA4_SCOPES)
    if creds is not None:
        return BetaAnalyticsDataClient(credentials=creds, transport="rest")
    if GA4_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GA4_TOKEN_FILE), GA4_SCOPES)
        return BetaAnalyticsDataClient(credentials=creds, transport="rest")
    raise SystemExit("❌ GA4認証情報なし (GOOGLE_SERVICE_ACCOUNT_JSON / auth/ga4_token.json)")


def get_property_id() -> str:
    prop_id = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if prop_id:
        return prop_id
    if GA4_CONFIG_FILE.exists():
        config = json.loads(GA4_CONFIG_FILE.read_text(encoding="utf-8"))
        prop_id = str(config.get("property_id", "")).strip()
        if prop_id:
            return prop_id
    raise SystemExit("❌ GA4 property_id 不明 (GA4_PROPERTY_ID env or auth/ga4_config.json)")


def fetch_campaign_sessions(client, property_id: str, days: int = 30) -> dict:
    """UTM_campaign別のセッション数取得"""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    end = datetime.now(JST).strftime("%Y-%m-%d")
    start = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d")
    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name="sessionCampaignName")],
        metrics=[Metric(name="sessions"), Metric(name="screenPageViews")],
    )
    resp = client.run_report(req)
    result = {}
    for row in resp.rows:
        cmp_name = row.dimension_values[0].value
        sessions = int(row.metric_values[0].value) if row.metric_values[0].value else 0
        views = int(row.metric_values[1].value) if row.metric_values[1].value else 0
        result[cmp_name] = {"sessions": sessions, "views": views}
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="note記事別LP流入 GA4→スプシ同期(クラウド版)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    property_id = get_property_id()
    print(f"=== note記事別LP流入取得 ({args.days}日分) property={property_id} ===\n")

    # GA4 データ取得
    client = get_ga4_client()
    campaign_data = fetch_campaign_sessions(client, property_id, days=args.days)
    print(f"GA4から取得: {len(campaign_data)} campaigns")

    note_cmp = {k: v for k, v in campaign_data.items() if k.startswith("note_")}
    print(f"  うち note_*: {len(note_cmp)} campaigns\n")
    for cmp, d in sorted(note_cmp.items(), key=lambda x: -x[1]["sessions"])[:10]:
        print(f"  {cmp:35s} sessions={d['sessions']:4d} views={d['views']:4d}")

    # スプシ読み取り + 列動的解決 (行1ヘッダー)
    svc = get_sheets_service()
    rows = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A{HEADER_ROW}:AZ100"
    ).execute().get("values", [])
    if not rows:
        raise SystemExit("❌ 記事一覧タブが空")

    headers = [str(h).strip() for h in rows[0]]

    def find_header(name: str) -> int:
        for i, h in enumerate(headers):
            if h == name:
                return i
        raise SystemExit(f"❌ 行1ヘッダーに「{name}」が見つからない — 列名変更の可能性。書き込み中止")

    col_lp = find_header(HEADER_LP_INFLOW)
    col_utm = find_header(HEADER_UTM)
    lp_letter = col_letter(col_lp)
    print(f"\n列解決: {HEADER_LP_INFLOW}={lp_letter}列 / {HEADER_UTM}={col_letter(col_utm)}列")

    # 記事番号 → UTM_campaigns[] のマップ
    article_utms = {}
    for i, r in enumerate(rows[1:], start=DATA_START_ROW):
        if not r or not str(r[0]).strip():
            continue
        num = str(r[0]).strip()
        utm_col = str(r[col_utm]).strip() if len(r) > col_utm else ""
        if utm_col:
            article_utms[num] = {"row": i, "utms": [u.strip() for u in utm_col.split(",")]}

    print("\n=== 記事別集計 ===")
    batch = []
    for num, info in article_utms.items():
        total_sessions = 0
        for utm in info["utms"]:
            if utm in campaign_data:
                total_sessions += campaign_data[utm]["sessions"]
        print(f"  #{num:3s}: LP流入 {total_sessions:4d} ({len(info['utms'])}UTM)")
        batch.append({
            "range": f"'{SHEET_NAME}'!{lp_letter}{info['row']}",
            "values": [[total_sessions]],
        })

    if not batch:
        print("\nUTM_campaigns 登録記事なし。書き込みなしで終了")
        return 0

    if args.dry_run:
        print(f"\n[DRY RUN] {len(batch)}セル更新予定 ({lp_letter}列)")
        return 0

    # 既存セルの update のみ (append禁止)
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": batch},
    ).execute()
    print(f"\n✅ スプシ更新: {len(batch)}セル ({lp_letter}列)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
