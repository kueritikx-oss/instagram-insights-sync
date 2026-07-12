#!/usr/bin/env python3
"""LP_AB追跡_2026 日次同期(クラウド版)

ローカル utils/sync_lp_ab_tracking_daily.py (run_daily_funnel_pipeline.sh 内 step) の
クラウド移植。2026-07-13移行(決定ログ追補143)。

LP page_view / sessions / cta_click(GA4) + LINE登録(既存「日ごとデータ」AP列) を
LP_AB追跡_2026 タブに書き込む。CVRは数式で自動計算。

行構造: 1日=1行・D列=all固定・B列version=日付境界自動判定(既存設計をそのまま踏襲)。
既存の(日付,version)行があればその行をupdate、なければ表末尾の次行へ書く。
「日ごとデータ」タブには一切書き込まない(AP列のreadのみ)。

認証:
  - GA4: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → auth/ga4_token.json fallback
  - Sheets: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → GOOGLE_TOKEN_JSON fallback
  - GA4 property_id: GA4_PROPERTY_ID env → auth/ga4_config.json

Usage:
    python3 scripts/sync_lp_ab_tracking_daily.py --days 3
    python3 scripts/sync_lp_ab_tracking_daily.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import re

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

AUTH_DIR = Path(os.environ.get("GA4_GOOGLE_AUTH_DIR", "auth"))
GA4_TOKEN_FILE = AUTH_DIR / "ga4_token.json"
GA4_CONFIG_FILE = AUTH_DIR / "ga4_config.json"

SPREADSHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
LP_AB_TAB = "LP_AB追跡_2026"
DAILY_TAB = "日ごとデータ"

LP_V2_DEPLOY_DATE = date(2026, 4, 18)
OBSERVATION_TOOL = "GA4+Lステップ"
UTM_SOURCE_VALUE = "all"

LP_PATH_PATTERNS = ["lp.php", "/lp", "clearahadaprogress-members/lp"]

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]
JST = timezone(timedelta(hours=9))


def _sa_credentials(scopes):
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(sa_json)
        if info.get("type") == "service_account":
            return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    if sa_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        return service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
    return None


def get_ga4_credentials():
    creds = _sa_credentials(GA4_SCOPES)
    if creds is not None:
        return creds
    if GA4_TOKEN_FILE.exists():
        return Credentials.from_authorized_user_file(str(GA4_TOKEN_FILE), GA4_SCOPES)
    raise SystemExit("❌ GA4認証情報なし (GOOGLE_SERVICE_ACCOUNT_JSON / auth/ga4_token.json)")


def get_sheets_service():
    creds = _sa_credentials(SHEETS_SCOPES)
    if creds is None:
        token_json = os.environ.get("GOOGLE_TOKEN_JSON")
        if not token_json:
            raise SystemExit("❌ Sheets認証情報なし (GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_TOKEN_JSON)")
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def load_ga4_property_id() -> str:
    prop_id = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if prop_id:
        return prop_id
    if GA4_CONFIG_FILE.exists():
        with open(GA4_CONFIG_FILE, encoding="utf-8") as f:
            return str(json.load(f)["property_id"])
    raise SystemExit("❌ GA4 property_id 不明 (GA4_PROPERTY_ID env or auth/ga4_config.json)")


def get_version_for_date(d: date) -> str:
    return "v2_trust_badge" if d >= LP_V2_DEPLOY_DATE else "v1_baseline"


def get_weekday_jp(d: date) -> str:
    return WEEKDAY_JP[d.weekday()]


def get_target_dates(args) -> List[date]:
    if args.start and args.end:
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        e = (datetime.now(JST) - timedelta(days=1)).date()
        s = e - timedelta(days=args.days - 1)
    out = []
    cur = s
    while cur <= e:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def fetch_daily_lp_metrics(creds, property_id: str, target_date: date) -> Dict[str, Any]:
    """GA4から指定日のLP page_view / sessions / 滞在秒数を取得"""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )

    client = BetaAnalyticsDataClient(credentials=creds, transport="rest")
    date_str = target_date.strftime("%Y-%m-%d")

    page_request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="sessions"),
            Metric(name="averageSessionDuration"),
        ],
    )
    resp = client.run_report(page_request)

    lp_pv = 0
    lp_sessions = 0
    lp_avg_sec_weighted = 0.0
    lp_session_total_for_avg = 0

    for row in resp.rows:
        path = row.dimension_values[0].value.lower()
        if not any(pat in path for pat in LP_PATH_PATTERNS):
            continue
        pv = int(row.metric_values[0].value)
        sess = int(row.metric_values[1].value)
        avg_sec = float(row.metric_values[2].value)
        lp_pv += pv
        lp_sessions += sess
        if sess > 0:
            lp_avg_sec_weighted += avg_sec * sess
            lp_session_total_for_avg += sess

    avg_engagement_sec = lp_avg_sec_weighted / lp_session_total_for_avg if lp_session_total_for_avg > 0 else 0.0

    return {
        "page_views": lp_pv,
        "sessions": lp_sessions,
        "avg_engagement_sec": round(avg_engagement_sec, 1),
    }


def fetch_daily_cta_clicks(creds, property_id: str, target_date: date) -> Dict[str, Any]:
    """GA4から指定日のcta_clickイベント取得。位置別カスタムディメンション(cta_position)を試す"""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, Metric, RunReportRequest,
    )

    client = BetaAnalyticsDataClient(credentials=creds, transport="rest")
    date_str = target_date.strftime("%Y-%m-%d")

    total_request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="eventName",
                string_filter=Filter.StringFilter(value="cta_click"),
            )
        ),
    )
    total_resp = client.run_report(total_request)
    cta_total = 0
    if total_resp.rows:
        cta_total = int(total_resp.rows[0].metric_values[0].value)

    hero = mid = bottom = 0
    cta_position_available = False

    try:
        position_request = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date=date_str, end_date=date_str)],
            dimensions=[
                Dimension(name="customEvent:cta_position"),
            ],
            metrics=[Metric(name="eventCount")],
            dimension_filter=FilterExpression(
                filter=Filter(
                    field_name="eventName",
                    string_filter=Filter.StringFilter(value="cta_click"),
                )
            ),
        )
        pos_resp = client.run_report(position_request)
        cta_position_available = True
        for row in pos_resp.rows:
            pos = row.dimension_values[0].value.lower()
            cnt = int(row.metric_values[0].value)
            if pos == "hero":
                hero = cnt
            elif pos == "mid":
                mid = cnt
            elif pos == "bottom":
                bottom = cnt
    except Exception as e:
        msg = str(e).lower()
        if "customevent" in msg or "dimension" in msg or "field" in msg:
            cta_position_available = False
        else:
            raise

    return {
        "cta_total": cta_total,
        "hero": hero,
        "mid": mid,
        "bottom": bottom,
        "cta_position_available": cta_position_available,
    }


def fetch_lstep_registrations_from_daily_sheet(sheets_service, target_date: date) -> int:
    """既存『日ごとデータ』AP列から該当日のLINE登録数(全体・reg)を取得(readのみ)"""
    target_weekday = WEEKDAY_JP[target_date.weekday()]
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{DAILY_TAB}'!A:AP",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])

    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if not row or len(row) == 0:
            continue
        cell = str(row[0]).strip()
        m = re.search(r"(\d{1,2})/(\d{1,2})", cell)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        if month != target_date.month or day != target_date.day:
            continue
        wd_match = re.search(r"[月火水木金土日]", cell)
        if wd_match and wd_match.group() != target_weekday:
            continue
        if len(row) >= 42:
            v = row[41]  # AP列(0-based 41)
            if v == "" or v is None:
                return 0
            try:
                return int(v)
            except (ValueError, TypeError):
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    return 0
        return 0
    return 0


def get_existing_lp_ab_rows(sheets_service) -> Dict[Tuple[str, str], int]:
    """LP_AB追跡_2026の既存行を (date, version) → row_num で返す"""
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{LP_AB_TAB}'!A:B",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])
    out = {}
    for i, row in enumerate(rows):
        if i < 3:
            continue
        if len(row) < 2:
            continue
        d = str(row[0]).strip()
        v = str(row[1]).strip()
        if d and v:
            out[(d, v)] = i + 1
    return out


def build_row_values(target_date: date, lp: Dict, cta: Dict, line_regs: int, row_num: int) -> List:
    version = get_version_for_date(target_date)
    date_str = target_date.strftime("%Y-%m-%d")
    weekday_jp = get_weekday_jp(target_date)

    if version == "v1_baseline":
        note = "パッチ前(v1)"
    else:
        note = "trust_badge適用後(v2)"
    if not cta["cta_position_available"]:
        note += " / cta_position未設定"

    return [
        date_str,                                            # A 日付
        version,                                             # B バージョン
        weekday_jp,                                          # C 曜日
        UTM_SOURCE_VALUE,                                    # D UTM source
        lp["page_views"],                                    # E LP到達PV
        lp["sessions"],                                      # F LPセッション
        lp["avg_engagement_sec"],                            # G 滞在秒数
        line_regs,                                           # H LINE登録数
        f'=IFERROR(H{row_num}/F{row_num}*100,"")',           # I CVR%
        cta["cta_total"],                                    # J cta_click合計
        cta["hero"],                                         # K Hero_click
        cta["mid"],                                          # L Mid_click
        cta["bottom"],                                       # M Bottom_click
        f'=IFERROR(K{row_num}/F{row_num}*100,"")',           # N Hero_CTR%
        f'=IFERROR(L{row_num}/F{row_num}*100,"")',           # O Mid_CTR%
        f'=IFERROR(M{row_num}/F{row_num}*100,"")',           # P Bottom_CTR%
        "",                                                  # Q CVR変化幅(Day1空)
        "",                                                  # R CVR変化率(Day1空)
        OBSERVATION_TOOL,                                    # S 観測ツール
        note,                                                # T 備考
    ]


def get_next_append_row(sheets_service) -> int:
    """LP_AB追跡_2026 で次に書くべき行番号を返す(既存設計どおり)"""
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{LP_AB_TAB}'!A:A",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])
    last_filled = 0
    for i, row in enumerate(rows):
        if row and str(row[0]).strip():
            last_filled = i + 1
    return max(last_filled + 1, 4)  # 行1=セクション・行2=ヘッダ・行3=型例・行4から実データ


def main():
    parser = argparse.ArgumentParser(description="LP_AB追跡_2026 日次同期(クラウド版)")
    parser.add_argument("--days", type=int, default=1, help="昨日からN日分を取得")
    parser.add_argument("--start", type=str, help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="終了日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="取得のみ・書き込みしない")
    args = parser.parse_args()

    print("=== LP_AB追跡_2026 日次同期 (クラウド版) ===\n")

    creds = get_ga4_credentials()
    property_id = load_ga4_property_id()
    print(f"GA4 property_id: {property_id}\n")

    sheets_service = get_sheets_service()

    target_dates = get_target_dates(args)
    print(f"対象期間: {target_dates[0]} 〜 {target_dates[-1]} ({len(target_dates)}日)\n")

    existing = get_existing_lp_ab_rows(sheets_service)
    print(f"LP_AB既存行数: {len(existing)}\n")

    next_append = get_next_append_row(sheets_service)

    updates = []
    errors = []

    for d in target_dates:
        version = get_version_for_date(d)
        key = (d.strftime("%Y-%m-%d"), version)

        try:
            lp = fetch_daily_lp_metrics(creds, property_id, d)
            cta = fetch_daily_cta_clicks(creds, property_id, d)
            line_regs = fetch_lstep_registrations_from_daily_sheet(sheets_service, d)

            if key in existing:
                row_num = existing[key]
                action = "UPDATE"
            else:
                row_num = next_append
                next_append += 1
                action = "APPEND"

            row = build_row_values(d, lp, cta, line_regs, row_num)
            updates.append({"row_num": row_num, "values": row, "action": action})

            cta_pos = "✓" if cta["cta_position_available"] else "✗"
            print(f"  [{action}] {d} ({version}) row{row_num}: PV={lp['page_views']} Sess={lp['sessions']} "
                  f"LINE={line_regs} CTA={cta['cta_total']} (H={cta['hero']}/M={cta['mid']}/B={cta['bottom']} pos:{cta_pos})")

        except Exception as e:
            errors.append((d, str(e)[:200]))
            print(f"  [ERROR] {d}: {e}")

    if args.dry_run:
        print(f"\n[DRY-RUN] {len(updates)}行 書き込みスキップ")
        return 1 if errors else 0

    if not updates:
        print("\n書き込み対象なし")
        return 1 if errors else 0

    print(f"\n{len(updates)}行 書き込み中...")
    batch_data = []
    for u in updates:
        batch_data.append({
            "range": f"'{LP_AB_TAB}'!A{u['row_num']}:T{u['row_num']}",
            "values": [u["values"]],
        })

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": batch_data},
    ).execute()

    print(f"完了: {len(updates)}行")
    if errors:
        print(f"\n⚠️ エラー {len(errors)}件:")
        for d, e in errors:
            print(f"  {d}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
