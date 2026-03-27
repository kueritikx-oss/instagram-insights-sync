#!/usr/bin/env python3
"""Instagram戦略ループ — データ→洞察→仮説→実験→学習の科学的PDCAを回す。

世界基準のSNSマーケティング分析フレームワーク（Buffer/Later/Sprout Social/
Alex Hormozi/Justin Welsh）を統合。既存スプレッドシートのデータだけで完結。

Modes:
    weekly    週次レビュー: TOP3/WORST3、多次元クロス分析、非フォロワーリーチ推移
    monthly   月次分析: ピラーヒートマップ、エバーグリーンスコア、収益相関
    hooks     フック銀行: 2x超えフックを自動抽出→リミックス候補提示
    experiment 実験管理: 仮説登録→ICEスコア→結果記録→学習サイクル

Usage:
    python3 utils/instagram_strategy_loop.py weekly
    python3 utils/instagram_strategy_loop.py weekly --dry-run
    python3 utils/instagram_strategy_loop.py monthly
    python3 utils/instagram_strategy_loop.py hooks
    python3 utils/instagram_strategy_loop.py hooks --top 20
    python3 utils/instagram_strategy_loop.py experiment --add "フック: 分類型" --variable hook --ice 8,7,9
    python3 utils/instagram_strategy_loop.py experiment --review
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 認証 ──────────────────────────────────────────────────────
_auth_dir = os.environ.get(
    "INSTAGRAM_INSIGHTS_GOOGLE_AUTH_DIR",
    "タッキー/02_SNS集客/instagram-auto-post",
)
TOKEN_FILE = os.path.join(_auth_dir, "token.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── スプレッドシートID ────────────────────────────────────────
POSTDATA_SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
POSTDATA_SHEET_NAME = "Instagram投稿毎データ"
WEEKLY_SHEET_ID = "12fghSF68JkhgqSvPmCa_nGeSXowRizo2MtRz4WyeXyo"
WEEKLY_SHEET_NAME = "週ごとInstagramデータ"
DAILY_SHEET_ID = "14IUZeZJPjP6CcpmQQZ6NRg6Vi1_CUIJL2hQ0tU-i_LE"
DAILY_SHEET_NAME = "日ごとデータ"
OPTIN_SHEET_ID = "1phTRkrdGACY4Vfgmv1zu9vJfHwlxsUIxQctS2Y5TAx0"

# 出力先: 投稿毎データと同じスプレッドシート内の新タブ
STRATEGY_SHEET_NAME = "戦略ループ"
HOOK_BANK_SHEET_NAME = "フック銀行"
EXPERIMENT_SHEET_NAME = "実験管理"

DATA_START_ROW = 4  # ヘッダーは1-3行

# ── 列マッピング（0-based）──────────────────────────────────
COL_DATE = 0        # A
COL_NUM = 2         # C
COL_TIME = 3        # D
COL_TITLE = 4       # E
COL_CTA = 5         # F: CTA種別（いいね/保存/フォロー/ウェブタップ/コメント）
COL_FORMAT = 6      # G: 形式（認知/価値提供/誘導）
COL_URL = 7         # H
COL_INTENT = 8      # I
COL_LF8 = 12        # M
COL_EMOTION = 13    # N
COL_RESULT1 = 16    # Q
COL_ANALYSIS = 20   # U
COL_NEXT = 21       # V

# 1日後
COL_1D_REACH = 22       # W
COL_1D_REACH_FW = 23    # X
COL_1D_REACH_NF = 24    # Y
COL_1D_LIKES = 39       # AN
COL_1D_SAVES = 40       # AO
COL_1D_COMMENTS = 41    # AP
COL_1D_SHARES = 42      # AQ
COL_1D_PROF = 43        # AR
COL_1D_FOLLOW = 44      # AS
COL_1D_SAVE_RATE = 46   # AU
COL_1D_PROF_RATE = 48   # AW

# 7日後
COL_7D_REACH = 51       # AZ
COL_7D_REACH_NF = 53    # BB
COL_7D_LIKES = 69       # BR
COL_7D_SAVES = 70       # BS
COL_7D_COMMENTS = 71    # BT
COL_7D_SHARES = 72      # BU
COL_7D_PROF = 73        # BV
COL_7D_FOLLOW = 74      # BW
COL_7D_SAVE_RATE = 76   # BY

# テーマ
COL_BIG_CATEGORY = 86   # CI

# 週ごとデータ列
WCOL_REACH_TOTAL = 18   # S
WCOL_REACH_FW = 19      # T
WCOL_REACH_NF = 20      # U: フォロワー以外（＝発見の数）
WCOL_PROF_ACCESS = 26   # AA
WCOL_FOLLOW_TOTAL = 27  # AB
WCOL_FOLLOW_UP = 28     # AC
WCOL_FOLLOW_DOWN = 29   # AD
WCOL_WEB_TAP = 30       # AE

# ── フックパターン検出 ─────────────────────────────────────
HOOK_PATTERNS = {
    "分類型": [r"部位別", r"ランキング", r"TOP\d", r"一覧", r"まとめ", r"\d+選"],
    "タブー型": [r"嫌われ", r"言えない", r"本音", r"NG", r"ダメ", r"やばい"],
    "逆説型": [r"やめた", r"しない", r"逆に", r"実は", r"むしろ"],
    "恐怖型": [r"一生", r"悪化", r"危険", r"取り返し", r"手遅れ"],
    "数字型": [r"\d+秒", r"\d+回", r"\d+日", r"\d+つ", r"\d+個"],
    "共感型": [r"あるある", r"わかる", r"やりがち", r"あなたも"],
    "驚き型": [r"まさか", r"意外", r"知らない", r"嘘", r"え？"],
    "体験型": [r"11年", r"350万", r"ビフォー", r"変わっ", r"before"],
}

CATEGORY_KEYWORDS = {
    "食事×ニキビ": ["食べ", "食材", "食事", "フルーツ", "サラダ", "キムチ", "はちみつ",
                   "ヨーグルト", "ビタミン", "コンビニ", "チョコ", "牛乳", "アイス",
                   "納豆", "サプリ", "プロテイン", "野菜", "ドリンク", "ルイボス"],
    "スキンケア": ["洗顔", "保湿", "化粧", "スキンケア", "美容液", "日焼け",
                 "クレンジング", "ヒルドイド"],
    "皮膚科・薬": ["皮膚科", "ベピオ", "ディフェリン", "薬"],
    "ルーティン": ["ルーティ", "習慣", "朝", "夜", "白湯", "ルール"],
    "睡眠": ["睡眠", "枕", "寝"],
    "属性×ニキビ": ["生理", "受験", "就活", "ストレス", "年齢", "思春期",
                  "大人", "男", "学生", "社会人"],
    "ニキビ知識": ["ニキビ", "毛穴", "肌荒れ", "ニキビ跡"],
    "Before/After": ["ビフォー", "before", "変化", "変わっ"],
    "メンタル": ["メンタル", "自信", "悩み", "ストレス"],
    "モテ・自己啓発": ["モテ", "カッコ", "好かれ", "好感度"],
    "運動・姿勢": ["姿勢", "猫背", "運動", "筋トレ", "ストレッチ"],
}

# ── 2026年グローバルベンチマーク（Sprout Social / Buffer / Social Insider）──
BENCHMARKS = {
    "nano_er": (4.0, 7.0),        # エンゲージメント率 (nano: <10K)
    "nano_save_rate": (1.0, 2.0), # 保存率
    "nano_share_rate": (0.5, 1.0),# DMシェア率
    "nano_prof_rate": (1.0, 3.0), # プロフアクセス率
    "nf_reach_pct": (30, 60),     # 非フォロワーリーチ割合(%)
}


# ── ユーティリティ ─────────────────────────────────────────
def get_service():
    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds)


def safe_int(val) -> int:
    if not val or (isinstance(val, str) and not val.strip()):
        return 0
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def safe_float(val) -> float:
    if not val or (isinstance(val, str) and not val.strip()):
        return 0.0
    try:
        return float(str(val).replace("%", "").replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def cell(row: list, idx: int) -> str:
    if idx < len(row):
        v = row[idx]
        return v.strip() if isinstance(v, str) else str(v)
    return ""


def parse_date(date_str: str) -> Optional[datetime]:
    date_str = date_str.strip()
    if not date_str:
        return None
    # "3/9 月" → "3/9"
    date_str = re.sub(r"\s*[月火水木金土日]$", "", date_str)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d", "%m月%d日"):
        try:
            d = datetime.strptime(date_str, fmt)
            if d.year == 1900:
                d = d.replace(year=2026)
            return d
        except ValueError:
            continue
    return None


def pct_rank(value: float, sorted_vals: List[float]) -> int:
    """上位何%かを返す (1=最上位)"""
    if not sorted_vals:
        return 50
    pos = sum(1 for v in sorted_vals if v >= value)
    return max(1, round(pos / len(sorted_vals) * 100))


def median_val(vals: List[float]) -> float:
    nums = [v for v in vals if v > 0]
    return statistics.median(nums) if nums else 0


def mean_val(vals: List[float]) -> float:
    nums = [v for v in vals if v > 0]
    return sum(nums) / len(nums) if nums else 0


def detect_category(title: str) -> str:
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in title for kw in keywords):
            return cat
    return "その他"


def detect_hooks(title: str) -> List[str]:
    hooks = []
    for pattern_name, patterns in HOOK_PATTERNS.items():
        for p in patterns:
            if re.search(p, title):
                hooks.append(pattern_name)
                break
    return hooks


def composite_score(post: dict) -> float:
    """Sprout Social式重み付き総合スコア: シェア40% + 保存30% + プロフ20% + いいね10%"""
    shares = post.get("shares_7d") or post.get("shares_1d") or 0
    saves = post.get("saves_7d") or post.get("saves_1d") or 0
    prof = post.get("profile_7d") or post.get("profile_1d") or 0
    likes = post.get("likes_7d") or post.get("likes_1d") or 0
    return shares * 0.4 + saves * 0.3 + prof * 0.2 + likes * 0.1


def evergreen_score(post: dict) -> Optional[float]:
    """エバーグリーンスコア = 7日リーチ / 1日リーチ。1.0超 = 伸び続けている"""
    r1 = post.get("reach_1d", 0)
    r7 = post.get("reach_7d", 0)
    if r1 and r1 > 0 and r7 and r7 > 0:
        return round(r7 / r1, 2)
    return None


# ── データ読み取り ─────────────────────────────────────────
def read_all_posts(service) -> List[dict]:
    result = service.spreadsheets().values().get(
        spreadsheetId=POSTDATA_SHEET_ID,
        range=f"'{POSTDATA_SHEET_NAME}'!A{DATA_START_ROW}:CT",
    ).execute()
    raw = result.get("values", [])
    posts = []
    for i, row in enumerate(raw):
        date_str = cell(row, COL_DATE)
        dt = parse_date(date_str)
        if not dt:
            continue
        reach_1d = safe_int(cell(row, COL_1D_REACH))
        if not reach_1d:
            continue
        title = cell(row, COL_TITLE)
        posts.append({
            "row": DATA_START_ROW + i,
            "date": dt,
            "date_str": date_str,
            "post_num": cell(row, COL_NUM),
            "title": title,
            "cta": cell(row, COL_CTA),
            "format": cell(row, COL_FORMAT),
            "url": cell(row, COL_URL),
            "lf8": cell(row, COL_LF8),
            "emotion": cell(row, COL_EMOTION),
            "category": cell(row, COL_BIG_CATEGORY) or detect_category(title),
            "hooks": detect_hooks(title),
            # 1日後
            "reach_1d": reach_1d,
            "reach_nf_1d": safe_int(cell(row, COL_1D_REACH_NF)),
            "likes_1d": safe_int(cell(row, COL_1D_LIKES)),
            "saves_1d": safe_int(cell(row, COL_1D_SAVES)),
            "comments_1d": safe_int(cell(row, COL_1D_COMMENTS)),
            "shares_1d": safe_int(cell(row, COL_1D_SHARES)),
            "profile_1d": safe_int(cell(row, COL_1D_PROF)),
            "follows_1d": safe_int(cell(row, COL_1D_FOLLOW)),
            "save_rate_1d": safe_float(cell(row, COL_1D_SAVE_RATE)),
            "prof_rate_1d": safe_float(cell(row, COL_1D_PROF_RATE)),
            # 7日後
            "reach_7d": safe_int(cell(row, COL_7D_REACH)),
            "reach_nf_7d": safe_int(cell(row, COL_7D_REACH_NF)),
            "likes_7d": safe_int(cell(row, COL_7D_LIKES)),
            "saves_7d": safe_int(cell(row, COL_7D_SAVES)),
            "comments_7d": safe_int(cell(row, COL_7D_COMMENTS)),
            "shares_7d": safe_int(cell(row, COL_7D_SHARES)),
            "profile_7d": safe_int(cell(row, COL_7D_PROF)),
            "follows_7d": safe_int(cell(row, COL_7D_FOLLOW)),
        })
    return posts


def read_weekly_data(service) -> List[dict]:
    # Row 1=header, Row 2=subheader, Row 3+=data
    result = service.spreadsheets().values().get(
        spreadsheetId=WEEKLY_SHEET_ID,
        range=f"'{WEEKLY_SHEET_NAME}'!A3:AF",
    ).execute()
    raw = result.get("values", [])
    weeks = []
    for row in raw:
        if len(row) < 19:
            continue
        reach = safe_int(cell(row, WCOL_REACH_TOTAL))
        if not reach:
            continue  # リーチデータなし行はスキップ
        weeks.append({
            "start": cell(row, 0),
            "end": cell(row, 1),
            "reach_total": reach,
            "reach_fw": safe_int(cell(row, WCOL_REACH_FW)),
            "reach_nf": safe_int(cell(row, WCOL_REACH_NF)),
            "prof_access": safe_int(cell(row, WCOL_PROF_ACCESS)),
            "follow_total": safe_int(cell(row, WCOL_FOLLOW_TOTAL)),
            "follow_up": safe_int(cell(row, WCOL_FOLLOW_UP)),
            "follow_down": safe_int(cell(row, WCOL_FOLLOW_DOWN)),
            "web_tap": safe_int(cell(row, WCOL_WEB_TAP)),
        })
    return weeks


# ═══════════════════════════════════════════════════════════
#  MODE 1: WEEKLY REVIEW（週次レビュー）
# ═══════════════════════════════════════════════════════════
def run_weekly(posts: List[dict], weekly_data: List[dict], dry_run: bool, service=None):
    today = datetime.now()
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)

    this_week = [p for p in posts if p["date"] >= week_ago]
    last_week = [p for p in posts if two_weeks_ago <= p["date"] < week_ago]
    all_reaches = sorted([p["reach_1d"] for p in posts])
    all_saves = sorted([p["saves_1d"] for p in posts])

    if not this_week:
        print("⚠️  今週の投稿データがありません（1日後データ取得済みの投稿のみ対象）")
        return

    # ── 1. TOP3 / WORST3 ──
    scored = sorted(this_week, key=composite_score, reverse=True)
    top3 = scored[:3]
    worst3 = scored[-3:] if len(scored) >= 3 else scored

    output = []
    output.append("=" * 70)
    output.append(f"📊 週次戦略レビュー  {week_ago.strftime('%m/%d')}〜{today.strftime('%m/%d')}")
    output.append(f"   投稿数: {len(this_week)} | 全期間データ: {len(posts)}投稿")
    output.append("=" * 70)

    # ── ヘルスチェック（5秒概観）──
    avg_reach = mean_val([p["reach_1d"] for p in this_week])
    avg_saves = mean_val([p["saves_1d"] for p in this_week])
    avg_shares = mean_val([p["shares_1d"] for p in this_week])
    avg_prof = mean_val([p["profile_1d"] for p in this_week])
    total_follows = sum(p["follows_1d"] for p in this_week)
    nf_reaches = [p["reach_nf_1d"] for p in this_week if p["reach_1d"] > 0]
    avg_nf_pct = mean_val([p["reach_nf_1d"] / p["reach_1d"] * 100
                           for p in this_week if p["reach_1d"] > 0])

    # 前週比
    prev_reach = mean_val([p["reach_1d"] for p in last_week]) if last_week else 0
    reach_delta = ((avg_reach / prev_reach) - 1) * 100 if prev_reach > 0 else 0

    output.append("")
    output.append("── ① ヘルスチェック（5秒概観）──")
    output.append(f"  平均リーチ:     {avg_reach:,.0f}  (前週比 {reach_delta:+.1f}%)")
    output.append(f"  平均保存:       {avg_saves:,.1f}  (全体中央値: {median_val(all_saves):,.0f})")
    output.append(f"  平均シェア:     {avg_shares:,.1f}")
    output.append(f"  平均プロフ:     {avg_prof:,.1f}")
    output.append(f"  新規フォロー:   {total_follows}")
    output.append(f"  非フォロワー率: {avg_nf_pct:.1f}%  "
                  f"({'✅ 発見タブ好調' if avg_nf_pct >= 30 else '⚠️ 発見タブ弱い'})")

    # ── TOP3 ──
    output.append("")
    output.append("── ② TOP3（総合スコア: シェア40%+保存30%+プロフ20%+いいね10%）──")
    for i, p in enumerate(top3, 1):
        score = composite_score(p)
        eg = evergreen_score(p)
        eg_str = f"  🌿EG={eg}" if eg else ""
        hooks_str = "・".join(p["hooks"]) if p["hooks"] else "—"
        output.append(f"  {i}. [{p['post_num']}] {p['title'][:40]}")
        output.append(f"     リーチ={p['reach_1d']:,} 保存={p['saves_1d']} "
                      f"シェア={p['shares_1d']} プロフ={p['profile_1d']}")
        output.append(f"     CTA={p['cta']} 形式={p['format']} "
                      f"カテゴリ={p['category']} フック=[{hooks_str}]{eg_str}")
        output.append(f"     上位: リーチ{pct_rank(p['reach_1d'], all_reaches)}% "
                      f"保存{pct_rank(p['saves_1d'], all_saves)}%")

    # ── WORST3 ──
    output.append("")
    output.append("── ③ WORST3（改善ポイント特定）──")
    for i, p in enumerate(worst3, 1):
        hooks_str = "・".join(p["hooks"]) if p["hooks"] else "—"
        output.append(f"  {i}. [{p['post_num']}] {p['title'][:40]}")
        output.append(f"     リーチ={p['reach_1d']:,} 保存={p['saves_1d']} "
                      f"シェア={p['shares_1d']}")
        output.append(f"     CTA={p['cta']} 形式={p['format']} フック=[{hooks_str}]")

    # ── 多次元クロス分析 ──
    output.append("")
    output.append("── ④ 多次元クロス分析（カテゴリ × CTA × フック）──")

    # カテゴリ別
    cat_stats = defaultdict(lambda: {"reach": [], "saves": [], "shares": [], "count": 0})
    for p in this_week:
        c = cat_stats[p["category"]]
        c["reach"].append(p["reach_1d"])
        c["saves"].append(p["saves_1d"])
        c["shares"].append(p["shares_1d"])
        c["count"] += 1

    output.append("")
    output.append("  [カテゴリ別パフォーマンス]")
    output.append(f"  {'カテゴリ':<16} {'投稿数':>4} {'平均リーチ':>10} {'平均保存':>8} {'平均シェア':>8}")
    for cat, s in sorted(cat_stats.items(), key=lambda x: mean_val(x[1]["reach"]), reverse=True):
        output.append(f"  {cat:<16} {s['count']:>4} "
                      f"{mean_val(s['reach']):>10,.0f} "
                      f"{mean_val(s['saves']):>8,.1f} "
                      f"{mean_val(s['shares']):>8,.1f}")

    # CTA別
    cta_stats = defaultdict(lambda: {"reach": [], "saves": [], "prof": [], "count": 0})
    for p in this_week:
        c = cta_stats[p["cta"] or "未分類"]
        c["reach"].append(p["reach_1d"])
        c["saves"].append(p["saves_1d"])
        c["prof"].append(p["profile_1d"])
        c["count"] += 1

    output.append("")
    output.append("  [CTA別パフォーマンス]")
    output.append(f"  {'CTA':<12} {'投稿数':>4} {'平均リーチ':>10} {'平均保存':>8} {'平均プロフ':>8}")
    for cta, s in sorted(cta_stats.items(), key=lambda x: mean_val(x[1]["reach"]), reverse=True):
        output.append(f"  {cta:<12} {s['count']:>4} "
                      f"{mean_val(s['reach']):>10,.0f} "
                      f"{mean_val(s['saves']):>8,.1f} "
                      f"{mean_val(s['prof']):>8,.1f}")

    # フック別
    hook_stats = defaultdict(lambda: {"reach": [], "saves": [], "count": 0})
    for p in this_week:
        for h in p["hooks"]:
            hs = hook_stats[h]
            hs["reach"].append(p["reach_1d"])
            hs["saves"].append(p["saves_1d"])
            hs["count"] += 1
        if not p["hooks"]:
            hs = hook_stats["フックなし"]
            hs["reach"].append(p["reach_1d"])
            hs["saves"].append(p["saves_1d"])
            hs["count"] += 1

    output.append("")
    output.append("  [フック型別パフォーマンス]")
    output.append(f"  {'フック型':<14} {'使用回数':>6} {'平均リーチ':>10} {'平均保存':>8}")
    for hook, s in sorted(hook_stats.items(), key=lambda x: mean_val(x[1]["reach"]), reverse=True):
        output.append(f"  {hook:<14} {s['count']:>6} "
                      f"{mean_val(s['reach']):>10,.0f} "
                      f"{mean_val(s['saves']):>8,.1f}")

    # カテゴリ×フック クロス（最強の組み合わせ発見）
    cross = defaultdict(lambda: {"reach": [], "saves": [], "count": 0})
    for p in this_week:
        for h in (p["hooks"] or ["フックなし"]):
            key = f"{p['category']}×{h}"
            cross[key]["reach"].append(p["reach_1d"])
            cross[key]["saves"].append(p["saves_1d"])
            cross[key]["count"] += 1

    if cross:
        output.append("")
        output.append("  [カテゴリ×フック 最強組み合わせTOP5]")
        top_cross = sorted(cross.items(), key=lambda x: mean_val(x[1]["reach"]), reverse=True)[:5]
        for combo, s in top_cross:
            output.append(f"    {combo:<30} n={s['count']} "
                          f"リーチ={mean_val(s['reach']):,.0f} 保存={mean_val(s['saves']):,.1f}")

    # ── 非フォロワーリーチ推移 ──
    output.append("")
    output.append("── ⑤ 非フォロワーリーチ推移（発見タブ＝成長エンジン）──")
    if weekly_data and len(weekly_data) >= 2:
        recent_weeks = weekly_data[-8:]  # 直近8週
        for w in recent_weeks:
            nf_pct = (w["reach_nf"] / w["reach_total"] * 100) if w["reach_total"] > 0 else 0
            bar = "█" * int(nf_pct / 5)
            output.append(f"  {w['start']:<8}〜{w['end']:<8} "
                          f"全体={w['reach_total']:>8,} "
                          f"非FW={w['reach_nf']:>8,} "
                          f"({nf_pct:>5.1f}%) {bar}")

        # トレンド判定
        if len(recent_weeks) >= 4:
            first_half = mean_val([w["reach_nf"] for w in recent_weeks[:len(recent_weeks)//2]])
            second_half = mean_val([w["reach_nf"] for w in recent_weeks[len(recent_weeks)//2:]])
            trend = ((second_half / first_half) - 1) * 100 if first_half > 0 else 0
            if trend > 10:
                output.append(f"  📈 上昇トレンド (+{trend:.0f}%) — 発見タブ好調！")
            elif trend < -10:
                output.append(f"  📉 下降トレンド ({trend:.0f}%) — コンテンツ戦略要見直し")
            else:
                output.append(f"  ➡️  横ばい ({trend:+.0f}%)")
    else:
        output.append("  （週ごとデータ不足）")

    # ── エバーグリーンスコアTOP5 ──
    eg_posts = [(p, evergreen_score(p)) for p in this_week if evergreen_score(p) is not None]
    if eg_posts:
        output.append("")
        output.append("── ⑥ エバーグリーンスコアTOP5（7日リーチ÷1日リーチ。1.0超=伸び続け）──")
        eg_sorted = sorted(eg_posts, key=lambda x: x[1], reverse=True)[:5]
        for p, eg in eg_sorted:
            emoji = "🌿" if eg >= 1.5 else ("✅" if eg >= 1.0 else "⚡")
            output.append(f"  {emoji} [{p['post_num']}] EG={eg:.2f} "
                          f"(1d={p['reach_1d']:,} → 7d={p['reach_7d']:,}) "
                          f"{p['title'][:35]}")

    # ── 次週のアクション提案 ──
    output.append("")
    output.append("── ⑦ 次週のアクション（ICE式優先度）──")

    actions = []
    # 最強の組み合わせを推奨
    if top_cross:
        best_combo = top_cross[0][0]
        actions.append(f"🔥 最強の組み合わせ「{best_combo}」で新規投稿を作る (I=9 C=8 E=9 → 26)")

    # 非フォロワーリーチが低い場合
    if avg_nf_pct < 30:
        actions.append("📢 発見タブ対策: 分類型 or タブー型フックで非FWリーチを狙う (I=8 C=7 E=8 → 23)")

    # 保存率が低い場合
    if avg_saves < median_val(all_saves):
        actions.append("💾 保存率UP: リスト・図解形式で「保存推奨」CTAを入れる (I=7 C=8 E=9 → 24)")

    # TOP3の共通点を活かす
    if top3:
        top_hooks = set()
        for p in top3:
            top_hooks.update(p["hooks"])
        if top_hooks:
            actions.append(f"🎯 TOP3共通フック「{'・'.join(top_hooks)}」を次週も使う (I=8 C=9 E=9 → 26)")

    for a in actions[:4]:
        output.append(f"  {a}")

    output.append("")
    output.append("=" * 70)

    report = "\n".join(output)
    print(report)

    if not dry_run and service:
        _write_strategy_sheet(service, "週次レビュー", report, today)
        print(f"\n✅ 「{STRATEGY_SHEET_NAME}」シートに書き込みました")


# ═══════════════════════════════════════════════════════════
#  MODE 2: MONTHLY REVIEW（月次分析）
# ═══════════════════════════════════════════════════════════
def run_monthly(posts: List[dict], weekly_data: List[dict], dry_run: bool, service=None):
    today = datetime.now()
    month_ago = today - timedelta(days=30)
    this_month = [p for p in posts if p["date"] >= month_ago]
    prev_month = [p for p in posts if (month_ago - timedelta(days=30)) <= p["date"] < month_ago]

    if not this_month:
        print("⚠️  今月の投稿データがありません")
        return

    all_reaches = sorted([p["reach_1d"] for p in posts])
    all_saves = sorted([p["saves_1d"] for p in posts])

    output = []
    output.append("=" * 70)
    output.append(f"📈 月次戦略分析  {month_ago.strftime('%m/%d')}〜{today.strftime('%m/%d')}")
    output.append(f"   投稿数: {len(this_month)} | 全期間: {len(posts)}投稿")
    output.append("=" * 70)

    # ── 1. コンテンツピラーヒートマップ ──
    output.append("")
    output.append("── ① コンテンツピラーヒートマップ ──")
    output.append(f"  {'ピラー':<16} {'n':>3} {'平均リーチ':>10} {'保存率':>7} {'シェア率':>7} "
                  f"{'プロフ率':>7} {'FW数':>5} {'判定':>6}")

    pillar_data = defaultdict(lambda: {
        "reach": [], "save_rate": [], "share_rate": [], "prof_rate": [],
        "follows": [], "saves": [], "shares": [], "count": 0
    })
    for p in this_month:
        pd = pillar_data[p["category"]]
        pd["reach"].append(p["reach_1d"])
        pd["saves"].append(p["saves_1d"])
        pd["shares"].append(p["shares_1d"])
        pd["follows"].append(p["follows_1d"])
        if p["reach_1d"] > 0:
            pd["save_rate"].append(p["saves_1d"] / p["reach_1d"] * 100)
            pd["share_rate"].append(p["shares_1d"] / p["reach_1d"] * 100)
            pd["prof_rate"].append(p["profile_1d"] / p["reach_1d"] * 100)
        pd["count"] += 1

    overall_reach = mean_val([p["reach_1d"] for p in this_month])
    for pillar, d in sorted(pillar_data.items(), key=lambda x: mean_val(x[1]["reach"]), reverse=True):
        r = mean_val(d["reach"])
        sr = mean_val(d["save_rate"])
        shr = mean_val(d["share_rate"])
        pr = mean_val(d["prof_rate"])
        fw = sum(d["follows"])

        # 判定: 全体平均との比較
        r_mark = "🟢" if r > overall_reach * 1.2 else ("🔴" if r < overall_reach * 0.8 else "🟡")
        output.append(f"  {pillar:<16} {d['count']:>3} {r:>10,.0f} {sr:>6.1f}% {shr:>6.1f}% "
                      f"{pr:>6.1f}% {fw:>5} {r_mark}")

    # ── 2. エバーグリーンスコア分布 ──
    output.append("")
    output.append("── ② エバーグリーンスコア分布（7日÷1日リーチ）──")
    eg_scores = []
    for p in this_month:
        eg = evergreen_score(p)
        if eg is not None:
            eg_scores.append((p, eg))

    if eg_scores:
        evergreen_count = sum(1 for _, eg in eg_scores if eg >= 1.5)
        growing_count = sum(1 for _, eg in eg_scores if 1.0 <= eg < 1.5)
        spike_count = sum(1 for _, eg in eg_scores if eg < 1.0)
        output.append(f"  🌿 エバーグリーン (EG≥1.5): {evergreen_count}投稿 "
                      f"({evergreen_count/len(eg_scores)*100:.0f}%)")
        output.append(f"  ✅ 成長型 (1.0≤EG<1.5):     {growing_count}投稿")
        output.append(f"  ⚡ スパイク型 (EG<1.0):      {spike_count}投稿")
        output.append(f"  平均EGスコア: {mean_val([eg for _, eg in eg_scores]):.2f}")

        # エバーグリーンTOP5
        eg_top = sorted(eg_scores, key=lambda x: x[1], reverse=True)[:5]
        output.append("")
        output.append("  エバーグリーンTOP5（リポスト最優先候補）:")
        for p, eg in eg_top:
            output.append(f"    EG={eg:.2f} [{p['post_num']}] {p['title'][:40]}")

    # ── 3. 週次パフォーマンス推移 ──
    output.append("")
    output.append("── ③ 週次パフォーマンス推移 ──")

    # 投稿を週ごとにグループ化
    week_groups = defaultdict(list)
    for p in this_month:
        week_num = p["date"].isocalendar()[1]
        week_groups[week_num].append(p)

    output.append(f"  {'週':>4} {'投稿数':>5} {'平均リーチ':>10} {'平均保存':>8} {'平均シェア':>8} {'フォロー':>7}")
    for wk in sorted(week_groups.keys()):
        wp = week_groups[wk]
        output.append(f"  W{wk:>2} {len(wp):>5} "
                      f"{mean_val([p['reach_1d'] for p in wp]):>10,.0f} "
                      f"{mean_val([p['saves_1d'] for p in wp]):>8,.1f} "
                      f"{mean_val([p['shares_1d'] for p in wp]):>8,.1f} "
                      f"{sum(p['follows_1d'] for p in wp):>7}")

    # ── 4. 収益相関シグナル ──
    output.append("")
    output.append("── ④ 収益相関シグナル（投稿→LINE→売上の兆候）──")
    # プロフアクセスが高い投稿 = LINEリンク→リード転換の入口
    high_prof = sorted(this_month, key=lambda p: p["profile_1d"], reverse=True)[:5]
    output.append("  プロフアクセスTOP5（=LINE遷移候補の入口）:")
    for p in high_prof:
        output.append(f"    [{p['post_num']}] プロフ={p['profile_1d']} "
                      f"FW+{p['follows_1d']} {p['title'][:35]}")

    # ウェブタップ推移（週ごとデータから）
    if weekly_data and len(weekly_data) >= 4:
        recent = weekly_data[-4:]
        output.append("")
        output.append("  ウェブタップ推移（直近4週）:")
        for w in recent:
            bar = "█" * max(1, w["web_tap"])
            output.append(f"    {w['start']:<8} ウェブタップ={w['web_tap']:>3} {bar}")

    # ── 5. ベンチマーク比較 ──
    output.append("")
    output.append("── ⑤ グローバルベンチマーク比較（2026 Sprout Social/Buffer基準）──")

    avg_er = mean_val([(p["likes_1d"] + p["saves_1d"] + p["comments_1d"] + p["shares_1d"]) /
                       p["reach_1d"] * 100 for p in this_month if p["reach_1d"] > 0])
    avg_sr = mean_val([p["saves_1d"] / p["reach_1d"] * 100 for p in this_month if p["reach_1d"] > 0])
    avg_shr = mean_val([p["shares_1d"] / p["reach_1d"] * 100 for p in this_month if p["reach_1d"] > 0])
    avg_nf = mean_val([p["reach_nf_1d"] / p["reach_1d"] * 100
                       for p in this_month if p["reach_1d"] > 0])

    def bench_check(val, low, high, name):
        if val >= high:
            return f"  🟢 {name}: {val:.1f}%  (ベンチ: {low}-{high}%) ★世界基準超え"
        elif val >= low:
            return f"  🟡 {name}: {val:.1f}%  (ベンチ: {low}-{high}%) 基準内"
        else:
            return f"  🔴 {name}: {val:.1f}%  (ベンチ: {low}-{high}%) 要改善"

    output.append(bench_check(avg_er, *BENCHMARKS["nano_er"], "エンゲージメント率"))
    output.append(bench_check(avg_sr, *BENCHMARKS["nano_save_rate"], "保存率"))
    output.append(bench_check(avg_shr, *BENCHMARKS["nano_share_rate"], "DMシェア率"))
    output.append(bench_check(avg_nf, *BENCHMARKS["nf_reach_pct"], "非フォロワーリーチ%"))

    # ── 6. 来月のピラー配分提案 ──
    output.append("")
    output.append("── ⑥ 来月のピラー配分提案 ──")

    # スコアリング: リーチ×0.3 + 保存率×0.3 + プロフ率×0.2 + シェア率×0.2
    pillar_scores = {}
    for pillar, d in pillar_data.items():
        if d["count"] >= 2:  # 最低2投稿以上
            r_norm = mean_val(d["reach"]) / overall_reach if overall_reach > 0 else 0
            sr_norm = mean_val(d["save_rate"]) / 2.0  # 2%を1.0として正規化
            pr_norm = mean_val(d["prof_rate"]) / 2.0
            shr_norm = mean_val(d["share_rate"]) / 1.0
            pillar_scores[pillar] = r_norm * 0.3 + sr_norm * 0.3 + pr_norm * 0.2 + shr_norm * 0.2

    if pillar_scores:
        total_score = sum(pillar_scores.values())
        output.append(f"  {'ピラー':<16} {'スコア':>7} {'推奨配分':>7} {'現状配分':>7}")
        for pillar, score in sorted(pillar_scores.items(), key=lambda x: x[1], reverse=True):
            recommended = score / total_score * 100 if total_score > 0 else 0
            current = pillar_data[pillar]["count"] / len(this_month) * 100
            arrow = "↑" if recommended > current + 5 else ("↓" if recommended < current - 5 else "→")
            output.append(f"  {pillar:<16} {score:>7.2f} {recommended:>6.0f}% {current:>6.0f}% {arrow}")

    output.append("")
    output.append("=" * 70)

    report = "\n".join(output)
    print(report)

    if not dry_run and service:
        _write_strategy_sheet(service, "月次分析", report, today)
        print(f"\n✅ 「{STRATEGY_SHEET_NAME}」シートに書き込みました")


# ═══════════════════════════════════════════════════════════
#  MODE 3: HOOK BANK（フック銀行）
# ═══════════════════════════════════════════════════════════
def run_hooks(posts: List[dict], dry_run: bool, top_n: int, service=None):
    median_reach = median_val([p["reach_1d"] for p in posts])
    threshold = median_reach * 2

    output = []
    output.append("=" * 70)
    output.append(f"🎣 フック銀行 — リーチ中央値({median_reach:,.0f})の2倍({threshold:,.0f})超え")
    output.append("=" * 70)

    # 全投稿からフックを抽出
    hook_winners = []
    for p in posts:
        if p["reach_1d"] >= threshold:
            hook_winners.append(p)

    hook_winners.sort(key=lambda p: p["reach_1d"], reverse=True)

    # フック型別の成功率
    hook_success = defaultdict(lambda: {"total": 0, "winner": 0, "avg_reach": []})
    for p in posts:
        for h in p["hooks"]:
            hook_success[h]["total"] += 1
            hook_success[h]["avg_reach"].append(p["reach_1d"])
            if p["reach_1d"] >= threshold:
                hook_success[h]["winner"] += 1

    output.append("")
    output.append("── フック型 成功率ランキング ──")
    output.append(f"  {'フック型':<14} {'使用回数':>6} {'2x超え':>6} {'成功率':>6} {'平均リーチ':>10}")
    for h, s in sorted(hook_success.items(),
                       key=lambda x: x[1]["winner"]/max(x[1]["total"], 1), reverse=True):
        rate = s["winner"] / s["total"] * 100 if s["total"] > 0 else 0
        output.append(f"  {h:<14} {s['total']:>6} {s['winner']:>6} {rate:>5.0f}% "
                      f"{mean_val(s['avg_reach']):>10,.0f}")

    # 勝者フック一覧
    output.append("")
    output.append(f"── 2x超えフック一覧 TOP{top_n} ──")
    output.append(f"  {'#':>4} {'リーチ':>8} {'保存':>4} {'フック型':<20} タイトル")

    for i, p in enumerate(hook_winners[:top_n], 1):
        hooks_str = "・".join(p["hooks"]) if p["hooks"] else "検出なし"
        output.append(f"  {i:>4} {p['reach_1d']:>8,} {p['saves_1d']:>4} "
                      f"{hooks_str:<20} {p['title'][:45]}")

    # リミックス候補（上位フックの組み合わせ提案）
    output.append("")
    output.append("── リミックス候補（TOP組み合わせ × 未使用カテゴリ）──")

    # 最も成功率の高いフック型
    top_hooks = sorted(hook_success.items(),
                       key=lambda x: mean_val(x[1]["avg_reach"]), reverse=True)[:3]
    # 各カテゴリで使用されたフックを把握
    cat_hook_used = defaultdict(set)
    for p in posts:
        for h in p["hooks"]:
            cat_hook_used[p["category"]].add(h)

    remix_count = 0
    for hook_name, _ in top_hooks:
        for cat in CATEGORY_KEYWORDS.keys():
            if hook_name not in cat_hook_used.get(cat, set()):
                output.append(f"  💡 「{cat}」×「{hook_name}」— 未テスト！試す価値あり")
                remix_count += 1
                if remix_count >= 8:
                    break
        if remix_count >= 8:
            break

    # スプレッドシートに書き込む用のデータ
    hook_bank_data = []
    for p in hook_winners[:top_n]:
        hooks_str = "・".join(p["hooks"]) if p["hooks"] else "—"
        hook_bank_data.append([
            p["date"].strftime("%Y-%m-%d"),
            p["post_num"],
            p["title"],
            hooks_str,
            p["category"],
            p["cta"],
            p["reach_1d"],
            p["saves_1d"],
            p["shares_1d"],
            evergreen_score(p) or "",
        ])

    output.append("")
    output.append("=" * 70)
    report = "\n".join(output)
    print(report)

    if not dry_run and service and hook_bank_data:
        _write_hook_bank(service, hook_bank_data)
        print(f"\n✅ 「{HOOK_BANK_SHEET_NAME}」シートに{len(hook_bank_data)}件書き込みました")


# ═══════════════════════════════════════════════════════════
#  MODE 4: EXPERIMENT（実験管理）
# ═══════════════════════════════════════════════════════════
def run_experiment(service, args, dry_run: bool):
    if args.add:
        _experiment_add(service, args, dry_run)
    elif args.review:
        _experiment_review(service, dry_run)
    else:
        _experiment_list(service)


def _experiment_add(service, args, dry_run: bool):
    hypothesis = args.add
    variable = args.variable or "未指定"
    ice = args.ice or "5,5,5"
    parts = ice.split(",")
    i_score = int(parts[0]) if len(parts) > 0 else 5
    c_score = int(parts[1]) if len(parts) > 1 else 5
    e_score = int(parts[2]) if len(parts) > 2 else 5
    total = i_score + c_score + e_score

    today = datetime.now().strftime("%Y-%m-%d")
    row = [
        today,                  # 登録日
        f"EXP-{today}",        # 実験ID
        hypothesis,             # 仮説
        variable,               # テスト変数
        f"I={i_score}",        # Impact
        f"C={c_score}",        # Confidence
        f"E={e_score}",        # Ease
        str(total),            # ICE合計
        "計画中",              # ステータス
        "",                     # テスト投稿番号
        "",                     # 結果
        "",                     # 学び
        "",                     # 次のアクション
    ]

    print(f"📝 新しい実験を登録:")
    print(f"   仮説: {hypothesis}")
    print(f"   変数: {variable}")
    print(f"   ICE:  I={i_score} C={c_score} E={e_score} → {total}")

    if not dry_run:
        _append_experiment(service, row)
        print(f"\n✅ 「{EXPERIMENT_SHEET_NAME}」に追加しました")
    else:
        print("\n(--dry-run: 書き込みスキップ)")


def _experiment_review(service, dry_run: bool):
    result = service.spreadsheets().values().get(
        spreadsheetId=POSTDATA_SHEET_ID,
        range=f"'{EXPERIMENT_SHEET_NAME}'!A2:M",
    ).execute()
    rows = result.get("values", [])

    if not rows:
        print("📋 実験データがありません。`--add` で追加してください")
        return

    output = []
    output.append("=" * 70)
    output.append("🧪 実験管理ダッシュボード")
    output.append("=" * 70)

    planning = [r for r in rows if len(r) > 8 and r[8] in ("計画中", "")]
    running = [r for r in rows if len(r) > 8 and r[8] == "実行中"]
    completed = [r for r in rows if len(r) > 8 and r[8] == "完了"]

    output.append(f"\n  計画中: {len(planning)}  実行中: {len(running)}  完了: {len(completed)}")

    if planning:
        output.append("")
        output.append("── 計画中（ICEスコア順）──")
        planning_sorted = sorted(planning, key=lambda r: safe_int(r[7]) if len(r) > 7 else 0, reverse=True)
        for r in planning_sorted:
            output.append(f"  [{r[1]}] ICE={r[7] if len(r) > 7 else '?'} | {r[2]} (変数: {r[3]})")

    if running:
        output.append("")
        output.append("── 実行中 ──")
        for r in running:
            output.append(f"  [{r[1]}] {r[2]} | 投稿: {r[9] if len(r) > 9 else '未設定'}")

    if completed:
        output.append("")
        output.append("── 完了（学びアーカイブ）──")
        for r in completed[-5:]:
            result_text = r[10] if len(r) > 10 else ""
            learning = r[11] if len(r) > 11 else ""
            output.append(f"  [{r[1]}] {r[2]}")
            if result_text:
                output.append(f"    結果: {result_text}")
            if learning:
                output.append(f"    学び: {learning}")

    output.append("")
    output.append("=" * 70)
    print("\n".join(output))


def _experiment_list(service):
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=POSTDATA_SHEET_ID,
            range=f"'{EXPERIMENT_SHEET_NAME}'!A2:M",
        ).execute()
        rows = result.get("values", [])
        if not rows:
            print("📋 実験なし。`--add` で追加してください")
            return
        print(f"📋 実験一覧 ({len(rows)}件):")
        for r in rows:
            status = r[8] if len(r) > 8 else "?"
            ice = r[7] if len(r) > 7 else "?"
            print(f"  [{r[1]}] ICE={ice} {status} | {r[2]}")
    except Exception:
        print("📋 実験管理シートがまだありません。`--add` で最初の実験を登録すると自動作成されます")


# ── シート書き込み ─────────────────────────────────────────
def _ensure_sheet_exists(service, sheet_name: str, headers: List[str] = None):
    """タブが存在しなければ作成する"""
    meta = service.spreadsheets().get(spreadsheetId=POSTDATA_SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if sheet_name not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=POSTDATA_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
        time.sleep(0.5)
        if headers:
            service.spreadsheets().values().update(
                spreadsheetId=POSTDATA_SHEET_ID,
                range=f"'{sheet_name}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [headers]},
            ).execute()


def _write_strategy_sheet(service, mode_name: str, report: str, date: datetime):
    _ensure_sheet_exists(service, STRATEGY_SHEET_NAME,
                         ["日付", "モード", "レポート"])
    lines = report.split("\n")
    # 1セルにレポート全文を入れる
    service.spreadsheets().values().append(
        spreadsheetId=POSTDATA_SHEET_ID,
        range=f"'{STRATEGY_SHEET_NAME}'!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [[date.strftime("%Y-%m-%d"), mode_name, report]]},
    ).execute()


def _write_hook_bank(service, data: List[list]):
    headers = ["日付", "番号", "タイトル", "フック型", "カテゴリ", "CTA",
               "リーチ", "保存", "シェア", "EGスコア"]
    _ensure_sheet_exists(service, HOOK_BANK_SHEET_NAME, headers)

    # 既存データをクリアして最新で上書き
    service.spreadsheets().values().update(
        spreadsheetId=POSTDATA_SHEET_ID,
        range=f"'{HOOK_BANK_SHEET_NAME}'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": data},
    ).execute()


def _append_experiment(service, row: list):
    headers = ["登録日", "実験ID", "仮説", "テスト変数",
               "Impact", "Confidence", "Ease", "ICE合計",
               "ステータス", "テスト投稿番号", "結果", "学び", "次のアクション"]
    _ensure_sheet_exists(service, EXPERIMENT_SHEET_NAME, headers)
    service.spreadsheets().values().append(
        spreadsheetId=POSTDATA_SHEET_ID,
        range=f"'{EXPERIMENT_SHEET_NAME}'!A:M",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()


# ── メイン ─────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Instagram戦略ループ — データ→洞察→仮説→実験→学習")
    parser.add_argument("mode", choices=["weekly", "monthly", "hooks", "experiment"],
                        help="実行モード")
    parser.add_argument("--dry-run", action="store_true",
                        help="スプレッドシートに書き込まずレポートだけ表示")
    parser.add_argument("--top", type=int, default=15,
                        help="フック銀行: 表示件数 (default: 15)")
    # 実験管理
    parser.add_argument("--add", type=str, help="実験の仮説を登録")
    parser.add_argument("--variable", type=str,
                        help="テスト変数 (hook/format/cta/time/pillar)")
    parser.add_argument("--ice", type=str,
                        help="ICEスコア (Impact,Confidence,Ease) 例: 8,7,9")
    parser.add_argument("--review", action="store_true",
                        help="実験ダッシュボードを表示")

    args = parser.parse_args()

    print(f"🔄 Google Sheets API に接続中...")
    service = get_service()

    if args.mode == "experiment":
        run_experiment(service, args, args.dry_run)
        return

    print(f"📥 投稿毎データを読み込み中...")
    posts = read_all_posts(service)
    print(f"   → {len(posts)}投稿のデータを取得")

    weekly_data = []
    if args.mode in ("weekly", "monthly"):
        print(f"📥 週ごとデータを読み込み中...")
        weekly_data = read_weekly_data(service)
        print(f"   → {len(weekly_data)}週分のデータを取得")

    if args.mode == "weekly":
        run_weekly(posts, weekly_data, args.dry_run, service)
    elif args.mode == "monthly":
        run_monthly(posts, weekly_data, args.dry_run, service)
    elif args.mode == "hooks":
        run_hooks(posts, args.dry_run, args.top, service)


if __name__ == "__main__":
    main()
