#!/usr/bin/env python3
"""「日ごとデータ」タブ全セル監査(クラウド版)。

ローカル utils/audit_daily_data_cells.py (run_daily_funnel_pipeline.sh 最終step) の
クラウド移植。2026-07-13移行(決定ログ追補143)。

- 出力はローカルファイルのみ(GHAではartifactとして保存)。スプシには一切書かない。
- 列定義ヘルパー(COUNT_COLS/RATE_COLS/source_for_col等)は
  utils/update_daily_data_quality_tabs.py からインライン移植(read専用定義)。
- Issueセルが1件以上あり DISCORD_WEBHOOK が設定されていればDiscordへwarn通知。
- --fail-on-issue でIssueあり時に終了コード2。

認証: GOOGLE_SERVICE_ACCOUNT_JSON (SA) 優先 → GOOGLE_TOKEN_JSON fallback。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


JST = timezone(timedelta(hours=9))
SPREADSHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
TAB = "日ごとデータ"
RANGE = f"'{TAB}'!A1:FF3727"
TOTAL_ROWS = 3727
TOTAL_COLS = 162

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

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
        creds = Credentials.from_authorized_user_info(
            json.loads(token_json), ["https://www.googleapis.com/auth/spreadsheets"]
        )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ========== 列定義(utils/update_daily_data_quality_tabs.py からインライン移植) ==========

def col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def num_to_col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def expand_cols(start: str, end: str) -> set[str]:
    return {num_to_col(n) for n in range(col_to_num(start), col_to_num(end) + 1)}


RATE_COLS = (
    expand_cols("AS", "AU")
    | expand_cols("BA", "BC")
    | expand_cols("BI", "BM")
    | expand_cols("BS", "BW")
    | expand_cols("DK", "DN")
    | expand_cols("EE", "EH")
    | expand_cols("ET", "EW")
)

COUNT_COLS = (
    expand_cols("AM", "AR")
    | expand_cols("AV", "AZ")
    | expand_cols("BD", "BH")
    | expand_cols("BN", "BR")
    | expand_cols("BX", "CJ")
    | expand_cols("CK", "CT")
    | expand_cols("CU", "DJ")
    | expand_cols("DO", "ED")
    | expand_cols("EI", "ES")
    | expand_cols("EX", "FE")
) - RATE_COLS


def source_for_col(col: str) -> tuple[str, str, str]:
    n = col_to_num(col)
    if col == "A":
        return ("日付軸", "年ブロック固定", "手動変更禁止")
    if 2 <= n <= 4:
        return ("Instagramフォロバ", "旧運用/手動", "正本未接続")
    if 5 <= n <= 7:
        return ("Instagramフォロー", "自動", "sync_instagram_account_insights.py")
    if 8 <= n <= 38:
        return ("Instagram DM", "未接続/手動", "DMログ正本なし・0埋め候補")
    if col in {"AM", "AN", "AO"}:
        return ("LP PV", "自動", "sync_lp_shared_sheet_to_daily.py / GA4 fallback")
    if col in {"AV", "AW", "AX", "AY", "AZ", "BN", "BO", "BP", "BQ", "BR", "BX", "CK", "CU", "CW", "DO", "DQ", "EI", "EK"}:
        return ("Googleフォーム回答", "自動", "sync_daily_funnel_forms_from_drive.py")
    if col in {"AP", "AQ", "AR", "BD", "BE", "BF", "BG", "BH"}:
        return ("LステップCSV", "自動", "sync_lstep_daily_v2.py")
    if col in {"CB", "CC", "CD"}:
        return ("媒体別LINE", "自動ミラー", "sync_lstep_daily_v2.py / 正本は📱SNS横断_Daily AO:AU")
    if col in RATE_COLS:
        return ("計算列", "数式", "分母0は空欄で正常")
    if col in {"CI", "CJ", "CS", "CT", "DI", "DJ", "EC", "ED", "ER", "ES", "FD", "FE"}:
        return ("売上/成約", "一部自動", "sync_funnel_outcomes_to_daily.py")
    if col in COUNT_COLS:
        return ("ファネル件数", "0埋め管理", "fill_daily_funnel_blanks.py")
    return ("未分類", "要確認", "")


def metric_type(col: str, label: str = "") -> str:
    if col == "A":
        return "日付"
    if col in RATE_COLS or "率" in label:
        return "率/計算"
    if col in COUNT_COLS:
        return "件数"
    return "値"


def build_row_to_date(rows: list[list[str]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for year, start in YEAR_BLOCK_START.items():
        end = YEAR_BLOCK_START.get(year + 1, 3728) - 1
        for row_num in range(start, min(end, len(rows)) + 1):
            row = rows[row_num - 1] if row_num - 1 < len(rows) else []
            cell = str(row[0]).strip() if row else ""
            match = re.search(r"(\d{1,2})/(\d{1,2})", cell)
            if not match:
                continue
            month, day = int(match.group(1)), int(match.group(2))
            actual_year = year if month >= 4 or (month == 3 and day >= 22) else year + 1
            try:
                out[row_num] = datetime(actual_year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return out


# ========== 監査本体(ローカル版と同一ロジック) ==========

def get_cell(rows: list[list[Any]], row_idx: int, col_idx: int) -> Any:
    if row_idx >= len(rows):
        return ""
    row = rows[row_idx]
    if col_idx >= len(row):
        return ""
    return row[col_idx]


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.replace(",", "").replace("%", "").strip()
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            return None
        return n / 100 if "%" in value else n
    return None


def combined_header(formatted_rows: list[list[Any]], col_idx: int) -> str:
    top = as_text(get_cell(formatted_rows, 0, col_idx))
    sub = as_text(get_cell(formatted_rows, 1, col_idx))
    if top and sub:
        return f"{top} / {sub}"
    return top or sub


def row_kind(row_num: int, row_date: str | None, formatted_rows: list[list[Any]]) -> str:
    if row_num <= 2:
        return "header"
    if row_date:
        return "date_row"
    values = [as_text(v) for v in (formatted_rows[row_num - 1] if row_num - 1 < len(formatted_rows) else [])]
    return "empty" if not any(values) else "non_date_row"


def evaluate_cell(
    col: str,
    row_kind_value: str,
    row_date: str | None,
    formatted: Any,
    raw: Any,
    active_start_iso: str,
    today_iso: str,
    formula: Any = "",
) -> tuple[str, list[str]]:
    text = as_text(formatted)
    num = as_number(raw)
    has_formula = as_text(formula).startswith("=")
    issues: list[str] = []

    is_active_date_row = bool(row_date and active_start_iso <= row_date <= today_iso)
    if text.startswith("#"):
        issues.append("FORMULA_ERROR")
    if is_active_date_row and col == "A" and not text:
        issues.append("DATE_BLANK")
    if is_active_date_row and col in COUNT_COLS and not text:
        issues.append("COUNT_BLANK")
    if is_active_date_row and col in COUNT_COLS and num is not None and num < 0:
        issues.append("COUNT_NEGATIVE")
    # 率/計算セルが空欄でも、数式が入っていれば設計通り（分母0のとき
    # IFERROR(IF(分母>0,...,""),"") が意図的に空欄を返す）なので異常としない。
    if is_active_date_row and col in RATE_COLS and not text and not has_formula:
        issues.append("RATE_BLANK")
    if is_active_date_row and col in RATE_COLS and num is not None:
        if num > 1.0000001:
            issues.append("RATE_GT_100")
        if num < -0.0000001:
            issues.append("RATE_NEGATIVE")

    if issues:
        return "ISSUE", issues
    if not text:
        if row_kind_value == "date_row" and is_active_date_row:
            return "BLANK_OK_OR_NOT_APPLICABLE", []
        return "BLANK_OK", []
    return "OK", []


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def notify_discord_if_issues(summary: dict, issue_rows: list[dict[str, Any]]) -> None:
    """Issueあり かつ DISCORD_WEBHOOK 設定時のみwarn送信。未設定でも落とさない。"""
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook or not issue_rows:
        return
    try:
        import requests

        sample = "\n".join(
            f"• {r['a1']} ({r['date']}) {r['header'][:40]}: {r['issues']}" for r in issue_rows[:8]
        )
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "kueritikx-oss/instagram-insights-sync")
        run_url = f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else ""
        payload = {
            "embeds": [{
                "title": "⚠️ 日ごとデータ 全セル監査: Issueあり",
                "description": f"Issueセル {len(issue_rows)}件\n{sample}\n\n詳細はGHA artifact参照\n{run_url}",
                "color": 0xF39C12,
                "footer": {"text": "audit_daily_data_cells.py (cloud)"},
            }]
        }
        requests.post(webhook, json=payload, timeout=15)
        print(f"Discord warn送信: {len(issue_rows)} issues")
    except Exception as e:
        print(f"Discord通知失敗(継続): {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="日ごとデータの全列・全行・全セル監査(クラウド版)")
    parser.add_argument("--out-dir", default="", help="出力ディレクトリ。省略時は _tmp 配下に作成")
    parser.add_argument("--active-start", default="2025-03-22", help="ハードIssue判定する開始日 YYYY-MM-DD")
    parser.add_argument("--active-end", default="", help="ハードIssue判定する終了日 YYYY-MM-DD。省略時は今日からlag-daysを引く")
    parser.add_argument("--lag-days", type=int, default=0, help="active-end省略時に今日から除外する日数")
    parser.add_argument("--fail-on-issue", action="store_true", help="Issueセルが1件以上あれば終了コード2にする")
    args = parser.parse_args()

    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path("_tmp") / f"daily_data_cell_audit_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    svc = get_sheets_service()
    formatted_rows = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE,
        valueRenderOption="FORMATTED_VALUE",
    ).execute().get("values", [])
    raw_rows = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    formula_rows = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE,
        valueRenderOption="FORMULA",
    ).execute().get("values", [])

    row_dates = build_row_to_date([[as_text(c) for c in row] for row in formatted_rows])
    active_end = datetime.now(JST).date() - timedelta(days=max(0, args.lag_days))
    today_iso = args.active_end or active_end.isoformat()
    active_start_iso = args.active_start

    column_stats: dict[str, Counter] = {num_to_col(c): Counter() for c in range(1, TOTAL_COLS + 1)}
    row_stats: dict[int, Counter] = {r: Counter() for r in range(1, TOTAL_ROWS + 1)}
    issue_rows: list[dict[str, Any]] = []
    status_counts: Counter = Counter()
    issue_counts: Counter = Counter()

    cells_path = out_dir / "cells.csv.gz"
    with gzip.open(cells_path, "wt", newline="", encoding="utf-8") as f:
        fieldnames = [
            "row",
            "col",
            "a1",
            "row_kind",
            "date",
            "metric_type",
            "source",
            "status",
            "issues",
            "value",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in range(1, TOTAL_ROWS + 1):
            row_date = row_dates.get(r)
            kind = row_kind(r, row_date, formatted_rows)
            for c in range(1, TOTAL_COLS + 1):
                col = num_to_col(c)
                formatted = get_cell(formatted_rows, r - 1, c - 1)
                raw = get_cell(raw_rows, r - 1, c - 1)
                formula = get_cell(formula_rows, r - 1, c - 1)
                label = combined_header(formatted_rows, c - 1)
                category, source_status, script = source_for_col(col)
                mtype = metric_type(col, label)
                status, issues = evaluate_cell(col, kind, row_date, formatted, raw, active_start_iso, today_iso, formula)
                value_text = as_text(formatted)

                status_counts[status] += 1
                column_stats[col][status] += 1
                row_stats[r][status] += 1
                if value_text:
                    column_stats[col]["populated"] += 1
                    row_stats[r]["populated"] += 1
                if issues:
                    for issue in issues:
                        issue_counts[issue] += 1
                        column_stats[col][issue] += 1
                        row_stats[r][issue] += 1
                    issue_rows.append({
                        "row": r,
                        "col": col,
                        "a1": f"{col}{r}",
                        "date": row_date or "",
                        "header": label,
                        "source": category,
                        "status": status,
                        "issues": "|".join(issues),
                        "value": value_text,
                    })

                writer.writerow({
                    "row": r,
                    "col": col,
                    "a1": f"{col}{r}",
                    "row_kind": kind,
                    "date": row_date or "",
                    "metric_type": mtype,
                    "source": category,
                    "status": status,
                    "issues": "|".join(issues),
                    "value": value_text[:200],
                })

    column_rows = []
    for c in range(1, TOTAL_COLS + 1):
        col = num_to_col(c)
        label = combined_header(formatted_rows, c - 1)
        category, source_status, script = source_for_col(col)
        stats = column_stats[col]
        column_rows.append({
            "col": col,
            "header": label,
            "source": category,
            "source_status": source_status,
            "script": script,
            "metric_type": metric_type(col, label),
            "total_cells": TOTAL_ROWS,
            "populated": stats["populated"],
            "issues": sum(stats[k] for k in issue_counts),
            "count_blank": stats["COUNT_BLANK"],
            "rate_blank": stats["RATE_BLANK"],
            "formula_error": stats["FORMULA_ERROR"],
            "rate_gt_100": stats["RATE_GT_100"],
            "count_negative": stats["COUNT_NEGATIVE"],
        })

    row_rows = []
    for r in range(1, TOTAL_ROWS + 1):
        row_date = row_dates.get(r)
        kind = row_kind(r, row_date, formatted_rows)
        stats = row_stats[r]
        row_rows.append({
            "row": r,
            "date": row_date or "",
            "row_kind": kind,
            "populated": stats["populated"],
            "issues": sum(stats[k] for k in issue_counts),
            "count_blank": stats["COUNT_BLANK"],
            "rate_blank": stats["RATE_BLANK"],
            "formula_error": stats["FORMULA_ERROR"],
            "rate_gt_100": stats["RATE_GT_100"],
            "count_negative": stats["COUNT_NEGATIVE"],
        })

    write_csv(out_dir / "columns.csv", column_rows, list(column_rows[0].keys()))
    write_csv(out_dir / "rows.csv", row_rows, list(row_rows[0].keys()))
    write_csv(
        out_dir / "cell_issues.csv",
        issue_rows,
        ["row", "col", "a1", "date", "header", "source", "status", "issues", "value"],
    )

    summary = {
        "spreadsheet_id": SPREADSHEET_ID,
        "tab": TAB,
        "range": RANGE,
        "generated_at": datetime.now(JST).isoformat(),
        "active_start": active_start_iso,
        "today": today_iso,
        "rows_evaluated": TOTAL_ROWS,
        "columns_evaluated": TOTAL_COLS,
        "cells_evaluated": TOTAL_ROWS * TOTAL_COLS,
        "date_rows": len(row_dates),
        "status_counts": dict(status_counts),
        "issue_counts": dict(issue_counts),
        "issue_cells": len(issue_rows),
        "outputs": {
            "cells": str(cells_path),
            "cell_issues": str(out_dir / "cell_issues.csv"),
            "columns": str(out_dir / "columns.csv"),
            "rows": str(out_dir / "rows.csv"),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    top_issue_cols = sorted(
        [row for row in column_rows if row["issues"]],
        key=lambda x: int(x["issues"]),
        reverse=True,
    )[:20]
    top_issue_rows = sorted(
        [row for row in row_rows if row["issues"]],
        key=lambda x: int(x["issues"]),
        reverse=True,
    )[:20]

    def table(rows: list[dict[str, Any]], fields: list[str]) -> str:
        if not rows:
            return "なし\n"
        out = ["|" + "|".join(fields) + "|", "|" + "|".join(["---"] * len(fields)) + "|"]
        for row in rows:
            out.append("|" + "|".join(str(row.get(f, "")) for f in fields) + "|")
        return "\n".join(out) + "\n"

    md = [
        "# 日ごとデータ 全セル監査 (クラウド版)",
        "",
        f"- 対象: `{TAB}!A1:FF3727`",
        f"- 評価セル数: {summary['cells_evaluated']:,}",
        f"- 評価列数: {TOTAL_COLS}",
        f"- 評価行数: {TOTAL_ROWS}",
        f"- 日付行数: {len(row_dates)}",
        f"- issueセル数: {len(issue_rows):,}",
        "",
        "## Issue Counts",
        "```json",
        json.dumps(summary["issue_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Issueが多い列 Top20",
        table(top_issue_cols, ["col", "header", "source", "issues", "count_blank", "rate_blank", "formula_error", "rate_gt_100", "count_negative"]),
        "## Issueが多い行 Top20",
        table(top_issue_rows, ["row", "date", "row_kind", "issues", "count_blank", "rate_blank", "formula_error", "rate_gt_100", "count_negative"]),
    ]
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    notify_discord_if_issues(summary, issue_rows)
    if args.fail_on_issue and issue_rows:
        sys.exit(2)


if __name__ == "__main__":
    main()
