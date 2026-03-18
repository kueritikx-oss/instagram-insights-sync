"""Google Token が更新されていたら GitHub Secret に書き戻す。
GitHub Actions の sync ステップ後に呼ばれる。"""
import json, os, sys, base64
import requests
from nacl import encoding, public
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_FILE = "auth/token.json"
SHEET_ID = "1xtEaMoZSWqrz7Z_fROS9QKgIHX3cydscVqLhQPckORg"
REPO = "kueritikx-oss/instagram-insights-sync"


def validate_token():
    """新しいトークンが有効か API コールで検証"""
    with open(TOKEN_FILE) as f:
        data = json.load(f)
    if not data.get("refresh_token"):
        print("❌ refresh_token がない。書き戻しを中止。")
        return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    if not creds.valid:
        creds.refresh(Request())
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="spreadsheetId"
    ).execute()
    print("✅ 新しいトークンのバリデーション成功")
    return creds.to_json()


def save_to_secret(validated_token):
    """バリデーション済みトークンを GitHub Secret に書き戻す"""
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
        print(f"❌ GitHub API エラー: {r.status_code}")
        return False
    key_data = r.json()
    pub_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pub_key)
    encrypted = base64.b64encode(sealed.encrypt(validated_token.encode())).decode()
    r = requests.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/GOOGLE_TOKEN_JSON",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
    )
    ok = r.status_code in (201, 204)
    print(f"{'✅' if ok else '❌'} GOOGLE_TOKEN_JSON Secret 更新: {r.status_code}")
    return ok


def main():
    if not os.path.exists(TOKEN_FILE):
        print("⚠️ token.json が見つからない。スキップ。")
        return
    try:
        validated = validate_token()
    except Exception as e:
        print(f"❌ トークンバリデーション失敗: {e}")
        print("元の Secret を保持。")
        return
    if validated:
        save_to_secret(validated)


if __name__ == "__main__":
    main()
