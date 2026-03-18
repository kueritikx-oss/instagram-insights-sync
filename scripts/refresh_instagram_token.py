"""Instagram トークンの有効期限をチェックし、残り30日以内なら自動延長する。
GitHub Actions の weekly cron から呼ばれる。"""
import json, os, sys, base64, time
import requests as req
from datetime import datetime, timezone

REPO = "kueritikx-oss/instagram-insights-sync"


def create_issue(title, body):
    """GitHub Issue を作成（同タイトルの open Issue があればスキップ）"""
    gh_pat = os.environ["GH_PAT"]
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    existing = req.get(
        f"https://api.github.com/repos/{REPO}/issues?state=open&labels=auto-alert",
        headers=headers,
    ).json()
    if any(title in i.get("title", "") for i in existing):
        print(f"既存のアラート Issue あり。スキップ: {title}")
        return
    req.post(
        f"https://api.github.com/repos/{REPO}/issues",
        headers=headers,
        json={"title": title, "body": body, "labels": ["auto-alert"]},
    )
    print(f"🚨 Issue 作成: {title}")


def check_token():
    """現在のトークンの有効期限を確認"""
    token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    r = req.get(
        "https://graph.facebook.com/v19.0/debug_token",
        params={"input_token": token, "access_token": token},
        timeout=10,
    )
    data = r.json().get("data", {})
    return data.get("is_valid", False), data.get("expires_at", 0)


def refresh_token():
    """Long-lived token を新しい Long-lived token に交換（リトライ付き）"""
    token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    app_id = os.environ["META_APP_ID"]
    app_secret = os.environ["META_APP_SECRET"]
    result = {}
    for attempt in range(3):
        try:
            r = req.get(
                "https://graph.facebook.com/v19.0/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "fb_exchange_token": token,
                },
                timeout=15,
            )
            result = r.json()
            if "access_token" in result:
                print(f"✅ 新しいトークン取得成功 (attempt {attempt + 1})")
                return result["access_token"]
            if r.status_code >= 500:
                wait = 10 * (3**attempt)
                print(f"⚠️ サーバーエラー {r.status_code}。{wait}秒後にリトライ...")
                time.sleep(wait)
            else:
                print(f"❌ トークン延長失敗 (HTTP {r.status_code}): {result}")
                return None
        except req.exceptions.Timeout:
            wait = 10 * (3**attempt)
            print(f"⚠️ タイムアウト。{wait}秒後にリトライ...")
            time.sleep(wait)
    return None


def validate_new_token(new_token):
    """新トークンで軽量 API コールを実行してバリデーション"""
    r = req.get(
        "https://graph.facebook.com/v19.0/me",
        params={"access_token": new_token},
        timeout=10,
    )
    return r.status_code == 200


def save_to_secret(new_token):
    """GitHub Secret に新しいトークンを保存"""
    from nacl import encoding, public

    gh_pat = os.environ["GH_PAT"]
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = req.get(
        f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
        headers=headers,
    )
    key_data = r.json()
    pub_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pub_key)
    encrypted = base64.b64encode(sealed.encrypt(new_token.encode())).decode()
    r = req.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/INSTAGRAM_ACCESS_TOKEN",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
    )
    ok = r.status_code in (201, 204)
    print(f"{'✅' if ok else '❌'} INSTAGRAM_ACCESS_TOKEN Secret 更新: {r.status_code}")
    return ok


def main():
    print("=" * 50)
    print("🔍 Instagram トークン有効期限チェック")
    print("=" * 50)

    is_valid, expires_at = check_token()

    if not is_valid:
        print("❌ トークンは既に無効です。")
        create_issue(
            "🚨 Instagram トークンが無効 - 手動再発行が必要",
            "## 手順\n"
            "1. [Meta Graph API Explorer](https://developers.facebook.com/tools/explorer/) を開く\n"
            "2. App: 'Insights Auto Export' を選択\n"
            "3. `instagram_content_publish` にチェック → Generate Access Token\n"
            "4. `instagram_insights_config.json` を更新\n"
            "5. `python3 utils/sync_secrets_to_github.py` を実行\n",
        )
        sys.exit(1)

    if not expires_at:
        print("✅ 無期限トークン。更新不要。")
        return

    expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    days_left = (expiry - now).total_seconds() / 86400
    print(f"📅 有効期限: {expiry.strftime('%Y-%m-%d')} (残り {days_left:.0f} 日)")

    if days_left > 30:
        print(f"✅ 残り {days_left:.0f} 日。更新不要。")
        return

    print(f"\n⚠️ 残り {days_left:.0f} 日。自動延長を実行...")
    new_token = refresh_token()

    if not new_token:
        level = "🔴" if days_left < 7 else "🟡"
        create_issue(
            f"{level} Instagram トークン残り {days_left:.0f} 日 - 自動延長失敗",
            f"自動延長が失敗しました。手動で再発行してください。",
        )
        sys.exit(1)

    print("🔍 新しいトークンをバリデーション中...")
    if not validate_new_token(new_token):
        print("❌ 新トークンのバリデーション失敗。書き戻しを中止。")
        create_issue(
            "🟡 Instagram トークン延長: バリデーション失敗",
            "新トークンを取得したが、バリデーション API コールが失敗。手動確認が必要。",
        )
        sys.exit(1)
    print("✅ バリデーション成功")

    save_to_secret(new_token)

    # 新しいトークンの有効期限を表示
    r = req.get(
        "https://graph.facebook.com/v19.0/debug_token",
        params={"input_token": new_token, "access_token": new_token},
        timeout=10,
    )
    new_exp = r.json().get("data", {}).get("expires_at", 0)
    if new_exp:
        new_expiry = datetime.fromtimestamp(new_exp, tz=timezone.utc)
        print(f"📅 新しい有効期限: {new_expiry.strftime('%Y-%m-%d')}")
    print("\n🎉 Instagram トークン自動延長完了！")


if __name__ == "__main__":
    main()
