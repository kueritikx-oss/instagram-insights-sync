"""Sync 失敗時にマルチチャネル通知を送る。
1. Discord Webhook（即時Push）
2. GitHub Issue（永続記録、重複防止）
3. Healthchecks.io /fail ping（Dead-man's switch連携、UUIDが設定済みなら）

環境変数:
- DISCORD_WEBHOOK: Discord Webhook URL（未設定ならスキップ）
- GH_PAT: GitHub Personal Access Token（Issue作成用）
- HEALTHCHECK_UUID: healthchecks.io の UUID（未設定ならスキップ）
- GITHUB_WORKFLOW, GITHUB_REPOSITORY, GITHUB_RUN_ID: Actions標準変数
"""
import os
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discord_ring  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "kueritikx-oss/instagram-insights-sync")
WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "Unknown Workflow")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}" if RUN_ID else f"https://github.com/{REPO}/actions"


def previous_run_failed(gh_pat: str) -> bool:
    """同じワークフローの1つ前の実行も失敗していたか。分からない時は False（=鳴らす側に倒す）。

    auto-post-all は1時間ごとに走るので、壊れたままだと日中ずっと鳴り続ける。
    連続失敗の2回目以降は置くだけにして、鳴るのは「壊れ始めた1回目」だけにする。
    """
    if not (gh_pat and RUN_ID):
        return False
    headers = {"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github+json"}
    try:
        cur = requests.get(f"https://api.github.com/repos/{REPO}/actions/runs/{RUN_ID}",
                           headers=headers, timeout=10).json()
        runs = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/workflows/{cur['workflow_id']}/runs",
            headers=headers, params={"status": "completed", "per_page": 5}, timeout=10,
        ).json().get("workflow_runs", [])
        prev = [r for r in runs if str(r.get("id")) != str(RUN_ID)]
        return bool(prev) and prev[0].get("conclusion") == "failure"
    except Exception as e:
        print(f"⚠️ 前回の実行結果を読めない（鳴らす側に倒す）: {e}", file=sys.stderr)
        return False


def send_discord(webhook_url: str) -> bool:
    """Discord Webhook で即時通知。成功ならTrue。"""
    if not webhook_url:
        print("DISCORD_WEBHOOK 未設定 → Discord通知スキップ")
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "username": "GitHub Actions Alert",
        "embeds": [{
            "title": f"🔴 {WORKFLOW} 失敗",
            "description": f"[実行ログを開く]({RUN_URL})",
            "color": 15158332,  # red
            "fields": [
                {"name": "発生時刻", "value": now, "inline": True},
                {"name": "リポジトリ", "value": REPO, "inline": True},
                {"name": "次のアクション",
                 "value": (
                     "1. ログ確認: " + RUN_URL + "\n"
                     "2. 手動バックフィル: "
                     "`gh workflow run sync-instagram-insights.yml --field backfill_days=14`\n"
                     "3. トークン期限切れなら: "
                     "`gh workflow run refresh-instagram-token.yml`"
                 ),
                 "inline": False},
            ],
            "footer": {"text": "GitHub Actions"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    ring, why = discord_ring.ring_ok("critical")
    if ring and previous_run_failed(os.environ.get("GH_PAT", "")):
        ring, why = False, "連続失敗の2回目以降"
    discord_ring.apply(payload, ring, why)
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        print("🚨 Discord通知送信完了")
        return True
    except Exception as e:
        print(f"⚠️ Discord通知失敗: {e}", file=sys.stderr)
        return False


def send_github_issue(gh_pat: str) -> bool:
    """GitHub Issue 作成（重複防止付き）"""
    if not gh_pat:
        print("GH_PAT 未設定 → Issue作成スキップ")
        return False
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/issues?state=open&labels=auto-alert",
            headers=headers, timeout=10,
        )
        existing = [i for i in r.json() if f"{WORKFLOW} 失敗" in i.get("title", "")]
        if existing:
            print(f"既存の Issue あり ({existing[0]['html_url']})。スキップ。")
            return True
    except Exception as e:
        print(f"⚠️ Issue検索失敗: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/issues",
            headers=headers,
            json={
                "title": f"🔴 {WORKFLOW} 失敗",
                "body": (
                    f"## 発生日時\n{now}\n\n"
                    f"## 実行ログ\n{RUN_URL}\n\n"
                    f"## 確認手順\n"
                    f"1. Actions ログを確認\n"
                    f"2. Google Token: `python3 utils/sync_secrets_to_github.py --check`\n"
                    f"3. 手動バックフィル: `gh workflow run sync-instagram-insights.yml --field backfill_days=14`\n\n"
                    f"## よくある原因\n"
                    f"- 日ごとデータA列の日付生成漏れ → 2027年ブロック追加時など\n"
                    f"- Google refresh_token が無効（ローカルで再認証→Secret同期）\n"
                    f"- Instagram token 期限切れ（refresh-instagram-token.yml 実行）"
                ),
                "labels": ["auto-alert"],
            },
            timeout=10,
        )
        r.raise_for_status()
        print("📋 GitHub Issue 作成完了")
        return True
    except Exception as e:
        print(f"⚠️ Issue作成失敗: {e}", file=sys.stderr)
        return False


def send_healthcheck_fail(uuid: str) -> bool:
    """Healthchecks.io /fail エンドポイントに ping"""
    if not uuid:
        print("HEALTHCHECK_UUID 未設定 → healthchecks pingスキップ")
        return False
    try:
        requests.get(f"https://hc-ping.com/{uuid}/fail", timeout=10)
        print("📡 Healthchecks.io に失敗 ping")
        return True
    except Exception as e:
        print(f"⚠️ Healthchecks ping失敗: {e}", file=sys.stderr)
        return False


def main():
    print(f"=== Alert on Failure: {WORKFLOW} ===")
    discord_ok = send_discord(os.environ.get("DISCORD_WEBHOOK", ""))
    issue_ok = send_github_issue(os.environ.get("GH_PAT", ""))
    hc_ok = send_healthcheck_fail(os.environ.get("HEALTHCHECK_UUID", ""))
    print(f"\n通知結果: Discord={discord_ok} Issue={issue_ok} HealthcheckFail={hc_ok}")


if __name__ == "__main__":
    main()
