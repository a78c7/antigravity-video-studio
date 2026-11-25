"""
反重力视频工坊（Antigravity Video Studio）— 单进程 FastAPI 主入口
支持双运行模式：
- local_producer（默认，本机 127.0.0.1:${AGVS_PORT:-8765}）：完整本地 AI 短剧制作链路、下载目录收件箱监听与可选远程协作同步；
- cloud_collab（可选自托管多人协作看板，兼容根路径 / 与反向代理子路径）：
  强制账号登录鉴权（Session Cookie / X-Sync-Token）、屏蔽本地生成与海外算力专属接口、
  仅保留协作审片、剧本分镜共创、复盘与可选通用通知。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as app_config
from .auth import (
    LOCAL_DEFAULT_ACTOR,
    SESSION_COOKIE_NAME,
    reset_request_actor,
    set_request_actor,
    verify_session_token,
    verify_sync_token,
)
from .config import (
    APP_PACKAGE_NAME,
    DATA_DIR,
    DEFAULT_BRAND_NAME,
    STATIC_DIR,
    get_app_mode,
    is_cloud_collab,
    load_config,
)
from .db import init_db
from .jobs import recover_stale_jobs_on_startup
from .routers import analytics, collab, delivery, studio, topics
from .worker_colab import resolve_colab_cli

APP_VERSION = "1.0.1"
_HEALTH_CACHE_TTL_SEC = 30.0
_health_env_cache: dict[str, Any] = {"ts": 0.0, "data_dir": "", "values": None}

try:
    from .watcher import downloads_inbox
except ImportError:  # 极简环境未装 watchdog 时容错降级
    downloads_inbox = None  # type: ignore[assignment]


# 自托管协作模式下彻底禁用（直接返回 HTTP 404）的本地生成、算力与 OpenAPI 文档端点
_CLOUD_BLOCKED_PREFIXES: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/quota",
    "/api/inbox",
    "/api/jobs",
    "/api/settings/probe-colab",
    "/api/settings/colab",
    "/api/settings/gemini-models",
    "/api/system",
    "/api/local-sync",
    "/api/flow-meta",
    "/api/prompt-templates",
)

_CLOUD_BLOCKED_SEGMENTS: tuple[str, ...] = (
    "/cli-generate-",
    "/dispatch-colab",
    "/render-local",
    "/export-colab-pack",
    "/sync-dock-tray",
    "/prepare-manual-flow-pack",
    "/cleanup-drafts",
    "/export-fcpxml",
    "/extract-last-frame",
    "/qc-frames-sheet",
    "/qc-inspect-videos",
)

# 自托管协作模式下免登录可访问的公开 API 白名单
_CLOUD_AUTH_EXEMPT_PATHS: set[str] = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/me",
    "/api/auth/logout",
}


def _is_cloud_blocked_path(path: str) -> bool:
    for prefix in _CLOUD_BLOCKED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    if path.startswith("/api/"):
        for seg in _CLOUD_BLOCKED_SEGMENTS:
            if seg in path:
                return True
    return False


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    recover_stale_jobs_on_startup()
    absorb_fn = getattr(topics, "absorb_offline_campaigns_inbox", None)
    if callable(absorb_fn):
        absorb_fn()
    watcher_started = False
    if not is_cloud_collab() and downloads_inbox is not None:
        downloads_inbox.start()
        watcher_started = True
    try:
        yield
    finally:
        if watcher_started and downloads_inbox is not None:
            downloads_inbox.stop()


_cloud_mode_at_boot = is_cloud_collab()
_agvs_port = (os.environ.get("AGVS_PORT") or "8765").strip() or "8765"

app = FastAPI(
    title=f"{DEFAULT_BRAND_NAME} · AI 短剧全流程制片台",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None if _cloud_mode_at_boot else "/docs",
    redoc_url=None if _cloud_mode_at_boot else "/redoc",
    openapi_url=None if _cloud_mode_at_boot else "/openapi.json",
)

# 开发态允许本地 Vite 端口跨域，生产单进程同源零 CORS
_cors_origins = list(
    dict.fromkeys(
        [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            f"http://127.0.0.1:{_agvs_port}",
            f"http://localhost:{_agvs_port}",
        ]
    )
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def mode_and_auth_middleware(request: Request, call_next):
    """统一双模式路由拦截与 Session Cookie / X-Sync-Token 鉴权中间件"""
    path = request.url.path
    cloud_mode = is_cloud_collab()
    request.state.user = None
    request.state.is_sync = False

    # 1. 自托管协作模式下，拦截所有本地生成与第三方算力专属路径（返回 404）
    if cloud_mode and _is_cloud_blocked_path(path):
        return JSONResponse(
            status_code=404,
            content={"detail": "协作模式已禁用本地生成与算力调度接口"},
        )

    # 2. 解析当前会话 Cookie（若存在）
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    session_user = verify_session_token(cookie_token) if cookie_token else None

    actor_for_ctx: dict | None = None
    if cloud_mode:
        if request.method == "OPTIONS":
            return await call_next(request)

        if path.startswith("/api/"):
            if path in _CLOUD_AUTH_EXEMPT_PATHS:
                request.state.user = session_user
                actor_for_ctx = session_user
            elif path == "/api/sync" or path.startswith("/api/sync/"):
                sync_tok = request.headers.get("X-Sync-Token") or request.headers.get("x-sync-token")
                if verify_sync_token(sync_tok):
                    sync_actor = {
                        "id": 0,
                        "username": "sync_client",
                        "display_name": "本地制片台同步",
                        "role": "admin",
                    }
                    request.state.is_sync = True
                    request.state.user = sync_actor
                    actor_for_ctx = sync_actor
                elif session_user and session_user.get("role") == "admin":
                    request.state.user = session_user
                    actor_for_ctx = session_user
                else:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "同步鉴权失败：请提供有效的 X-Sync-Token"},
                    )
            else:
                if not session_user:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "请先登录协作账号"},
                    )
                request.state.user = session_user
                actor_for_ctx = session_user
        else:
            request.state.user = session_user
            actor_for_ctx = session_user
    else:
        # 本地制片模式免登录可用，默认识别为本地导演 (admin)
        actor_for_ctx = session_user or dict(LOCAL_DEFAULT_ACTOR)
        request.state.user = actor_for_ctx

    ctx_token = set_request_actor(actor_for_ctx)
    try:
        return await call_next(request)
    finally:
        reset_request_actor(ctx_token)


app.include_router(topics.router)
app.include_router(studio.router)
app.include_router(delivery.router)
app.include_router(analytics.router)
app.include_router(collab.router)

# 仅在非云端协作模式下按需挂载 sync_local 路由
if not is_cloud_collab():
    try:
        from .routers import sync_local

        app.include_router(sync_local.router)
    except ImportError:
        pass


def _probe_google_genai_sdk_ok() -> bool:
    fn = getattr(app_config, "is_google_genai_sdk_available", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    try:
        return importlib.util.find_spec("google.genai") is not None
    except Exception:
        return False


def _probe_gemini_cli_ok() -> bool:
    fn = getattr(app_config, "is_gemini_cli_available", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return bool(shutil.which("gemini-api") or shutil.which("gemini-api.exe"))


def _get_cached_env_health(*, include_google_tools: bool) -> dict[str, Any]:
    """轻量缓存本机环境红绿灯体检（30s TTL），避免高频轮询反复探测子进程"""
    now = time.monotonic()
    data_dir_str = str(app_config.DATA_DIR)
    cached_vals = _health_env_cache.get("values")
    if (
        isinstance(cached_vals, dict)
        and _health_env_cache.get("data_dir") == data_dir_str
        and (now - float(_health_env_cache.get("ts") or 0.0)) < _HEALTH_CACHE_TTL_SEC
        and (not include_google_tools or "google_genai_sdk_ok" in cached_vals)
    ):
        base = dict(cached_vals)
    else:
        ffmpeg_ok = bool(shutil.which("ffmpeg"))
        ffprobe_ok = bool(shutil.which("ffprobe"))
        try:
            usage = shutil.disk_usage(data_dir_str if Path(data_dir_str).exists() else str(Path(data_dir_str).parent))
            disk_free_gb = round(usage.free / (1024 ** 3), 2)
        except Exception:
            disk_free_gb = 0.0

        has_non_ascii = any(ord(ch) > 127 for ch in data_dir_str)
        has_space = " " in data_dir_str
        if has_space or has_non_ascii:
            path_warning = (
                "当前数据目录路径包含空格或中文/非 ASCII 字符，部分视频滤镜在 Windows 下可能受影响；"
                "建议设置环境变量 STUDIO_DATA_DIR 为纯英文无空格路径。"
            )
        else:
            path_warning = ""

        base = {
            "ffmpeg_ok": ffmpeg_ok,
            "ffprobe_ok": ffprobe_ok,
            "disk_free_gb": disk_free_gb,
            "path_warning": path_warning,
        }
        if include_google_tools:
            base["google_genai_sdk_ok"] = _probe_google_genai_sdk_ok()
            base["gemini_cli_ok"] = _probe_gemini_cli_ok()
            try:
                base["colab_cli_ok"] = bool(resolve_colab_cli())
            except Exception:
                base["colab_cli_ok"] = False

        _health_env_cache["ts"] = now
        _health_env_cache["data_dir"] = data_dir_str
        _health_env_cache["values"] = dict(base)

    if not include_google_tools:
        return {
            "ffmpeg_ok": base["ffmpeg_ok"],
            "ffprobe_ok": base["ffprobe_ok"],
            "disk_free_gb": base["disk_free_gb"],
            "path_warning": base["path_warning"],
        }
    return base


@app.get("/api/health")
def health() -> dict[str, Any]:
    cfg = load_config()
    brand_name = (cfg.get("BRAND_NAME") or DEFAULT_BRAND_NAME).strip() or DEFAULT_BRAND_NAME
    app_mode = get_app_mode(cfg)
    cloud_mode = app_mode == "cloud_collab"
    env_health = _get_cached_env_health(include_google_tools=not cloud_mode)
    payload: dict[str, Any] = {
        "status": "ok",
        "app": APP_PACKAGE_NAME,
        "version": APP_VERSION,
        "brand_name": brand_name,
        "app_mode": app_mode,
        "ffmpeg_ok": env_health["ffmpeg_ok"],
        "ffprobe_ok": env_health["ffprobe_ok"],
        "disk_free_gb": env_health["disk_free_gb"],
        "path_warning": env_health["path_warning"],
    }
    if not cloud_mode:
        payload["google_genai_sdk_ok"] = env_health["google_genai_sdk_ok"]
        payload["gemini_cli_ok"] = env_health["gemini_cli_ok"]
        payload["colab_cli_ok"] = env_health["colab_cli_ok"]
    return payload


@app.get("/api/media")
def serve_local_media(
    path: str = Query(..., description="受控媒体路径（支持相对 DATA_DIR 路径或受控目录内绝对路径）"),
    download: bool = Query(False, description="是否作为附件触发下载"),
) -> FileResponse:
    """安全流式返回图片/视频/音频/证明文档（支持视频 Range 拖动与严格防路径穿越）"""
    raw = (path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="媒体路径不能为空")

    data_root = DATA_DIR.resolve()
    cloud_mode = is_cloud_collab()

    if cloud_mode:
        allowed_roots = [data_root]
    else:
        allowed_roots = [
            data_root,
            Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser().resolve(),
        ]

    normalized_raw = raw.replace("\\", "/")
    if normalized_raw.startswith("~") or Path(normalized_raw).is_absolute():
        p = Path(normalized_raw).expanduser().resolve()
    else:
        p = (data_root / normalized_raw.lstrip("/")).resolve()

    if not any(p.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="禁止访问受控工作目录以外的文件")

    if p.name.startswith(".") or p.suffix.lower() in (".db", ".sqlite", ".key", ".env", ".pem") or p.name.startswith("studio.db"):
        raise HTTPException(status_code=403, detail="禁止访问受保护系统文件")

    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")

    if download:
        return FileResponse(str(p), filename=p.name)
    return FileResponse(str(p))


# 若前端已 build 到 backend/static，由单进程 FastAPI 直接同端口托管（兼容根路径 / 与反向代理子路径）
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API 不存在")
        static_root = STATIC_DIR.resolve()
        candidate = (static_root / full_path).resolve()
        if full_path and candidate.is_relative_to(static_root) and candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(STATIC_DIR / "index.html"))
