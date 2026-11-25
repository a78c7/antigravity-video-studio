"""
国内直连免登录实时热榜抓取模块（零登录凭据、零自动化风控风险）：
- 所有请求均通过 `make_cn_async_client(trust_env=False)` 强制直连，不经海外代理；
- 内置 10 分钟 SQLite 缓存 + `DATA_DIR/.hot_cache_<source>.json` 磁盘历史缓存；
- 第三方热榜源返回 403/500/超时时自动优雅降级至最近一次成功缓存并返回 `degraded=True` 与中文 `warning` 提示，不抛 502 炸页；
- 可在系统设置中通过 `ENABLE_HOT_FEEDS` 一键关闭。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import config as app_config
from .db import db_ctx, now_str
from .net import make_cn_async_client

CACHE_TTL_SECONDS = 600

SUPPORTED_SOURCES: dict[str, str] = {
    "douyin": "抖音热搜",
    "toutiao": "今日头条",
    "baidu": "百度实时热搜",
    "bilibili": "B站热搜",
    "weibo": "微博热搜",
}


async def _fetch_douyin() -> list[dict[str, Any]]:
    async with make_cn_async_client(timeout=8.0) as client:
        # 优先富字段公开源（含 video_count，便于判断热点是否刚起势）
        try:
            r = await client.get("https://aweme.snssdk.com/aweme/v1/hot/search/list/")
            if r.status_code == 200 and r.content:
                data = r.json()
                words = (data.get("data") or {}).get("word_list") or []
                if words:
                    items = []
                    for idx, w in enumerate(words[:50], start=1):
                        title = str(w.get("word") or "").strip()
                        if not title:
                            continue
                        items.append(
                            {
                                "rank": idx,
                                "title": title,
                                "hot_value": int(w.get("hot_value") or 0),
                                "video_count": int(w.get("video_count") or 0),
                                "label": str(w.get("label") or ""),
                                "summary": f"关联视频 {int(w.get('video_count') or 0)} 条" if w.get("video_count") else "",
                                "url": f"https://www.douyin.com/search/{title}",
                                "source": "douyin",
                            }
                        )
                    if items:
                        return items
        except Exception:
            pass

        # 备选公开轻量源
        try:
            r = await client.get("https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/")
            if r.status_code == 200 and r.content:
                data = r.json()
                words = data.get("word_list") or []
                items = []
                for idx, w in enumerate(words[:50], start=1):
                    title = str(w.get("word") or "").strip()
                    if not title:
                        continue
                    items.append(
                        {
                            "rank": idx,
                            "title": title,
                            "hot_value": int(w.get("hot_value") or 0),
                            "video_count": 0,
                            "label": str(w.get("label") or ""),
                            "summary": "",
                            "url": f"https://www.douyin.com/search/{title}",
                            "source": "douyin",
                        }
                    )
                if items:
                    return items
        except Exception:
            pass

        # 公开聚合兜底
        r = await client.get("https://v2.xxapi.cn/api/douyinhot")
        r.raise_for_status()
        data = r.json().get("data") or []
        return [
            {
                "rank": idx,
                "title": str(x.get("word") or x.get("title") or "").strip(),
                "hot_value": int(x.get("hot_value") or 0),
                "video_count": 0,
                "label": "",
                "summary": "",
                "url": str(x.get("url") or ""),
                "source": "douyin",
            }
            for idx, x in enumerate(data[:50], start=1)
            if (x.get("word") or x.get("title"))
        ]


async def _fetch_toutiao() -> list[dict[str, Any]]:
    async with make_cn_async_client(timeout=8.0) as client:
        r = await client.get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
        r.raise_for_status()
        rows = r.json().get("data") or []
        items = []
        for idx, x in enumerate(rows[:50], start=1):
            title = str(x.get("Title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "rank": idx,
                    "title": title,
                    "hot_value": int(float(x.get("HotValue") or 0)),
                    "video_count": 0,
                    "label": str(x.get("LabelDesc") or ""),
                    "summary": "",
                    "url": str(x.get("Url") or ""),
                    "source": "toutiao",
                }
            )
        return items


async def _fetch_baidu() -> list[dict[str, Any]]:
    async with make_cn_async_client(timeout=8.0) as client:
        r = await client.get("https://top.baidu.com/api/board?platform=pc&tab=realtime")
        r.raise_for_status()
        cards = (r.json().get("data") or {}).get("cards") or []
        content = cards[0].get("content") if cards else []
        items = []
        for idx, x in enumerate(content[:50], start=1):
            title = str(x.get("word") or x.get("query") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "rank": idx,
                    "title": title,
                    "hot_value": int(float(x.get("hotScore") or 0)),
                    "video_count": 0,
                    "label": str(x.get("hotTag") or ""),
                    "summary": str(x.get("desc") or "").strip(),
                    "url": str(x.get("rawUrl") or x.get("url") or ""),
                    "source": "baidu",
                }
            )
        return items


async def _fetch_bilibili() -> list[dict[str, Any]]:
    async with make_cn_async_client(timeout=8.0) as client:
        r = await client.get("https://api.bilibili.com/x/web-interface/wbi/search/square?limit=20")
        r.raise_for_status()
        rows = (((r.json().get("data") or {}).get("trending") or {}).get("list")) or []
        items = []
        for idx, x in enumerate(rows[:30], start=1):
            title = str(x.get("show_name") or x.get("keyword") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "rank": idx,
                    "title": title,
                    "hot_value": int(x.get("heat_score") or 0),
                    "video_count": 0,
                    "label": "",
                    "summary": "",
                    "url": f"https://search.bilibili.com/all?keyword={title}",
                    "source": "bilibili",
                }
            )
        return items


async def _fetch_weibo() -> list[dict[str, Any]]:
    async with make_cn_async_client(timeout=8.0) as client:
        try:
            r = await client.get("https://v2.xxapi.cn/api/weibohot")
            if r.status_code == 200:
                rows = r.json().get("data") or []
                items = []
                for idx, x in enumerate(rows[:50], start=1):
                    title = str(x.get("title") or "").strip()
                    if not title:
                        continue
                    hot_raw = str(x.get("hot") or "0")
                    hot_digits = "".join(ch for ch in hot_raw if ch.isdigit())
                    items.append(
                        {
                            "rank": idx,
                            "title": title,
                            "hot_value": int(hot_digits) if hot_digits else 0,
                            "video_count": 0,
                            "label": "",
                            "summary": "",
                            "url": str(x.get("url") or ""),
                            "source": "weibo",
                        }
                    )
                if items:
                    return items
        except Exception:
            pass

        r = await client.get("https://newsnow.busiyi.world/api/s?id=weibo")
        r.raise_for_status()
        rows = r.json().get("items") or []
        return [
            {
                "rank": idx,
                "title": str(x.get("title") or "").strip(),
                "hot_value": 0,
                "video_count": 0,
                "label": "",
                "summary": "",
                "url": str(x.get("url") or ""),
                "source": "weibo",
            }
            for idx, x in enumerate(rows[:30], start=1)
            if x.get("title")
        ]


FETCHERS = {
    "douyin": _fetch_douyin,
    "toutiao": _fetch_toutiao,
    "baidu": _fetch_baidu,
    "bilibili": _fetch_bilibili,
    "weibo": _fetch_weibo,
}


def _disk_cache_path(source: str) -> Path:
    """热榜历史磁盘缓存文件路径：DATA_DIR/.hot_cache_<source>.json"""
    return app_config.DATA_DIR / f".hot_cache_{source}.json"


def _save_disk_cache(source: str, items: list[dict[str, Any]], fetched_at: str) -> None:
    """原子写入最近一次成功拉取的热榜磁盘快照，供第三方源失效时优雅降级"""
    if not items:
        return
    try:
        cache_path = _disk_cache_path(source)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".json.tmp")
        payload = {
            "source": source,
            "fetched_at": fetched_at,
            "items": items,
        }
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(cache_path)
    except OSError:
        pass


def _load_disk_cache(source: str) -> tuple[list[dict[str, Any]], str] | None:
    """读取最近一次成功落盘的历史热榜快照"""
    cache_path = _disk_cache_path(source)
    if not cache_path.exists():
        return None
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            items = raw.get("items")
            fetched_at = str(raw.get("fetched_at") or "")
            if isinstance(items, list) and items:
                return items, fetched_at
    except Exception:
        return None
    return None


def _load_stale_db_cache(source: str) -> tuple[list[dict[str, Any]], str] | None:
    """兜底读取 SQLite hot_cache 中可能已过期的历史记录"""
    try:
        with db_ctx() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM hot_cache WHERE source = ?",
                (source,),
            ).fetchone()
            if row:
                items = json.loads(row["payload"])
                if isinstance(items, list) and items:
                    return items, str(row["fetched_at"] or "")
    except Exception:
        return None
    return None


def _summarize_fetch_error(exc: Exception) -> str:
    """将上游热榜异常提炼为简短友好的中文降级说明"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"上游返回 HTTP {code} 访问受限"
        if code == 429:
            return "上游触发频率限制（HTTP 429）"
        if code >= 500:
            return f"上游服务暂不可用（HTTP {code}）"
        return f"上游返回 HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.RequestError):
        return "网络连接失败"
    msg = str(exc).strip()
    return msg[:80] if msg else "上游接口异常"


