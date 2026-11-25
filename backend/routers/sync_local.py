"""
本地出片模式（local_producer）专用：可选自托管远程协作服务端推拉同步路由。
由 backend/main.py 在 local_producer 模式下条件挂载；cloud_collab 模式下自动禁用（返回 404）。
未配置 CLOUD_STUDIO_URL 时均优雅返回清晰未启用状态而不抛出错误。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..cloud_sync import (
    check_cloud_status,
    pull_changes_from_cloud,
    push_episode_to_cloud,
)
from ..config import load_config

router = APIRouter(prefix="/api/local-sync", tags=["local-sync"])


class PushEpisodeRequest(BaseModel):
    notify_stage: str = "update"  # frames_ready | video_360p_ready | master_ready | covers_ready | update | silent
    include_final_master: bool = True
    notify_note: str = ""
    force_upload: bool = False


class PullChangesRequest(BaseModel):
    since: str = ""


def _remote_sync_unconfigured_state() -> dict[str, Any] | None:
    cfg = load_config()
    url = (cfg.get("CLOUD_STUDIO_URL") or "").strip()
    if not url:
        return {
            "ok": False,
            "enabled": False,
            "configured": False,
            "online": False,
            "cloud_url": "",
            "latency_ms": 0,
            "message": "未配置远程协作服务端地址（当前为纯本地单机模式；如需多人协作，请在系统设置中开启自托管协作并填写服务端 URL）。",
        }
    return None


@router.get("/status")
def local_sync_status() -> dict[str, Any]:
    """检测本机到自托管远程协作服务端的连通状态、延迟与同步游标"""
    unconfigured = _remote_sync_unconfigured_state()
    if unconfigured is not None:
        return unconfigured
    res = check_cloud_status()
    if isinstance(res, dict):
        res.setdefault("enabled", True)
    return res


@router.post("/push-episode/{episode_id}")
def local_sync_push_episode(
    episode_id: int,
    body: PushEpisodeRequest | None = None,
) -> dict[str, Any]:
    """
    一键将本集脱敏制作包推送至远程协作服务端，并按节点触发可选通知给团队成员：
    自动压制轻量 web_preview.mp4 供在线流畅审片，同时支持附带高清终版供下载。
    """
    unconfigured = _remote_sync_unconfigured_state()
    if unconfigured is not None:
        return unconfigured
    req = body or PushEpisodeRequest()
    try:
        return push_episode_to_cloud(
            episode_id=episode_id,
            notify_stage=req.notify_stage,
            include_final_master=req.include_final_master,
            notify_note=req.notify_note,
            force_upload=req.force_upload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"推送至远程协作服务端失败：{exc}")


@router.post("/pull")
def local_sync_pull_changes(body: PullChangesRequest | None = None) -> dict[str, Any]:
    """
    从远程协作服务端拉回最新变更（团队成员的剧本/分镜修改、审片投票、规则建议、新选题与评论回复留痕），
    按自然键安全合并入本地数据库并返回中文摘要。
    """
    unconfigured = _remote_sync_unconfigured_state()
    if unconfigured is not None:
        return unconfigured
    req = body or PullChangesRequest()
    try:
        return pull_changes_from_cloud(since=req.since)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"拉回远程协作服务端变更失败：{exc}")
