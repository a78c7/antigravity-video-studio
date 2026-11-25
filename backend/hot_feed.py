"""
国内直连免登录实时热榜抓取模块（零登录凭据、零自动化风控风险）：
- 所有请求均通过 `make_cn_async_client(trust_env=False)` 强制直连，不经海外代理；
- 内置 10 分钟 SQLite 缓存，支持 `force=True` 实时刷新；
- 可在系统设置中通过 `ENABLE_HOT_FEEDS` 一键关闭。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

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


async def get_hot_list(source: str = "douyin", force: bool = False) -> dict[str, Any]:
    """获取指定平台公开热榜（优先走 10 分钟 SQLite 缓存，强制直连）"""
    if source not in FETCHERS:
        raise ValueError(f"不支持的热榜来源: {source}")

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
                        return {
                            "source": source,
                            "source_label": SUPPORTED_SOURCES[source],
                            "cached": True,
                            "fetched_at": row["fetched_at"],
                            "latency_ms": 0,
                            "items": json.loads(row["payload"]),
                        }
                except Exception:
                    pass

    t0 = time.perf_counter()
    items = await FETCHERS[source]()
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

    return {
        "source": source,
        "source_label": SUPPORTED_SOURCES[source],
        "cached": False,
        "fetched_at": ts,
        "latency_ms": latency_ms,
        "items": items,
    }
