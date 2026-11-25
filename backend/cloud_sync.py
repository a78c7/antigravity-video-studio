"""
反重力视频工坊｜可选本地制片台 ↔ 自托管远程协作服务端（直连双向同步引擎）

安全与网络原则：
1. 强制直连：所有访问用户自托管 `CLOUD_STUDIO_URL` 的 httpx 客户端一律显式设置 `trust_env=False`；
2. 零明文凭据泄露：`SYNC_API_TOKEN` 通过跨平台安全存储（macOS Keychain / `.secrets.env` / 环境变量 / `SYNC_TOKEN_FILE`）读取，
   经 `X-Sync-Token` Header 发送，绝不打印或返回明文；
3. 白名单脱敏同步：仅上传 `DATA_DIR` 受控目录内的本集制作资产（图片、视频、封面、质检图、AI 原创证明等），
   绝不发送 `.env`、`.secrets.env`、`studio.db`、`quota_state`、`api_usage_logs`、代理配置或任何凭据；
4. 精准增量上传：上传前调用 `GET /api/sync/check-file?rel_path=...` 比对远端存在与大小，同名同大小直接跳过；
5. 网页流畅预览优化：终版/高码率 MP4 超过 15 MB 或为 10-bit HLG HDR 时，自动按本机可用编码器压制轻量 `web_preview.mp4` 供网页流畅播放；
6. 自然键安全双向合并：`pull` 拉回协作成员的剧本/分镜修改、审片投票、规则建议、新选题与评论回复留痕，
   优先按 `(project_slug, ep_number, shot_no)` 自然键对齐并按时间戳安全合并，绝不覆盖本地出片媒体路径。

命令行用法：
    python -m backend.cloud_sync status
    python -m backend.cloud_sync push --episode 1 --stage master_ready
    python -m backend.cloud_sync pull [--since "2026-01-01 00:00:00"]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .config import DATA_DIR, ENV_PATH, EXPORTS_DIR, MEDIA_DIR, get_secret, load_config
from .db import db_ctx, now_str, rows_to_dicts
from .media_ops import select_video_encoder

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None  # type: ignore[assignment]

# 默认为空：纯本地单机模式不连接任何远程服务端，仅在用户主动配置自托管地址后启用
DEFAULT_CLOUD_STUDIO_URL = ""

# 本地同步游标持久化（仅存非敏感时间戳，不含任何密钥）
SYNC_STATE_PATH = DATA_DIR / ".cloud_sync_state.json"

# 触发网页流畅预览压制的体积阈值（15 MB）
WEB_PREVIEW_SIZE_THRESHOLD_BYTES = 15 * 1024 * 1024

# 绝不允许同步上传的文件名与关键词黑名单（多重保险防泄密）
FORBIDDEN_FILENAMES = {
    ".env",
    ".secrets.env",
    "studio.db",
    "studio.db-wal",
    "studio.db-shm",
    ".cloud_sync_state.json",
    ".session_secret",
    "cloud_settings.json",
    "mcp_config.json",
}
FORBIDDEN_PATH_TOKENS = (
    "secret",
    "token",
    "keychain",
    "oauth",
    "credential",
    "api_key",
    ".env",
)

# 允许从远程协作服务端安全合并到本地的协作文本字段白名单（绝不覆盖本地出片媒体路径）
EPISODE_PULL_FIELDS = (
    "title",
    "hook_0_3s",
    "ending_cta",
    "next_ep_hook",
    "script_text",
    "continuity_summary",
    "bgm_prompt",
    "status",
    "selected_cover_path",
)
SHOT_PULL_FIELDS = (
    "shot_title",
    "camera_type",
    "duration_sec",
    "cn_action_desc",
    "cn_dialogue",
    "subtitle_text",
    "frame_prompt_en",
    "end_frame_prompt_en",
    "video_prompt_en",
    "audio_strategy",
)

# 面向远程协作看板展示文本的脱敏规则（剥离本地生成后端细节、费用与本机绝对路径）
_CLOUD_TEXT_SANITIZE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Google\s+Colab\s+Pro\+(?:\s*官方\s*GPU\s*通道)?", re.IGNORECASE), "云端超分渲染引擎"),
    (re.compile(r"Colab\s+Pro\+", re.IGNORECASE), "云端超分渲染引擎"),
    (re.compile(r"\bColab\b", re.IGNORECASE), "云端超分引擎"),
    (re.compile(r"Google\s+Flow", re.IGNORECASE), "智能视频生成引擎"),
    (re.compile(r"Nano\s+Banana(?:\s*2\.1(?:\s*Pro)?)?", re.IGNORECASE), "高清静帧引擎"),
    (re.compile(r"(?:Gemini\s+)?Omni\s*(?:Flash)?\s*1\.2(?:\s*Flash)?", re.IGNORECASE), "角色动态引擎"),
    (re.compile(r"gemini-omni-[a-z0-9.\-]+", re.IGNORECASE), "动态视频引擎"),
    (re.compile(r"gemini-[a-z0-9.\-]+", re.IGNORECASE), "AI 创作引擎"),
    (re.compile(r"Veo\s*3\.1(?:\s*Lite)?", re.IGNORECASE), "电影视频引擎"),
    (re.compile(r"\bVeo\b", re.IGNORECASE), "电影视频引擎"),
    (re.compile(r"Flow\s+Music(?:\s*\(\s*Lyria(?:\s*3\.5)?\s*\))?", re.IGNORECASE), "原创配乐引擎"),
    (re.compile(r"Lyria(?:\s*3\.5)?", re.IGNORECASE), "原创配乐引擎"),
    (re.compile(r"Gemini\s+API", re.IGNORECASE), "AI 创作助手"),
    (re.compile(r"\bGemini\b", re.IGNORECASE), "AI 创作助手"),
    (re.compile(r"[（(]?\s*(?:约\s*)?\$\s*\d+(?:\.\d+)?(?:/[a-zA-Z\u4e00-\u9fa5]+)?\s*[）)]?"), ""),
    (re.compile(r"[·•]\s*0\s*积分"), ""),
    (re.compile(r"[（(]\s*0\s*分\s*[）)]"), ""),
    (re.compile(r"\d+\s*积分(?:/[条镜])?"), "算力额度"),
    (re.compile(r"省积分|省分"), "省算力"),
    (re.compile(r"Flow", re.IGNORECASE), "生成引擎"),
    (re.compile(r"\bOmni\b", re.IGNORECASE), "角色动态"),
    (re.compile(r"Google", re.IGNORECASE), "生成平台"),
    (re.compile(r"\bCLI\b", re.IGNORECASE), "自动合成"),
]
_LOCAL_ABS_PATH_RE = re.compile(r"(?:/Users/[^\s\"'<>|]+|/home/[^\s\"'<>|]+|[A-Za-z]:\\[^\s\"'<>|]+)")


def _strip_local_abs_paths(text: str) -> str:
    """将文本中的本机绝对路径转为 DATA_DIR 相对路径或纯文件名"""
    if not text:
        return ""
    data_root_str = str(DATA_DIR.resolve()).replace("\\", "/").rstrip("/") + "/"

    def _repl(match: re.Match[str]) -> str:
        raw = match.group(0).replace("\\", "/")
        if raw.startswith(data_root_str):
            return raw[len(data_root_str) :]
        for marker in ("/data/media/", "/data/exports/"):
            idx = raw.find(marker)
            if idx != -1:
                return raw[idx + len("/data/") :]
        return Path(raw).name

    return _LOCAL_ABS_PATH_RE.sub(_repl, text)


def _sanitize_cloud_text(text: str) -> str:
    """脱敏面向自托管协作看板展示的说明/备注/剧本，去除本地工具/费用/本机绝对路径"""
    if not text:
        return ""
    out = _strip_local_abs_paths(str(text))
    for pattern, repl in _CLOUD_TEXT_SANITIZE_RULES:
        out = pattern.sub(repl, out)
    out = re.sub(r"\s*[·•]\s*(?=[）)])", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def _sanitize_take_notes(notes: str, resolution_label: str = "", is_final: int = 0) -> str:
    """清洗 takes.notes：移除本地生成引擎日志与费用"""
    raw = (notes or "").strip()
    res = (resolution_label or "").strip()
    if not raw or re.search(r"CLI\s*直出|gemini|omni|veo|\$\s*\d", raw, re.IGNORECASE):
        base = "高清定稿视频" if is_final else "分镜成品视频"
        return f"{base}（{res}）" if res else base
    return _sanitize_cloud_text(raw)


def _sanitize_take_filename(filename: str, fallback_rel_path: str = "") -> str:
    """清洗 takes.original_filename：剥除本地引擎前缀"""
    raw = Path(filename or fallback_rel_path or "video.mp4").name
    cleaned = re.sub(r"^(?:cli|gemini|omni\d*|veo\d*|flow)[_\-]+", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:omni\d*|veo\d*|gemini|flow|cli)[_\-]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_(?:cli|omni\d*|veo\d*|gemini)(?=_|\.)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_")
    return cleaned or Path(fallback_rel_path or "video.mp4").name


def _sanitize_json_recursive(value: Any, *, sanitize_text: bool = True) -> Any:
    """递归清洗 JSON 对象/数组中的本机绝对路径与本地引擎词"""
    if isinstance(value, dict):
        return {k: _sanitize_json_recursive(v, sanitize_text=sanitize_text) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_recursive(v, sanitize_text=sanitize_text) for v in value]
    if isinstance(value, str):
        rel, abs_p = to_rel_data_path(value)
        if rel and abs_p:
            return rel
        stripped = _strip_local_abs_paths(value)
        return _sanitize_cloud_text(stripped) if sanitize_text else stripped
    return value


def _sanitize_qc_report_json(raw_json: str) -> str:
    """清洗 shots.qc_report_json：剥离本机绝对路径，保留完整质检指标"""
    if not raw_json or not raw_json.strip():
        return "{}"
    try:
        parsed = json.loads(raw_json)
    except Exception:
        return json.dumps({"summary": _sanitize_cloud_text(raw_json)}, ensure_ascii=False)
    cleaned = _sanitize_json_recursive(parsed, sanitize_text=True)
    return json.dumps(cleaned, ensure_ascii=False)


def _sanitize_json_str_text(raw_json: str, default_str: str) -> str:
    """对 JSON 字符串字段做无损递归文本脱敏"""
    if not raw_json or not raw_json.strip():
        return default_str
    try:
        parsed = json.loads(raw_json)
    except Exception:
        return _sanitize_cloud_text(raw_json)
    cleaned = _sanitize_json_recursive(parsed, sanitize_text=True)
    return json.dumps(cleaned, ensure_ascii=False)


# ==================== 配置与安全凭据读取 ====================


def get_cloud_studio_url() -> str:
    """解析自托管远程协作服务端根 URL（未配置时返回空字符串）"""
    cfg = load_config()
    url = (cfg.get("CLOUD_STUDIO_URL") or "").strip()
    if not url:
        url = (os.environ.get("CLOUD_STUDIO_URL") or "").strip()
    if not url and ENV_PATH.exists() and dotenv_values is not None:
        try:
            env_vals = dotenv_values(ENV_PATH)
            url = str(env_vals.get("CLOUD_STUDIO_URL") or "").strip()
        except Exception:
            url = ""
    return (url or DEFAULT_CLOUD_STUDIO_URL).rstrip("/")


def get_sync_api_token() -> str:
    """
    安全解析 SYNC_API_TOKEN（绝不在日志、异常或返回值中明文输出）：
    优先级：load_config() -> 跨平台安全存储 -> 环境变量 SYNC_API_TOKEN -> SYNC_TOKEN_FILE。
    """
    cfg = load_config()
    tok = (cfg.get("SYNC_API_TOKEN") or "").strip()
    if tok:
        return tok

    stored = get_secret("SYNC_API_TOKEN")
    if stored:
        return stored

    env_tok = (os.environ.get("SYNC_API_TOKEN") or "").strip()
    if env_tok:
        return env_tok

    token_file = (cfg.get("SYNC_TOKEN_FILE") or os.environ.get("SYNC_TOKEN_FILE") or "").strip()
    if token_file:
        p = Path(token_file).expanduser()
        if p.exists() and p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    return ""


def _make_direct_cloud_client(timeout: httpx.Timeout) -> httpx.Client:
    """创建强制直连自托管协作服务端的 httpx 客户端（trust_env=False）"""
    return httpx.Client(
        trust_env=False,
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": "AntigravityVideoStudio-LocalSync/2.0",
            "Accept": "application/json",
        },
    )


def _sync_headers(token: str) -> dict[str, str]:
    return {"X-Sync-Token": token} if token else {}


# ==================== 同步状态持久化 ====================


def _load_sync_state() -> dict[str, Any]:
    if not SYNC_STATE_PATH.exists():
        return {"last_pull_at": "", "last_push_at": ""}
    try:
        raw = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("last_pull_at", "")
            raw.setdefault("last_push_at", "")
            return raw
    except Exception:
        pass
    return {"last_pull_at": "", "last_push_at": ""}


def _save_sync_state(state: dict[str, Any]) -> None:
    try:
        tmp = SYNC_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SYNC_STATE_PATH)
    except Exception:
        pass


# ==================== 路径归一化与安全过滤 ====================


def _is_forbidden_file(p: Path) -> bool:
    name_low = p.name.lower()
    if name_low in FORBIDDEN_FILENAMES or name_low.startswith("."):
        return True
    if p.suffix.lower() in (".db", ".sqlite", ".key", ".env", ".pem"):
        return True
    path_low = str(p).lower().replace("\\", "/")
    for bad in FORBIDDEN_PATH_TOKENS:
        if bad in name_low or f"/{bad}" in path_low:
            return True
    return False


def to_rel_data_path(raw_path: str | Path | None) -> tuple[str, Path | None]:
    """
    将本地绝对/相对路径安全归一化为相对 DATA_DIR 的 POSIX 路径（如 `media/demo-companion-drama/...`）。
    仅允许严格落在 DATA_DIR 内、真实存在且通过安全黑名单校验的文件。
    """
    if not raw_path:
        return "", None
    s = str(raw_path).strip()
    if not s:
        return "", None
    data_root = DATA_DIR.resolve()
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = data_root / p
    try:
        resolved = p.resolve()
        rel = resolved.relative_to(data_root)
    except Exception:
        return "", None
    if not resolved.exists() or not resolved.is_file():
        return "", None
    if _is_forbidden_file(resolved):
        return "", None
    return rel.as_posix(), resolved


def _rel_path_to_local_str(rel_or_abs: str | None) -> str:
    """把远端回传的相对 DATA_DIR 路径安全还原为本地绝对路径（若越界则返回空字符串）"""
    s = (rel_or_abs or "").strip()
    if not s:
        return ""
    data_root = DATA_DIR.resolve()
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = data_root / p
    try:
        resolved = p.resolve()
        resolved.relative_to(data_root)
        return str(resolved)
    except Exception:
        return ""


# ==================== 跨平台轻量网页预览版转码 ====================


def _probe_video_meta(video_path: Path) -> dict[str, Any]:
    """用 ffprobe 探测视频尺寸、色彩空间、码率与是否包含音频流"""
    meta: dict[str, Any] = {
        "width": 0,
        "height": 0,
        "codec_name": "",
        "pix_fmt": "",
        "color_space": "",
        "color_transfer": "",
        "color_primaries": "",
        "bit_rate": 0,
        "duration": 0.0,
        "has_audio": False,
        "size_bytes": video_path.stat().st_size if video_path.exists() else 0,
    }
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt,width,height,color_space,color_transfer,color_primaries,bit_rate,duration",
                "-show_entries",
                "format=bit_rate,duration",
                "-of",
                "json",
                str(video_path),
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10.0,
        )
        info = json.loads(out or "{}")
        for st in info.get("streams") or []:
            ctype = st.get("codec_type")
            if ctype == "video" and not meta["codec_name"]:
                meta["codec_name"] = str(st.get("codec_name") or "").lower()
                meta["pix_fmt"] = str(st.get("pix_fmt") or "").lower()
                meta["width"] = int(st.get("width") or 0)
                meta["height"] = int(st.get("height") or 0)
                meta["color_space"] = str(st.get("color_space") or "").lower()
                meta["color_transfer"] = str(st.get("color_transfer") or "").lower()
                meta["color_primaries"] = str(st.get("color_primaries") or "").lower()
                if st.get("bit_rate"):
                    meta["bit_rate"] = int(float(st["bit_rate"]))
                if st.get("duration"):
                    meta["duration"] = float(st["duration"])
            elif ctype == "audio":
                meta["has_audio"] = True
        fmt = info.get("format") or {}
        if not meta["bit_rate"] and fmt.get("bit_rate"):
            meta["bit_rate"] = int(float(fmt["bit_rate"]))
        if not meta["duration"] and fmt.get("duration"):
            meta["duration"] = float(fmt["duration"])
    except Exception:
        pass
    return meta


def ensure_web_preview_mp4(
    source_video: Path,
    proj_slug: str,
    ep_number: int,
    *,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """
    为远程协作网页流畅预览生成轻量 H.264 版本（自动适配 VideoToolbox / NVENC / libx264）：
    - 若源视频 <= 15 MB 且已是浏览器兼容的 8-bit H.264，直接复用源文件；
    - 否则压缩为 ~2.2 Mbps 720p H.264 BT.709 + AAC 128k + faststart 方便移动与弱网预览。
    """
    src = source_video.expanduser().resolve()
    if not src.exists() or not src.is_file():
        return {"ok": False, "path": "", "reused": False, "transcoded": False, "reason": "源视频不存在"}

    meta = _probe_video_meta(src)
    size_bytes = meta["size_bytes"]
    w, h = meta["width"], meta["height"]
    short_side = min(w, h) if (w and h) else max(w, h)
    is_hdr_or_10bit = (
        "10" in meta["pix_fmt"]
        or meta["color_transfer"] in ("arib-std-b67", "smpte2084")
        or meta["color_space"].startswith("bt2020")
        or meta["codec_name"] in ("hevc", "h265")
    )
    is_high_res_heavy = size_bytes > WEB_PREVIEW_SIZE_THRESHOLD_BYTES and (
        short_side >= 720 or meta["bit_rate"] > 3_500_000
    )

    if not force_rebuild and not is_high_res_heavy and not is_hdr_or_10bit:
        return {
            "ok": True,
            "path": str(src),
            "reused": True,
            "transcoded": False,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "reason": "源视频体积轻量且已兼容网页播放，直接复用",
        }

    out_dir = EXPORTS_DIR / f"{proj_slug}_ep{int(ep_number):02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{src.stem}_web_preview.mp4"

    if (
        not force_rebuild
        and out_path.exists()
        and out_path.stat().st_size > 10 * 1024
        and out_path.stat().st_mtime >= src.stat().st_mtime
    ):
        return {
            "ok": True,
            "path": str(out_path.resolve()),
            "reused": True,
            "transcoded": False,
            "size_mb": round(out_path.stat().st_size / (1024 * 1024), 2),
            "reason": "复用已生成的网页流畅预览版缓存",
        }

    vf_parts: list[str] = []
    if h >= w:
        if w > 720:
            vf_parts.append("scale=720:-2:flags=lanczos")
        else:
            vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        if h > 720:
            vf_parts.append("scale=-2:720:flags=lanczos")
        else:
            vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    if meta["color_transfer"] in ("arib-std-b67", "smpte2084") or meta["color_space"].startswith("bt2020"):
        vf_parts.append("colorspace=all=bt709:iall=bt2020:fast=1")
    vf_parts.append("format=yuv420p")
    vf_chain = ",".join(vf_parts)

    encoder_name, _, _ = select_video_encoder(hdr_hlg=False)
    tmp_path = out_path.with_name(f".{out_path.stem}.tmp.mp4")

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vf",
        vf_chain,
    ]
    if encoder_name == "h264_videotoolbox":
        cmd.extend(
            [
                "-c:v", "h264_videotoolbox",
                "-b:v", "2200k",
                "-maxrate", "2800k",
                "-bufsize", "5000k",
                "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
            ]
        )
    elif encoder_name == "h264_nvenc":
        cmd.extend(
            [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-rc", "vbr",
                "-b:v", "2200k",
                "-maxrate", "2800k",
                "-bufsize", "5000k",
                "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
            ]
        )
    else:
        cmd.extend(
            [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-b:v", "2200k",
                "-maxrate", "2800k",
                "-bufsize", "5000k",
                "-pix_fmt", "yuv420p",
            ]
        )

    if meta["has_audio"]:
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"])
    else:
        cmd.append("-an")

    cmd.extend(["-movflags", "+faststart", str(tmp_path)])

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180.0)
        tmp_path.replace(out_path)
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if "colorspace=" in vf_chain:
            fallback_vf = ",".join(p for p in vf_parts if not p.startswith("colorspace="))
            cmd_fb = [fallback_vf if x == vf_chain else x for x in cmd]
            try:
                subprocess.run(cmd_fb, check=True, capture_output=True, text=True, timeout=180.0)
                tmp_path.replace(out_path)
            except Exception as exc2:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                return {"ok": False, "path": str(src), "reused": True, "transcoded": False, "reason": f"转码失败回退源文件：{exc2}"}
        else:
            return {"ok": False, "path": str(src), "reused": True, "transcoded": False, "reason": f"转码失败回退源文件：{exc}"}

    out_size = out_path.stat().st_size
    return {
        "ok": True,
        "path": str(out_path.resolve()),
        "reused": False,
        "transcoded": True,
        "encoder": encoder_name,
        "source_size_mb": round(size_bytes / (1024 * 1024), 2),
        "size_mb": round(out_size / (1024 * 1024), 2),
        "reason": f"已使用 {encoder_name} 压制网页流畅预览版",
    }


# ==================== 辅助工具 ====================


def _latest_glob(directory: Path, pattern: str) -> Path | None:
    if not directory.exists() or not directory.is_dir():
        return None
    matches = [p for p in directory.glob(pattern) if p.is_file()]
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


# ==================== 核心功能 1：自托管远程协作连通性检测 ====================


def check_cloud_status() -> dict[str, Any]:
    """直连检测自托管远程协作服务端健康状态与延迟（未配置时优雅返回未启用状态）"""
    cloud_url = get_cloud_studio_url()
    token = get_sync_api_token()
    state = _load_sync_state()

    if not cloud_url:
        return {
            "ok": False,
            "configured": False,
            "reachable": False,
            "status_code": 0,
            "latency_ms": 0.0,
            "cloud_url": "",
            "direct_connection": True,
            "token_configured": bool(token),
            "message": "尚未配置自托管远程协作服务端地址（CLOUD_STUDIO_URL），当前为纯本地单机模式。",
            "last_push_at": state.get("last_push_at", ""),
            "last_pull_at": state.get("last_pull_at", ""),
        }

    health_url = f"{cloud_url}/api/health"
    t0 = time.perf_counter()
    try:
        with _make_direct_cloud_client(httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)) as client:
            resp = client.get(health_url, headers=_sync_headers(token))
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        payload: Any = {}
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text[:200]}

        return {
            "ok": resp.status_code == 200,
            "configured": True,
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "cloud_url": cloud_url,
            "direct_connection": True,
            "token_configured": bool(token),
            "remote_health": payload,
            "last_push_at": state.get("last_push_at", ""),
            "last_pull_at": state.get("last_pull_at", ""),
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "ok": False,
            "configured": True,
            "reachable": False,
            "status_code": 0,
            "latency_ms": latency_ms,
            "cloud_url": cloud_url,
            "direct_connection": True,
            "token_configured": bool(token),
            "error": str(exc),
            "last_push_at": state.get("last_push_at", ""),
            "last_pull_at": state.get("last_pull_at", ""),
        }


# ==================== 核心功能 2：安全脱敏推送单集至远程协作服务端 ====================


def _check_remote_file_matches(
    client: httpx.Client,
    cloud_url: str,
    token: str,
    rel_path: str,
    expected_size: int,
) -> bool:
    try:
        resp = client.get(
            f"{cloud_url}/api/sync/check-file",
            headers=_sync_headers(token),
            params={"rel_path": rel_path},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        )
        if resp.status_code == 200:
            data = resp.json()
            return bool(data.get("exists")) and int(data.get("size") or -1) == expected_size
    except Exception:
        pass
    return False


def _upload_single_file_direct(
    client: httpx.Client,
    cloud_url: str,
    token: str,
    rel_path: str,
    abs_path: Path,
    *,
    force_upload: bool = False,
) -> dict[str, Any]:
    size_bytes = abs_path.stat().st_size

    if not force_upload and _check_remote_file_matches(client, cloud_url, token, rel_path, size_bytes):
        return {
            "rel_path": rel_path,
            "size": size_bytes,
            "status": "skipped_remote_exists",
        }

    upload_url = f"{cloud_url}/api/sync/upload-file"
    mime, _ = mimetypes.guess_type(abs_path.name)
    mime = mime or "application/octet-stream"

    last_err = ""
    for attempt in range(1, 4):
        try:
            with abs_path.open("rb") as fh:
                resp = client.post(
                    upload_url,
                    headers=_sync_headers(token),
                    data={"rel_path": rel_path},
                    files={"file": (abs_path.name, fh, mime)},
                )
            if resp.status_code in (200, 201):
                body = {}
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                return {
                    "rel_path": rel_path,
                    "size": size_bytes,
                    "status": "uploaded",
                    "sha256": body.get("sha256", ""),
                }
            last_err = f"HTTP {resp.status_code}: {resp.text[:240]}"
            if resp.status_code in (400, 401, 403, 404, 413):
                break
        except Exception as exc:
            last_err = str(exc)
        if attempt < 3:
            time.sleep(2.0 * attempt)

    raise RuntimeError(f"上传文件失败 ({rel_path}): {last_err}")


def _collect_episode_sync_package(
    episode_id: int,
    *,
    include_final_master: bool = True,
) -> dict[str, Any]:
    """采集单集的全部脱敏业务数据与白名单上传文件清单"""
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise ValueError(f"分集不存在：episode_id={episode_id}")
        ep = dict(ep_row)
        proj_row = conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone()
        if not proj_row:
            raise ValueError(f"分集关联剧目不存在：project_id={ep['project_id']}")
        proj = dict(proj_row)

        assets = rows_to_dicts(
            conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY id ASC", (proj["id"],)).fetchall()
        )
        shots = rows_to_dicts(
            conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
                (episode_id,),
            ).fetchall()
        )
        shot_id_to_no = {int(s["id"]): int(s["shot_no"]) for s in shots}

        selected_takes: list[dict[str, Any]] = []
        for s in shots:
            final_rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM takes WHERE shot_id = ? AND is_final = 1 ORDER BY id DESC",
                    (s["id"],),
                ).fetchall()
            )
            if final_rows:
                selected_takes.extend(final_rows)
            else:
                latest_vid = conn.execute(
                    "SELECT * FROM takes WHERE shot_id = ? AND media_type = 'video' ORDER BY id DESC LIMIT 1",
                    (s["id"],),
                ).fetchone()
                if latest_vid:
                    selected_takes.append(dict(latest_vid))

        ai_proof_row = conn.execute(
            """
            SELECT output_path FROM render_jobs
            WHERE episode_id = ? AND job_type = 'ai_proof' AND status = 'done'
            ORDER BY id DESC LIMIT 1
            """,
            (episode_id,),
        ).fetchone()
        latest_preview_job = conn.execute(
            """
            SELECT output_path FROM render_jobs
            WHERE episode_id = ? AND job_type IN ('local_preview', 'colab_render') AND status = 'done'
            ORDER BY id DESC LIMIT 1
            """,
            (episode_id,),
        ).fetchone()

    proj_slug = proj["slug"]
    ep_num = int(ep["ep_number"])
    export_dir = EXPORTS_DIR / f"{proj_slug}_ep{ep_num:02d}"
    qc_dir = export_dir / "qc"
    covers_dir = MEDIA_DIR / proj_slug / f"ep{ep_num:02d}" / "covers"

    files_to_upload: dict[str, Path] = {}

    def _register_file(path_str: str | Path | None) -> str:
        rel, abs_p = to_rel_data_path(path_str)
        if rel and abs_p:
            files_to_upload[rel] = abs_p
            return rel
        return ""

    clean_assets: list[dict[str, Any]] = []
    for a in assets:
        clean_assets.append(
            {
                "id": a["id"],
                "project_id": a["project_id"],
                "asset_type": a.get("asset_type") or "character",
                "name": a.get("name") or "",
                "flow_tag": a.get("flow_tag") or "",
                "raw_image_path": _register_file(a.get("raw_image_path")),
                "clean_image_path": _register_file(a.get("clean_image_path")),
                "en_anchor_prompt": a.get("en_anchor_prompt") or "",
                "negative_guard_en": a.get("negative_guard_en") or "",
                "voice_mode": a.get("voice_mode") or "flow_sfx",
                "voice_en_desc": a.get("voice_en_desc") or "",
                "voice_clone_sample_path": _register_file(a.get("voice_clone_sample_path")),
                "notes": _sanitize_cloud_text(a.get("notes") or ""),
                "created_at": a.get("created_at") or now_str(),
                "updated_at": a.get("updated_at") or now_str(),
            }
        )

    clean_shots: list[dict[str, Any]] = []
    for s in shots:
        clean_shots.append(
            {
                "id": s["id"],
                "episode_id": s["episode_id"],
                "shot_no": s["shot_no"],
                "shot_title": s.get("shot_title") or "",
                "camera_type": s.get("camera_type") or "中景",
                "duration_sec": int(s.get("duration_sec") or 6),
                "flow_mode": s.get("flow_mode") or "omni12_i2v",
                "ref_asset_ids": s.get("ref_asset_ids") or "[]",
                "cn_action_desc": _sanitize_cloud_text(s.get("cn_action_desc") or ""),
                "cn_dialogue": s.get("cn_dialogue") or "",
                "subtitle_text": s.get("subtitle_text") or "",
                "frame_prompt_en": s.get("frame_prompt_en") or "",
                "end_frame_prompt_en": s.get("end_frame_prompt_en") or "",
                "video_prompt_en": s.get("video_prompt_en") or "",
                "start_frame_path": _register_file(s.get("start_frame_path")),
                "end_frame_path": _register_file(s.get("end_frame_path")),
                "audio_strategy": s.get("audio_strategy") or "keep_flow_sfx",
                "upscale_target": s.get("upscale_target") or "1080p_free",
                "upscale_done": int(s.get("upscale_done") or 0),
                "trim_start": float(s.get("trim_start") or 0.0),
                "trim_end": float(s.get("trim_end") or 0.0),
                "sort_order": int(s.get("sort_order") or s["shot_no"]),
                "vo_file_path": _register_file(s.get("vo_file_path")),
                "vo_source": s.get("vo_source") or "",
                "qc_strip_path": _register_file(s.get("qc_strip_path")),
                "qc_report_json": _sanitize_qc_report_json(s.get("qc_report_json") or "{}"),
                "created_at": s.get("created_at") or now_str(),
                "updated_at": s.get("updated_at") or now_str(),
            }
        )

    clean_takes: list[dict[str, Any]] = []
    for t in selected_takes:
        rel_fp = _register_file(t.get("file_path"))
        if not rel_fp:
            continue
        sid = int(t["shot_id"])
        res_label = t.get("resolution_label") or ""
        is_final_int = int(t.get("is_final") or 0)
        clean_takes.append(
            {
                "id": t["id"],
                "shot_id": sid,
                "shot_no": shot_id_to_no.get(sid),
                "media_type": t.get("media_type") or "video",
                "file_path": rel_fp,
                "original_filename": _sanitize_take_filename(t.get("original_filename") or "", rel_fp),
                "width": int(t.get("width") or 0),
                "height": int(t.get("height") or 0),
                "duration_sec": float(t.get("duration_sec") or 0.0),
                "resolution_label": res_label,
                "rating": int(t.get("rating") or 0),
                "is_final": is_final_int,
                "notes": _sanitize_take_notes(t.get("notes") or "", res_label, is_final_int),
                "created_at": t.get("created_at") or now_str(),
            }
        )

    raw_covers: list[dict[str, Any]] = []
    try:
        parsed_covers = json.loads(ep.get("cover_candidates_json") or "[]")
        if isinstance(parsed_covers, list):
            raw_covers = parsed_covers
    except Exception:
        raw_covers = []

    clean_covers: list[dict[str, Any]] = []
    for c in raw_covers:
        if not isinstance(c, dict):
            continue
        c_copy = dict(c)
        c_copy["path"] = _register_file(c.get("path"))
        clean_covers.append(c_copy)

    selected_cover_rel = _register_file(ep.get("selected_cover_path"))
    covers_sheet_rel = _register_file(ep.get("covers_sheet_path") or _latest_glob(covers_dir, "E*_covers_sheet_*.jpg"))
    frames_sheet_rel = _register_file(ep.get("frames_sheet_path") or _latest_glob(qc_dir, "E*_frames_sheet_*.jpg"))
    qc_overview_rel = _register_file(ep.get("qc_overview_path") or _latest_glob(qc_dir, "E*_qc_overview_*.jpg"))

    ai_proof_candidate = (ep.get("ai_proof_path") or (ai_proof_row["output_path"] if ai_proof_row else "") or "").strip()
    if not ai_proof_candidate or not Path(ai_proof_candidate).exists():
        found_proof = _latest_glob(export_dir, "*AI原创制作证明*.md")
        ai_proof_candidate = str(found_proof) if found_proof else ""
    ai_proof_rel = _register_file(ai_proof_candidate)

    bgm_rel = _register_file(ep.get("bgm_file_path"))

    master_candidate = (ep.get("latest_output_path") or "").strip()
    if (not master_candidate or not Path(master_candidate).exists()) and latest_preview_job:
        master_candidate = str(latest_preview_job["output_path"] or "").strip()

    web_preview_info: dict[str, Any] = {"ok": False, "path": "", "transcoded": False}
    web_preview_rel = ""
    master_rel = ""
    srt_rel = ""
    render_jobs_out: list[dict[str, Any]] = []

    if master_candidate and Path(master_candidate).exists():
        master_p = Path(master_candidate).expanduser().resolve()
        web_preview_info = ensure_web_preview_mp4(master_p, proj_slug, ep_num)
        if web_preview_info.get("path"):
            web_preview_rel = _register_file(web_preview_info["path"])
            try:
                with db_ctx() as conn:
                    ep_cols = _table_columns(conn, "episodes")
                    local_updates: dict[str, str] = {}
                    if "web_preview_path" in ep_cols:
                        local_updates["web_preview_path"] = str(Path(web_preview_info["path"]).resolve())
                    if "frames_sheet_path" in ep_cols and frames_sheet_rel:
                        local_updates["frames_sheet_path"] = str((DATA_DIR / frames_sheet_rel).resolve())
                    if "covers_sheet_path" in ep_cols and covers_sheet_rel:
                        local_updates["covers_sheet_path"] = str((DATA_DIR / covers_sheet_rel).resolve())
                    if "qc_overview_path" in ep_cols and qc_overview_rel:
                        local_updates["qc_overview_path"] = str((DATA_DIR / qc_overview_rel).resolve())
                    if "ai_proof_path" in ep_cols and ai_proof_rel:
                        local_updates["ai_proof_path"] = str((DATA_DIR / ai_proof_rel).resolve())
                    if local_updates:
                        set_sql = ", ".join(f"{k} = ?" for k in local_updates)
                        conn.execute(f"UPDATE episodes SET {set_sql} WHERE id = ?", (*local_updates.values(), episode_id))
            except Exception:
                pass

        if include_final_master:
            master_rel = _register_file(master_p)
            if master_rel:
                render_jobs_out.append(
                    {
                        "job_type": "final_master",
                        "target_host": "cloud_sync",
                        "status": "done",
                        "output_path": master_rel,
                        "log_text": "已同步高清终版成片（支持直接下载）",
                    }
                )
        else:
            master_rel = web_preview_rel

        if web_preview_rel:
            render_jobs_out.append(
                {
                    "job_type": "web_preview",
                    "target_host": "cloud_sync",
                    "status": "done",
                    "output_path": web_preview_rel,
                    "log_text": "已同步网页流畅预览版（~2.2 Mbps H.264，支持网页秒播）",
                }
            )

        srt_p = _latest_glob(export_dir, "*.srt")
        if srt_p:
            srt_rel = _register_file(srt_p)

    clean_project = {
        "id": proj["id"],
        "name": proj["name"],
        "slug": proj["slug"],
        "track": proj.get("track") or "",
        "visual_mode": proj.get("visual_mode") or "",
        "aspect_ratio": proj.get("aspect_ratio") or "9:16",
        "target_duration_sec": int(proj.get("target_duration_sec") or 70),
        "global_style_prompt": proj.get("global_style_prompt") or "",
        "description": _sanitize_cloud_text(proj.get("description") or ""),
        "season_outline": _sanitize_json_str_text(proj.get("season_outline") or "[]", "[]"),
        "continuity_ledger": _sanitize_json_str_text(proj.get("continuity_ledger") or "{}", "{}"),
        "created_at": proj.get("created_at") or now_str(),
        "updated_at": proj.get("updated_at") or now_str(),
    }

    clean_episode = {
        "id": ep["id"],
        "project_id": ep["project_id"],
        "ep_number": ep_num,
        "title": ep.get("title") or "",
        "topic_id": ep.get("topic_id"),
        "hook_0_3s": _sanitize_cloud_text(ep.get("hook_0_3s") or ""),
        "ending_cta": _sanitize_cloud_text(ep.get("ending_cta") or ""),
        "next_ep_hook": _sanitize_cloud_text(ep.get("next_ep_hook") or ""),
        "script_text": _sanitize_cloud_text(ep.get("script_text") or ""),
        "continuity_summary": _sanitize_cloud_text(ep.get("continuity_summary") or ""),
        "bgm_prompt": ep.get("bgm_prompt") or "",
        "bgm_file_path": bgm_rel,
        "bgm_volume": float(ep.get("bgm_volume") if ep.get("bgm_volume") is not None else 0.25),
        "status": ep.get("status") or "draft",
        "latest_output_path": master_rel,
        "web_preview_path": web_preview_rel,
        "frames_sheet_path": frames_sheet_rel,
        "covers_sheet_path": covers_sheet_rel,
        "qc_overview_path": qc_overview_rel,
        "ai_proof_path": ai_proof_rel,
        "vo_mode": ep.get("vo_mode") or "pending",
        "cover_candidates_json": json.dumps(clean_covers, ensure_ascii=False),
        "selected_cover_path": selected_cover_rel,
        "created_at": ep.get("created_at") or now_str(),
        "updated_at": ep.get("updated_at") or now_str(),
    }

    extras = {
        "frames_sheet_path": frames_sheet_rel,
        "qc_overview_path": qc_overview_rel,
        "covers_sheet_path": covers_sheet_rel,
        "ai_proof_path": ai_proof_rel,
        "srt_path": srt_rel,
        "web_preview_path": web_preview_rel,
        "final_master_path": master_rel if include_final_master else "",
    }

    return {
        "project": clean_project,
        "assets": clean_assets,
        "episode": clean_episode,
        "shots": clean_shots,
        "takes": clean_takes,
        "render_jobs": render_jobs_out,
        "extras": extras,
        "files_to_upload": files_to_upload,
        "web_preview_info": web_preview_info,
    }


def push_episode_to_cloud(
    episode_id: int,
    notify_stage: str = "update",
    include_final_master: bool = True,
    *,
    notify_note: str = "",
    force_upload: bool = False,
) -> dict[str, Any]:
    """将本集脱敏制作包推送到自托管远程协作服务端并按需触发通知"""
    cloud_url = get_cloud_studio_url()
    if not cloud_url:
        raise RuntimeError("尚未配置自托管远程协作服务端地址（CLOUD_STUDIO_URL），请先在「系统设置」开启多人协作并填写服务端地址。")
    token = get_sync_api_token()
    if not token:
        raise RuntimeError("未配置 SYNC_API_TOKEN，请先在「系统设置」填写同步令牌或通过受控环境变量提供。")

    pkg = _collect_episode_sync_package(episode_id, include_final_master=include_final_master)
    files_map: dict[str, Path] = pkg["files_to_upload"]

    uploaded_count = 0
    skipped_count = 0
    uploaded_bytes = 0
    file_results: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    upload_timeout = httpx.Timeout(connect=15.0, read=300.0, write=300.0, pool=15.0)
    with _make_direct_cloud_client(upload_timeout) as client:
        ordered_files = sorted(files_map.items(), key=lambda kv: kv[1].stat().st_size)
        for rel_path, abs_path in ordered_files:
            res = _upload_single_file_direct(
                client,
                cloud_url,
                token,
                rel_path,
                abs_path,
                force_upload=force_upload,
            )
            file_results.append(
                {
                    "rel_path": rel_path,
                    "size_kb": round(res["size"] / 1024, 1),
                    "status": res["status"],
                }
            )
            if res["status"] == "uploaded":
                uploaded_count += 1
                uploaded_bytes += res["size"]
            else:
                skipped_count += 1

        bundle_payload: dict[str, Any] = {
            "project": pkg["project"],
            "assets": pkg["assets"],
            "episode": pkg["episode"],
            "shots": pkg["shots"],
            "takes": pkg["takes"],
            "render_jobs": pkg["render_jobs"],
            "ai_proof_path": pkg["extras"]["ai_proof_path"],
            "frames_sheet_path": pkg["extras"]["frames_sheet_path"],
            "covers_sheet_path": pkg["extras"]["covers_sheet_path"],
            "qc_overview_path": pkg["extras"]["qc_overview_path"],
            "web_preview_path": pkg["extras"]["web_preview_path"],
            "notify_stage": notify_stage or "update",
            "notify_note": (notify_note or "").strip(),
        }

        push_url = f"{cloud_url}/api/sync/push-bundle"
        resp = client.post(
            push_url,
            headers=_sync_headers(token),
            json=bundle_payload,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"推送结构化数据失败 (HTTP {resp.status_code}): {resp.text[:300]}")
        try:
            remote_bundle_res = resp.json()
        except Exception:
            remote_bundle_res = {"raw": resp.text[:300]}

    ts = now_str()
    state = _load_sync_state()
    state["last_push_at"] = ts
    _save_sync_state(state)

    try:
        with db_ctx() as conn:
            if "sync_updated_at" in _table_columns(conn, "episodes"):
                conn.execute("UPDATE episodes SET sync_updated_at = ? WHERE id = ?", (ts, episode_id))
    except Exception:
        pass

    elapsed_sec = round(time.perf_counter() - t0, 2)
    return {
        "ok": True,
        "episode_id": episode_id,
        "episode_title": pkg["episode"]["title"],
        "notify_stage": notify_stage or "update",
        "include_final_master": include_final_master,
        "cloud_url": cloud_url,
        "direct_connection": True,
        "elapsed_sec": elapsed_sec,
        "files_total": len(ordered_files),
        "files_uploaded": uploaded_count,
        "files_skipped": skipped_count,
        "uploaded_mb": round(uploaded_bytes / (1024 * 1024), 2),
        "web_preview": pkg["web_preview_info"],
        "extras": pkg["extras"],
        "remote_response": remote_bundle_res,
        "pushed_at": ts,
    }


# ==================== 核心功能 3：拉回远程协作团队修改与互动留痕 ====================


def _build_local_natural_key_maps(
    conn: sqlite3.Connection,
) -> tuple[dict[str, int], dict[tuple[str, int], dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    """构建本地自然键索引，避免远端与本地自增 ID 不同引发合并错位"""
    proj_rows = conn.execute("SELECT id, slug FROM projects").fetchall()
    slug_to_pid = {str(r["slug"]): int(r["id"]) for r in proj_rows}

    ep_rows = conn.execute(
        """
        SELECT e.*, p.slug AS project_slug
        FROM episodes e
        JOIN projects p ON p.id = e.project_id
        """
    ).fetchall()
    ep_by_nat: dict[tuple[str, int], dict[str, Any]] = {}
    for r in ep_rows:
        ep_by_nat[(str(r["project_slug"]), int(r["ep_number"]))] = dict(r)

    shot_rows = conn.execute("SELECT * FROM shots").fetchall()
    shot_by_nat: dict[tuple[int, int], dict[str, Any]] = {}
    for r in shot_rows:
        shot_by_nat[(int(r["episode_id"]), int(r["shot_no"]))] = dict(r)

    return slug_to_pid, ep_by_nat, shot_by_nat


def _extract_score_theme(tp: dict[str, Any]) -> int:
    if tp.get("score_theme") is not None:
        try:
            return int(tp["score_theme"])
        except (TypeError, ValueError):
            pass
    try:
        parsed = json.loads(tp.get("scores_json") or "{}")
        if isinstance(parsed, dict) and parsed.get("theme") is not None:
            return int(parsed["theme"])
    except Exception:
        pass
    return 0


def _safe_merge_pulled_changes(payload: dict[str, Any]) -> dict[str, Any]:
    """将远程协作服务端拉回的增量变更安全合并到本地数据库"""
    summary_counts = {
        "episodes_updated": 0,
        "shots_updated": 0,
        "revisions_added": 0,
        "votes_added": 0,
        "rule_suggestions_synced": 0,
        "topics_added": 0,
        "comments_synced": 0,
        "publish_logs_synced": 0,
    }
    highlights: list[str] = []

    episodes_in = payload.get("episodes") or []
    shots_in = payload.get("shots") or []
    revisions_in = payload.get("revisions") or []
    votes_in = payload.get("review_votes") or payload.get("votes") or []
    suggestions_in = payload.get("rule_suggestions") or []
    topics_in = payload.get("topics") or []
    comments_in = payload.get("comment_drafts") or payload.get("comments") or []
    pub_logs_in = payload.get("publish_logs") or []

    with db_ctx() as conn:
        ep_cols = _table_columns(conn, "episodes")
        shot_cols = _table_columns(conn, "shots")
        topic_cols = _table_columns(conn, "topics")
        has_revisions = _table_exists(conn, "revisions")
        has_votes = _table_exists(conn, "review_votes")
        has_suggestions = _table_exists(conn, "rule_suggestions")
        comment_cols = _table_columns(conn, "comment_drafts")

        _slug_to_pid, ep_by_nat, shot_by_nat = _build_local_natural_key_maps(conn)

        def _resolve_local_episode(item: dict[str, Any]) -> dict[str, Any] | None:
            slug = str(item.get("project_slug") or "").strip()
            ep_no = item.get("ep_number")
            if slug and ep_no is not None:
                hit = ep_by_nat.get((slug, int(ep_no)))
                if hit:
                    return hit
            eid = item.get("episode_id") if "episode_id" in item else item.get("id")
            if eid:
                row = conn.execute("SELECT * FROM episodes WHERE id = ?", (int(eid),)).fetchone()
                if row:
                    return dict(row)
            return None

        # 1. 合并分集协作文本修改
        for ep in episodes_in:
            if not isinstance(ep, dict):
                continue
            local_ep = _resolve_local_episode(ep)
            if not local_ep:
                continue
            remote_upd = str(ep.get("updated_at") or "")
            local_upd = str(local_ep.get("updated_at") or "")
            if remote_upd and local_upd and remote_upd <= local_upd:
                continue

            updates: dict[str, Any] = {}
            changed_fields: list[str] = []
            for field in EPISODE_PULL_FIELDS:
                if field not in ep or field not in ep_cols:
                    continue
                val = ep[field]
                if val is None:
                    continue
                if field == "selected_cover_path" and val:
                    val = _rel_path_to_local_str(str(val))
                    if not val:
                        continue
                if val != local_ep.get(field):
                    updates[field] = val
                    changed_fields.append(field)

            if updates:
                updates["updated_at"] = remote_upd or now_str()
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE episodes SET {set_clause} WHERE id = ?",
                    (*updates.values(), local_ep["id"]),
                )
                local_ep.update(updates)
                summary_counts["episodes_updated"] += 1
                highlights.append(
                    f"分集 E{int(local_ep['ep_number']):02d}《{updates.get('title', local_ep['title'])}》更新字段：{', '.join(changed_fields)}"
                )

        # 2. 合并分镜协作文本修改
        for sh in shots_in:
            if not isinstance(sh, dict):
                continue
            local_sh = None
            local_ep = _resolve_local_episode(sh)
            if local_ep and sh.get("shot_no") is not None:
                local_sh = shot_by_nat.get((int(local_ep["id"]), int(sh["shot_no"])))
            if not local_sh and sh.get("id"):
                row = conn.execute("SELECT * FROM shots WHERE id = ?", (int(sh["id"]),)).fetchone()
                if row:
                    local_sh = dict(row)
            if not local_sh:
                continue

            remote_upd = str(sh.get("updated_at") or "")
            local_upd = str(local_sh.get("updated_at") or "")
            if remote_upd and local_upd and remote_upd <= local_upd:
                continue

            updates = {}
            changed_fields = []
            for field in SHOT_PULL_FIELDS:
                if field not in sh or field not in shot_cols:
                    continue
                val = sh[field]
                if val is None:
                    continue
                if val != local_sh.get(field):
                    updates[field] = val
                    changed_fields.append(field)

            if updates:
                updates["updated_at"] = remote_upd or now_str()
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE shots SET {set_clause} WHERE id = ?",
                    (*updates.values(), local_sh["id"]),
                )
                local_sh.update(updates)
                summary_counts["shots_updated"] += 1
                highlights.append(f"分镜 S{int(local_sh['shot_no']):02d} 更新字段：{', '.join(changed_fields)}")

        # 3. 幂等合入修改留痕（revisions）
        if has_revisions:
            for rv in revisions_in:
                if not isinstance(rv, dict):
                    continue
                local_ep = _resolve_local_episode(rv)
                mapped_ep_id = int(local_ep["id"]) if local_ep else rv.get("episode_id")
                entity_type = str(rv.get("entity_type") or "")
                mapped_entity_id = int(mapped_ep_id) if (entity_type == "episode" and mapped_ep_id) else int(rv.get("entity_id") or 0)

                exists = conn.execute(
                    """
                    SELECT 1 FROM revisions
                    WHERE entity_type = ? AND entity_id = ? AND field_name = ?
                      AND actor_username = ? AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        entity_type,
                        mapped_entity_id,
                        rv.get("field_name") or "",
                        rv.get("actor_username") or "",
                        rv.get("created_at") or "",
                    ),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO revisions (
                        entity_type, entity_id, episode_id, field_name, old_value, new_value,
                        actor_username, actor_name, actor_role, note, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_type,
                        mapped_entity_id,
                        mapped_ep_id,
                        rv.get("field_name") or "",
                        rv.get("old_value") or "",
                        rv.get("new_value") or "",
                        rv.get("actor_username") or "",
                        rv.get("actor_name") or "",
                        rv.get("actor_role") or "partner",
                        rv.get("note") or "",
                        rv.get("created_at") or now_str(),
                    ),
                )
                summary_counts["revisions_added"] += 1
                if len(highlights) < 15:
                    actor = rv.get("actor_name") or rv.get("actor_username") or "协作成员"
                    flabel = rv.get("field_label") or rv.get("field_name") or ""
                    highlights.append(f"[留痕] {actor} 修改了 {entity_type}·{flabel}")

        # 4. 幂等合入审片投票（review_votes）
        if has_votes:
            for vt in votes_in:
                if not isinstance(vt, dict):
                    continue
                local_ep = _resolve_local_episode(vt)
                mapped_ep_id = int(local_ep["id"]) if local_ep else int(vt.get("episode_id") or 0)
                if not mapped_ep_id:
                    continue

                exists = conn.execute(
                    """
                    SELECT 1 FROM review_votes
                    WHERE episode_id = ? AND target_type = ? AND target_key = ?
                      AND actor_username = ? AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        mapped_ep_id,
                        vt.get("target_type") or "",
                        vt.get("target_key") or "",
                        vt.get("actor_username") or "",
                        vt.get("created_at") or "",
                    ),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO review_votes (
                        episode_id, target_type, target_key, choice, verdict, comment,
                        actor_username, actor_name, actor_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mapped_ep_id,
                        vt.get("target_type") or "cover",
                        vt.get("target_key") or "",
                        vt.get("choice") or "",
                        vt.get("verdict") or "comment",
                        vt.get("comment") or "",
                        vt.get("actor_username") or "",
                        vt.get("actor_name") or "",
                        vt.get("actor_role") or "partner",
                        vt.get("created_at") or now_str(),
                    ),
                )
                summary_counts["votes_added"] += 1
                if len(highlights) < 15:
                    actor = vt.get("actor_name") or vt.get("actor_username") or "协作成员"
                    cmt = (vt.get("comment") or "").strip()[:30]
                    highlights.append(
                        f"[投票] {actor} 对 {vt.get('target_type')}({vt.get('choice') or vt.get('target_key') or '整集'}) 表态 {vt.get('verdict')}"
                        + (f"：{cmt}" if cmt else "")
                    )

        # 5. 幂等合入创作规范修改建议（rule_suggestions）
        if has_suggestions:
            for sg in suggestions_in:
                if not isinstance(sg, dict):
                    continue
                existing = conn.execute(
                    """
                    SELECT id, status, admin_reply, handled_at FROM rule_suggestions
                    WHERE template_key = ? AND actor_username = ? AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        sg.get("template_key") or "",
                        sg.get("actor_username") or "",
                        sg.get("created_at") or "",
                    ),
                ).fetchone()
                if existing:
                    if (
                        existing["status"] != (sg.get("status") or "open")
                        or existing["admin_reply"] != (sg.get("admin_reply") or "")
                        or existing["handled_at"] != (sg.get("handled_at") or "")
                    ):
                        conn.execute(
                            """
                            UPDATE rule_suggestions
                            SET status = ?, admin_reply = ?, handled_by = ?, handled_at = ?
                            WHERE id = ?
                            """,
                            (
                                sg.get("status") or "open",
                                sg.get("admin_reply") or "",
                                sg.get("handled_by") or "",
                                sg.get("handled_at") or "",
                                existing["id"],
                            ),
                        )
                        summary_counts["rule_suggestions_synced"] += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO rule_suggestions (
                        template_key, rule_title, suggestion_text, actor_username, actor_name, actor_role,
                        status, admin_reply, handled_by, handled_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sg.get("template_key") or "",
                        sg.get("rule_title") or "",
                        sg.get("suggestion_text") or "",
                        sg.get("actor_username") or "",
                        sg.get("actor_name") or "",
                        sg.get("actor_role") or "partner",
                        sg.get("status") or "open",
                        sg.get("admin_reply") or "",
                        sg.get("handled_by") or "",
                        sg.get("handled_at") or "",
                        sg.get("created_at") or now_str(),
                    ),
                )
                summary_counts["rule_suggestions_synced"] += 1
                if len(highlights) < 15:
                    actor = sg.get("actor_name") or sg.get("actor_username") or "协作成员"
                    highlights.append(
                        f"[规范建议] {actor} 针对《{sg.get('rule_title') or sg.get('template_key')}》：{(sg.get('suggestion_text') or '')[:35]}"
                    )

        # 6. 合并团队成员在协作端新增或更新的选题（topics）
        has_score_theme = "score_theme" in topic_cols
        for tp in topics_in:
            if not isinstance(tp, dict):
                continue
            title = (tp.get("title") or "").strip()
            if not title:
                continue
            created_at = tp.get("created_at") or ""
            score_theme_val = _extract_score_theme(tp)
            existing = conn.execute(
                "SELECT id, updated_at FROM topics WHERE title = ? AND created_at = ? LIMIT 1",
                (title, created_at),
            ).fetchone()
            if existing:
                if (tp.get("updated_at") or "") > (existing["updated_at"] or ""):
                    if has_score_theme:
                        conn.execute(
                            """
                            UPDATE topics
                            SET summary = ?, scores_json = ?, score_theme = ?, total_score = ?, grade = ?,
                                veto = ?, veto_reason = ?, ai_pitch = ?, status = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                tp.get("summary") or "",
                                tp.get("scores_json") or "{}",
                                score_theme_val,
                                int(tp.get("total_score") or 0),
                                tp.get("grade") or "UNSCORED",
                                int(tp.get("veto") or 0),
                                tp.get("veto_reason") or "",
                                tp.get("ai_pitch") or "",
                                tp.get("status") or "new",
                                tp.get("updated_at") or now_str(),
                                existing["id"],
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE topics
                            SET summary = ?, scores_json = ?, total_score = ?, grade = ?,
                                veto = ?, veto_reason = ?, ai_pitch = ?, status = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                tp.get("summary") or "",
                                tp.get("scores_json") or "{}",
                                int(tp.get("total_score") or 0),
                                tp.get("grade") or "UNSCORED",
                                int(tp.get("veto") or 0),
                                tp.get("veto_reason") or "",
                                tp.get("ai_pitch") or "",
                                tp.get("status") or "new",
                                tp.get("updated_at") or now_str(),
                                existing["id"],
                            ),
                        )
                    summary_counts["topics_added"] += 1
                continue

            if has_score_theme:
                conn.execute(
                    """
                    INSERT INTO topics (
                        title, source, source_url, hot_value, video_count, summary,
                        scores_json, score_theme, total_score, grade, veto, veto_reason, ai_pitch,
                        recommended_project_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        tp.get("source") or "cloud_collab",
                        tp.get("source_url") or "",
                        int(tp.get("hot_value") or 0),
                        int(tp.get("video_count") or 0),
                        tp.get("summary") or "",
                        tp.get("scores_json") or "{}",
                        score_theme_val,
                        int(tp.get("total_score") or 0),
                        tp.get("grade") or "UNSCORED",
                        int(tp.get("veto") or 0),
                        tp.get("veto_reason") or "",
                        tp.get("ai_pitch") or "",
                        tp.get("recommended_project_id"),
                        tp.get("status") or "new",
                        created_at or now_str(),
                        tp.get("updated_at") or now_str(),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO topics (
                        title, source, source_url, hot_value, video_count, summary,
                        scores_json, total_score, grade, veto, veto_reason, ai_pitch,
                        recommended_project_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        tp.get("source") or "cloud_collab",
                        tp.get("source_url") or "",
                        int(tp.get("hot_value") or 0),
                        int(tp.get("video_count") or 0),
                        tp.get("summary") or "",
                        tp.get("scores_json") or "{}",
                        int(tp.get("total_score") or 0),
                        tp.get("grade") or "UNSCORED",
                        int(tp.get("veto") or 0),
                        tp.get("veto_reason") or "",
                        tp.get("ai_pitch") or "",
                        tp.get("recommended_project_id"),
                        tp.get("status") or "new",
                        created_at or now_str(),
                        tp.get("updated_at") or now_str(),
                    ),
                )
            summary_counts["topics_added"] += 1
            if len(highlights) < 15:
                highlights.append(f"[新选题] {title[:36]}（{tp.get('grade') or '待打分'}）")

        # 7. 合并观众评论草稿与已回复留痕（comment_drafts）
        has_reply_cols = {"status", "replied_text", "replied_by", "replied_at"}.issubset(comment_cols)
        for cm in comments_in:
            if not isinstance(cm, dict):
                continue
            fan_comment = (cm.get("fan_comment") or "").strip()
            created_at = cm.get("created_at") or ""
            if not fan_comment:
                continue
            existing = conn.execute(
                "SELECT * FROM comment_drafts WHERE fan_comment = ? AND created_at = ? LIMIT 1",
                (fan_comment, created_at),
            ).fetchone()
            if existing:
                if has_reply_cols and (
                    existing["status"] != (cm.get("status") or "draft")
                    or existing["replied_text"] != (cm.get("replied_text") or "")
                ):
                    conn.execute(
                        """
                        UPDATE comment_drafts
                        SET status = ?, replied_text = ?, replied_by = ?, replied_at = ?
                        WHERE id = ?
                        """,
                        (
                            cm.get("status") or "draft",
                            cm.get("replied_text") or "",
                            cm.get("replied_by") or "",
                            cm.get("replied_at") or "",
                            existing["id"],
                        ),
                    )
                    summary_counts["comments_synced"] += 1
                    if len(highlights) < 15 and cm.get("status") == "replied":
                        highlights.append(f"[评论已回复] {cm.get('replied_by') or '协作成员'} 回复了「{fan_comment[:20]}」")
                continue

            if has_reply_cols:
                conn.execute(
                    """
                    INSERT INTO comment_drafts (
                        publish_log_id, episode_id, platform, fan_comment, tone,
                        drafts_json, topic_idea, converted_topic_id,
                        status, replied_text, replied_by, replied_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cm.get("publish_log_id"),
                        cm.get("episode_id"),
                        cm.get("platform") or "抖音",
                        fan_comment,
                        cm.get("tone") or "balanced",
                        cm.get("drafts_json") or "[]",
                        cm.get("topic_idea") or "",
                        cm.get("converted_topic_id"),
                        cm.get("status") or "draft",
                        cm.get("replied_text") or "",
                        cm.get("replied_by") or "",
                        cm.get("replied_at") or "",
                        created_at or now_str(),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO comment_drafts (
                        publish_log_id, episode_id, platform, fan_comment, tone,
                        drafts_json, topic_idea, converted_topic_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cm.get("publish_log_id"),
                        cm.get("episode_id"),
                        cm.get("platform") or "抖音",
                        fan_comment,
                        cm.get("tone") or "balanced",
                        cm.get("drafts_json") or "[]",
                        cm.get("topic_idea") or "",
                        cm.get("converted_topic_id"),
                        created_at or now_str(),
                    ),
                )
            summary_counts["comments_synced"] += 1

        # 8. 合并发布复盘记录（publish_logs）
        for pl in pub_logs_in:
            if not isinstance(pl, dict):
                continue
            post_title = (pl.get("post_title") or "").strip()
            pub_at = pl.get("published_at") or ""
            if not post_title:
                continue
            existing = conn.execute(
                "SELECT id, updated_at FROM publish_logs WHERE platform = ? AND post_title = ? AND published_at = ? LIMIT 1",
                (pl.get("platform") or "微信视频号", post_title, pub_at),
            ).fetchone()
            if existing:
                if (pl.get("updated_at") or "") > (existing["updated_at"] or ""):
                    conn.execute(
                        """
                        UPDATE publish_logs
                        SET views = ?, likes = ?, comments = ?, shares = ?, favorites = ?,
                            completion_rate = ?, moments_feedback = ?, ai_review = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            int(pl.get("views") or 0),
                            int(pl.get("likes") or 0),
                            int(pl.get("comments") or 0),
                            int(pl.get("shares") or 0),
                            int(pl.get("favorites") or 0),
                            float(pl.get("completion_rate") or 0.0),
                            pl.get("moments_feedback") or "",
                            pl.get("ai_review") or "",
                            pl.get("updated_at") or now_str(),
                            existing["id"],
                        ),
                    )
                    summary_counts["publish_logs_synced"] += 1
                continue
            conn.execute(
                """
                INSERT INTO publish_logs (
                    project_id, episode_id, platform, post_title, published_at,
                    views, likes, comments, shares, favorites, completion_rate,
                    moments_feedback, ai_review, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pl.get("project_id"),
                    pl.get("episode_id"),
                    pl.get("platform") or "微信视频号",
                    post_title,
                    pub_at or now_str(),
                    int(pl.get("views") or 0),
                    int(pl.get("likes") or 0),
                    int(pl.get("comments") or 0),
                    int(pl.get("shares") or 0),
                    int(pl.get("favorites") or 0),
                    float(pl.get("completion_rate") or 0.0),
                    pl.get("moments_feedback") or "",
                    pl.get("ai_review") or "",
                    pl.get("created_at") or now_str(),
                    pl.get("updated_at") or now_str(),
                ),
            )
            summary_counts["publish_logs_synced"] += 1

    total_changes = sum(summary_counts.values())
    if total_changes == 0:
        readable = "远程协作服务端暂无新的变更（剧本、分镜、投票、规则建议与评论留痕均已是最新）。"
    else:
        parts = []
        if summary_counts["episodes_updated"]:
            parts.append(f"剧本/分集改动 {summary_counts['episodes_updated']} 处")
        if summary_counts["shots_updated"]:
            parts.append(f"分镜修改 {summary_counts['shots_updated']} 镜")
        if summary_counts["revisions_added"]:
            parts.append(f"修改留痕 {summary_counts['revisions_added']} 条")
        if summary_counts["votes_added"]:
            parts.append(f"审片投票 {summary_counts['votes_added']} 条")
        if summary_counts["rule_suggestions_synced"]:
            parts.append(f"规则修改建议 {summary_counts['rule_suggestions_synced']} 条")
        if summary_counts["topics_added"]:
            parts.append(f"新增/更新选题 {summary_counts['topics_added']} 条")
        if summary_counts["comments_synced"]:
            parts.append(f"评论留痕 {summary_counts['comments_synced']} 条")
        if summary_counts["publish_logs_synced"]:
            parts.append(f"发布复盘 {summary_counts['publish_logs_synced']} 条")
        readable = "已拉回远程协作最新变更：" + "、".join(parts) + "。"

    return {
        "counts": summary_counts,
        "total_changes": total_changes,
        "readable_summary": readable,
        "highlights": highlights,
    }


def pull_changes_from_cloud(since: str = "") -> dict[str, Any]:
    """直连自托管远程协作服务端 `GET {CLOUD_STUDIO_URL}/api/sync/pull-changes`，拉回并合并变更"""
    cloud_url = get_cloud_studio_url()
    if not cloud_url:
        raise RuntimeError("尚未配置自托管远程协作服务端地址（CLOUD_STUDIO_URL），请先在「系统设置」开启多人协作并填写服务端地址。")
    token = get_sync_api_token()
    if not token:
        raise RuntimeError("未配置 SYNC_API_TOKEN，请先在「系统设置」填写同步令牌或通过受控环境变量提供。")

    state = _load_sync_state()
    effective_since = (since or "").strip() or str(state.get("last_pull_at") or "").strip()

    pull_url = f"{cloud_url}/api/sync/pull-changes"
    params = {"since": effective_since} if effective_since else {}

    t0 = time.perf_counter()
    with _make_direct_cloud_client(httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=10.0)) as client:
        resp = client.get(pull_url, headers=_sync_headers(token), params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"拉取远端变更失败 (HTTP {resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    merge_res = _safe_merge_pulled_changes(payload if isinstance(payload, dict) else {})

    server_time = str(payload.get("server_time") or now_str()) if isinstance(payload, dict) else now_str()
    state["last_pull_at"] = server_time
    _save_sync_state(state)

    return {
        "ok": True,
        "cloud_url": cloud_url,
        "direct_connection": True,
        "since_used": effective_since,
        "synced_until": server_time,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "counts": merge_res["counts"],
        "total_changes": merge_res["total_changes"],
        "readable_summary": merge_res["readable_summary"],
        "highlights": merge_res["highlights"],
        "raw_remote_counts": {
            "episodes": len(payload.get("episodes") or []) if isinstance(payload, dict) else 0,
            "shots": len(payload.get("shots") or []) if isinstance(payload, dict) else 0,
            "revisions": len(payload.get("revisions") or []) if isinstance(payload, dict) else 0,
            "review_votes": len(payload.get("review_votes") or []) if isinstance(payload, dict) else 0,
            "rule_suggestions": len(payload.get("rule_suggestions") or []) if isinstance(payload, dict) else 0,
            "topics": len(payload.get("topics") or []) if isinstance(payload, dict) else 0,
            "comment_drafts": len(payload.get("comment_drafts") or []) if isinstance(payload, dict) else 0,
            "publish_logs": len(payload.get("publish_logs") or []) if isinstance(payload, dict) else 0,
        },
    }


# ==================== 命令行直接运行入口 ====================


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cloud_sync",
        description="反重力视频工坊：本地制片台 ↔ 自托管远程协作服务端双向同步 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="检测自托管远程协作服务端直连连通性与延迟")

    p_push = sub.add_parser("push", help="打包并推送指定分集至远程协作服务端（自动生成网页预览版）")
    p_push.add_argument("--episode", "-e", type=int, required=True, help="本地分集 episode_id")
    p_push.add_argument(
        "--stage",
        "-s",
        default="update",
        help="通知节点类型：frames_ready | video_360p_ready | master_ready | covers_ready | update | silent",
    )
    p_push.add_argument("--note", default="", help="附带推送通知备注（可选）")
    p_push.add_argument("--no-master", action="store_true", help="仅上传轻量 web_preview.mp4，跳过高清终版大文件")
    p_push.add_argument("--force-upload", action="store_true", help="跳过 check-file 增量校验，强制重传全部文件")

    p_pull = sub.add_parser("pull", help="从远程协作服务端拉回团队成员最新剧本修改、投票、规则建议与评论留痕")
    p_pull.add_argument("--since", default="", help="增量时间起点（YYYY-MM-DD HH:MM:SS），留空自动使用上次游标")

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            res = check_cloud_status()
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1

        if args.command == "push":
            res = push_episode_to_cloud(
                episode_id=args.episode,
                notify_stage=args.stage,
                include_final_master=not args.no_master,
                notify_note=args.note,
                force_upload=args.force_upload,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.command == "pull":
            res = pull_changes_from_cloud(since=args.since)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