async def get_hot_list(source: str = "douyin", force: bool = False) -> dict[str, Any]:
    """
    获取指定平台公开热榜（优先 10 分钟 SQLite 缓存，强制直连）：
    - 拉取成功时同步落盘 `DATA_DIR/.hot_cache_<source>.json`；
    - 第三方源 403/500/超时时优先回退历史缓存，返回 `degraded=True` 与中文 `warning` 提示，不抛 502。
    """
    if source not in FETCHERS:
        raise ValueError(f"不支持的热榜来源: {source}")

    label = SUPPORTED_SOURCES[source]

    if not force:
        with db_ctx() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM hot_cache WHERE source = ?",
                (source,),
            ).fetchone()
            if row:
                try:
                    fetched_dt = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - fetched_dt < timedelta(seconds=CACHE_TTL_SECONDS):
                        cached_items = json.loads(row["payload"])
                        if isinstance(cached_items, list) and cached_items:
                            return {
                                "source": source,
                                "source_label": label,
                                "cached": True,
                                "degraded": False,
                                "warning": "",
                                "fetched_at": row["fetched_at"],
                                "latency_ms": 0,
                                "items": cached_items,
                            }
                except Exception:
                    pass

    t0 = time.perf_counter()
    try:
        items = await FETCHERS[source]()
        if not items:
            raise RuntimeError("返回列表为空")
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        reason = _summarize_fetch_error(exc)
        fallback = _load_disk_cache(source) or _load_stale_db_cache(source)
        if fallback is not None:
            stale_items, stale_ts = fallback
            time_hint = f" {stale_ts}" if stale_ts else ""
            return {
                "source": source,
                "source_label": label,
                "cached": True,
                "degraded": True,
                "warning": f"{label}实时源暂不可用（{reason}），已自动回退至{time_hint}的历史缓存。",
                "fetched_at": stale_ts,
                "latency_ms": latency_ms,
                "items": stale_items,
            }
        return {
            "source": source,
            "source_label": label,
            "cached": False,
            "degraded": True,
            "warning": f"{label}实时源暂不可用（{reason}），且尚无历史缓存；请稍后重试或切换其他热榜源。",
            "fetched_at": "",
            "latency_ms": latency_ms,
            "items": [],
        }

    latency_ms = int((time.perf_counter() - t0) * 1000)
    ts = now_str()

    with db_ctx() as conn:
        conn.execute(
            """
            INSERT INTO hot_cache (source, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at
            """,
            (source, json.dumps(items, ensure_ascii=False), ts),
        )
    _save_disk_cache(source, items, ts)

    return {
        "source": source,
        "source_label": label,
        "cached": False,
        "degraded": False,
        "warning": "",
        "fetched_at": ts,
        "latency_ms": latency_ms,
        "items": items,
    }


async def get_hot_list_with_status(source: str = "douyin", force: bool = False) -> dict[str, Any]:
    """带降级状态字段的热榜获取入口（与 get_hot_list 返回结构完全一致）"""
    return await get_hot_list(source=source, force=force)
