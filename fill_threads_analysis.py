"""Threads投稿毎データの結果①〜④・考察・次に活かすポイントを自動生成する。

IG版 fill_post_analysis.py と同じ設計思想:
  - 列番号ハードコード禁止。行1(セクション)+行3(列名)ヘッダーから resolve_columns() で動的解決
  - パーセンタイル順位 × フック型ベンチマーク × 個別化考察
  - 書き込みは既存列(結果①〜次の投稿に活かすポイント)への update のみ。append/列追加禁止

Threads固有の観点:
  - views中心(リーチ概念なし)。ER% と 会話率%(リプライ/views)が反応密度の軸
  - アルゴリズムはリプライ(会話)を最重視するため、会話率を強み/課題判定に組み込む
  - 7日後メトリクスがあれば views 伸び率からエバーグリーン度を判定(結果③)
  - 分類タグ(フック型/大分類)が未記入の投稿は「投稿の意図」列・本文キーワードでフォールバック

Usage:
    python3 fill_threads_analysis.py                  # 空欄のみ補完
    python3 fill_threads_analysis.py --force          # 既存の考察も上書き
    python3 fill_threads_analysis.py --dry-run        # 書き込みせず内容だけ表示
    python3 fill_threads_analysis.py --rows 4-10      # 対象行を絞る
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from sheet_column_map import _find_section_range, col_letter

SHEET_ID = "1hdBlZBn9s688f1ZwkTiO3suY27tJEHtXMEPkopLdBNI"
SHEET_NAME = "Threads投稿毎データ"
DATA_START_ROW = 4

GOOGLE_AUTH_DIR = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post"
TOKEN_FILE = GOOGLE_AUTH_DIR / "token.json"
SA_KEY_FILE = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-dashboard/service_account_key.json"

# ── Column indices (0-based) ──
# 🔴 起動時に resolve_columns(service) で行1+行3ヘッダーから動的解決される。
# 初期値 None はハードコード列番号の廃止(列挿入/改名時に即エラー停止させるため)。
COL_DATE = None       # 日付
COL_NUM = None        # 番号
COL_TIME = None       # 時刻
COL_TITLE = None      # ファイル名(本文冒頭)
COL_KIND = None       # 投稿種別(リプライ誘導/LINE誘導等)
COL_FORMAT = None     # 形式(認知/誘導/価値提供 — ファネル目的)
COL_INTENT = None     # 投稿の意図
COL_RESULT1 = None    # 結果①
COL_RESULT2 = None    # 結果②
COL_RESULT3 = None    # 結果③
COL_RESULT4 = None    # 結果④
COL_ANALYSIS = None   # 考察・仮説
COL_NEXT = None       # 次の投稿に活かすポイント
# 1日後メトリクス(Threadsメトリクス セクション)
COL_VIEWS = None
COL_LIKES = None
COL_REPLIES = None
COL_REPOSTS = None
COL_SHARES = None
COL_QUOTES = None
COL_ER = None
COL_CONV = None
COL_1D_CAPTURED = None
# 分類タグ
COL_CAT_MAIN = None   # 大分類
COL_CAT_SUB = None    # 小分類
COL_HOOK = None       # フック型
COL_CTA = None        # CTA型
# 7日後メトリクス(列名が 7d_ プレフィックスで一意)
COL_7D_VIEWS = None
COL_7D_LIKES = None
COL_7D_REPLIES = None
COL_7D_ER = None
COL_7D_CONV = None

# 行1のセクションヘッダー
_SECTION_PLAN = "企画・設計"
_SECTION_METRICS = "Threadsメトリクス"
_SECTION_TAGS = "分類タグ"

# (変数名, 行3キーワード候補, 行1セクション制約, 何番目の一致か(1-based), 必須か)
_COLUMN_SPECS = [
    ("COL_DATE",       ("日付",),                       None, 1, True),
    ("COL_NUM",        ("番号",),                       None, 1, True),
    ("COL_TIME",       ("時刻",),                       None, 1, False),
    ("COL_TITLE",      ("ファイル名",),                 None, 1, True),
    ("COL_KIND",       ("投稿種別",),                   None, 1, False),
    ("COL_FORMAT",     ("形式",),                       None, 1, False),
    ("COL_INTENT",     ("投稿の意図",),                 _SECTION_PLAN, 1, False),
    # 書き込み先6列(絶対にズレてはいけない)
    ("COL_RESULT1",    ("結果①",),                     None, 1, True),
    ("COL_RESULT2",    ("結果②",),                     None, 1, True),
    ("COL_RESULT3",    ("結果③",),                     None, 1, True),
    ("COL_RESULT4",    ("結果④",),                     None, 1, True),
    ("COL_ANALYSIS",   ("考察・仮説",),                 None, 1, True),
    ("COL_NEXT",       ("次の投稿に活かすポイント", "次投稿への示唆"), None, 1, True),
    # 1日後メトリクス
    ("COL_VIEWS",      ("views",),                      _SECTION_METRICS, 1, True),
    ("COL_LIKES",      ("いいね",),                     _SECTION_METRICS, 1, True),
    ("COL_REPLIES",    ("リプライ数",),                 _SECTION_METRICS, 1, True),
    ("COL_REPOSTS",    ("リポスト数",),                 _SECTION_METRICS, 1, True),
    ("COL_SHARES",     ("シェア数",),                   _SECTION_METRICS, 1, True),
    ("COL_QUOTES",     ("引用数",),                     _SECTION_METRICS, 1, False),
    ("COL_ER",         ("ER%",),                        _SECTION_METRICS, 1, True),
    ("COL_CONV",       ("会話率%",),                    _SECTION_METRICS, 1, True),
    ("COL_1D_CAPTURED", ("1d取得日時",),                _SECTION_METRICS, 1, False),
    # 分類タグ
    ("COL_CAT_MAIN",   ("大分類",),                     _SECTION_TAGS, 1, True),
    ("COL_CAT_SUB",    ("小分類",),                     _SECTION_TAGS, 1, False),
    ("COL_HOOK",       ("フック型",),                   _SECTION_TAGS, 1, True),
    ("COL_CTA",        ("CTA型",),                      _SECTION_TAGS, 1, False),
    # 7日後メトリクス(名前が一意なのでセクション制約なし)
    ("COL_7D_VIEWS",   ("7d_views",),                   None, 1, True),
    ("COL_7D_LIKES",   ("7d_いいね",),                  None, 1, False),
    ("COL_7D_REPLIES", ("7d_リプライ数",),              None, 1, False),
    ("COL_7D_ER",      ("7d_ER%",),                     None, 1, False),
    ("COL_7D_CONV",    ("7d_会話率%",),                 None, 1, False),
]


def resolve_columns(service) -> None:
    """行1(セクション)+行3(列名)ヘッダーを読み、COL_* を名前で動的解決する。

    必須列が見つからない場合は即エラー終了(ズレたまま書き込むより安全)。
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
            "❌ 必須列がヘッダーから見つかりません(列挿入/改名の可能性):\n  "
            + "\n  ".join(missing)
        )

    globals().update(resolved)

    # 書き込み先6列は連続していることを検証(範囲書き込みの前提)
    write_cols = [COL_RESULT1, COL_RESULT2, COL_RESULT3, COL_RESULT4, COL_ANALYSIS, COL_NEXT]
    if write_cols != list(range(COL_RESULT1, COL_RESULT1 + 6)):
        raise SystemExit(f"❌ 結果①〜次ポイントの6列が連続していません: {write_cols}")
    print(f"🧭 列マップ解決: 結果①〜次={col_letter(COL_RESULT1)}:{col_letter(COL_NEXT)} "
          f"views={col_letter(COL_VIEWS)} ER%={col_letter(COL_ER)} "
          f"7d_views={col_letter(COL_7D_VIEWS)}")


