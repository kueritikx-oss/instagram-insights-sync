#!/usr/bin/env python3
"""X (旧Twitter) 投稿メトリクス同期 — twikit版 (sync_x_insights.py)

自分(@tackey_clear)のタイムラインをtwikitで取得し、「X投稿毎データ」の
ツイートIDと突合して views / likes / RT / リプ を書き込む。

Addy設計(2026-07 X最小PDCA基盤): IG98列のフルコピーは禁止。
メトリクスは 1日後 + 7日後 の2スナップショットのみ。

対象行の判定:
  - status=posted かつ ツイートIDあり
  - 投稿から20時間以上経過 かつ 1d未取得 → 1d列(1d_views〜1d取得日時)
  - 投稿から7日(±1日)経過 かつ 7d未取得 → 7d列(7d_views〜7d取得日時)

安全設計:
  - 列位置はヘッダー行(行1〜3から動的検出)の名前解決。ハードコード禁止
  - 取得失敗(タイムラインに見つからない等)はスキップ+警告。0埋め禁止(偽データ防止)
  - 既存データ列(A〜R)には一切書かない

認証:
  - X: twikit セッションCookie (TWITTER_COOKIES env / x_twikit_cookies.json)
  - Google Sheets: Service Account (GOOGLE_SERVICE_ACCOUNT_JSON) 優先

Usage:
    python3 sync_x_insights.py --dry-run
    python3 sync_x_insights.py
    python3 sync_x_insights.py --max-pages 20
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# cloud_auto_post_x のimport時にtwikit v2.3.3モンキーパッチ
# (ClientTransaction / User新GraphQL対応)が適用される。実装は流用のみ・編集禁止。
from cloud_auto_post_x import (
    JST,
    X_SHEET_NAME,
    X_SPREADSHEET_ID,
    DATA_START_ROW,
    _col_idx_to_letter,
    get_col_value,
    get_own_user_id,
    get_sheets_service,
    get_twikit_client,
    parse_schedule_time,
    save_cookies,
    snowflake_to_datetime,
)

SHEET_MAX_ROW = 1000

# 1d/7d 判定しきい値
H1D_MIN_HOURS = 20          # 20時間以上経過で1d取得対象
D7_MIN = timedelta(days=6)  # 7日±1日
D7_MAX = timedelta(days=8)

# ヘッダー名 → 変数キー(名前解決・ハードコード禁止)
REQUIRED_COLUMNS = {
    "date": "日付",
    "num": "番号",
    "time": "時刻",
    "status": "ステータス",
    "tweet_id": "ツイートID",
    "v1_views": "1d_views",
    "v1_likes": "1d_likes",
    "v1_rt": "1d_RT",
    "v1_reply": "1d_リプ",
    "v1_at": "1d取得日時",
    "v7_views": "7d_views",
    "v7_likes": "7d_likes",
    "v7_rt": "7d_RT",
    "v7_reply": "7d_リプ",
    "v7_at": "7d取得日時",
}


def resolve_columns(service) -> Dict[str, int]:
    """行1〜3からヘッダー行を自動検出し、列名→indexを解決する。

    実測ではシートの列名は行2(行1=セクション名、行3=書式ヒント)にあるが、
    行構成が変わっても壊れないよう「必要な列名を最も多く含む行」をヘッダー行とする。
    必須列が1つでも欠けたら即エラー終了(ズレたまま書くより安全)。
    """
    rows = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID,
        range=f"'{X_SHEET_NAME}'!1:3",
        valueRenderOption="FORMATTED_VALUE",
    ).execute().get("values", [])
    if not rows:
        raise SystemExit(f"❌ ヘッダーが読めません: {X_SHEET_NAME}")

    names = list(REQUIRED_COLUMNS.values())
    best_row, best_hits = None, -1
    for row in rows:
        stripped = [str(c).strip() for c in row]
        hits = sum(1 for n in names if n in stripped)
        if hits > best_hits:
            best_row, best_hits = stripped, hits

    cols: Dict[str, int] = {}
    missing: List[str] = []
    for key, name in REQUIRED_COLUMNS.items():
        try:
            cols[key] = best_row.index(name)
        except ValueError:
            missing.append(name)
    if missing:
        raise SystemExit(
            "❌ 必須列がヘッダーから見つかりません(列挿入/改名の可能性): "
            + ", ".join(missing)
        )

    # 1d/7dの各5列(views,likes,RT,リプ,取得日時)が連続していることを検証
    for prefix in ("v1", "v7"):
        seq = [cols[f"{prefix}_{k}"] for k in ("views", "likes", "rt", "reply", "at")]
        if seq != list(range(seq[0], seq[0] + 5)):
            raise SystemExit(f"❌ {prefix}の5列が連続していません: {seq}")

    print(f"🧭 列マップ解決: 1d={_col_idx_to_letter(cols['v1_views'])}:"
          f"{_col_idx_to_letter(cols['v1_at'])} "
          f"7d={_col_idx_to_letter(cols['v7_views'])}:"
          f"{_col_idx_to_letter(cols['v7_at'])} "
          f"tweetID={_col_idx_to_letter(cols['tweet_id'])}")
    return cols


def find_targets(rows: List[List[str]], cols: Dict[str, int], now: datetime):
    """1d/7d取得対象行を洗い出す。posted_atはsnowflake優先(実投稿時刻)、
    復元不能ならスケジュール日時にフォールバック。"""
    targets = []
    for i, row in enumerate(rows):
        actual_row = DATA_START_ROW + i
        if get_col_value(row, cols["status"]) != "posted":
            continue
        tweet_id = get_col_value(row, cols["tweet_id"])
        if not tweet_id or not tweet_id.isdigit():
            continue
        posted_at = snowflake_to_datetime(tweet_id)
        if posted_at is None:
            posted_at = parse_schedule_time(
                get_col_value(row, cols["date"]), get_col_value(row, cols["time"]))
        if posted_at is None:
            print(f"  ⚠️ 行{actual_row}: 投稿時刻を特定できずスキップ")
            continue
        age = now - posted_at
        need_1d = (age >= timedelta(hours=H1D_MIN_HOURS)
                   and not get_col_value(row, cols["v1_at"]))
        need_7d = (D7_MIN <= age <= D7_MAX
                   and not get_col_value(row, cols["v7_at"]))
        if not need_1d and not need_7d:
            continue
        targets.append({
            "row": actual_row,
            "num": get_col_value(row, cols["num"]),
            "tweet_id": tweet_id,
            "posted_at": posted_at,
            "need_1d": need_1d,
            "need_7d": need_7d,
        })
    return targets


def _tweet_metrics(tw) -> Dict[str, Optional[int]]:
    """twikit Tweetオブジェクトからメトリクスを抽出。欠落はNone(0埋め禁止)。"""
    def _num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {
        "views": _num(getattr(tw, "view_count", None)),
        "likes": _num(getattr(tw, "favorite_count", None)),
        "rt": _num(getattr(tw, "retweet_count", None)),
        "reply": _num(getattr(tw, "reply_count", None)),
    }


async def collect_timeline_metrics(client, needed_ids: set, oldest_needed: datetime,
                                   max_pages: int) -> Dict[str, Dict]:
    """自分のタイムラインをページングし、必要なtweet_idのメトリクスを収集する。"""
    metrics: Dict[str, Dict] = {}
    user_id = await get_own_user_id(client)
    if not user_id:
        print("❌ 自分のuser_idが特定できずタイムライン取得不可")
        return metrics

    tweets = await client.get_user_tweets(user_id, "Tweets", count=40)
    for page in range(max_pages):
        if not tweets:
            break
        oldest_in_page = None
        for tw in tweets:
            tid = str(tw.id)
            created = snowflake_to_datetime(tid)
            if tid in needed_ids:
                metrics[tid] = _tweet_metrics(tw)
            if created and (oldest_in_page is None or created < oldest_in_page):
                oldest_in_page = created
        if len(metrics) >= len(needed_ids):
            break
        if oldest_in_page and oldest_in_page < oldest_needed:
            break
        await asyncio.sleep(2)
        tweets = await tweets.next()
    print(f"   タイムライン照合: {len(metrics)}/{len(needed_ids)}件ヒット")
    return metrics


async def fetch_missing_by_id(client, missing_ids: List[str], limit: int = 10):
    """タイムラインで拾えなかった分をget_tweet_by_idで個別回収(上限つき)。"""
    metrics: Dict[str, Dict] = {}
    for tid in missing_ids[:limit]:
        try:
            tw = await client.get_tweet_by_id(tid)
            if tw is not None:
                metrics[tid] = _tweet_metrics(tw)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"  ⚠️ 個別取得失敗 {tid}: {str(e)[:100]}")
    return metrics


async def async_main(args) -> int:
    now = datetime.now(JST)
    print(f"📊 Xメトリクス同期 (twikit) - {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"   DryRun: {args.dry_run}")
    print()

    service = get_sheets_service()
    cols = resolve_columns(service)

    last_col = _col_idx_to_letter(max(cols.values()))
    rows = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID,
        range=f"'{X_SHEET_NAME}'!A{DATA_START_ROW}:{last_col}{SHEET_MAX_ROW}",
    ).execute().get("values", [])
    print(f"📋 {len(rows)}行を読み込み")

    targets = find_targets(rows, cols, now)
    if args.limit and len(targets) > args.limit:
        # 新しい投稿を優先(1dの鮮度が大事)
        targets.sort(key=lambda t: t["posted_at"], reverse=True)
        targets = targets[:args.limit]
    if not targets:
        print("📭 取得対象なし(全行取得済み or 経過時間の条件未達)")
        return 0

    n1 = sum(1 for t in targets if t["need_1d"])
    n7 = sum(1 for t in targets if t["need_7d"])
    print(f"🎯 対象: {len(targets)}行 (1d: {n1}件 / 7d: {n7}件)")

    client = await get_twikit_client()
    needed_ids = {t["tweet_id"] for t in targets}
    oldest_needed = min(t["posted_at"] for t in targets) - timedelta(hours=6)
    metrics = await collect_timeline_metrics(
        client, needed_ids, oldest_needed, args.max_pages)

    missing = [tid for tid in needed_ids if tid not in metrics]
    if missing:
        print(f"   個別回収を試行: {len(missing)}件(上限{args.by_id_limit})")
        metrics.update(await fetch_missing_by_id(client, missing, args.by_id_limit))

    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    written_1d = written_7d = skipped = 0
    for t in targets:
        m = metrics.get(t["tweet_id"])
        if not m or all(v is None for v in m.values()):
            print(f"  ⚠️ SKIP {t['num']} (行{t['row']}): "
                  f"メトリクス取得失敗 tweet_id={t['tweet_id']} (0埋めせず未取得のまま)")
            skipped += 1
            continue
        # None(欠落)は空文字=未取得のまま。0埋め禁止
        values = [("" if m[k] is None else m[k])
                  for k in ("views", "likes", "rt", "reply")] + [now_str]
        for flag, prefix in ((t["need_1d"], "v1"), (t["need_7d"], "v7")):
            if not flag:
                continue
            start = cols[f"{prefix}_views"]
            rng = (f"'{X_SHEET_NAME}'!{_col_idx_to_letter(start)}{t['row']}:"
                   f"{_col_idx_to_letter(start + 4)}{t['row']}")
            updates.append({"range": rng, "values": [values]})
            if prefix == "v1":
                written_1d += 1
            else:
                written_7d += 1
            label = "1d" if prefix == "v1" else "7d"
            print(f"  ✅ {t['num']} (行{t['row']}) [{label}]: "
                  f"views={m['views']} likes={m['likes']} RT={m['rt']} リプ={m['reply']}")

    save_cookies(client)

    print()
    print(f"📝 書き込み予定: 1d {written_1d}件 / 7d {written_7d}件 / スキップ {skipped}件")
    if args.dry_run:
        print("🔍 dry-runモード: 書き込みはしません")
        return 0
    if not updates:
        print("更新対象がありません。")
        return 0

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=X_SPREADSHEET_ID,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()

    # 読み戻し検証: 先頭の更新レンジを読み直して一致確認
    first = updates[0]
    back = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID, range=first["range"],
    ).execute().get("values", [[]])[0]
    expected = [str(v) for v in first["values"][0]]
    if [str(v) for v in back] != expected:
        print(f"❌ 読み戻し不一致: {first['range']} 期待={expected} 実際={back}")
        return 1
    print(f"✅ 読み戻し検証OK: {first['range']}")
    print(f"🎉 完了: {len(updates)}レンジを書き込みました")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Xメトリクス同期 (twikit)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-pages", type=int, default=40,
                        help="タイムライン取得の最大ページ数")
    parser.add_argument("--limit", type=int, default=0,
                        help="1回の実行で処理する最大行数(0=無制限)")
    parser.add_argument("--by-id-limit", type=int, default=10,
                        help="タイムライン照合漏れの個別取得上限")
    args = parser.parse_args()
    sys.exit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
