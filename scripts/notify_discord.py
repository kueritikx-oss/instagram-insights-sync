"""GitHub Actions から Discord（サーバー「タッキーSNS通知」#一般）へ要点を1通送る。

2026-09-24 通知の送り先を Discord 1本に統一（ntfy をやめた）。
ローカルの utils/ntfy_notify.py（中身は Discord）と同じ Webhook・同じ色分けに揃える。

環境変数:
- DISCORD_WEBHOOK: Webhook URL（未設定ならスキップ）
- NOTIFY_TITLE / NOTIFY_BODY: 見出しと本文
- NOTIFY_LEVEL: info | warn | critical（critical だけ @here を付ける。
  スマホが鳴るのは日中の critical だけ。判定は scripts/discord_ring.py）

通知の失敗でジョブを落とさない（常に exit 0。理由はログに出す）。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discord_ring  # noqa: E402

COLOR = {"critical": 0xE74C3C, "warn": 0xF39C12, "info": 0x3498DB}
EMOJI = {"critical": "🚨", "warn": "⚠️", "info": "ℹ️"}


def main() -> int:
    url = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not url:
        print("DISCORD_WEBHOOK 未設定 → 通知スキップ")
        return 0
    level = os.environ.get("NOTIFY_LEVEL", "info")
    if level not in COLOR:
        level = "info"
    title = os.environ.get("NOTIFY_TITLE", "通知")
    payload = {"embeds": [{
        "title": f"{EMOJI[level]} {title}"[:256],
        "description": os.environ.get("NOTIFY_BODY", "")[:4000],
        "color": COLOR[level],
    }]}
    if level == "critical":
        payload["content"] = "@here"
    # 鳴らすのは日中の critical だけ。それ以外は置くだけ（2026-09-24 充電が持たないほど鳴っていた）
    discord_ring.apply(payload, *discord_ring.ring_ok(level))
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Python 既定の UA は Discord 側で弾かれることがあるので名乗る
            "User-Agent": "DiscordBot (https://github.com/kueritikx-oss/instagram-insights-sync, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"Discord通知 HTTP {r.status}: {title}")
    except Exception as exc:  # 通知失敗でジョブを落とさない
        print(f"⚠️ Discord通知失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