# ── コンテンツカテゴリ(大分類が未記入の場合のフォールバック) ──
CATEGORY_KEYWORDS = {
    "食事": ["食べ", "食材", "食事", "フルーツ", "ヨーグルト", "野菜", "ビタミン",
             "コンビニ", "飲み物", "ジュース", "白湯", "プロテイン", "糖", "油"],
    "スキンケア": ["洗顔", "保湿", "化粧水", "スキンケア", "美容液", "クレンジング", "日焼け止め"],
    "皮膚科・薬": ["皮膚科", "べピオ", "ベピオ", "薬", "処方"],
    "生活習慣": ["睡眠", "寝", "枕", "習慣", "ルーティ", "朝", "夜", "シャワー", "風呂"],
    "ニキビ知識": ["ニキビ", "毛穴", "肌荒れ", "皮脂", "ターンオーバー", "跡"],
    "ストーリー・実体験": ["僕が", "僕は", "だった僕", "変わった", "ありがとう", "日間"],
    "募集・案内": ["相談", "締切", "枠", "LINE", "募集", "受付"],
    "メンタル": ["メンタル", "自信", "悩み", "ストレス", "自分を"],
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
    if SA_KEY_FILE.exists() and not os.environ.get("SKIP_SERVICE_ACCOUNT"):
        creds = service_account.Credentials.from_service_account_file(str(SA_KEY_FILE), scopes=scopes)
        return build("sheets", "v4", credentials=creds)

    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
    else:
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
    if not val or not str(val).strip():
        return 0
    try:
        return int(float(str(val).strip().replace(",", "").replace("%", "")))
    except ValueError:
        return 0


def safe_float(val: str) -> float:
    if not val or not str(val).strip():
        return 0.0
    try:
        return float(str(val).strip().replace("%", "").replace(",", ""))
    except ValueError:
        return 0.0


def cell(row: list, idx: Optional[int]) -> str:
    if idx is not None and idx < len(row):
        return row[idx].strip() if isinstance(row[idx], str) else str(row[idx])
    return ""


def detect_category(title: str) -> str:
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in title for kw in keywords):
            return cat
    return "その他"


