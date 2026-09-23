"""Discord に送る通知を「鳴らす」か「置くだけ（@silent）」かを決める。

2026-09-24 タイキ「discord通知来すぎで充電なくなる。根本解決して」。
ローカル側（事業リポの utils/discord_notify._ring_decision）と同じ考え方に揃える:
  - 鳴らすのは critical だけ。warn / info / report は置くだけ
  - 深夜 0:00〜6:59 JST は critical も置くだけ（その時間に本人が動けることはない）
置くだけの通知も #一般 には残るので、見落としにはならない。

Actions は実行ごとに状態を持てないので、「同じ見出しは6時間に1回」の代わりに
alert_on_failure.py 側で「同じワークフローの前回も失敗なら置くだけ」を判定する。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
QUIET_HOURS_JST = range(0, 7)
SUPPRESS_NOTIFICATIONS = 4096


def ring_ok(level: str, now: datetime | None = None) -> tuple[bool, str]:
    """鳴らしてよいか。(True, "") か (False, 置くだけにした理由)。"""
    if level != "critical":
        return False, f"{level}は置くだけ"
    hour = (now or datetime.now(JST)).astimezone(JST).hour
    if hour in QUIET_HOURS_JST:
        return False, "深夜は置くだけ"
    return True, ""


def apply(payload: dict, ring: bool, why: str = "") -> dict:
    """鳴らさない時は @silent を付け、@here を外し、フッターに理由を足す。"""
    if ring:
        return payload
    payload["flags"] = SUPPRESS_NOTIFICATIONS
    if payload.get("content", "").strip() == "@here":
        payload.pop("content")
    for embed in payload.get("embeds", []):
        footer = embed.setdefault("footer", {})
        footer["text"] = (footer.get("text", "") + f" / 🔕 {why}").strip(" /")[:2048]
    return payload
