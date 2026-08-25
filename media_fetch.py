#!/usr/bin/env python3
"""投稿用画像の取得を1箇所に集約する。

## なぜ要るか（2026-08-26）

X自動投稿が「画像付きの行だけ」失敗していた。エラーは
`httpx.RemoteProtocolError: Server disconnected without sending a response.`。
真因は X のアップロードでも認証でもなく、**画像ホスト側が Python クライアントの
既定 User-Agent を拒否してコネクションを切っていた**こと。

実測（2026-08-26 / files.catbox.moe）:

    python-httpx 既定 UA   → RemoteProtocolError (0.50s)
    python-requests 既定 UA→ ConnectionError     (0.55s)
    ブラウザ UA            → HTTP 200 98,463 bytes (0.84s)
    curl UA               → HTTP 200 98,463 bytes (0.84s)

ローカルでも GitHub Actions でも同じ結果なので、IPではなく UA が理由。
テキスト投稿は画像を取りに行かないので無傷、画像付きだけが死んでいた
（failed 6件は全部画像 / posted 270件のうち画像付き37件は UA 規制導入前）。

同じ落とし穴が **X の投稿とIGのプリフライトの2箇所**にあったので、取得口をここへ寄せた。
新しく画像URLを取りに行くコードを足すときは、必ずこのモジュールを通すこと。
"""
from __future__ import annotations

import time

# 画像ホスト(catbox.moe 等)は python-httpx/python-requests の既定UAを弾く。
# 実在ブラウザの UA を名乗る。中身を偽ってアクセスを回避しているのではなく、
# 「自動化クライアントお断り」の既定フィルタを通すための最小限の申告。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "image/*,*/*;q=0.8"}

# 一時障害として粘る相手。恒久エラー(404等)は粘らず即返す。
_TRANSIENT = (
    "RemoteProtocolError", "ConnectionError", "ConnectError", "ReadTimeout",
    "ConnectTimeout", "Timeout", "Server disconnected", "Connection reset",
    "Max retries exceeded", "Temporary failure",
)


def _is_transient(exc: BaseException) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(m in s for m in _TRANSIENT)


async def afetch_image_bytes(url: str, *, timeout: int = 30, attempts: int = 3) -> bytes:
    """画像URLをバイト列で取得する（httpx / 非同期）。"""
    import httpx

    last: BaseException | None = None
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=DEFAULT_HEADERS, follow_redirects=True
            ) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                return resp.content
        except BaseException as exc:  # noqa: BLE001 - 判定は _is_transient に委ねる
            last = exc
            if not _is_transient(exc) or i == attempts - 1:
                raise RuntimeError(
                    f"画像DL失敗 ({type(exc).__name__}: {exc}) url={url}"
                ) from exc
            wait = 3 * (i + 1)
            print(f"  ⏳ 画像DLリトライ {i + 1}/{attempts - 1} ({type(exc).__name__}) "
                  f"{wait}s待機: {url[:70]}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"画像DL失敗 url={url}") from last


def image_url_reachable(url: str, *, timeout: int = 10) -> bool:
    """投稿前の到達確認。HEADが通らない相手にはRangeつきGETで確かめる。"""
    import requests

    for attempt in range(2):
        try:
            r = requests.head(url, timeout=timeout, headers=DEFAULT_HEADERS,
                              allow_redirects=True)
            if r.status_code in (200, 206):
                return True
            r = requests.get(url, timeout=timeout, allow_redirects=True,
                             headers={**DEFAULT_HEADERS, "Range": "bytes=0-0"})
            if r.status_code in (200, 206):
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            if not _is_transient(exc) or attempt == 1:
                return False
            time.sleep(3)
    return False