def percentile_rank(value: float, sorted_values: List[float]) -> int:
    """値のパーセンタイル順位を返す(上位X%)"""
    if not sorted_values:
        return 50
    count_below = sum(1 for v in sorted_values if v < value)
    rank = (count_below / len(sorted_values)) * 100
    return 100 - int(rank)


def compute_benchmarks(all_posts: List[Dict]) -> Dict[str, Any]:
    """全投稿データからベンチマーク統計を算出"""
    bm: Dict[str, Any] = {}
    data_posts = [p for p in all_posts if p["views"] > 0]
    if not data_posts:
        return bm

    views = sorted(p["views"] for p in data_posts)
    likes = sorted(p["likes"] for p in data_posts)
    replies = sorted(p["replies"] for p in data_posts)
    ers = sorted(p["er"] for p in data_posts)
    convs = sorted(p["conv"] for p in data_posts)

    bm["all"] = {
        "views": views,
        "likes": likes,
        "replies": replies,
        "er": ers,
        "conv": convs,
        "mean_views": statistics.mean(views),
        "median_views": statistics.median(views),
        "mean_er": statistics.mean(ers),
        "mean_conv": statistics.mean(convs),
        "mean_likes": statistics.mean(likes),
        "count": len(data_posts),
    }

    # フック型別統計(フック型タグ→なければ「形式」でフォールバック)
    hook_groups = defaultdict(list)
    for p in data_posts:
        if p["hook"]:
            hook_groups[p["hook"]].append(p)
    bm["hook"] = {}
    for hook, posts in hook_groups.items():
        if len(posts) >= 2:
            bm["hook"][hook] = {
                "mean_views": statistics.mean(pp["views"] for pp in posts),
                "mean_er": statistics.mean(pp["er"] for pp in posts),
                "mean_replies": statistics.mean(pp["replies"] for pp in posts),
                "count": len(posts),
            }

    # 大分類別統計(未記入は本文キーワード判定でフォールバック)
    cat_groups = defaultdict(list)
    for p in data_posts:
        cat_groups[p["category"]].append(p)
    bm["category"] = {}
    for cat, posts in cat_groups.items():
        if len(posts) >= 2:
            bm["category"][cat] = {
                "mean_views": statistics.mean(pp["views"] for pp in posts),
                "mean_er": statistics.mean(pp["er"] for pp in posts),
                "count": len(posts),
            }

    # 投稿種別(CTA)別統計
    kind_groups = defaultdict(list)
    for p in data_posts:
        if p["kind"]:
            kind_groups[p["kind"]].append(p)
    bm["kind"] = {}
    for kind, posts in kind_groups.items():
        if len(posts) >= 2:
            bm["kind"][kind] = {
                "mean_views": statistics.mean(pp["views"] for pp in posts),
                "mean_replies": statistics.mean(pp["replies"] for pp in posts),
                "count": len(posts),
            }

    # TOP投稿
    bm["top_views"] = sorted(data_posts, key=lambda p: p["views"], reverse=True)[:10]
    bm["top_er"] = sorted(
        [p for p in data_posts if p["views"] >= bm["all"]["median_views"]],
        key=lambda p: p["er"], reverse=True)[:10]

    return bm


