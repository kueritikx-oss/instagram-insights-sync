#!/usr/bin/env python3
"""ファネル出口データ(成約/着金)を「日ごとデータ」に自動転記(クラウド版)

ローカル utils/sync_funnel_outcomes_to_daily.py (LaunchAgent com.tackey.funnel-outcomes-sync
毎日22:30) のクラウド移植。2026-07-13移行・ローカルLAは退役済(決定ログ追補143)。

【データソース】
- CSP_売上マスター「入金一覧」: 日付+ステータス=着金済 → CJ列(着金・全体)
- CSP_売上マスター「契約一覧」: 契約日 → CI列(成約・全体)
  ※契約日が空の行は「最初の入金日 = 契約日」とみなす

【書込列マッピング(0-indexed)】
- CI(86): 成約(全体・契約数)
- CJ(87): 着金(全体・着金件数)

【年ブロック構造尊重・append絶対禁止】
- 行1829-2193 = 2026年(3/22-3/21翌年)。既存行への update のみ。
- A列日付で行が見つからなければ警告してスキップ(新規行は作らない)。

【既存値保護】
- 既存セルに値があれば上書きしない(タッキー手動入力を尊重)

認証: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → GOOGLE_TOKEN_JSON fallback。

Usage:
    python3 scripts/sync_funnel_outcomes_to_daily.py --dry-run
    python3 scripts/sync_funnel_outcomes_to_daily.py --backfill 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))

DAILY_FUNNEL_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
DAILY_TAB = "日ごとデータ"
SALES_MASTER_ID = "1wThcN02LgnT0FfB7Flp5KEEwdFbVj02EoikZ56djRd0"

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# 列マッピング(0-indexed)
COL_CI = 86  # 成約・全体
COL_CJ = 87  # 着金・全体


def get_sheets_service():
    """SA優先 → OAuth token fallback (既存クラウドscriptsと同パターン)"""
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


def safe_int(v):
    try:
        s = str(v).replace(',', '').replace('¥', '').strip()
        return int(float(s)) if s else 0
    except Exception:
        return 0


def parse_iso_date(s: str) -> str | None:
    """日付文字列をISO形式(YYYY-MM-DD)に正規化"""
    if not s:
        return None
    s = s.strip().replace('/', '-')
    m = re.match(r'^(\d{4}-\d{1,2}-\d{1,2})', s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), '%Y-%m-%d')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            return None
    return None


def collect_payments_by_date(svc) -> dict[str, int]:
    """入金一覧から日付ごとの「着金済」件数を集計"""
    print("[1] 入金一覧 集計...")
    rows = svc.spreadsheets().values().get(
        spreadsheetId=SALES_MASTER_ID,
        range="'入金一覧'!A2:I3500",
    ).execute().get('values', [])

    counter = defaultdict(int)
    for r in rows:
        if len(r) < 6:
            continue
        date_iso = parse_iso_date(r[2] if len(r) > 2 else '')
        status = (r[5] if len(r) > 5 else '').strip()
        if date_iso and status == '着金済':
            counter[date_iso] += 1

    print(f"  入金集計完了: {len(counter)} 日分・合計 {sum(counter.values())} 件")
    return dict(counter)


def collect_contracts_by_date(svc) -> dict[str, int]:
    """契約一覧から日付ごとの契約件数を集計
    契約日が空の場合は契約IDから入金一覧の最初の日付を契約日とみなす
    """
    print("[2] 契約一覧 集計...")
    contracts = svc.spreadsheets().values().get(
        spreadsheetId=SALES_MASTER_ID,
        range="'契約一覧'!A2:M1100",
    ).execute().get('values', [])
    payments = svc.spreadsheets().values().get(
        spreadsheetId=SALES_MASTER_ID,
        range="'入金一覧'!A2:I3500",
    ).execute().get('values', [])

    # 契約ID → 最初の着金日のマップ
    first_payment_by_contract = {}
    for p in payments:
        if len(p) < 6:
            continue
        contract_id = (p[1] if len(p) > 1 else '').strip()
        date_iso = parse_iso_date(p[2] if len(p) > 2 else '')
        status = (p[5] if len(p) > 5 else '').strip()
        if contract_id and date_iso and status == '着金済':
            if contract_id not in first_payment_by_contract or date_iso < first_payment_by_contract[contract_id]:
                first_payment_by_contract[contract_id] = date_iso

    counter = defaultdict(int)
    contract_count = 0
    fallback_count = 0
    for r in contracts:
        if len(r) < 3:
            continue
        contract_id = (r[0] if len(r) > 0 else '').strip()
        date_iso = parse_iso_date(r[2] if len(r) > 2 else '')

        if not date_iso and contract_id in first_payment_by_contract:
            date_iso = first_payment_by_contract[contract_id]
            fallback_count += 1

        if date_iso:
            counter[date_iso] += 1
            contract_count += 1

    print(f"  契約集計完了: {len(counter)} 日分・合計 {contract_count} 件 (フォールバック {fallback_count} 件)")
    return dict(counter)


def find_year_block(year: int) -> tuple[int | None, int | None]:
    """年ブロックの行範囲を返す(1-indexed)。ブロック開始日: 3/22"""
    YEAR_BLOCK_START = {
        2021: 3, 2022: 368, 2023: 733, 2024: 1099, 2025: 1464, 2026: 1829, 2027: 2194,
    }
    if year not in YEAR_BLOCK_START:
        return None, None
    start = YEAR_BLOCK_START[year]
    end = YEAR_BLOCK_START.get(year + 1, 3727) - 1
    return start, end


def build_date_to_row_map(svc) -> dict[str, int]:
    """日ごとデータA列を読んで {ISO日付: 行番号} マップを作る"""
    print("[3] 日ごとデータA列読込(マッピング構築)...")
    res = svc.spreadsheets().values().get(
        spreadsheetId=DAILY_FUNNEL_ID,
        range=f"'{DAILY_TAB}'!A1:A3727",
    ).execute()
    rows = res.get('values', [])

    date_to_row = {}
    for year in [2021, 2022, 2023, 2024, 2025, 2026, 2027]:
        start, end = find_year_block(year)
        if not start:
            continue

        for offset in range(end - start + 1):
            row_num = start + offset
            if row_num >= len(rows) + 1:
                break

            cell = rows[row_num - 1] if row_num <= len(rows) else None
            if not cell:
                continue
            cell_str = (cell[0] if cell else '').strip()
            if not cell_str:
                continue

            m = re.search(r'(\d{1,2})/(\d{1,2})', cell_str)
            if not m:
                continue
            month, day = int(m.group(1)), int(m.group(2))

            # 年判定: 3/22(start行)を基準
            try:
                if month >= 4 or (month == 3 and day >= 22):
                    actual_year = year
                else:
                    actual_year = year + 1
                dt = datetime(actual_year, month, day)
                iso = dt.strftime('%Y-%m-%d')
                if iso not in date_to_row:  # 重複時は最初を優先
                    date_to_row[iso] = row_num
            except ValueError:
                continue

    print(f"  マッピング完了: {len(date_to_row)} 日分")
    return date_to_row


def update_daily_funnel(svc, payments_by_date, contracts_by_date,
                        date_to_row, target_date_filter, dry_run):
    """日ごとデータCI列(成約)/CJ列(着金)に転記(既存行updateのみ・append禁止)"""
    print("\n[4] 日ごとデータ更新...")

    # 既存値を一括取得(上書き保護用)
    res = svc.spreadsheets().values().get(
        spreadsheetId=DAILY_FUNNEL_ID,
        range=f"'{DAILY_TAB}'!CI1:CJ3727",
    ).execute()
    existing_rows = res.get('values', [])

    updates = []
    update_count_ci = 0
    update_count_cj = 0
    skip_count = 0
    missing_rows = 0

    for iso_date in sorted(set(list(payments_by_date.keys()) + list(contracts_by_date.keys()))):
        if target_date_filter and iso_date < target_date_filter:
            continue
        if iso_date not in date_to_row:
            # append禁止: 行が見つからなければ警告してスキップ(新規行は作らない)
            print(f"  ⚠️ {iso_date}: 日ごとデータに該当行なし → スキップ(append禁止)")
            missing_rows += 1
            continue

        row_num = date_to_row[iso_date]
        existing = existing_rows[row_num - 1] if row_num <= len(existing_rows) else []
        existing_ci = safe_int(existing[0] if len(existing) > 0 else 0)
        existing_cj = safe_int(existing[1] if len(existing) > 1 else 0)

        new_ci = contracts_by_date.get(iso_date, 0)
        new_cj = payments_by_date.get(iso_date, 0)

        # 既存値保護: 既存値があれば上書きしない(0は空とみなす)
        if existing_ci > 0:
            ci_to_write = existing_ci
            ci_changed = False
        else:
            ci_to_write = new_ci if new_ci > 0 else ''
            ci_changed = (new_ci > 0)

        if existing_cj > 0:
            cj_to_write = existing_cj
            cj_changed = False
        else:
            cj_to_write = new_cj if new_cj > 0 else ''
            cj_changed = (new_cj > 0)

        if ci_changed or cj_changed:
            updates.append({
                'range': f"'{DAILY_TAB}'!CI{row_num}:CJ{row_num}",
                'values': [[ci_to_write, cj_to_write]],
            })
            if ci_changed:
                update_count_ci += 1
            if cj_changed:
                update_count_cj += 1
        elif (existing_ci > 0 and new_ci != existing_ci) or (existing_cj > 0 and new_cj != existing_cj):
            skip_count += 1

    print(f"  CI(成約)更新: {update_count_ci} 行")
    print(f"  CJ(着金)更新: {update_count_cj} 行")
    print(f"  既存値保護でスキップ: {skip_count} 行")
    if missing_rows:
        print(f"  ⚠️ 行なしスキップ: {missing_rows} 日分")

    if dry_run:
        print("  [DRY RUN] サンプル5件:")
        for u in updates[:5]:
            print(f"    {u['range']}: CI={u['values'][0][0]} / CJ={u['values'][0][1]}")
        return 0

    if updates:
        # 200件ずつバッチ(既存セルのupdateのみ・append APIは使わない)
        for i in range(0, len(updates), 200):
            batch = updates[i:i+200]
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=DAILY_FUNNEL_ID,
                body={'valueInputOption': 'USER_ENTERED', 'data': batch}
            ).execute()
        print(f"  ✅ {len(updates)} 行更新")

    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", type=int, default=30,
                    help="過去N日分(デフォ30)・--backfill 0で全期間")
    args = ap.parse_args()

    svc = get_sheets_service()

    if args.backfill > 0:
        cutoff = (datetime.now(JST) - timedelta(days=args.backfill)).strftime('%Y-%m-%d')
        print(f"=== 対象期間: {cutoff} 以降 ===\n")
    else:
        cutoff = None
        print("=== 対象期間: 全期間 ===\n")

    payments_by_date = collect_payments_by_date(svc)
    contracts_by_date = collect_contracts_by_date(svc)
    date_to_row = build_date_to_row_map(svc)

    return update_daily_funnel(
        svc, payments_by_date, contracts_by_date,
        date_to_row, cutoff, args.dry_run
    )


if __name__ == "__main__":
    sys.exit(main())
