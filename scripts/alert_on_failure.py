"""Sync 失敗時に GitHub Issue を作成してアラートする。"""
import os
import requests
from datetime import datetime, timezone

REPO = "kueritikx-oss/instagram-insights-sync"


def main():
    gh_pat = os.environ["GH_PAT"]
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    # 同じタイトルの open な Issue があればスキップ
    r = requests.get(
        f"https://api.github.com/repos/{REPO}/issues?state=open&labels=auto-alert",
        headers=headers,
    )
    existing = [
        i
        for i in r.json()
        if "Sync Instagram Insights 失敗" in i.get("title", "")
    ]
    if existing:
        print("既存のアラート Issue あり。スキップ。")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    requests.post(
        f"https://api.github.com/repos/{REPO}/issues",
        headers=headers,
        json={
            "title": "🔴 Sync Instagram Insights 失敗",
            "body": (
                f"## 発生日時\n{now}\n\n"
                f"## 確認手順\n"
                f"1. [Actions ログ](https://github.com/{REPO}/actions) を確認\n"
                f"2. Google Token: `python3 utils/sync_secrets_to_github.py --check`\n"
                f"3. 修正後: `python3 utils/sync_secrets_to_github.py` で Secret 同期\n\n"
                f"## よくある原因\n"
                f"- Google refresh_token が無効（ローカルで再認証→Secret同期）\n"
                f"- Instagram token 期限切れ（Meta Graph API Explorer で再発行）"
            ),
            "labels": ["auto-alert"],
        },
    )
    print("🚨 アラート Issue 作成完了")


if __name__ == "__main__":
    main()