def generate_result1(p: Dict, bm: Dict) -> str:
    """結果①: viewsパーセンタイル順位 + フック型別平均との差"""
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    n = bm["all"]["count"]
    parts = [f"views {p['views']:,}（全{n}本中 上位{views_pct}%）"]

    hook = p["hook"]
    if hook and hook in bm.get("hook", {}):
        hb = bm["hook"][hook]
        if hb["mean_views"] > 0:
            ratio = (p["views"] - hb["mean_views"]) / hb["mean_views"] * 100
            sign = "+" if ratio >= 0 else ""
            parts.append(f"フック型「{hook}」平均{hb['mean_views']:.0f}比{sign}{ratio:.0f}%（{hb['count']}本）")

    parts.append(f"いいね{p['likes']} リプ{p['replies']} リポスト{p['reposts']}")
    return " | ".join(parts)


def generate_result2(p: Dict, bm: Dict) -> str:
    """結果②: 強み/課題(ER%・会話率の相対評価)"""
    strengths, weaknesses = [], []

    er_pct = percentile_rank(p["er"], bm["all"]["er"])
    conv_pct = percentile_rank(p["conv"], bm["all"]["conv"])
    views_pct = percentile_rank(p["views"], bm["all"]["views"])

    if p["er"] > 0:
        if er_pct <= 20:
            strengths.append(f"ER{p['er']:.2f}%（上位{er_pct}%）")
        elif er_pct >= 70:
            weaknesses.append(f"ER{p['er']:.2f}%（下位{100 - er_pct}%）")
    else:
        weaknesses.append("エンゲージメント0（無反応）")

    if p["conv"] > 0:
        if conv_pct <= 20:
            strengths.append(f"会話率{p['conv']:.2f}%（上位{conv_pct}%・リプ{p['replies']}件）")
    elif p["replies"] == 0 and p["views"] >= bm["all"]["median_views"]:
        weaknesses.append("会話率0%（露出はあるのにリプが生まれていない）")

    if views_pct <= 20:
        strengths.append(f"露出（views上位{views_pct}%）")
    elif views_pct >= 70:
        weaknesses.append(f"露出（views下位{100 - views_pct}%）")

    if p["reposts"] + p["quotes"] >= 1:
        strengths.append(f"拡散反応あり（リポスト{p['reposts']}+引用{p['quotes']}）")

    parts = []
    if strengths:
        parts.append("強み: " + ", ".join(strengths))
    if weaknesses:
        parts.append("課題: " + ", ".join(weaknesses))
    if not parts:
        parts.append(f"全指標中位圏（views上位{views_pct}% ER上位{er_pct}%）")
    return " | ".join(parts)


