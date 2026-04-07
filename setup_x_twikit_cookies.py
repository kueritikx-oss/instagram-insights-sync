#!/usr/bin/env python3
"""
X twikit Cookie取得スクリプト（ローカル専用）

Xのユーザー名+パスワードでログインし、セッションCookieを保存。
このCookieをGitHub SecretsのTWITTER_COOKIESに設定する。

⚠️ CIでは絶対に実行しない（Captcha/2FA/アカウントロックのリスク）

Usage:
    python3 setup_x_twikit_cookies.py
"""

# twikit v2.3.3 モンキーパッチ
import re
_tx_mod = __import__('twikit.x_client_transaction.transaction', fromlist=['ClientTransaction'])
_tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
    r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE))
_tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
_tx_mod.INDICES_REGEX = re.compile(r"\[(\d+)\],\s*16")

async def _patched_get_indices(self, home_page_response, session, headers):
    key_byte_indices = []
    response = self.validate_response(home_page_response) or self.home_page_response
    response_str = str(response)
    on_demand_file = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_str)
    if on_demand_file:
        idx = on_demand_file.group(1)
        hash_regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(idx))
        hash_match = hash_regex.search(response_str)
        if hash_match:
            url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_match.group(1)}a.js"
            resp = await session.request(method="GET", url=url, headers=headers)
            for item in _tx_mod.INDICES_REGEX.finditer(str(resp.text)):
                key_byte_indices.append(item.group(1))
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]

_tx_mod.ClientTransaction.get_indices = _patched_get_indices

import asyncio
import json
import os
import sys
from pathlib import Path
from twikit import Client

COOKIES_FILE = Path.home() / "Projects/事業/タッキー/02_SNS集客/instagram-auto-post/x_twikit_cookies.json"


async def main():
    print("=" * 60)
    print(" X twikit Cookie取得")
    print("=" * 60)
    print()

    # 認証情報を入力
    username = input("Xのユーザー名 (@なし): ").strip()
    email = input("Xに登録したメールアドレス: ").strip()
    password = input("Xのパスワード: ").strip()
    totp = input("2FAシークレット (なければ空Enter): ").strip() or None

    print()
    print("🔐 ログイン中...")

    client = Client('ja')

    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
            totp_secret=totp,
        )
    except Exception as e:
        print(f"❌ ログイン失敗: {e}")
        print()
        print("考えられる原因:")
        print("  - パスワードが間違っている")
        print("  - 2FAが有効なのにシークレットを入力しなかった")
        print("  - Captchaが要求された（別のIPで試す）")
        print("  - アカウントがロックされた")
        sys.exit(1)

    # Cookie保存
    client.save_cookies(str(COOKIES_FILE))
    cookies = client.get_cookies()
    cookies_json = json.dumps(cookies)

    print(f"✅ ログイン成功！")
    print(f"   Cookie保存先: {COOKIES_FILE}")
    print()
    print("=" * 60)
    print(" 次のステップ: GitHub SecretsにCookieを設定")
    print("=" * 60)
    print()
    print("以下のコマンドを実行:")
    print()
    print(f"echo '{cookies_json}' | gh secret set TWITTER_COOKIES -R kueritikx-oss/instagram-insights-sync")
    print()
    print("または、以下のJSONをGitHub Secrets > TWITTER_COOKIES に直接貼り付け:")
    print()
    print(cookies_json[:200] + "..." if len(cookies_json) > 200 else cookies_json)
    print()
    print(f"Cookie keys: {list(cookies.keys())}")
    print(f"auth_token: {cookies.get('auth_token', '?')[:10]}...")


asyncio.run(main())
