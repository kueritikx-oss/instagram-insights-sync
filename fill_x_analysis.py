#!/usr/bin/env python3
"""「X投稿毎データ」の結果①〜④・考察・次への示唆を自動生成する (fill_x_analysis.py)

fill_post_analysis.py (IG版) の設計・文体を踏襲したX最小PDCA版。
1d_views が到着済みの行に対して、パーセンタイル順位・フック別/カテゴリ別
ベンチマーク比較から個別化された考察を生成して書き込む。

書き込み6列(結果①〜次の投稿に活かすポイント)は行ヘッダーから名前解決し、
連続していることを起動時に検証する(ズレたまま書くより安全)。

Usage:
    python3 fill_x_analysis.py --dry-run          # 内容だけ表示
    python3 fill_x_analysis.py                    # 空欄のみ補完
    python3 fill_x_analysis.py --force            # 既存の考察も上書き
    python3 fill_x_analysis.py --rows 4-30        # 対象行範囲を限定
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

# Sheets認証・シート定数はX自動投稿の実装を流用(cloud_auto_post_x.py自体は編集禁止)
from cloud_auto_post_x import (
    X_SHEET_NAME,
    X_SPREADSHEET_ID,
    DATA_START_ROW,
    _col_idx_to_letter,
    get_col_value,
    get_sheets_service,
)

SHEET_MAX_ROW = 1000

# ヘッダー名 → キー(名前解決・ハードコード禁止)
REQUIRED_COLUMNS = {
    "num": "番号",
    "hook": "フック",
    "cta": "CTA型",
    "frame": "施策枠",
    "category": "カテゴリ",
    "type": "タイプ",
    "body": "テキスト(280字)",
    "status": "ステータス",
    "v1_views": "1d_views",
    "v1_likes": "1d_likes",
    "v1_rt": "1d_RT",
    "v1_reply": "1d_リプ",
    "v7_views": "7d_views",
    "v7_likes": "7d_likes",
    "v7_rt": "7d_RT",
    "v7_reply": "7d_リプ",
    "result1": "結果①",
    "result2": "結果②",
    "result3": "結果③",
    "result4": "結果④",
    "analysis": "考察・仮説",
    "next": "次の投稿に活かすポイント",
}

# ── X特化フックパターン(x-flagship-post 10種ベース) ──
HOOK_PATTERNS = {
    "衝撃数字": [r"\d+[%％]", r"\d+人", r"\d+年", r"\d+日", r"\d+回", r"\d+選", r"\d+つ"],
    "否定・逆張り": ["やめ", "しない", "いらない", "実は", "逆に", "間違い", "むしろ"],
    "断言": ["断言", "結論", "これだけ", "一択", "しかない"],
    "恐怖・危機感": ["一生", "悪化", "手遅れ", "NG", "危険", "やばい", "損"],
    "問いかけ": [r"[？?]"],
    "共感・あるある": ["あるある", "わかる", "やりがち", "ありがち"],
    "Before/After": ["ビフォー", "before", "変わっ", "変化"],
    "自己体験": ["僕", "俺", "自分が", "体験", "実際に"],
    "リスト・具体性": ["リスト", "まとめ", "ランキング", "一覧", "手順", "ステップ"],
    "メカニズム": ["理由", "原因", "仕組み", "なぜ", "メカニズム"],
}


def detect_hooks(text: str) -> List[str]:
    hooks = []
    for name, patterns in HOOK_PATTERNS.items():
        for p in patterns:
            if re.search(p, text):
                hooks.append(name)
                break
    return hooks


def resolve_columns(service) -> Dict[str, int]:
    """行1〜3から「必要な列名を最も多く含む行」をヘッダー行として列を解決する。"""
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
            + ", ".join(missing))

    # 書き込み先6列(結果①〜次)の連続性を検証
    write_cols = [cols[k] for k in ("result1", "result2", "result3", "result4",
                                    "analysis", "next")]
    if write_cols != list(range(write_cols[0], write_cols[0] + 6)):
        raise SystemExit(f"❌ 結果①〜次の6列が連続していません: {write_cols}")

    print(f"🧭 列マップ解決: 結果①〜次={_col_idx_to_letter(cols['result1'])}:"
          f"{_col_idx_to_letter(cols['next'])} "
          f"1d_views={_col_idx_to_letter(cols['v1_views'])}")
    return cols


def safe_int(val: str) -> Optional[int]:
    """カンマ区切り数値をintに。空/非数値はNone(未取得と0を区別する)"""
    if not val or not str(val).strip():
        return None
    try:
        return int(str(val).strip().replace(",", ""))
    except ValueError:
        return None


def nz(v: Optional[int]) -> int:
    return v if v is not None else 0


def percentile_rank(value: float, sorted_values: List[float]) -> int:
    """値のパーセンタイル順位を返す(上位X%)"""
    if not sorted_values:
        return 50
    count_below = sum(1 for v in sorted_values if v < value)
    return 100 - int((count_below / len(sorted_values)) * 100)


def compute_benchmarks(posts: List[Dict]) -> Dict[str, Any]:
    bm: Dict[str, Any] = {}
    data = [p for p in posts if p["views"] is not None and p["views"] > 0]
    if not data:
        return bm
    bm["all"] = {
        "views": sorted(p["views"] for p in data),
        "likes": sorted(nz(p["likes"]) for p in data),
        "rt": sorted(nz(p["rt"]) for p in data),
        "reply": sorted(nz(p["reply"]) for p in data),
        "eng_rate": sorted(p["eng_rate"] for p in data),
        "mean_views": statistics.mean(p["views"] for p in data),
        "median_views": statistics.median(p["views"] for p in data),
        "mean_likes": statistics.mean(nz(p["likes"]) for p in data),
        "mean_eng_rate": statistics.mean(p["eng_rate"] for p in data),
        "count": len(data),
    }
    for group_key in ("hook_types", "category", "frame", "type"):
        groups = defaultdict(list)
        for p in data:
            keys = p[group_key] if isinstance(p[group_key], list) else [p[group_key]]
            for k in keys:
                if k:
                    groups[k].append(p)
        bm[group_key] = {
            k: {
                "mean_views": statistics.mean(x["views"] for x in v),
                "mean_likes": statistics.mean(nz(x["likes"]) for x in v),
                "mean_eng_rate": statistics.mean(x["eng_rate"] for x in v),
                "count": len(v),
            }
            for k, v in groups.items() if len(v) >= 2
        }
    bm["_data"] = data
    return bm


def generate_result1(p: Dict, bm: Dict) -> str:
    """結果①: コアKPI + 全投稿中パーセンタイル + フック別平均との差"""
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    parts = [f"views {p['views']:,}（全{bm['all']['count']}投稿中 上位{views_pct}%）",
             f"いいね{nz(p['likes'])}", f"RT{nz(p['rt'])}", f"リプ{nz(p['reply'])}",
             f"エンゲージ率{p['eng_rate']:.2f}%"]
    # フック別平均との差(最初にヒットしたフック型)
    for h in p["hook_types"]:
        hb = bm.get("hook_types", {}).get(h)
        if hb and hb["mean_views"] > 0:
            diff = (p["views"] - hb["mean_views"]) / hb["mean_views"] * 100
            parts.append(f"フック「{h}」平均{hb['mean_views']:.0f}比"
                         f"{'+' if diff >= 0 else ''}{diff:.0f}%（{hb['count']}件）")
            break
    return " | ".join(parts)


def generate_result2(p: Dict, bm: Dict) -> str:
    """結果②: 強み・課題の特定"""
    strengths, weaknesses = [], []
    metrics = [
        ("views", p["views"], bm["all"]["views"]),
        ("いいね", nz(p["likes"]), bm["all"]["likes"]),
        ("RT", nz(p["rt"]), bm["all"]["rt"]),
        ("リプ", nz(p["reply"]), bm["all"]["reply"]),
        ("エンゲージ率", p["eng_rate"], bm["all"]["eng_rate"]),
    ]
    for name, value, sorted_vals in metrics:
        pct = percentile_rank(value, sorted_vals)
        if pct <= 20 and value > 0:
            strengths.append(f"{name}（上位{pct}%）")
        elif pct >= 70 and sorted_vals and sorted_vals[-1] > 0:
            # 全投稿0の指標は課題として挙げない(比較の意味がない)
            weaknesses.append(f"{name}（下位{max(100 - pct, 1)}%）")
    parts = []
    if strengths:
        parts.append(f"強み: {', '.join(strengths)}")
    if weaknesses:
        parts.append(f"課題: {', '.join(weaknesses)}")
    if not parts:
        parts.append("全指標中位圏。突出も欠落もなし")
    cat = p["category"]
    cb = bm.get("category", {}).get(cat)
    if cb and cb["count"] >= 3 and cb["mean_views"] > 0:
        ratio = p["views"] / cb["mean_views"]
        if ratio > 1.3:
            parts.append(f"「{cat}」カテゴリ内で高パフォーマンス（平均比+{(ratio - 1) * 100:.0f}%）")
        elif ratio < 0.7:
            parts.append(f"「{cat}」カテゴリ平均を下回る（平均比{(ratio - 1) * 100:.0f}%）")
    return " | ".join(parts)


def generate_result3(p: Dict, bm: Dict) -> str:
    """結果③: 1d→7dの伸び率(7dデータがある場合のみ)"""
    if p["views_7d"] is None or p["views_7d"] <= 0:
        return "7dデータ未取得"
    parts = []
    growth = (p["views_7d"] - p["views"]) / p["views"] * 100 if p["views"] > 0 else 0
    parts.append(f"1d→7d views成長: {'+' if growth >= 0 else ''}{growth:.0f}%"
                 f"（{p['views']:,}→{p['views_7d']:,}）")
    if p["likes_7d"] is not None and nz(p["likes"]) > 0 and p["likes_7d"] > nz(p["likes"]):
        lg = (p["likes_7d"] - nz(p["likes"])) / nz(p["likes"]) * 100
        parts.append(f"いいね成長: +{lg:.0f}%（{nz(p['likes'])}→{p['likes_7d']}）")
    if p["rt_7d"] is not None and p["rt_7d"] > nz(p["rt"]):
        parts.append(f"RT成長: {nz(p['rt'])}→{p['rt_7d']}")
    if growth >= 30:
        parts.append("初動後も伸び続けるロングテール型")
    elif growth <= 5:
        parts.append("初動で完結する短命型（Xの標準的減衰）")
    return " | ".join(parts)


def generate_result4(p: Dict, bm: Dict) -> str:
    """結果④: エンゲージメント効率・拡散の質"""
    parts = []
    avg = bm["all"]["mean_eng_rate"]
    diff = (p["eng_rate"] - avg) / avg * 100 if avg > 0 else 0
    parts.append(f"エンゲージ効率{p['eng_rate']:.2f}%"
                 f"（平均{avg:.2f}%比{'+' if diff >= 0 else ''}{diff:.0f}%）")
    if nz(p["rt"]) > 0 and p["views"] > 0:
        parts.append(f"RT率{nz(p['rt']) / p['views'] * 100:.3f}%（拡散寄与）")
    if nz(p["reply"]) >= 3:
        parts.append(f"リプ{nz(p['reply'])}件は会話誘発型。アルゴリズム上の追い風")
    tb = bm.get("type", {}).get(p["type"])
    if tb and tb["count"] >= 3 and tb["mean_views"] > 0:
        ratio = p["views"] / tb["mean_views"]
        sign = "+" if ratio >= 1 else ""
        parts.append(f"タイプ「{p['type']}」平均比{sign}{(ratio - 1) * 100:.0f}%")
    return " | ".join(parts)


def generate_analysis(p: Dict, bm: Dict) -> str:
    """考察・仮説: フック×カテゴリ×数値根拠の個別化文"""
    insights = []
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    hooks = p["hook_types"]

    # 1. views分析(なぜ伸びた/伸びなかった)
    if views_pct <= 10:
        if hooks:
            insights.append(f"views {p['views']:,}は上位{views_pct}%。"
                            f"フック「{'・'.join(hooks[:2])}」が初動のインプレッション獲得を牽引した可能性が高い")
        else:
            insights.append(f"views {p['views']:,}は上位{views_pct}%。"
                            f"テーマ自体の需要の高さ、または投稿時間帯の追い風")
    elif views_pct <= 25:
        insights.append(f"views {p['views']:,}は上位{views_pct}%で平均以上。安定した露出を確保")
    elif views_pct >= 70:
        cb = bm.get("category", {}).get(p["category"])
        if cb and cb["count"] >= 3 and p["views"] < cb["mean_views"] * 0.7:
            insights.append(f"views {p['views']:,}は「{p['category']}」カテゴリ平均"
                            f"{cb['mean_views']:.0f}を大きく下回る。冒頭1行のフック力に改善余地")
        else:
            insights.append(f"views {p['views']:,}は下位{100 - views_pct}%。"
                            f"初動30分の反応が弱くタイムライン露出が伸びなかった可能性")
    else:
        insights.append(f"views {p['views']:,}（上位{views_pct}%）は中位圏")

    # 2. フック型の効果検証
    for h in hooks[:1]:
        hb = bm.get("hook_types", {}).get(h)
        if not hb:
            continue
        all_mean = bm["all"]["mean_views"]
        h_vs_all = (hb["mean_views"] - all_mean) / all_mean * 100 if all_mean > 0 else 0
        if h_vs_all > 30:
            if p["views"] >= hb["mean_views"]:
                insights.append(f"フック「{h}」は高効果（平均views{hb['mean_views']:.0f}、"
                                f"全体比+{h_vs_all:.0f}%）で、この投稿もポテンシャルを活かせている")
            else:
                insights.append(f"フック「{h}」は本来高効果（平均views{hb['mean_views']:.0f}）だが"
                                f"この投稿は未達。本文の具体性・数字の強さに改善余地")
        elif h_vs_all < -20:
            insights.append(f"フック「{h}」は全体平均より低効果（平均views{hb['mean_views']:.0f}）。"
                            f"「衝撃数字」「否定・逆張り」等の高効果フックへの切り替えを検討")

    # 3. いいね vs RT のバランス(共感 vs 拡散)
    if nz(p["likes"]) == 0 and nz(p["rt"]) == 0 and nz(p["reply"]) == 0:
        insights.append(f"views {p['views']:,}に対しいいね・RT・リプすべて0。"
                        f"見られてはいるが反応するほどの引っかかりがなく、"
                        f"主張の尖り・具体性・共感ポイントの明示が弱い")
    elif nz(p["likes"]) > 0 and nz(p["rt"]) == 0:
        insights.append("いいねのみでRT 0。共感は得たが「他人に見せたい」拡散価値が弱い")
    elif nz(p["rt"]) >= 2:
        insights.append(f"RT{nz(p['rt'])}件は拡散シグナル。フォロワー外への露出ルートが機能")

    # 4. リプ(会話)分析
    if nz(p["reply"]) == 0 and views_pct <= 30:
        insights.append("露出は取れたがリプ0。問いかけ・余白がなく会話が生まれていない")

    # 5. 高views×低エンゲージ = 素通り
    eng_pct = percentile_rank(p["eng_rate"], bm["all"]["eng_rate"])
    if views_pct <= 25 and eng_pct >= 70:
        insights.append("高viewsだがエンゲージ率は低め。露出先で素通りされており、"
                        "本文の深さ・具体性が足りない可能性")
    if views_pct >= 60 and eng_pct <= 20:
        insights.append(f"viewsは少ないがエンゲージ率{p['eng_rate']:.2f}%は高い。"
                        f"コアフォロワーに刺さるテーマで、露出さえ増えればスケールする余地あり")

    return "。".join(insights[:5])


def generate_next_action(p: Dict, bm: Dict) -> str:
    """次の投稿に活かすポイント: 具体的で再現可能なアクション"""
    actions = []
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    hooks = p["hook_types"]

    if views_pct <= 15:
        if hooks:
            actions.append(f"このフック（{'・'.join(hooks[:2])}）は高viewsに直結。"
                           f"同パターンで別テーマを展開する")
        cb = bm.get("category", {}).get(p["category"])
        if cb:
            actions.append(f"「{p['category']}」×この構成は勝ちパターン。シリーズ化を検討")

    if views_pct >= 60:
        top_hooks = sorted(bm.get("hook_types", {}).items(),
                           key=lambda x: -x[1]["mean_views"])[:2]
        if top_hooks:
            hs = ", ".join(f"{h}（平均views{d['mean_views']:.0f}）" for h, d in top_hooks)
            actions.append(f"views改善: 高効果フックを導入 → {hs}")
        else:
            actions.append("views改善: 冒頭1行に衝撃数字か否定・逆張りを入れて初見の引きを強化")

    if nz(p["reply"]) == 0:
        actions.append("末尾に問いかけを1つ入れてリプ(会話シグナル)を誘発する")
    if nz(p["rt"]) == 0 and views_pct <= 40:
        actions.append("「知らないと損する」型の情報密度を上げてRT(拡散)動機を作る")
    if p["eng_rate"] < bm["all"]["mean_eng_rate"] * 0.7:
        actions.append("エンゲージ率が平均以下。1ツイート1メッセージに絞り、具体的数字と体験談で密度を上げる")

    if not actions:
        best = sorted(bm.get("hook_types", {}).items(),
                      key=lambda x: -x[1]["mean_views"])[:1]
        if best:
            actions.append(f"全指標中位圏。次はフック「{best[0][0]}」"
                           f"（平均views{best[0][1]['mean_views']:.0f}）で初見の引きを強化する")
        else:
            actions.append("全指標中位圏。冒頭1行のフックを数字入りに変えてABテストする")

    return "。".join(actions[:3])


def main():
    parser = argparse.ArgumentParser(description="X投稿分析を自動生成")
    parser.add_argument("--force", action="store_true", help="既存の考察も上書き")
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず内容だけ表示")
    parser.add_argument("--rows", type=str, default=None, help="対象行範囲（例: 4-30）")
    args = parser.parse_args()

    service = get_sheets_service()
    cols = resolve_columns(service)

    last_col = _col_idx_to_letter(max(cols.values()))
    raw_rows = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID,
        range=f"'{X_SHEET_NAME}'!A{DATA_START_ROW}:{last_col}{SHEET_MAX_ROW}",
    ).execute().get("values", [])
    print(f"📊 {len(raw_rows)}行を読み込み")

    posts: List[Dict] = []
    for offset, row in enumerate(raw_rows):
        num = get_col_value(row, cols["num"])
        if not num:
            continue
        views = safe_int(get_col_value(row, cols["v1_views"]))
        likes = safe_int(get_col_value(row, cols["v1_likes"]))
        rt = safe_int(get_col_value(row, cols["v1_rt"]))
        reply = safe_int(get_col_value(row, cols["v1_reply"]))
        eng = nz(likes) + nz(rt) + nz(reply)
        hook_text = get_col_value(row, cols["hook"])
        body = get_col_value(row, cols["body"])
        posts.append({
            "row_idx": DATA_START_ROW + offset,
            "num": num,
            "hook_text": hook_text,
            "hook_types": detect_hooks(hook_text or body[:60]),
            "category": get_col_value(row, cols["category"]),
            "frame": get_col_value(row, cols["frame"]),
            "type": get_col_value(row, cols["type"]),
            "views": views,
            "likes": likes,
            "rt": rt,
            "reply": reply,
            "eng_rate": (eng / views * 100) if views else 0.0,
            "views_7d": safe_int(get_col_value(row, cols["v7_views"])),
            "likes_7d": safe_int(get_col_value(row, cols["v7_likes"])),
            "rt_7d": safe_int(get_col_value(row, cols["v7_rt"])),
            "existing_result1": get_col_value(row, cols["result1"]),
            "existing_analysis": get_col_value(row, cols["analysis"]),
            "existing_next": get_col_value(row, cols["next"]),
        })

    bm = compute_benchmarks(posts)
    if not bm.get("all"):
        print("📭 1d_viewsが入った投稿がまだありません。先に sync_x_insights.py を実行してください")
        return
    print(f"📈 ベンチマーク算出（データあり{bm['all']['count']}件）: "
          f"views平均{bm['all']['mean_views']:.0f} 中央値{bm['all']['median_views']:.0f} "
          f"エンゲージ率平均{bm['all']['mean_eng_rate']:.2f}%")
    top_hooks = sorted(bm.get("hook_types", {}).items(),
                       key=lambda x: -x[1]["mean_views"])[:3]
    if top_hooks:
        print("   フックTOP: " + ", ".join(
            f"{h}({d['mean_views']:.0f}/{d['count']}件)" for h, d in top_hooks))

    if args.rows:
        start, end = args.rows.split("-")
        row_start, row_end = int(start), int(end)
    else:
        row_start, row_end = DATA_START_ROW, SHEET_MAX_ROW

    updates = []
    generated = skipped = 0
    for p in posts:
        if not (row_start <= p["row_idx"] <= row_end):
            continue
        if p["views"] is None or p["views"] <= 0:
            skipped += 1
            continue
        if not args.force and (p["existing_result1"] and p["existing_analysis"]
                               and p["existing_next"]):
            skipped += 1
            continue

        r1 = generate_result1(p, bm)
        r2 = generate_result2(p, bm)
        r3 = generate_result3(p, bm)
        r4 = generate_result4(p, bm)
        analysis = generate_analysis(p, bm)
        next_action = generate_next_action(p, bm)

        if args.dry_run:
            print(f"\n{'=' * 60}")
            print(f"Row{p['row_idx']} {p['num']} [{p['category']}] {p['hook_text'][:50]}")
            print(f"  結果①: {r1}")
            print(f"  結果②: {r2}")
            print(f"  結果③: {r3}")
            print(f"  結果④: {r4}")
            print(f"  考察: {analysis}")
            print(f"  次: {next_action}")

        updates.append({
            "range": (f"'{X_SHEET_NAME}'!{_col_idx_to_letter(cols['result1'])}"
                      f"{p['row_idx']}:{_col_idx_to_letter(cols['next'])}{p['row_idx']}"),
            "majorDimension": "ROWS",
            "values": [[r1, r2, r3, r4, analysis, next_action]],
        })
        generated += 1

    print(f"\n📝 生成: {generated}件 | スキップ: {skipped}件")
    if args.dry_run:
        print("🔍 dry-runモード: 書き込みはしません")
        return
    if not updates:
        print("更新対象がありません。")
        return

    print(f"⬆️  {len(updates)}行を書き込み中...")
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=X_SPREADSHEET_ID,
            body={"valueInputOption": "RAW", "data": batch},
        ).execute()
        print(f"  バッチ {i // batch_size + 1}: {len(batch)}行更新完了")
        if i + batch_size < len(updates):
            time.sleep(1)

    # 読み戻し検証
    first = updates[0]
    back = service.spreadsheets().values().get(
        spreadsheetId=X_SPREADSHEET_ID, range=first["range"],
    ).execute().get("values", [[]])[0]
    if [str(v) for v in back] != [str(v) for v in first["values"][0]]:
        print(f"❌ 読み戻し不一致: {first['range']}")
        sys.exit(1)
    print(f"✅ 読み戻し検証OK: {first['range']}")
    print(f"✅ 完了: {generated}件の投稿分析を書き込みました")


if __name__ == "__main__":
    main()