def generate_result3(p: Dict, bm: Dict) -> str:
    """結果③: 7日後があれば伸び率(エバーグリーン度)"""
    if p["views_7d"] <= 0 or p["views"] <= 0:
        return ""
    growth = (p["views_7d"] - p["views"]) / p["views"] * 100
    if growth >= 30:
        label = "エバーグリーン性が高い（1日目以降も伸び続けている）"
    elif growth >= 10:
        label = "初速後も継続露出あり"
    elif growth >= 0:
        label = "初速型（1日目でほぼ出切り）"
    else:
        label = "初速型（1日目でほぼ出切り・7d計測時点の誤差含む）"
    parts = [f"1d→7d views {p['views']:,}→{p['views_7d']:,}（{'+' if growth >= 0 else ''}{growth:.0f}%）。{label}"]
    if p["likes_7d"] > p["likes"]:
        parts.append(f"いいね{p['likes']}→{p['likes_7d']}")
    if p["replies_7d"] > p["replies"]:
        parts.append(f"リプ{p['replies']}→{p['replies_7d']}")
    return " | ".join(parts)


def generate_result4(p: Dict, bm: Dict) -> str:
    """結果④: 予備(空可)。拡散・引用など特記事項がある場合のみ記録"""
    parts = []
    if p["shares"] >= 1:
        parts.append(f"シェア{p['shares']}件（外部共有あり）")
    if p["quotes"] >= 1:
        parts.append(f"引用{p['quotes']}件（言及を誘発）")
    return " | ".join(parts)


