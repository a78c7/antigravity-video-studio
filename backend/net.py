"""
严格网络隔离层：
1. 直连客户端池（国内热榜、DeepSeek API、自托管协作服务端）：
   强制 trust_env=False，绕开终端代理环境变量，避免不必要代理转发；
2. Google 精准代理池（Gemini API）：
   仅当配置了 GOOGLE_PROXY_URL 时使用指定代理，留空则直接连接，
   同样 trust_env=False 防止被外部 ALL_PROXY 环境变量意外干扰。
"""

from __future__ import annotations

import httpx

from .config import load_config

DEFAULT_CN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def make_cn_async_client(timeout: float = 12.0) -> httpx.AsyncClient:
    """直连异步客户端：不读取系统代理环境变量"""
    return httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(timeout),
        headers=DEFAULT_CN_HEADERS,
        follow_redirects=True,
    )


def make_cn_sync_client(timeout: float = 45.0) -> httpx.Client:
    """直连同步客户端（供 DeepSeek OpenAI SDK 或直连接口使用）"""
    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(timeout),
    )


def _resolve_google_proxy(use_socks: bool = False) -> str | None:
    proxy = load_config().get("GOOGLE_PROXY_URL", "").strip()
    if not proxy:
        return None
    if use_socks and proxy.startswith("http://"):
        return "socks5://" + proxy[len("http://") :]
    return proxy


def make_google_sync_client(timeout: float = 90.0, use_socks: bool = False) -> httpx.Client:
    """
    仅给 Google Gemini API 使用的同步客户端：
    按配置按需挂接代理（留空直连），禁用 keepalive 连接复用防止代理隧道陈旧 TLS 断连。
    """
    return httpx.Client(
        proxy=_resolve_google_proxy(use_socks=use_socks),
        trust_env=False,
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
    )


def make_google_async_client(timeout: float = 90.0, use_socks: bool = False) -> httpx.AsyncClient:
    """仅给 Google Gemini API 使用的异步客户端（留空直连，配置代理时按需走代理）"""
    return httpx.AsyncClient(
        proxy=_resolve_google_proxy(use_socks=use_socks),
        trust_env=False,
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
    )
