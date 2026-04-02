"""Instagram 投稿シートの E/F 列（ファイル名・投稿種別）のプレースホルダ検出と補完。

クラウド自動投稿・prepare_cloud_post・GitHub Actions 等が
「自動投稿（@tackey）」「自動」を書き込んだ行を、1.Post フォルダ名とキャプション推定で戻す。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

# utils/ の親 = 事業 Cursor
_WORKSPACE = Path(__file__).resolve().parent.parent

# GitHub Actions 上では Mac の 1.Post パスが存在しない。探索はローカルのみ。
if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
    POST_FOLDER_ROOTS: List[Path] = []
else:
    POST_FOLDER_ROOTS = [
        _WORKSPACE / "SNS/Instagram/1.Post",
    ]

# 自動投稿パイプラインがシートに入れる既知プレースホルダ（再発時にここへ追加）
KNOWN_PLACEHOLDER_TITLES = frozenset(
    {
        "自動投稿（@tackey）",
        "自動投稿（プロフィール誘導）",
        "自動投稿（インサイトスケジューラー）",
    }
)

CTA_TYPES = ["いいね", "保存", "フォロー", "ウェブタップ", "コメント"]

CTA_PATTERNS_ORDERED: List[Tuple[str, List[str]]] = [
    (
        "ウェブタップ",
        [
            "テキスト",
            "PDF",
            "お渡し",
            "配布",
            "受け取り",
            "公式LINE",
            "プレゼント",
            "無料で",
            "リンク",
        ],
    ),
    ("コメント", ["コメント", "教えて", "コメントで"]),
    ("フォロー", ["フォロー", "全部見れる", "フォローして"]),
    (
        "保存",
        [
            "保存",
            "ブックマーク",
            "後で見れる",
            "保存推奨",
            # 定番キャプション（スライド側にCTAがある投稿でキーワードが本文に無い対策）
            "まとめた",
            "まとめ",
            "一覧",
            "ランキング",
            "TOP",
            "チェック",
        ],
    ),
    ("いいね", ["2回タップ", "トントン", "ハート", "いいね"]),
]


def col_letter(idx: int) -> str:
    """0-based → A, B, …, Z, AA…"""
    result = ""
    n = idx
    while n >= 0:
        result = chr(n % 26 + 65) + result
        n = n // 26 - 1
    return result


def is_autopost_placeholder_title(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return True
    if t in KNOWN_PLACEHOLDER_TITLES:
        return True
    # 「自動投稿（…）」形式はプレースホルダ扱い（手入力タイトルがこの形式になることは想定しない）
    if t.startswith("自動投稿（") and "）" in t[:40]:
        return True
    return False


def is_autopost_placeholder_cta(s: str) -> bool:
    t = (s or "").strip()
    return t == "" or t == "自動"


def find_post_folder(post_num: str) -> Optional[Path]:
    prefix = f"{str(post_num).strip()}."
    for root in POST_FOLDER_ROOTS:
        if not root.is_dir():
            continue
        for item in sorted(root.iterdir()):
            if item.is_dir() and item.name.startswith(prefix):
                return item
    return None


def classify_cta_from_caption(caption: str) -> Optional[str]:
    """キャプションから投稿種別（CTA）を推定（audit_instagram_sheet と同ロジック）。"""
    if not caption:
        return None
    tail = caption[-500:] if len(caption) > 500 else caption
    best_cta = None
    best_score = 0
    for cta_type, keywords in CTA_PATTERNS_ORDERED:
        tail_score = sum(2 for kw in keywords if kw in tail)
        full_score = sum(1 for kw in keywords if kw in caption)
        score = tail_score + full_score
        if score > best_score:
            best_score = score
            best_cta = cta_type
    return best_cta


def build_metadata_fixes(
    row_num: int,
    post_num: str,
    title: str,
    cta: str,
    caption: str,
    *,
    col_title: int = 4,
    col_cta: int = 5,
) -> List[Tuple[str, str]]:
    """E/F のプレースホルダを実データに差し替える (cell_ref, value) のリスト。

    cell_ref は列字母+行番号のみ（シート名なし）。
    """
    fixes: List[Tuple[str, str]] = []
    if is_autopost_placeholder_title(title):
        folder = find_post_folder(post_num)
        if folder:
            fixes.append((f"{col_letter(col_title)}{row_num}", folder.name))
    if is_autopost_placeholder_cta(cta):
        inferred = classify_cta_from_caption(caption)
        if inferred:
            fixes.append((f"{col_letter(col_cta)}{row_num}", inferred))
    return fixes