def generate_analysis(p: Dict, bm: Dict) -> str:
    """考察・仮説: フック型×大分類×数値根拠で「なぜ」を個別化して言語化"""
    insights = []
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    er_pct = percentile_rank(p["er"], bm["all"]["er"])
    hook = p["hook"]
    cat = p["category"]
    hb = bm.get("hook", {}).get(hook)
    cb = bm.get("category", {}).get(cat)
    mean_views = bm["all"]["mean_views"]

    # ── 1. なぜ伸びた/伸びなかった(views軸) ──
    if views_pct <= 10:
        if hook and hb and p["views"] >= hb["mean_views"]:
            insights.append(
                f"views {p['views']:,}は全体上位{views_pct}%かつフック型「{hook}」の平均{hb['mean_views']:.0f}も超えた。"
                f"この型×「{cat}」テーマの組み合わせがフィードで足を止めさせた可能性が高い")
        else:
            insights.append(
                f"views {p['views']:,}は全体上位{views_pct}%。テーマ「{cat}」自体の需要か、"
                f"初動のリプ・いいねがアルゴリズムの追い風を呼んだと考えられる")
        top_cats = [t["category"] for t in bm.get("top_views", [])[:5]]
        if cat != "その他" and top_cats.count(cat) >= 2:
            insights.append(f"「{cat}」はviews TOP5のうち{top_cats.count(cat)}本を占める勝ちテーマ")
    elif views_pct <= 30:
        insights.append(f"views {p['views']:,}（上位{views_pct}%）で平均{mean_views:.0f}を上回る安定圏")
    elif views_pct >= 70:
        if hook and hb and hb["mean_views"] > mean_views and p["views"] < hb["mean_views"] * 0.6:
            insights.append(
                f"views {p['views']:,}は下位{100 - views_pct}%。フック型「{hook}」自体は平均{hb['mean_views']:.0f}と"
                f"悪くない型なので、型ではなく冒頭1行の引きか投稿時間帯が原因の可能性")
        elif cb and p["views"] < cb["mean_views"] * 0.7:
            insights.append(
                f"views {p['views']:,}は「{cat}」テーマの平均{cb['mean_views']:.0f}も下回った。"
                f"冒頭1行で「自分ごと化」させる要素（数字・問いかけ・意外性）が弱かったと推測")
        else:
            insights.append(
                f"views {p['views']:,}は下位{100 - views_pct}%。Threadsは初動の会話量で露出が決まるため、"
                f"最初の1時間にリプが付かなかったことが拡散が止まった主因と考えられる")
    else:
        insights.append(f"views {p['views']:,}（上位{views_pct}%）は中位圏")

    # ── 2. 反応の質(ER・会話率) ──
    if p["er"] > 0 and er_pct <= 15:
        insights.append(
            f"ER{p['er']:.2f}%は上位{er_pct}%（全体平均{bm['all']['mean_er']:.2f}%）で、届いた人の反応密度が濃い。"
            f"露出さえ増えればスケールする内容")
    if p["replies"] >= 2:
        insights.append(
            f"リプ{p['replies']}件（会話率{p['conv']:.2f}%）。Threadsのアルゴリズムはリプを最重視するため、"
            f"この会話発生が露出の持続に効いている")
    elif p["views"] >= mean_views and p["replies"] == 0:
        insights.append(
            "露出量に対してリプ0件。読まれてはいるが「返信したくなる余白」（問いかけ・賛否が割れる断言）が"
            "本文になく、片方向の情報提供で終わった可能性")
    if p["likes"] == 0 and p["replies"] == 0 and p["views"] < bm["all"]["median_views"]:
        insights.append("反応ゼロは内容以前に露出不足の影響が大きく、この1本だけで内容の良し悪しは判断できない")

    # ── 3. 7日後の挙動 ──
    if p["views_7d"] > 0 and p["views"] > 0:
        growth = (p["views_7d"] - p["views"]) / p["views"] * 100
        if growth >= 30:
            insights.append(
                f"7日で views +{growth:.0f}%と後伸びしており、検索・おすすめ経由で読まれ続ける"
                f"エバーグリーン素材。同テーマの再投稿・深掘りの価値が高い")

    # ── 4. 投稿種別(CTA)との整合 ──
    kind = p["kind"]
    kb = bm.get("kind", {}).get(kind)
    if kind == "リプライ誘導" and p["replies"] == 0 and p["views"] >= bm["all"]["median_views"]:
        insights.append("リプライ誘導投稿だがリプ0件。問いの具体性（二択にする・答えやすくする）に改善余地")
    elif kind and kb and kb["count"] >= 3 and p["views"] > kb["mean_views"] * 1.5:
        insights.append(f"「{kind}」投稿の平均views {kb['mean_views']:.0f}を大きく超えており、この種別の当たり回")

    return "。".join(insights[:4])


