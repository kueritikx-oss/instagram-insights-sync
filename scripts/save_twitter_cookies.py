"""X(Twitter) Cookie が更新されていたら GitHub Secret に書き戻す。

cloud_auto_post_x.py / sync_x_insights.py が CI 実行時に
auth/x_cookies_updated.json へ最新Cookieを書き出すので、
このスクリプトが現行 Secret (TWITTER_COOKIES env) と比較し、
変化があれば暗号化して Secret を更新する。
save_google_token.py と同じ sealed-box 方式。
"""
import base64
import json
import os
import sys

import requests
from nacl import encoding, public

COOKIES_FILE = "auth/x_cookies_updated.json"
REPO = "kueritikx-oss/instagram-insights-sync"


def load_updated_cookies():
    if not os.path.exists(COOKIES_FILE):
        print("⚠️ auth/x_cookies_updated.json が無い(X未実行 or 書き出し失敗)。スキップ。")
        return None
    with open(COOKIES_FILE) as f:
        cookies = json.load(f)
    # 最低限のバリデーション: ログイン本体のauth_tokenが無いCookieでSecretを潰さない
    if not cookies.get("auth_token"):
        print("❌ auth_token が無いCookie。元のSecretを保持してスキップ。")
        return None
    return cookies


def save_to_secret(cookies):
    gh_token = os.environ["GH_PAT"]
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.get(
        f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
        headers=headers,
    )
    if r.status_code != 200:
        print(f"❌ GitHub API エラー(public-key): {r.status_code}")
        return False
    key_data = r.json()
    pub_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pub_key)
    payload = json.dumps(cookies)
    encrypted = base64.b64encode(sealed.encrypt(payload.encode())).decode()
    r = requests.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/TWITTER_COOKIES",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
    )
    ok = r.status_code in (201, 204)
    print(f"{'✅' if ok else '❌'} TWITTER_COOKIES Secret 更新: {r.status_code}")
    return ok


def main():
    cookies = load_updated_cookies()
    if cookies is None:
        return 0
    current = os.environ.get("TWITTER_COOKIES", "")
    try:
        if current and json.loads(current) == cookies:
            print("✅ Cookie 変更なし。スキップ。")
            return 0
    except json.JSONDecodeError:
        pass  # 現行Secretが壊れているなら上書きしてよい
    if not os.environ.get("GH_PAT"):
        print("⚠️ GH_PAT 未設定 → Secret書き戻しスキップ")
        return 0
    print("🔄 Cookie が更新された。Secret書き戻し...")
    return 0 if save_to_secret(cookies) else 1


if __name__ == "__main__":
    sys.exit(main())
