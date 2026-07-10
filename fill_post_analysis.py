"""投稿毎データの結果①〜④・考察・次に活かすポイントを世界水準で自動生成する。

パーセンタイル順位・CTA別ベンチマーク・カテゴリ別比較・TOP投稿パターンマッチングを
使って、投稿ごとに個別化された考察を生成する。

Usage:
    python3 utils/fill_post_analysis.py                  # 空欄のみ補完
    python3 utils/fill_post_analysis.py --force           # 既存の考察も上書き（テンプレ一掃）
    python3 utils/fill_post_analysis.py --dry-run         # 書き込みせず内容だけ表示
    python3 utils/fill_post_analysis.py --force --dry-run # 上書き内容を確認
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from sheet_column_map import _find_section_range, col_letter

import os
_auth_dir = os.environ.get("INSTAGRAM_INSIGHTS_GOOGLE_AUTH_DIR", "タッキー/02_SNS集客/instagram-auto-post")
TOKEN_FILE = os.path.join(_auth_dir, "token.json")
SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
SHEET_NAME = "Instagram投稿毎データ"

# ── Column indices (0-based from A) ──
# 🔴 起動時に resolve_columns(service) で行3ヘッダーから動的解決される。
# 下の値は「解決失敗を検知するための初期値 None」。ハードコード列番号は廃止
# （2026-06の列挿入で全列が-2ズレし、Q:V書き込みが別列を破壊していた再発防止）。
COL_DATE = None       # A: 日付
COL_NUM = None        # C: 番号
COL_TIME = None       # D: 時刻
COL_TITLE = None      # E: ファイル名
COL_CTA = None        # F: 投稿種別（CTA）
COL_FORMAT = None     # G: 形式/投稿目的
COL_INTENT = None     # I: 投稿の意図
COL_CONTENT = None    # J: 原稿（本文全文）
COL_CAPTION = None    # N: キャプション
COL_LF8 = None        # O: LF8欲求
COL_EMOTION = None    # P: 感情トリガー
COL_KPI = None        # Q: 成果指標メモ
COL_RESULT1 = None    # S: 結果①（CTA平均との差）
COL_RESULT2 = None    # T: 結果②（強み/課題）
COL_RESULT3 = None    # U: 結果③（効率/成長）
COL_RESULT4 = None    # V: 結果④（予備）
COL_ANALYSIS = None   # W: 考察・仮説
COL_NEXT = None       # X: 次投稿への示唆

# 1日後データ
COL_1D_REACH = None
COL_1D_REACH_FW = None
COL_1D_REACH_NF = None
COL_1D_IMP = None
COL_1D_HOME = None
COL_1D_HASHTAG = None
COL_1D_DISCOVER = None
COL_1D_PLAYS = None
COL_1D_AVG_WATCH = None
COL_1D_ENG = None
COL_1D_LIKES = None
COL_1D_SAVES = None
COL_1D_COMMENTS = None
COL_1D_SHARES = None
COL_1D_PROF = None
COL_1D_FOLLOW = None
COL_1D_WEB = None
COL_1D_SAVE_RATE = None
COL_1D_HOME_RATE = None
COL_1D_PROF_RATE = None
COL_1D_FOLLOW_RATE = None

# 1週間後データ
COL_7D_REACH = None
COL_7D_REACH_FW = None
COL_7D_REACH_NF = None
COL_7D_IMP = None
COL_7D_PLAYS = None
COL_7D_LIKES = None
COL_7D_SAVES = None
COL_7D_COMMENTS = None
COL_7D_SHARES = None
COL_7D_PROF = None
COL_7D_FOLLOW = None
COL_7D_SAVE_RATE = None

# 行1のセクションヘッダー（sheet_column_map.py と同じ規約）
_SECTION_1DAY = "1日後データ"
_SECTION_7DAY = "1週間後データ"

# (変数名, 行3キーワード候補, 行1セクション制約, 何番目の一致か(1-based), 必須か)
_COLUMN_SPECS = [
    ("COL_DATE",       ("日付",),                         None, 1, True),
    ("COL_NUM",        ("番号",),                         None, 1, True),
    ("COL_TIME",       ("時刻",),                         None, 1, False),
    ("COL_TITLE",      ("ファイル名",),                   None, 1, True),
    ("COL_CTA",        ("投稿種別",),                     None, 1, True),
    ("COL_FORMAT",     ("形式", "投稿目的"),              None, 1, True),
    ("COL_INTENT",     ("投稿の意図",),                   None, 1, False),
    ("COL_CONTENT",    ("原稿（本文全文）", "原稿", "内容"), None, 1, False),
    ("COL_CAPTION",    ("キャプション",),                 None, 1, False),
    ("COL_LF8",        ("LF8欲求", "LF8"),               None, 1, False),
    ("COL_EMOTION",    ("感情トリガー",),                 None, 1, False),  # 先頭一致（分類タグ側にも同名あり）
    ("COL_KPI",        ("成果指標メモ", "成果指標"),      None, 1, False),
    # 書き込み先6列（絶対にズレてはいけない）
    ("COL_RESULT1",    ("CTA平均との差", "結果①"),       None, 1, True),
    ("COL_RESULT2",    ("強み/課題", "結果②"),           None, 1, True),
    ("COL_RESULT3",    ("効率/成長", "結果③"),           None, 1, True),
    ("COL_RESULT4",    ("予備", "結果④"),                None, 1, True),
    ("COL_ANALYSIS",   ("考察・仮説",),                   None, 1, True),
    ("COL_NEXT",       ("次投稿への示唆", "次の投稿に活かすポイント"), None, 1, True),
    # 1日後データ
    ("COL_1D_REACH",     ("全体",),                   _SECTION_1DAY, 1, True),
    ("COL_1D_REACH_FW",  ("フォロワー",),             _SECTION_1DAY, 1, False),
    ("COL_1D_REACH_NF",  ("フォロー外", "フォロワー以外"), _SECTION_1DAY, 1, False),
    ("COL_1D_IMP",       ("インプレッション",),       _SECTION_1DAY, 1, False),
    ("COL_1D_HOME",      ("ホーム",),                 _SECTION_1DAY, 1, False),
    ("COL_1D_HASHTAG",   ("ハッシュタグ",),           _SECTION_1DAY, 1, False),
    ("COL_1D_DISCOVER",  ("発見",),                   _SECTION_1DAY, 1, False),
    ("COL_1D_PLAYS",     ("再生数",),                 _SECTION_1DAY, 1, False),
    ("COL_1D_AVG_WATCH", ("平均再生時間",),           _SECTION_1DAY, 1, False),
    ("COL_1D_ENG",       ("全体",),                   _SECTION_1DAY, 2, False),  # 2番目の「全体」
    ("COL_1D_LIKES",     ("いいね",),                 _SECTION_1DAY, 1, True),
    ("COL_1D_SAVES",     ("保存",),                   _SECTION_1DAY, 1, True),
    ("COL_1D_COMMENTS",  ("コメント",),               _SECTION_1DAY, 1, True),
    ("COL_1D_SHARES",    ("シェア",),                 _SECTION_1DAY, 1, True),
    ("COL_1D_PROF",      ("プロフアクセス",),         _SECTION_1DAY, 1, True),
    ("COL_1D_FOLLOW",    ("フォロー",),               _SECTION_1DAY, 1, True),
    ("COL_1D_WEB",       ("ウェブタップ",),           _SECTION_1DAY, 1, False),
    ("COL_1D_SAVE_RATE", ("保存率",),                 _SECTION_1DAY, 1, True),
    ("COL_1D_HOME_RATE", ("ホーム率",),               _SECTION_1DAY, 1, False),
    ("COL_1D_PROF_RATE", ("プロフアクセス率",),       _SECTION_1DAY, 1, False),
    ("COL_1D_FOLLOW_RATE", ("フォロワー転換率", "フォロー率"), _SECTION_1DAY, 1, False),
    # 1週間後データ
    ("COL_7D_REACH",     ("全体",),                   _SECTION_7DAY, 1, True),
    ("COL_7D_REACH_FW",  ("フォロワー",),             _SECTION_7DAY, 1, False),
    ("COL_7D_REACH_NF",  ("フォロー外", "フォロワー以外"), _SECTION_7DAY, 1, False),
    ("COL_7D_IMP",       ("インプレッション",),       _SECTION_7DAY, 1, False),
    ("COL_7D_PLAYS",     ("再生回数", "再生数"),      _SECTION_7DAY, 1, False),
    ("COL_7D_LIKES",     ("いいね",),                 _SECTION_7DAY, 1, True),
    ("COL_7D_SAVES",     ("保存",),                   _SECTION_7DAY, 1, True),
    ("COL_7D_COMMENTS",  ("コメント",),               _SECTION_7DAY, 1, False),
    ("COL_7D_SHARES",    ("シェア",),                 _SECTION_7DAY, 1, False),
    ("COL_7D_PROF",      ("プロフアクセス",),         _SECTION_7DAY, 1, True),
    ("COL_7D_FOLLOW",    ("フォロー",),               _SECTION_7DAY, 1, True),
    ("COL_7D_SAVE_RATE", ("保存率",),                 _SECTION_7DAY, 1, False),
]


def resolve_columns(service) -> None:
    """行1(セクション)+行3(列名)ヘッダーを読み、COL_* を名前で動的解決する。

    必須列が見つからない場合は即エラー終了（ズレたまま書き込むより安全）。
    オプション列は None のままにし、cell() が "" を返す。
    """
    r = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!1:3",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    header_rows = r.get("values", [])
    if len(header_rows) < 3:
        raise SystemExit(f"❌ ヘッダーが3行未満: {SHEET_NAME}")
    row1, row3 = header_rows[0], header_rows[2]

    resolved: Dict[str, Optional[int]] = {}
    missing: List[str] = []
    for var, keywords, section, nth, required in _COLUMN_SPECS:
        if section:
            start, end = _find_section_range(row1, section)
        else:
            start, end = 0, len(row3)
        matches = [
            i for i in range(start, min(end, len(row3)))
            if str(row3[i]).strip() in keywords
        ]
        if len(matches) >= nth:
            resolved[var] = matches[nth - 1]
        elif required:
            missing.append(f"{var}({'/'.join(keywords)})")
        else:
            resolved[var] = None

    if missing:
        raise SystemExit(
            "❌ 必須列がヘッダーから見つかりません（列挿入/改名の可能性）:\n  "
            + "\n  ".join(missing)
        )

    globals().update(resolved)

    # 書き込み先6列は連続していることを検証（範囲書き込みの前提）
    write_cols = [COL_RESULT1, COL_RESULT2, COL_RESULT3, COL_RESULT4, COL_ANALYSIS, COL_NEXT]
    if write_cols != list(range(COL_RESULT1, COL_RESULT1 + 6)):
        raise SystemExit(f"❌ 結果①〜次ポイントの6列が連続していません: {write_cols}")
    print(f"🧭 列マップ解決: 結果①〜次={col_letter(COL_RESULT1)}:{col_letter(COL_NEXT)} "
          f"リーチ1d={col_letter(COL_1D_REACH)} いいね1d={col_letter(COL_1D_LIKES)} "
          f"保存1d={col_letter(COL_1D_SAVES)}")

# ── Content category keywords ──
CATEGORY_KEYWORDS = {
    "食事": ["食べ", "食材", "食事", "フルーツ", "サラダ", "ドリンク", "寿司", "ヨーグルト",
             "食え", "食べ物", "食", "白い液体", "野菜", "ビタミン", "コンビニ", "置き換え"],
    "スキンケア": ["洗顔", "保湿", "化粧", "商品", "スキンケア", "やめる美容", "美容液"],
    "皮膚科": ["皮膚科", "べピオ", "ベピオ", "薬"],
    "ルーティン": ["ルーティ", "習慣", "1日の過ごし方", "朝", "夜", "入り方", "白湯", "ルール"],
    "睡眠": ["睡眠", "枕", "寝"],
    "ツボ・マッサージ": ["押し", "3秒", "ツボ", "マッサージ"],
    "モテ・自己啓発": ["モテ", "カッコ", "かっこ", "女の子", "デキる", "好かれ", "好感度"],
    "ニキビ知識": ["ニキビ", "毛穴", "肌荒れ", "ニキビ跡", "ニキビのもと"],
    "Before/After": ["ビフォー", "before", "変化", "変わっ"],
    "リスト・図解": ["リスト", "図解", "ランキング", "まとめ", "一覧", "部位別", "図"],
    "メンタル": ["メンタル", "自信", "悩み", "ストレス"],
    "姿勢・運動": ["姿勢", "猫背", "運動", "筋トレ", "ストレッチ"],
}

# ── Title hook patterns (what makes posts go viral) ──
HOOK_PATTERNS = {
    "伏せ字・数字": [r"〇〇", r"\d+回", r"\d+秒", r"\d+つ", r"\d+選"],
    "逆張り・常識破壊": ["実は", "やめ", "しない", "なのに", "だけでは", "なんか"],
    "恐怖・危機感": ["一生", "悪化", "NG", "ダメ", "危険", "やばい"],
    "リスト・具体性": ["リスト", "図解", "部位別", "ランキング", "vs", "一覧"],
    "共感・あるある": ["あるある", "わかる", "やりがち"],
    "驚き・意外性": ["え？", "まさか", "意外", "知らない", "嘘"],
}


def get_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if service_account_json and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        info = json.loads(service_account_json)
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
            return build("sheets", "v4", credentials=creds)
    if service_account_file and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
        return build("sheets", "v4", credentials=creds)

    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=scopes,
    )
    return build("sheets", "v4", credentials=creds)


def safe_int(val: str) -> int:
    """カンマ区切り数値を安全にintに変換"""
    if not val or not val.strip():
        return 0
    try:
        return int(val.strip().replace(",", ""))
    except ValueError:
        return 0


def safe_float(val: str) -> float:
    """%付き数値を安全にfloatに変換"""
    if not val or not val.strip():
        return 0.0
    try:
        return float(val.strip().replace("%", "").replace(",", ""))
    except ValueError:
        return 0.0


def cell(row: list, idx: Optional[int]) -> str:
    """行からセル値を安全に取得（idx=None は未解決のオプション列 → 空文字）"""
    if idx is not None and idx < len(row):
        return row[idx].strip() if isinstance(row[idx], str) else str(row[idx])
    return ""


def detect_category(title: str) -> str:
    """タイトルからコンテンツカテゴリを判定"""
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in title for kw in keywords):
            return cat
    return "その他"


def detect_hooks(title: str) -> List[str]:
    """タイトルからフックパターンを検出"""
    hooks = []
    for pattern_name, patterns in HOOK_PATTERNS.items():
        for p in patterns:
            if re.search(p, title):
                hooks.append(pattern_name)
                break
    return hooks


def percentile_rank(value: float, sorted_values: List[float]) -> int:
    """値のパーセンタイル順位を返す（上位X%）"""
    if not sorted_values:
        return 50
    count_below = sum(1 for v in sorted_values if v < value)
    rank = (count_below / len(sorted_values)) * 100
    return 100 - int(rank)  # 上位X%に変換


def compute_benchmarks(all_posts: List[Dict]) -> Dict[str, Any]:
    """全投稿データからベンチマーク統計を算出"""
    bm: Dict[str, Any] = {}

    # 全体統計
    data_posts = [p for p in all_posts if p["reach"] > 0]
    if not data_posts:
        return bm

    reaches = sorted([p["reach"] for p in data_posts])
    saves = sorted([p["saves"] for p in data_posts])
    likes = sorted([p["likes"] for p in data_posts])
    profs = sorted([p["prof"] for p in data_posts])
    follows = sorted([p["follow"] for p in data_posts])
    save_rates = sorted([p["save_rate"] for p in data_posts if p["save_rate"] > 0])
    eng_totals = sorted([p["likes"] + p["saves"] + p["comments"] + p["shares"] for p in data_posts])

    bm["all"] = {
        "reach": reaches,
        "saves": saves,
        "likes": likes,
        "prof": profs,
        "follow": follows,
        "save_rate": save_rates,
        "eng_total": eng_totals,
        "mean_reach": statistics.mean(reaches),
        "median_reach": statistics.median(reaches),
        "mean_saves": statistics.mean(saves),
        "median_saves": statistics.median(saves),
        "mean_likes": statistics.mean(likes),
        "mean_prof": statistics.mean(profs),
        "mean_follow": statistics.mean(follows),
        "count": len(data_posts),
    }

    # CTA別統計
    cta_groups = defaultdict(list)
    for p in data_posts:
        if p["cta"]:
            cta_groups[p["cta"]].append(p)

    bm["cta"] = {}
    for cta, posts in cta_groups.items():
        bm["cta"][cta] = {
            "mean_reach": statistics.mean([p["reach"] for p in posts]),
            "mean_saves": statistics.mean([p["saves"] for p in posts]),
            "mean_likes": statistics.mean([p["likes"] for p in posts]),
            "mean_prof": statistics.mean([p["prof"] for p in posts]),
            "count": len(posts),
        }

    # カテゴリ別統計
    cat_groups = defaultdict(list)
    for p in data_posts:
        cat_groups[p["category"]].append(p)

    bm["category"] = {}
    for cat, posts in cat_groups.items():
        bm["category"][cat] = {
            "mean_reach": statistics.mean([p["reach"] for p in posts]),
            "mean_saves": statistics.mean([p["saves"] for p in posts]),
            "mean_likes": statistics.mean([p["likes"] for p in posts]),
            "count": len(posts),
        }

    # TOP投稿のパターン分析
    top_by_reach = sorted(data_posts, key=lambda p: p["reach"], reverse=True)[:10]
    top_by_saves = sorted(data_posts, key=lambda p: p["saves"], reverse=True)[:10]
    top_by_eng = sorted(data_posts, key=lambda p: p["likes"] + p["saves"], reverse=True)[:10]

    bm["top_reach"] = top_by_reach
    bm["top_saves"] = top_by_saves
    bm["top_eng"] = top_by_eng

    # フックパターン別効果
    hook_groups = defaultdict(list)
    for p in data_posts:
        for h in p.get("hooks", []):
            hook_groups[h].append(p)

    bm["hooks"] = {}
    for hook, posts in hook_groups.items():
        if len(posts) >= 2:
            bm["hooks"][hook] = {
                "mean_reach": statistics.mean([p["reach"] for p in posts]),
                "mean_saves": statistics.mean([p["saves"] for p in posts]),
                "count": len(posts),
            }

    # ── 感情トリガー別効果 ──
    emotion_groups = defaultdict(list)
    for p in data_posts:
        emo = p.get("emotion_main", "")
        if emo:
            emotion_groups[emo].append(p)

    bm["emotion"] = {}
    for emo, posts in emotion_groups.items():
        if len(posts) >= 2:
            bm["emotion"][emo] = {
                "mean_reach": statistics.mean([p["reach"] for p in posts]),
                "mean_saves": statistics.mean([p["saves"] for p in posts]),
                "count": len(posts),
            }

    # ── LF8欲求別効果 ──
    lf8_groups = defaultdict(list)
    for p in data_posts:
        for lf in p.get("lf8_list", []):
            lf8_groups[lf].append(p)

    bm["lf8"] = {}
    for lf, posts in lf8_groups.items():
        if len(posts) >= 2:
            bm["lf8"][lf] = {
                "mean_reach": statistics.mean([p["reach"] for p in posts]),
                "mean_saves": statistics.mean([p["saves"] for p in posts]),
                "count": len(posts),
            }

    # ── 意図キーワード別効果 ──
    intent_kw_groups = defaultdict(list)
    INTENT_KEYWORDS = ["保存", "フォロー", "プロフ", "誘導", "信頼", "実用", "共感",
                       "驚き", "行動", "興味", "逆張り", "図解", "恐怖", "具体", "ランキング"]
    for p in data_posts:
        intent = p.get("intent", "")
        for kw in INTENT_KEYWORDS:
            if kw in intent:
                intent_kw_groups[kw].append(p)

    bm["intent_kw"] = {}
    for kw, posts in intent_kw_groups.items():
        if len(posts) >= 2:
            bm["intent_kw"][kw] = {
                "mean_reach": statistics.mean([p["reach"] for p in posts]),
                "mean_saves": statistics.mean([p["saves"] for p in posts]),
                "count": len(posts),
            }

    return bm


def generate_result1(p: Dict, bm: Dict) -> str:
    """結果①: コアKPI + パーセンタイル順位"""
    reach_pct = percentile_rank(p["reach"], bm["all"]["reach"])
    save_pct = percentile_rank(p["saves"], bm["all"]["saves"])

    parts = [f"リーチ{p['reach']:,}（上位{reach_pct}%）"]
    parts.append(f"保存{p['saves']}")
    parts.append(f"いいね{p['likes']}")

    if p["save_rate"] > 0:
        parts.append(f"保存率{p['save_rate']:.1f}%")

    eng_total = p["likes"] + p["saves"] + p["comments"] + p["shares"]
    eng_rate = (eng_total / p["reach"] * 100) if p["reach"] > 0 else 0
    parts.append(f"総エンゲージメント率{eng_rate:.1f}%")

    return " | ".join(parts)


def generate_result2(p: Dict, bm: Dict) -> str:
    """結果②: CTA種別ベンチマーク比較"""
    cta = p["cta"]
    if cta not in bm.get("cta", {}):
        return ""

    cta_bm = bm["cta"][cta]
    parts = [f"CTA「{cta}」平均比（{cta_bm['count']}件中）:"]

    # リーチ比較
    if cta_bm["mean_reach"] > 0:
        ratio = ((p["reach"] - cta_bm["mean_reach"]) / cta_bm["mean_reach"]) * 100
        sign = "+" if ratio >= 0 else ""
        parts.append(f"リーチ{sign}{ratio:.0f}%")

    # 保存比較
    if cta_bm["mean_saves"] > 0:
        ratio = ((p["saves"] - cta_bm["mean_saves"]) / cta_bm["mean_saves"]) * 100
        sign = "+" if ratio >= 0 else ""
        parts.append(f"保存{sign}{ratio:.0f}%")

    # いいね比較
    if cta_bm["mean_likes"] > 0:
        ratio = ((p["likes"] - cta_bm["mean_likes"]) / cta_bm["mean_likes"]) * 100
        sign = "+" if ratio >= 0 else ""
        parts.append(f"いいね{sign}{ratio:.0f}%")

    # プロフ比較
    if cta_bm["mean_prof"] > 0:
        ratio = ((p["prof"] - cta_bm["mean_prof"]) / cta_bm["mean_prof"]) * 100
        sign = "+" if ratio >= 0 else ""
        parts.append(f"プロフ{sign}{ratio:.0f}%")

    return " ".join(parts)


def generate_result3(p: Dict, bm: Dict) -> str:
    """結果③: 強み・弱みの特定"""
    strengths = []
    weaknesses = []

    reach_pct = percentile_rank(p["reach"], bm["all"]["reach"])
    save_pct = percentile_rank(p["saves"], bm["all"]["saves"])
    like_pct = percentile_rank(p["likes"], bm["all"]["likes"])
    prof_pct = percentile_rank(p["prof"], bm["all"]["prof"])

    metrics = [
        ("リーチ", reach_pct),
        ("保存", save_pct),
        ("いいね", like_pct),
        ("プロフ", prof_pct),
    ]

    for name, pct in metrics:
        if pct <= 20:
            strengths.append(f"{name}（上位{pct}%）")
        elif pct >= 70:
            weaknesses.append(f"{name}（下位{100-pct}%）")

    # 保存率は別途
    if p["save_rate"] > 0:
        sr_pct = percentile_rank(p["save_rate"], bm["all"].get("save_rate", []))
        if sr_pct <= 20:
            strengths.append(f"保存率（上位{sr_pct}%）")
        elif sr_pct >= 70:
            weaknesses.append(f"保存率（下位{100-sr_pct}%）")

    parts = []
    if strengths:
        parts.append(f"強み: {', '.join(strengths)}")
    if weaknesses:
        parts.append(f"課題: {', '.join(weaknesses)}")

    if not parts:
        # 全部中間の場合
        eng_total = p["likes"] + p["saves"] + p["comments"] + p["shares"]
        eng_pct = percentile_rank(eng_total, bm["all"]["eng_total"])
        parts.append(f"全指標中位圏。総エンゲージメント上位{eng_pct}%")

    # カテゴリ内順位
    cat = p["category"]
    if cat in bm.get("category", {}):
        cat_bm = bm["category"][cat]
        if cat_bm["count"] >= 3:
            cat_reach_ratio = p["reach"] / cat_bm["mean_reach"] if cat_bm["mean_reach"] > 0 else 1
            if cat_reach_ratio > 1.3:
                parts.append(f"「{cat}」カテゴリ内で高パフォーマンス（平均比+{(cat_reach_ratio-1)*100:.0f}%）")
            elif cat_reach_ratio < 0.7:
                parts.append(f"「{cat}」カテゴリ平均を下回る（平均比{(cat_reach_ratio-1)*100:.0f}%）")

    return " | ".join(parts)


def generate_result4(p: Dict, bm: Dict) -> str:
    """結果④: 1週間後の成長 or エンゲージメント効率"""
    parts = []

    # 1週間後データがあれば成長率
    if p.get("reach_7d", 0) > 0 and p["reach"] > 0:
        reach_growth = ((p["reach_7d"] - p["reach"]) / p["reach"]) * 100
        if abs(reach_growth) > 1:  # 1%以上の変化がある場合のみ
            parts.append(f"1日→7日リーチ成長: {'+' if reach_growth >= 0 else ''}{reach_growth:.0f}%")

        saves_7d = p.get("saves_7d", 0)
        if saves_7d > p["saves"] and p["saves"] > 0:
            save_growth = ((saves_7d - p["saves"]) / p["saves"]) * 100
            parts.append(f"保存成長: +{save_growth:.0f}%（{p['saves']}→{saves_7d}）")

    # エンゲージメント効率
    eng_total = p["likes"] + p["saves"] + p["comments"] + p["shares"]
    eng_rate = (eng_total / p["reach"] * 100) if p["reach"] > 0 else 0

    # 全体平均のエンゲージメント率
    all_eng_rates = []
    for pp in bm.get("_all_posts", []):
        if pp["reach"] > 0:
            e = pp["likes"] + pp["saves"] + pp["comments"] + pp["shares"]
            all_eng_rates.append(e / pp["reach"] * 100)
    avg_eng_rate = statistics.mean(all_eng_rates) if all_eng_rates else 1.0

    eng_vs_avg = ((eng_rate - avg_eng_rate) / avg_eng_rate * 100) if avg_eng_rate > 0 else 0
    parts.append(f"エンゲージメント効率{eng_rate:.1f}%（平均{avg_eng_rate:.1f}%比{'+' if eng_vs_avg >= 0 else ''}{eng_vs_avg:.0f}%）")

    # プロフ転換率
    if p["reach"] > 0 and p["prof"] > 0:
        prof_rate = p["prof"] / p["reach"] * 100
        parts.append(f"プロフ転換{prof_rate:.2f}%")

    # フォロー獲得
    if p["follow"] > 0:
        parts.append(f"フォロー+{p['follow']}")

    return " | ".join(parts)


def generate_analysis(p: Dict, bm: Dict) -> str:
    """考察・仮説: 投稿固有の「なぜ」分析"""
    insights = []

    reach_pct = percentile_rank(p["reach"], bm["all"]["reach"])
    save_pct = percentile_rank(p["saves"], bm["all"]["saves"])
    eng_total = p["likes"] + p["saves"] + p["comments"] + p["shares"]

    # ── 1. リーチ分析（なぜ伸びた/伸びなかった） ──
    if reach_pct <= 10:
        # TOP 10% — なぜバズった？
        hooks = p.get("hooks", [])
        if hooks:
            insights.append(f"リーチ{p['reach']:,}は上位{reach_pct}%。フック「{'・'.join(hooks)}」が発見タブでの拡散を促進した可能性が高い")
        else:
            insights.append(f"リーチ{p['reach']:,}は上位{reach_pct}%。テーマ自体の需要の高さ、またはアルゴリズムの追い風")

        # TOP投稿との共通点
        top_cats = [t["category"] for t in bm.get("top_reach", [])[:5]]
        if p["category"] in top_cats:
            cat_count = top_cats.count(p["category"])
            insights.append(f"「{p['category']}」はTOP5リーチの{cat_count}件を占める高需要カテゴリ")
    elif reach_pct <= 25:
        insights.append(f"リーチ{p['reach']:,}は上位{reach_pct}%で平均以上。安定した露出を確保")
    elif reach_pct >= 70:
        # 低リーチ — なぜ伸びなかった？
        cat = p["category"]
        if cat in bm.get("category", {}) and bm["category"][cat]["count"] >= 3:
            cat_avg = bm["category"][cat]["mean_reach"]
            if p["reach"] < cat_avg * 0.7:
                insights.append(f"リーチ{p['reach']:,}は「{cat}」カテゴリ平均{cat_avg:.0f}を大きく下回る。同カテゴリで伸びた投稿はフック（伏せ字・数字・逆張り）が強かった")
            else:
                insights.append(f"リーチ{p['reach']:,}は下位{100-reach_pct}%。テーマの需要orタイトルのフック力に改善余地あり")
        else:
            insights.append(f"リーチ{p['reach']:,}は下位{100-reach_pct}%。タイトルの初見インパクト不足の可能性")
    else:
        insights.append(f"リーチ{p['reach']:,}（上位{reach_pct}%）は中位圏")

    # ── 2. 保存分析（コンテンツの実用性） ──
    if p["saves"] > 0:
        if save_pct <= 15:
            insights.append(f"保存{p['saves']}件は上位{save_pct}%。「あとで見返したい」実用性が高いコンテンツ")
            # 保存率も高いか？
            if p["save_rate"] >= 1.0:
                insights.append(f"保存率{p['save_rate']:.1f}%も高水準。リーチした人の反応密度が濃い")
        elif p["cta"] == "保存" and save_pct >= 60:
            cta_bm = bm.get("cta", {}).get("保存", {})
            avg_saves = cta_bm.get("mean_saves", 7) if cta_bm else 7
            insights.append(f"保存CTA投稿だが保存{p['saves']}件は「保存」CTA平均{avg_saves:.0f}件を下回る。コンテンツの具体性・保存動機が弱い可能性")

    # ── 3. いいね vs 保存のバランス分析 ──
    if p["likes"] > 0 and p["saves"] > 0:
        ratio = p["likes"] / p["saves"] if p["saves"] > 0 else float("inf")
        if ratio > 3:
            insights.append("いいね優位（共感型）。感情的な反応は得ているが、保存される実用性は弱い")
        elif ratio < 0.5:
            insights.append("保存優位（実用型）。情報の有用性が評価されている。共感・感情要素を加えればいいねも伸びる可能性")
    elif p["likes"] == 0 and p["saves"] == 0:
        insights.append("いいね・保存ともに0。コンテンツがフォロワーの関心に刺さっていない、または露出不足")

    # ── 4. プロフ・フォロー分析 ──
    if p["prof"] >= 10:
        prof_rate = p["prof"] / p["reach"] * 100 if p["reach"] > 0 else 0
        insights.append(f"プロフアクセス{p['prof']}件（転換率{prof_rate:.2f}%）。「この人の他の投稿も見たい」という興味喚起に成功")
        if p["follow"] > 0:
            follow_conv = p["follow"] / p["prof"] * 100
            insights.append(f"プロフ→フォロー転換率{follow_conv:.0f}%。プロフページの訴求力{'は良好' if follow_conv >= 10 else 'に改善余地あり'}")
    elif p["cta"] == "フォロー" and p["prof"] < 5:
        insights.append(f"フォローCTA投稿だがプロフアクセス{p['prof']}件と少ない。プロフ誘導のCTA文言・配置を見直す")

    # ── 5. 特定パターンの洞察 ──
    # 高リーチ×低エンゲージメント = コンテンツとオーディエンスのミスマッチ
    if reach_pct <= 25 and save_pct >= 60:
        insights.append("高リーチだがエンゲージメント低め。発見タブで新規層に届いたが、コンテンツの深さが足りず素通りされた可能性")

    # 低リーチ×高保存率 = コアファンに刺さっている
    if reach_pct >= 60 and p["save_rate"] >= 1.0:
        insights.append(f"リーチは少ないが保存率{p['save_rate']:.1f}%は高い。コアフォロワーには刺さるニッチなテーマ。発見タブ露出を増やせばスケールする余地あり")

    # シェアが多い = バイラル要素
    if p["shares"] >= 3:
        insights.append(f"シェア{p['shares']}件は稀少。「誰かに教えたい」と思わせるバイラル要素あり。この構成を再現する価値が高い")

    # ── 6. コンテンツ内容分析 ──
    # 感情トリガー × パフォーマンス
    emo_main = p.get("emotion_main", "")
    if emo_main and emo_main in bm.get("emotion", {}):
        emo_bm = bm["emotion"][emo_main]
        emo_reach_ratio = p["reach"] / emo_bm["mean_reach"] if emo_bm["mean_reach"] > 0 else 1
        # この感情トリガー自体が強い/弱いか
        all_mean = bm["all"]["mean_reach"]
        emo_vs_all = (emo_bm["mean_reach"] - all_mean) / all_mean * 100 if all_mean > 0 else 0
        if emo_vs_all > 30:
            if emo_reach_ratio >= 1.0:
                insights.append(f"感情トリガー「{emo_main}」は高効果（平均リーチ{emo_bm['mean_reach']:.0f}、全体比+{emo_vs_all:.0f}%）で、この投稿もそのポテンシャルを活かせている")
            else:
                insights.append(f"感情トリガー「{emo_main}」は本来高効果（平均リーチ{emo_bm['mean_reach']:.0f}）だが、この投稿は活かしきれていない。タイトルや構成に改善余地")
        elif emo_vs_all < -20:
            insights.append(f"感情トリガー「{emo_main}」は全体平均より低効果（リーチ{emo_bm['mean_reach']:.0f}）。「気づき」「好奇心」等の高効果トリガーに切り替えを検討")

    # LF8欲求 × パフォーマンス
    lf8_list = p.get("lf8_list", [])
    for lf in lf8_list:
        if lf in bm.get("lf8", {}):
            lf_bm = bm["lf8"][lf]
            all_mean = bm["all"]["mean_reach"]
            lf_vs_all = (lf_bm["mean_reach"] - all_mean) / all_mean * 100 if all_mean > 0 else 0
            if lf_vs_all > 30 and p["reach"] >= lf_bm["mean_reach"]:
                insights.append(f"LF8「{lf}」は高需要欲求（平均リーチ{lf_bm['mean_reach']:.0f}）。この投稿はその需要に正しくヒットしている")
            elif lf_vs_all > 30 and p["reach"] < lf_bm["mean_reach"] * 0.6:
                insights.append(f"LF8「{lf}」は本来高需要（平均リーチ{lf_bm['mean_reach']:.0f}）だが未達。フック・タイトルで「恐怖」「危機感」を強調すると同LF8のTOP投稿に近づく")
            break  # 最初のLF8だけ

    # 投稿の意図 × 実績の整合チェック
    intent = p.get("intent", "")
    if intent:
        if "保存" in intent and p["saves"] < bm["all"]["mean_saves"]:
            insights.append(f"意図は「保存獲得」だが実際の保存{p['saves']}件は平均{bm['all']['mean_saves']:.0f}以下。コンテンツの実用性or保存CTA文言を見直す")
        elif "プロフ" in intent and p["prof"] < bm["all"]["mean_prof"]:
            insights.append(f"意図は「プロフ誘導」だが実際のプロフ{p['prof']}件は平均{bm['all']['mean_prof']:.0f}以下。最終スライドの誘導動線が弱い可能性")
        elif "フォロー" in intent and p["follow"] == 0:
            insights.append("意図は「フォロー獲得」だがフォロー0。プロフページの第一印象（ハイライト・プロフ文）or投稿からの誘導を見直す")

    return "。".join(insights[:5])  # 最大5文に拡張


def generate_next_action(p: Dict, bm: Dict) -> str:
    """次の投稿に活かすポイント: 具体的で再現可能なアクション"""
    actions = []

    reach_pct = percentile_rank(p["reach"], bm["all"]["reach"])
    save_pct = percentile_rank(p["saves"], bm["all"]["saves"])

    # ── 成功パターンの再現 ──
    if reach_pct <= 15:
        hooks = p.get("hooks", [])
        if hooks:
            actions.append(f"このフック（{'・'.join(hooks)}）は高リーチに直結。同パターンで別テーマを展開する")
        if p["category"] in bm.get("category", {}):
            actions.append(f"「{p['category']}」テーマ×この構成は勝ちパターン。週1以上でシリーズ化を検討")

    if save_pct <= 15:
        actions.append("保存上位投稿。同じ情報密度・具体性（数字・リスト・手順）を維持して横展開する")

    # ── 改善アクション ──
    if reach_pct >= 60:
        # リーチが低い → タイトル改善
        top_hooks = []
        for hook, data in sorted(bm.get("hooks", {}).items(), key=lambda x: -x[1]["mean_reach"]):
            top_hooks.append(f"{hook}（平均リーチ{data['mean_reach']:.0f}）")
            if len(top_hooks) >= 2:
                break
        if top_hooks:
            actions.append(f"リーチ改善: 高効果フックを導入 → {', '.join(top_hooks)}")
        else:
            actions.append("リーチ改善: タイトルに伏せ字（〇〇）・数字・逆張りフックを入れて初見の引きを強化")

    if p["cta"] == "保存" and p["saves"] < bm.get("cta", {}).get("保存", {}).get("mean_saves", 7):
        actions.append("保存CTA投稿の保存数が平均以下。「保存して見返してね」の明示CTA + 情報を箇条書き・番号付きにして保存動機を強化")

    if p["cta"] == "フォロー" and p["follow"] == 0:
        actions.append("フォローCTAだがフォロー0。最終スライドのCTAを「他の投稿も見る→プロフへ」ではなく「この情報が役に立ったらフォロー」に変更テスト")

    if p["likes"] > p["saves"] * 3 and p["saves"] > 0:
        actions.append("共感は得ているが保存されない。最後のスライドに「保存して1週間実践してみて」等の保存動機を追加する")

    if p["saves"] > p["likes"] * 2 and p["likes"] > 0:
        actions.append("実用性は高いが感情的共感が薄い。冒頭に「〇〇で悩んでた自分が変われた」等のストーリー要素を追加")

    # プロフアクセス改善
    if p["prof"] < 3 and p["reach"] > bm["all"]["median_reach"]:
        actions.append("リーチはあるのにプロフ訪問が少ない。キャプション末尾に「他の投稿はプロフから→」を追加")

    # ── コンテンツ改善提案 ──
    # 感情トリガーの最適化
    emo_main = p.get("emotion_main", "")
    if emo_main:
        # 高効果感情トリガーTOP3を取得
        top_emotions = sorted(
            [(e, d) for e, d in bm.get("emotion", {}).items()],
            key=lambda x: -x[1]["mean_reach"]
        )[:3]
        if top_emotions and emo_main not in [e for e, _ in top_emotions[:2]]:
            best_emo = top_emotions[0]
            if best_emo[1]["mean_reach"] > bm["all"]["mean_reach"] * 1.3:
                actions.append(f"感情トリガー変更提案: 現在「{emo_main}」→「{best_emo[0]}」（平均リーチ{best_emo[1]['mean_reach']:.0f}、+{(best_emo[1]['mean_reach']/bm['all']['mean_reach']-1)*100:.0f}%）に寄せると伸びる可能性")

    # LF8欲求の活用度
    lf8_list = p.get("lf8_list", [])
    top_lf8 = sorted(
        [(l, d) for l, d in bm.get("lf8", {}).items()],
        key=lambda x: -x[1]["mean_reach"]
    )[:3]
    if top_lf8 and lf8_list:
        best_lf = top_lf8[0]
        if best_lf[0] not in lf8_list and best_lf[1]["mean_reach"] > bm["all"]["mean_reach"] * 1.3:
            actions.append(f"LF8「{best_lf[0]}」（平均リーチ{best_lf[1]['mean_reach']:.0f}）の要素を取り入れると拡散力UP。恐怖×具体的対処の組み合わせが最も効果的")

    # カテゴリ別の勝ちパターン参照
    if not actions:
        cat = p["category"]
        if cat in bm.get("category", {}) and bm["category"][cat]["count"] >= 3:
            top_in_cat = sorted(
                [pp for pp in bm.get("_all_posts", []) if pp["category"] == cat],
                key=lambda x: x["reach"], reverse=True
            )
            if top_in_cat:
                top = top_in_cat[0]
                # TOP投稿の内容要素を参照
                top_emo = top.get("emotion_main", "")
                top_hooks = top.get("hooks", [])
                details = []
                if top_emo:
                    details.append(f"感情「{top_emo}」")
                if top_hooks:
                    details.append(f"フック「{'・'.join(top_hooks)}」")
                detail_str = f"（{', '.join(details)}）" if details else ""
                actions.append(f"「{cat}」TOP投稿#{top['num']}{detail_str}の構成を踏襲する")

    if not actions:
        actions.append("全指標が中位圏。感情トリガーを「気づき」「好奇心」に変更し、伏せ字・数字フックで初見の引きを強化する")

    return "。".join(actions[:3])  # 最大3アクション


def main():
    parser = argparse.ArgumentParser(description="投稿分析を世界水準で自動生成")
    parser.add_argument("--force", action="store_true", help="既存の考察も上書き")
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず内容だけ表示")
    parser.add_argument("--rows", type=str, default=None, help="対象行範囲（例: 4-30）")
    args = parser.parse_args()

    service = get_service()

    # 列マッピングをヘッダーから動的解決（必須列が見つからなければここで停止）
    resolve_columns(service)

    # 全データ取得（1日後 + 1週間後 + メタデータ）
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!A4:CT500",  # 2026-07-10: e1dd06a(220→500行)の直し漏れ。220のままだと行221以降の投稿に考察が永久生成されない
    ).execute()
    raw_rows = result.get("values", [])
    print(f"📊 {len(raw_rows)}行を読み込み")

    # ── 全投稿データをパース ──
    all_posts: List[Dict] = []
    for offset, row in enumerate(raw_rows):
        row_idx = 4 + offset
        row += [""] * (98 - len(row))

        num = cell(row, COL_NUM)
        title = cell(row, COL_TITLE)
        cta = cell(row, COL_CTA)
        reach = safe_int(cell(row, COL_1D_REACH))

        if not num:
            continue

        p = {
            "row_idx": row_idx,
            "offset": offset,
            "num": num,
            "date": cell(row, COL_DATE),
            "title": title,
            "cta": cta,
            "format": cell(row, COL_FORMAT),
            "intent": cell(row, COL_INTENT),
            "caption": cell(row, COL_CAPTION),
            "reach": reach,
            "reach_fw": safe_int(cell(row, COL_1D_REACH_FW)),
            "reach_nf": safe_int(cell(row, COL_1D_REACH_NF)),
            "imp": safe_int(cell(row, COL_1D_IMP)),
            "home": safe_int(cell(row, COL_1D_HOME)),
            "discover": safe_int(cell(row, COL_1D_DISCOVER)),
            "plays": safe_int(cell(row, COL_1D_PLAYS)),
            "likes": safe_int(cell(row, COL_1D_LIKES)),
            "saves": safe_int(cell(row, COL_1D_SAVES)),
            "comments": safe_int(cell(row, COL_1D_COMMENTS)),
            "shares": safe_int(cell(row, COL_1D_SHARES)),
            "prof": safe_int(cell(row, COL_1D_PROF)),
            "follow": safe_int(cell(row, COL_1D_FOLLOW)),
            "web": safe_int(cell(row, COL_1D_WEB)),
            "save_rate": safe_float(cell(row, COL_1D_SAVE_RATE)),
            # 1週間後
            "reach_7d": safe_int(cell(row, COL_7D_REACH)),
            "saves_7d": safe_int(cell(row, COL_7D_SAVES)),
            "likes_7d": safe_int(cell(row, COL_7D_LIKES)),
            "prof_7d": safe_int(cell(row, COL_7D_PROF)),
            "follow_7d": safe_int(cell(row, COL_7D_FOLLOW)),
            # 既存値
            "existing_result1": cell(row, COL_RESULT1),
            "existing_result2": cell(row, COL_RESULT2),
            "existing_result3": cell(row, COL_RESULT3),
            "existing_result4": cell(row, COL_RESULT4),
            "existing_analysis": cell(row, COL_ANALYSIS),
            "existing_next": cell(row, COL_NEXT),
            # コンテンツ情報
            "content": cell(row, COL_CONTENT),
            "lf8": cell(row, COL_LF8),
            "emotion": cell(row, COL_EMOTION),
            "kpi_target": cell(row, COL_KPI),
            # カテゴリ・フック
            "category": detect_category(title),
            "hooks": detect_hooks(title),
        }
        # 感情トリガーのメインを抽出（ベンチマーク用）
        emo_raw = p["emotion"]
        p["emotion_main"] = emo_raw.split("（")[0].split("・")[0].strip() if emo_raw else ""
        # LF8をリストに分割（ベンチマーク用）
        lf8_raw = p["lf8"]
        p["lf8_list"] = [x.strip() for x in lf8_raw.replace("／", "/").split("/") if x.strip()] if lf8_raw else []

        all_posts.append(p)

    print(f"📋 {len(all_posts)}件の投稿をパース")

    # ── ベンチマーク算出 ──
    bm = compute_benchmarks(all_posts)
    bm["_all_posts"] = [p for p in all_posts if p["reach"] > 0]

    data_count = len(bm.get("_all_posts", []))
    print(f"📈 ベンチマーク算出完了（データあり{data_count}件）")
    if bm.get("all"):
        print(f"   全体: リーチ平均{bm['all']['mean_reach']:.0f} 中央値{bm['all']['median_reach']:.0f} 保存平均{bm['all']['mean_saves']:.1f}")
    for cta, data in sorted(bm.get("cta", {}).items()):
        print(f"   CTA「{cta}」({data['count']}件): リーチ{data['mean_reach']:.0f} 保存{data['mean_saves']:.1f} いいね{data['mean_likes']:.1f}")
    # コンテンツベンチマーク表示
    if bm.get("emotion"):
        top_emo = sorted(bm["emotion"].items(), key=lambda x: -x[1]["mean_reach"])[:3]
        emo_strs = [f"{e}({d['mean_reach']:.0f})" for e, d in top_emo]
        print(f"   感情トリガーTOP: {', '.join(emo_strs)}")
    if bm.get("lf8"):
        top_lf = sorted(bm["lf8"].items(), key=lambda x: -x[1]["mean_reach"])[:3]
        lf_strs = [f"{l}({d['mean_reach']:.0f})" for l, d in top_lf]
        print(f"   LF8 TOP: {', '.join(lf_strs)}")

    # ── 行範囲フィルタ ──
    if args.rows:
        start, end = args.rows.split("-")
        row_start, row_end = int(start), int(end)
    else:
        row_start, row_end = 4, 999

    # ── 分析生成 & 書き込み ──
    updates = []
    generated = 0
    skipped = 0

    for p in all_posts:
        if p["row_idx"] < row_start or p["row_idx"] > row_end:
            continue

        # データなしの行はスキップ
        if p["reach"] == 0:
            skipped += 1
            continue

        # --forceでない場合、既に全部埋まっている行はスキップ
        if not args.force:
            if (p["existing_result1"] and p["existing_analysis"] and p["existing_next"]):
                skipped += 1
                continue

        # 生成
        r1 = generate_result1(p, bm)
        r2 = generate_result2(p, bm)
        r3 = generate_result3(p, bm)
        r4 = generate_result4(p, bm)
        analysis = generate_analysis(p, bm)
        next_action = generate_next_action(p, bm)

        if args.dry_run:
            print(f"\n{'='*60}")
            print(f"Row{p['row_idx']} #{p['num']} [{p['cta']}] {p['title'][:50]}")
            print(f"  結果①: {r1}")
            print(f"  結果②: {r2}")
            print(f"  結果③: {r3}")
            print(f"  結果④: {r4}")
            print(f"  考察: {analysis}")
            print(f"  次: {next_action}")

        # 結果①〜次ポイントの連続6列（ヘッダー解決済み・resolve_columnsで連続性検証済み）を更新
        update_values = [r1, r2, r3, r4, analysis, next_action]

        updates.append({
            "range": f"'{SHEET_NAME}'!{col_letter(COL_RESULT1)}{p['row_idx']}:{col_letter(COL_NEXT)}{p['row_idx']}",
            "majorDimension": "ROWS",
            "values": [update_values],
        })
        generated += 1

    print(f"\n📝 生成: {generated}件 | スキップ: {skipped}件")

    if args.dry_run:
        print("\n🔍 dry-runモード: 書き込みはしません")
        return

    if not updates:
        print("更新対象がありません。")
        return

    # バッチ書き込み（50件ずつ）
    print(f"\n⬆️  {len(updates)}行をスプレッドシートに書き込み中...")
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": batch,
        }
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body=body,
        ).execute()
        print(f"  バッチ {i // batch_size + 1}: {len(batch)}行更新完了")
        if i + batch_size < len(updates):
            time.sleep(1)

    print(f"\n✅ 完了: {generated}件の投稿分析を書き込みました")


if __name__ == "__main__":
    import os
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    main()