def generate_next_action(p: Dict, bm: Dict) -> str:
    """次の投稿に活かすポイント: 具体的で再現可能なアクション"""
    actions = []
    views_pct = percentile_rank(p["views"], bm["all"]["views"])
    er_pct = percentile_rank(p["er"], bm["all"]["er"])
    hook = p["hook"]
    cat = p["category"]
    hb = bm.get("hook", {}).get(hook)

    # 成功パターンの再現
    if views_pct <= 15:
        if hook:
            actions.append(f"フック型「{hook}」×「{cat}」は勝ちパターン。同じ型で別の切り口をシリーズ化する")
        else:
            actions.append(f"「{cat}」テーマの需要が実証された。冒頭1行の構造を保ったまま横展開する")
    if p["replies"] >= 2:
        actions.append(f"リプ{p['replies']}件を生んだ問いの立て方を再利用。投稿後30分はリプ返しに張り付いて会話を伸ばす")

    # 改善アクション
    if views_pct >= 60:
        top_hooks = sorted(bm.get("hook", {}).items(), key=lambda x: -x[1]["mean_views"])[:2]
        hook_strs = [f"「{h}」（平均views {d['mean_views']:.0f}）" for h, d in top_hooks if d["mean_views"] > bm["all"]["mean_views"]]
        if hook_strs and (not hook or hook not in [h for h, _ in top_hooks]):
            actions.append(f"露出改善: 高効果フック型{('・'.join(hook_strs))}に寄せてテスト")
        else:
            actions.append("露出改善: 冒頭1行に数字か問いかけを入れ、火・木・土の7:30/12:00/20:00帯で再テスト")
    if p["views"] >= bm["all"]["median_views"] and p["replies"] == 0:
        actions.append("本文末尾を「どっち派？」等の答えやすい二択質問で締めて会話率を作る")
    if er_pct <= 15 and views_pct >= 40:
        actions.append("反応密度は高いのに露出が足りていない。同内容を時間帯・冒頭だけ変えてもう1回出す価値あり")

    if not actions:
        cb = bm.get("category", {}).get(cat)
        if cb and cb["count"] >= 3:
            top_in_cat = max(
                (pp for pp in bm.get("top_views", []) if pp["category"] == cat),
                key=lambda x: x["views"], default=None)
            if top_in_cat and top_in_cat["num"] != p["num"]:
                actions.append(f"「{cat}」TOP投稿{top_in_cat['num']}（views {top_in_cat['views']:,}）の冒頭・構成を踏襲する")
    if not actions:
        actions.append("中位圏。冒頭1行を「意外な事実+数字」に差し替え、リプしたくなる余白を1つ残す構成でテスト")

    return "。".join(actions[:3])


