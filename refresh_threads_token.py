#!/usr/bin/env python3
"""
Threadsトークン自動リフレッシュ

60日で切れるトークンを自動で更新し、GitHub Secretsに書き戻す。
IGと違ってapp_secretが不要 — トークンだけでリフレッシュ可能。

Usage:
    python refresh_threads_token.py
"""

import json
import os
import sys
import time
import requests
from base64 import b64encode
from nacl import encoding, public


def refresh_token(current_token: str) -> dict:
    """Threadsトークンをリフレッシュ（リトライ3回）"""
    for attempt in range(3):
        try:
            r = requests.get(
                "https://graph.threads.net/refresh_access_token",
                params={
                    "grant_type": "th_refresh_token",
                    "access_token": current_token,
                },
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                if "access_token" in data:
                    return data
            print(f"  Attempt {attempt+1}: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"  Attempt {attempt+1}: {e}")

        if attempt < 2:
            wait = 10 * (3 ** attempt)  # 10s, 30s, 90s
            print(f"  Retrying in {wait}s...")
            time.sleep(wait)

    return None


def validate_token(token: str) -> bool:
    """新トークンが有効か検証"""
    try:
        r = requests.get(
            "https://graph.threads.net/v1.0/me",
            params={"fields": "id,username", "access_token": token},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


def encrypt_secret(public_key: str, secret_value: str) -> str:
    """GitHub Secretsの値をNaClで暗号化"""
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(secret_value.encode("utf-8"))
    return b64encode(sealed).decode("utf-8")


def update_github_secret(token: str, secret_name: str, secret_value: str, repo: str):
    """GitHub Secretを更新"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 公開鍵を取得
    r = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    key_data = r.json()

    # 暗号化して書き込み
    encrypted = encrypt_secret(key_data["key"], secret_value)
    r2 = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        timeout=15,
    )
    r2.raise_for_status()


def main():
    print("🔄 Threadsトークン自動リフレッシュ")

    current_token = os.environ.get("THREADS_ACCESS_TOKEN", "")
    gh_pat = os.environ.get("GH_PAT", "")
    repo = "kueritikx-oss/instagram-insights-sync"

    if not current_token:
        print("❌ THREADS_ACCESS_TOKEN が未設定")
        sys.exit(1)

    # Step 1: リフレッシュ
    print("  Step 1: トークンをリフレッシュ...")
    result = refresh_token(current_token)

    if not result:
        print("❌ リフレッシュ失敗（3回リトライ済み）")
        print("  → 手動でGraph API Explorerから再取得が必要")
        sys.exit(1)

    new_token = result["access_token"]
    expires_in = result.get("expires_in", 0)
    days = expires_in // 86400
    print(f"  ✅ 新トークン取得（有効期限: {days}日）")

    # Step 2: 検証
    print("  Step 2: 新トークンを検証...")
    if not validate_token(new_token):
        print("❌ 新トークンが無効。旧トークンを維持。")
        sys.exit(1)
    print("  ✅ 検証OK")

    # Step 3: GitHub Secretに書き戻し
    if gh_pat:
        print("  Step 3: GitHub Secretを更新...")
        try:
            update_github_secret(gh_pat, "THREADS_ACCESS_TOKEN", new_token, repo)
            print("  ✅ THREADS_ACCESS_TOKEN を更新")
        except Exception as e:
            print(f"  ❌ Secret更新失敗: {e}")
            sys.exit(1)
    else:
        print("  ⚠️ GH_PAT未設定。Secret更新スキップ。")
        print(f"  新トークン: {new_token[:20]}...{new_token[-10:]}")

    print(f"\n🎉 リフレッシュ完了。次の期限: {days}日後")


if __name__ == "__main__":
    main()
