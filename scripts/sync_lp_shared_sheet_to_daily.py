#!/usr/bin/env python3
"""共有LPヒートマップPVを「日ごとデータ」AM:AOに同期(クラウド版)。

ローカル utils/sync_lp_shared_sheet_to_daily.py (run_daily_funnel_pipeline.sh 内 step) の
クラウド移植。2026-07-13移行(決定ログ追補143)。

Source: `川上泰輝 データシート` / `データ` タブ
  B: date / C:D:E: Instagram PV/CLICK/CTR / F:G:H: TikTok PV/CLICK/CTR

`日ごとデータ` のLINE登録実数(AP:AR, Lステップ由来)には触らない。
書くのは LP PV列 AM:AO と CVR数式 AS:AU のみ。年ブロック既存行のupdateのみ(append禁止)。
行が見つからない日付はスキップ件数として報告する。

認証: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → GOOGLE_TOKEN_JSON fallback。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

LP_SOURCE_ID = "1fmK5tD_e11N5e1Z4EcHIpAchW2E2FhPeNXcBZ1LNGqo"
LP_SOURCE_TAB = "データ"

DAILY_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
DAILY_TAB = "日ごとデータ"

YEAR_BLOCK_START = {
    2021: 3,
    2022: 368,
    2023: 733,
    2024: 1099,
    2025: 1464,
    2026: 1829,
    2027: 2194,
}


def get_sheets_service():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    creds = None
    if sa_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(sa_json)
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
    if creds is None and sa_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=SHEETS_SCOPES)
    if creds is None:
        token_json = os.environ.get("GOOGLE_TOKEN_JSON")
        if not token_json:
            raise SystemExit("❌ Sheets認証情報なし (GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_TOKEN_JSON)")
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def execute_with_retry(request, retries: int = 6):
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status != 429 or attempt == retries - 1:
                raise
            sleep_sec = 15 * (attempt + 1)
            print(f"Sheets API 429: sleep {sleep_sec}s", flush=True)
            time.sleep(sleep_sec)


def safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except Exception:
        return None


def parse_lp_date(value: Any) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    match = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def read_lp_metrics(svc) -> dict[str, dict[str, int | None]]:
    rows = execute_with_retry(svc.spreadsheets().values().get(
        spreadsheetId=LP_SOURCE_ID,
        range=f"'{LP_SOURCE_TAB}'!B5:H1716",
        valueRenderOption="FORMATTED_VALUE",
    )).get("values", [])

    metrics: dict[str, dict[str, int | None]] = {}
    for row in rows:
        iso = parse_lp_date(row[0] if row else None)
        if not iso:
            continue
        ig_pv = safe_int_or_none(row[1] if len(row) > 1 else None)
        tk_pv = safe_int_or_none(row[4] if len(row) > 4 else None)
        if ig_pv is None and tk_pv is None:
            continue
        metrics[iso] = {
            "ig_pv": ig_pv,
            "tiktok_pv": tk_pv,
        }
    return metrics


def build_daily_row_map(rows: list[list[Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for year, start_row in YEAR_BLOCK_START.items():
        end_row = YEAR_BLOCK_START.get(year + 1, len(rows) + 1) - 1
        for row_num in range(start_row, min(end_row, len(rows)) + 1):
            row = rows[row_num - 1] if row_num - 1 < len(rows) else []
            cell = str(row[0]).strip() if row else ""
            match = re.search(r"(\d{1,2})/(\d{1,2})", cell)
            if not match:
                continue
            month, day = map(int, match.groups())
            actual_year = year if month >= 4 or (month == 3 and day >= 22) else year + 1
            try:
                out[date(actual_year, month, day).isoformat()] = row_num
            except ValueError:
                continue
    return out


def refresh_cvr_formulas(row_num: int) -> list[dict[str, Any]]:
    return [
        {
            "range": f"'{DAILY_TAB}'!AS{row_num}:AU{row_num}",
            "values": [[
                f'=IFERROR(IF(AND(AM{row_num}>0,AP{row_num}<=AM{row_num}),AP{row_num}/AM{row_num},0),0)',
                f'=IFERROR(IF(AND(AN{row_num}>0,AQ{row_num}<=AN{row_num}),AQ{row_num}/AN{row_num},0),0)',
                f'=IFERROR(IF(AND(AO{row_num}>0,AR{row_num}<=AO{row_num}),AR{row_num}/AO{row_num},0),0)',
            ]],
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="共有LPスプシから日ごとデータAM:AOを同期(クラウド版)")
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, help="今日からN日前まで。start/endより簡易")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.backfill and not (args.start or args.end):
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=args.backfill - 1)
    else:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    svc = get_sheets_service()
    lp_metrics = read_lp_metrics(svc)
    daily_rows = execute_with_retry(svc.spreadsheets().values().get(
        spreadsheetId=DAILY_ID,
        range=f"'{DAILY_TAB}'!A1:A3727",
        valueRenderOption="FORMATTED_VALUE",
    )).get("values", [])
    row_map = build_daily_row_map(daily_rows)

    updates: list[dict[str, Any]] = []
    skipped = 0
    for iso, metric in sorted(lp_metrics.items()):
        d = datetime.strptime(iso, "%Y-%m-%d").date()
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        row_num = row_map.get(iso)
        if not row_num:
            skipped += 1
            continue
        ig_pv = metric["ig_pv"] or 0
        tk_pv = metric["tiktok_pv"] or 0
        total_pv = ig_pv + tk_pv
        updates.append({
            "range": f"'{DAILY_TAB}'!AM{row_num}:AO{row_num}",
            "values": [[total_pv, ig_pv, tk_pv]],
        })
        updates.extend(refresh_cvr_formulas(row_num))

    print(f"source_days={len(lp_metrics)} updates={len(updates)} skipped={skipped}")
    for item in updates[:8]:
        print(item)
    if len(updates) > 8:
        print("...")
        for item in updates[-6:]:
            print(item)

    if args.dry_run or not updates:
        return 0

    for idx in range(0, len(updates), 400):
        execute_with_retry(svc.spreadsheets().values().batchUpdate(
            spreadsheetId=DAILY_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates[idx:idx + 400]},
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
