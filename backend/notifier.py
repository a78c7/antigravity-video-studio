"""
可选通用开源通知引擎（默认关闭，内置开源 ntfy 通道，预留 Webhook/自定义 Provider 扩展接口）：
- 仅在显式开启 NTFY_ENABLED 且配置目标主题时启用；
- 非阻塞后台线程发送 HTTP POST（trust_env=False 绕开代理），Header 携带 RFC 2047 UTF-8 安全编码 Title、Click、Tags、Priority；
- 任何网络/服务异常仅静默记录日志，绝不泄露 Token，也绝不拖垮主 API 请求。
"""

from __future__ import annotations

import base64
import logging
import threading
from abc import ABC, abstractmethod

import httpx

from .config import DEFAULT_BRAND_NAME, load_config

logger = logging.getLogger("agvs.notifier")


def _encode_header_utf8(text: str) -> str:
    """将含中文/Emoji 的 Header 值编码为 RFC 2047 UTF-8 Base64，兼容 HTTP/1.1 与 ntfy"""
    clean = " ".join((text or "").splitlines()).strip()[:160]
    if not clean:
        return ""
    try:
        clean.encode("ascii")
        return clean
    except UnicodeEncodeError:
        b64 = base64.b64encode(clean.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


def _resolve_click_url(click_url: str, cfg: dict[str, str]) -> str:
    raw = (click_url or "").strip()
    pub_base = (cfg.get("PUBLIC_STUDIO_URL") or "").strip().rstrip("/")
    if not raw:
        return pub_base
    if raw.startswith(("http://", "https://")):
        return raw
    if pub_base:
        if raw.startswith("?"):
            return f"{pub_base}/{raw}"
        return f"{pub_base}/{raw.lstrip('/')}"
    return ""


class NotificationProvider(ABC):
    """通知通道抽象基类，可按需扩展任意 Webhook / 推送通道"""

    name: str = "base"

    @abstractmethod
    def is_enabled(self, cfg: dict[str, str]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def deliver(
        self,
        *,
        target_role: str,
        title: str,
        message: str,
        click_url: str = "",
        tags: list[str] | None = None,
        priority: int = 3,
        cfg: dict[str, str] | None = None,
    ) -> bool:
        raise NotImplementedError


class NtfyProvider(NotificationProvider):
    """可选开源 ntfy 推送通道"""

    name = "ntfy"

    def is_enabled(self, cfg: dict[str, str]) -> bool:
        return (
            cfg.get("NTFY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
            and bool((cfg.get("NTFY_INTERNAL_URL") or "").strip())
        )

    def _resolve_topics(self, target_role: str, cfg: dict[str, str]) -> list[str]:
        role = (target_role or "").strip().lower()
        t_admin = (cfg.get("NTFY_TOPIC_ADMIN") or "").strip().strip("/")
        t_partner = (cfg.get("NTFY_TOPIC_PARTNER") or "").strip().strip("/")
        if role == "admin":
            return [t_admin] if t_admin else []
        if role == "partner":
            return [t_partner] if t_partner else []
        if role == "all":
            topics: list[str] = []
            for t in (t_admin, t_partner):
                if t and t not in topics:
                    topics.append(t)
            return topics
        if role and role in {t_admin, t_partner}:
            return [role]
        return []

    def deliver(
        self,
        *,
        target_role: str,
        title: str,
        message: str,
        click_url: str = "",
        tags: list[str] | None = None,
        priority: int = 3,
        cfg: dict[str, str] | None = None,
    ) -> bool:
        c = cfg or load_config()
        if not self.is_enabled(c):
            return False
        topics = self._resolve_topics(target_role, c)
        if not topics:
            return False

        base_url = c["NTFY_INTERNAL_URL"].strip().rstrip("/")
        token = (c.get("NTFY_TOKEN") or "").strip()
        resolved_click = _resolve_click_url(click_url, c)
        prio = str(max(1, min(5, int(priority or 3))))

        headers: dict[str, str] = {
            "Content-Type": "text/plain; charset=utf-8",
            "Priority": prio,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        encoded_title = _encode_header_utf8(title)
        if encoded_title:
            headers["Title"] = encoded_title
        if resolved_click:
            headers["Click"] = resolved_click
        clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if clean_tags:
            headers["Tags"] = ",".join(clean_tags)

        brand = (c.get("BRAND_NAME") or DEFAULT_BRAND_NAME).strip() or DEFAULT_BRAND_NAME
        body_bytes = (message or title or f"{brand} 有新动态").strip().encode("utf-8")
        ok_any = False
        try:
            with httpx.Client(trust_env=False, timeout=4.0) as client:
                for topic in topics:
                    url = f"{base_url}/{topic}"
                    try:
                        resp = client.post(url, content=body_bytes, headers=headers)
                        if 200 <= resp.status_code < 300:
                            ok_any = True
                        else:
                            logger.warning("ntfy 推送返回非 2xx（topic=%s, status=%s）", topic, resp.status_code)
                    except Exception as exc:
                        logger.warning("ntfy 推送失败（topic=%s, err=%s）", topic, type(exc).__name__)
        except Exception as exc:
            logger.warning("ntfy 客户端初始化异常：%s", type(exc).__name__)
        return ok_any


_PROVIDERS: list[NotificationProvider] = [NtfyProvider()]


def register_provider(provider: NotificationProvider) -> None:
    """注册自定义通知通道扩展"""
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)


def send_notification(
    target_role: str,
    title: str,
    message: str,
    click_url: str = "",
    tags: list[str] | None = None,
    priority: int = 3,
    *,
    sync: bool = False,
) -> bool:
    """
    发送可选协作通知：
    - 默认异步后台线程投递（零阻塞、异常静默记录日志）；
    - 未启用或未配置目标主题时立刻返回 False；
    - sync=True 时同步返回实际投递布尔结果。
    """
    cfg = load_config()
    active_providers = [p for p in _PROVIDERS if p.is_enabled(cfg)]
    if not active_providers:
        return False

    if isinstance(active_providers[0], NtfyProvider) and len(active_providers) == 1:
        if not active_providers[0]._resolve_topics(target_role, cfg):
            return False

    def _worker() -> bool:
        delivered = False
        for p in active_providers:
            try:
                if p.deliver(
                    target_role=target_role,
                    title=title,
                    message=message,
                    click_url=click_url,
                    tags=tags,
                    priority=priority,
                    cfg=cfg,
                ):
                    delivered = True
            except Exception as exc:
                logger.warning("通知通道 %s 异常：%s", p.name, type(exc).__name__)
        return delivered

    if sync:
        return _worker()

    t = threading.Thread(target=_worker, name="agvs-notifier", daemon=True)
    t.start()
    return True


def notify_other_party(
    actor_role: str,
    title: str,
    message: str,
    click_url: str = "",
    tags: list[str] | None = None,
    priority: int = 3,
) -> bool:
    """按操作者角色自动通知另一侧协作成员（partner 改动通知 admin，admin 改动通知 partner）"""
    target = "admin" if (actor_role or "").strip().lower() == "partner" else "partner"
    return send_notification(
        target_role=target,
        title=title,
        message=message,
        click_url=click_url,
        tags=tags,
        priority=priority,
    )