def main():
    parser = argparse.ArgumentParser(description="Threads投稿分析を自動生成")
    parser.add_argument("--force", action="store_true", help="既存の考察も上書き")
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず内容だけ表示")
    parser.add_argument("--rows", type=str, default=None, help="対象行範囲（例: 4-30）")
    parser.add_argument("--limit", type=int, default=None, help="生成件数の上限（dry-run確認用）")
    args = parser.parse_args()

    service = get_service()
    resolve_columns(service)

    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!A{DATA_START_ROW}:BF1000",
    ).execute()
    raw_rows = result.get("values", [])
    print(f"📊 {len(raw_rows)}行を読み込み")

    all_posts: List[Dict] = []
    for offset, row in enumerate(raw_rows):
        num = cell(row, COL_NUM)
        if not num:
            continue
        title = cell(row, COL_TITLE)
        cat_main = cell(row, COL_CAT_MAIN)
        hook_tag = cell(row, COL_HOOK)
        fmt = cell(row, COL_FORMAT)
        p = {
            "row_idx": DATA_START_ROW + offset,
            "num": num,
            "date": cell(row, COL_DATE),
            "title": title,
            "kind": cell(row, COL_KIND),
            "format": fmt,
            "intent": cell(row, COL_INTENT),
            "views": safe_int(cell(row, COL_VIEWS)),
            "likes": safe_int(cell(row, COL_LIKES)),
            "replies": safe_int(cell(row, COL_REPLIES)),
            "reposts": safe_int(cell(row, COL_REPOSTS)),
            "shares": safe_int(cell(row, COL_SHARES)),
            "quotes": safe_int(cell(row, COL_QUOTES)),
            "er": safe_float(cell(row, COL_ER)),
            "conv": safe_float(cell(row, COL_CONV)),
            "views_7d": safe_int(cell(row, COL_7D_VIEWS)),
            "likes_7d": safe_int(cell(row, COL_7D_LIKES)),
            "replies_7d": safe_int(cell(row, COL_7D_REPLIES)),
            "existing_result1": cell(row, COL_RESULT1),
            "existing_analysis": cell(row, COL_ANALYSIS),
            "existing_next": cell(row, COL_NEXT),
            # フック型タグ→なければ「投稿の意図」(質問型/分類型等が入っている)でフォールバック
            # ※「形式」列は認知/誘導/価値提供のファネル目的なのでフックには使わない
            "hook": hook_tag or cell(row, COL_INTENT),
            # 大分類→なければ本文キーワード判定
            "category": cat_main or detect_category(title),
        }
        # ER%/会話率% がシート未計算(同期前バックフィル等)の行は生値から再計算
        if p["views"] > 0:
            eng = p["likes"] + p["replies"] + p["reposts"] + p["quotes"] + p["shares"]
            if p["er"] == 0 and eng > 0:
                p["er"] = round(eng / p["views"] * 100, 2)
            if p["conv"] == 0 and p["replies"] > 0:
                p["conv"] = round(p["replies"] / p["views"] * 100, 2)
        all_posts.append(p)

    print(f"📋 {len(all_posts)}件の投稿をパース")

    bm = compute_benchmarks(all_posts)
    if not bm.get("all"):
        print("❌ viewsが入っている投稿がありません。終了")
        return
    print(f"📈 ベンチマーク算出完了（データあり{bm['all']['count']}件）")
    print(f"   views 平均{bm['all']['mean_views']:.0f} 中央値{bm['all']['median_views']:.0f} "
          f"ER平均{bm['all']['mean_er']:.2f}% 会話率平均{bm['all']['mean_conv']:.2f}%")
    for hook, d in sorted(bm.get("hook", {}).items(), key=lambda x: -x[1]["mean_views"])[:5]:
        print(f"   フック型「{hook}」({d['count']}本): views {d['mean_views']:.0f} ER {d['mean_er']:.2f}%")

    if args.rows:
        start, end = args.rows.split("-")
        row_start, row_end = int(start), int(end)
    else:
        row_start, row_end = DATA_START_ROW, 10**6

    updates = []
    generated = 0
    skipped = 0
    for p in all_posts:
        if p["row_idx"] < row_start or p["row_idx"] > row_end:
            continue
        if p["views"] == 0:
            skipped += 1
            continue
        if not args.force:
            if p["existing_result1"] and p["existing_analysis"] and p["existing_next"]:
                skipped += 1
                continue
        if args.limit is not None and generated >= args.limit:
            break

        r1 = generate_result1(p, bm)
        r2 = generate_result2(p, bm)
        r3 = generate_result3(p, bm)
        r4 = generate_result4(p, bm)
        analysis = generate_analysis(p, bm)
        next_action = generate_next_action(p, bm)

        if args.dry_run:
            print(f"\n{'=' * 60}")
            print(f"Row{p['row_idx']} {p['num']} [{p['kind']}/{p['hook']}] {p['title'][:40]!r}")
            print(f"  結果①: {r1}")
            print(f"  結果②: {r2}")
            print(f"  結果③: {r3}")
            print(f"  結果④: {r4}")
            print(f"  考察: {analysis}")
            print(f"  次: {next_action}")

        updates.append({
            "range": f"'{SHEET_NAME}'!{col_letter(COL_RESULT1)}{p['row_idx']}:{col_letter(COL_NEXT)}{p['row_idx']}",
            "majorDimension": "ROWS",
            "values": [[r1, r2, r3, r4, analysis, next_action]],
        })
        generated += 1

    print(f"\n📝 生成: {generated}件 | スキップ: {skipped}件")

    if args.dry_run:
        print("\n🔍 dry-runモード: 書き込みはしません")
        return
    if not updates:
        print("更新対象がありません。")
        return

    print(f"\n⬆️  {len(updates)}行をスプレッドシートに書き込み中...")
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": batch},
        ).execute()
        print(f"  バッチ {i // batch_size + 1}: {len(batch)}行更新完了")
        if i + batch_size < len(updates):
            time.sleep(1)

    print(f"\n✅ 完了: {generated}件の投稿分析を書き込みました")


if __name__ == "__main__":
    main()
