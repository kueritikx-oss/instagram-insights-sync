#!/usr/bin/env python3
"""
TikTokアクセストークン自動リフレッシュ (refresh_tiktok_token.py)

TikTokのアクセストークンは24時間で失効する。
このスクリプトは6時間ごとにGitHub Actionsで実行し、
トークンをリフレッシュしてGitHub Secretに書き戻す。

refresh_tokenは365日有効。新しいrefresh_tokenが返る場合は
古いものを上書きする。

環境変数:
  TIKTOK_TOKEN_JSON: 現在のトークン情報 (JSON文字列)
  TIKTOK_CLIENT_KEY: TikTokアプリのClient Key
  TIKTOK_CLIENT_SECRET: TikTokアプリのClient Secret
  GH_PAT: GitHub Personal Access Token (Secret書き戻し用)
  GITHUB_REPOSITORY: owner/repo (GitHub Actionsが自動設定)
"""

import json
import os
import sys
import time

import requests

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"


def refresh_token(token_data: dict, client_key: str, client_secret: str) -> dict:
    """TikTokアクセストークンをリフレッシュ"""
    refresh_tok = token_data.get("refresh_token")
    if not refresh_tok:
        print("ERROR: refresh_token が見つかりません")
        sys.exit(1)

    resp = requests.post(
        f"{TIKTOK_API_BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
        },
    )
    resp.raise_for_status()
    new_data = resp.json()

    error = new_data.get("error", {})
    if error and error.get("code") not in (None, "", "ok"):
        raise RuntimeError(f"Token refresh failed: {error}")

    # client_key/secretを保持
    new_data["client_key"] = client_key
    new_data["client_secret"] = client_secret
    new_data["saved_at"] = time.time()

    return new_data


def save_to_github_secret(secret_name: str, secret_value: str):
    """GitHub Secretにトークンを書き戻す"""
    gh_pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if not gh_pat or not repo:
        print("  ⚠️ GH_PAT or GITHUB_REPOSITORY not set → Secret書き戻しスキップ")
        return False

    try:
        from nacl.public import PublicKey, SealedBox
        import base64

        # リポジトリの公開鍵を取得
        headers = {
            "Authorization": f"Bearer {gh_pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
            headers=headers,
        )
        resp.raise_for_status()
        key_data = resp.json()

        # 暗号化
        public_key = PublicKey(base64.b64decode(key_data["key"]))
        sealed_box = SealedBox(public_key)
        encrypted = sealed_box.encrypt(secret_value.encode())
        encrypted_b64 = base64.b64encode(encrypted).decode()

        # Secret更新
        resp = requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
            headers=headers,
            json={
                "encrypted_value": encrypted_b64,
                "key_id": key_data["key_id"],
            },
        )
        resp.raise_for_status()
        print(f"  ✅ GitHub Secret '{secret_name}' を更新しました")
        return True

    except ImportError:
        print("  ⚠️ PyNaCl not installed → Secret書き戻しスキップ")
        print("    pip install pynacl で解決")
        return False
    except Exception as e:
        print(f"  ❌ Secret書き戻し失敗: {e}")
        return False


def main():
    print("🔄 TikTokトークンリフレッシュ")

    # 環境変数から読み込み
    token_json_str = os.environ.get("TIKTOK_TOKEN_JSON")
    client_key = os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET")

    if not token_json_str:
        print("ERROR: TIKTOK_TOKEN_JSON が未設定")
        sys.exit(1)
    if not client_key or not client_secret:
        print("ERROR: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET が未設定")
        sys.exit(1)

    token_data = json.loads(token_json_str)

    # 有効期限チェック
    saved_at = token_data.get("saved_at", 0)
    expires_in = token_data.get("expires_in", 86400)
    elapsed = time.time() - saved_at
    remaining = expires_in - elapsed

    print(f"  現在のトークン: 残り{remaining/3600:.1f}時間")

    if remaining > 3600:  # 1時間以上残っていてもリフレッシュする（安全マージン）
        print(f"  まだ有効だがリフレッシュを実行（安全マージン確保）")

    # リフレッシュ実行
    new_data = refresh_token(token_data, client_key, client_secret)
    print(f"  ✅ リフレッシュ成功")
    print(f"     expires_in: {new_data.get('expires_in')}秒")
    print(f"     open_id: {new_data.get('open_id', 'N/A')}")

    # GitHub Secretに書き戻し
    # TikTokのrefresh_tokenは回転式のため、書き戻し失敗を無視すると
    # 次回リフレッシュ時に旧refresh_tokenが失効していて恒久失効するリスクがある。
    # 失敗時はexit 1でworkflowをfailさせ、メール/アラートに乗せる。
    new_json = json.dumps(new_data)
    if not save_to_github_secret("TIKTOK_TOKEN_JSON", new_json):
        print("❌ Secret書き戻しに失敗 — 回転式refresh_tokenの恒久失効リスクがあるためworkflowをfailさせる")
        sys.exit(1)

    print("🎉 完了")


if __name__ == "__main__":
    main()
