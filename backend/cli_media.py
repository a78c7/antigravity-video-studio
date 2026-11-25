"""
Google Gemini 官方双通道媒体生成引擎（优先官方 `google-genai` Python SDK 直连，100% 完整保留 `gemini-api` CLI 通道）：
- 生图 / 首尾帧 / 封面 / 棚拍洗图（1K / 2K / 4K，支持多参考图与宽高比 `9:16` / `16:9` / `3:4`，
  兼容 `interactions.create` 与 `models.generate_content(response_modalities=["IMAGE"])` 及 `models.generate_images` 自动适配）；
- 生视频（支持 `gemini-omni-1.2-flash` 的 `interactions.create(background=True)` 异步轮询，
  以及 `veo-3.1-*` / `veo-2.0-*` 的 `models.generate_videos` + `operations.get` + `files.download`，
  支持首帧 / 尾帧 / 参考图输入与进度/取消回调）；
- 原创配乐 BGM（支持 `lyria-3.5` 等模型，自动识别 WAV / MP3 / OGG / FLAC 或封装标准 WAV）；
- 中文旁白 TTS（支持 `gemini-3.8-flash-tts` / `gemini-2.5-flash-preview-tts` 等，`speech_config` + 导演语气指引，
  若返回原始 PCM 自动使用 Python 标准库 `wave` 封装为标准 24kHz 单声道 WAV）。

引擎路由策略（由设置项 `MEDIA_ENGINE_MODE` 控制）：
- `auto`（默认）：若配置了 `GEMINI_API_KEY` 且已安装 `google-genai` SDK，优先走官方 SDK 直连（若选定模型仅 CLI 支持且本机装了 CLI，自动平滑回退 CLI）；
  未填 Key 但已装 `gemini-api` CLI 时自动走原 CLI 通道；二者皆未就绪时抛出清晰中文引导报错；
- `sdk`：强制走官方 `google-genai` SDK 直连（需配置 `GEMINI_API_KEY`）；
- `cli`：强制走原 `gemini-api` CLI 通道（通过 CLI 自身 keyring 鉴权）。

安全红线：
- SDK 直连通过 `net.make_google_sync_client` + `types.HttpOptions(httpx_client=...)` 精准注入 `GOOGLE_PROXY_URL`，绝不污染非 Google 流量；
- CLI 通道剥离环境变量明文密钥，完全保留原有 keyring 鉴权逻辑；
- 错误输出强制全模式密钥脱敏，瞬时 429 / 5xx / 超时 / 代理断连内置退避重试，400 / 401 / 403 / 余额不足 / 安全拦截绝不重试。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import httpx

try:
    from google import genai
    from google.genai import types
except ImportError:  # 自托管极简协作模式可不安装 google-genai
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

from .config import (
    DEFAULTS,
    DEFAULT_CLI_PRICING,
    find_gemini_cli_bin,
    get_cli_pricing,
    get_media_engine_mode,
    is_cloud_collab,
    is_gemini_cli_available,
    load_config,
)
from .db import db_ctx, now_str
from .media_ops import probe_media
from .net import make_google_sync_client

ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], Any]

# 默认模型常量（向后兼容导出；实际运行时均通过 _resolve_cli_runtime() 从配置动态读取）
IMAGE_MODEL = DEFAULTS["CLI_IMAGE_MODEL"]
VIDEO_MODEL = DEFAULTS["CLI_VIDEO_MODEL"]
MUSIC_MODEL = DEFAULTS["CLI_MUSIC_MODEL"]
TTS_MODEL = DEFAULTS["CLI_TTS_MODEL"]

# 默认参考单价（实际计费均通过 get_cli_pricing() 合并用户 CLI_PRICING_JSON 动态读取）
IMAGE_COST_USD: dict[str, float] = dict(DEFAULT_CLI_PRICING["image"])
VIDEO_COST_PER_SEC_USD: dict[str, float] = dict(DEFAULT_CLI_PRICING["video_per_sec"])
MUSIC_COST_USD = float(DEFAULT_CLI_PRICING["music"])
TTS_COST_EST_USD = float(DEFAULT_CLI_PRICING["tts"])

# 瞬时网络异常轻量重试参数
TRANSIENT_RETRY_MAX = 1
TRANSIENT_RETRY_BACKOFF_SEC = 3.0
SDK_TRANSIENT_RETRY_MAX = 2

# 明确不可重试的错误特征（参数错误 400、鉴权失败、余额/配额用尽、安全拦截等）
_NON_RETRYABLE_PATTERNS: tuple[str, ...] = (
    r"\b400\b",
    r"\b401\b",
    r"\b402\b",
    r"\b403\b",
    r"bad request",
    r"invalid[_\s-]?argument",
    r"unknown flag",
    r"missing required",
    r"unauthenticated",
    r"unauthorized",
    r"permission[_\s-]?denied",
    r"forbidden",
    r"api[_\s-]?key[_\s-]?(not valid|invalid)",
    r"payment required",
    r"billing",
    r"insufficient",
    r"out of credit",
    r"credit exhausted",
    r"budget exceeded",
    r"spend cap",
    r"exceeded your current quota",
    r"quota exceeded",
    r"余额不足",
    r"prohibited[_\s-]?content",
    r"blocked by",
    r"content policy",
    r"safety",
)

# 允许自动重试的瞬时网络/限流/服务端异常特征
_RETRYABLE_PATTERNS: tuple[str, ...] = (
    r"\b429\b",
    r"too many requests",
    r"rate[_\s-]?limit",
    r"resource[_\s-]?exhausted",
    r"\b500\b",
    r"\b502\b",
    r"\b503\b",
    r"\b504\b",
    r"\b(?:http|status|code|error)[\s:=]*5\d{2}\b",
    r"internal server error",
    r"bad gateway",
    r"service unavailable",
    r"gateway timeout",
    r"server error",
    r"unavailable",
    r"overloaded",
    r"deadline[_\s-]?exceeded",
    r"timed out",
    r"timeout",
    r"connection reset",
    r"connection refused",
    r"connection aborted",
    r"broken pipe",
    r"unexpected eof",
    r"\beof\b",
    r"tls handshake",
    r"\bssl\b",
    r"proxy",
    r"temporary failure",
    r"socket hang up",
    r"reset by peer",
    r"remote closed",
    r"network is unreachable",
    r"i/o timeout",
)

# 仅因 SDK 接口或模型不兼容导致的错误特征（用于 SDK 内双路回退，或在 auto 模式下平滑回退 CLI）
_COMPAT_FALLBACK_PATTERNS: tuple[str, ...] = (
    r"\b404\b",
    r"not[_\s-]?found",
    r"unsupported",
    r"not supported",
    r"no attribute.*interactions",
    r"unrecognized",
    r"unexpected keyword",
    r"unknown field",
    r"invalid modality",
    r"response_modalities",
    r"method not allowed",
    r"\b405\b",
    r"\b501\b",
    r"not implemented",
)

# 账号/鉴权/计费/安全拦截类硬错误（绝不因回退掩盖真实原因）
_HARD_ACCOUNT_OR_POLICY_PATTERNS: tuple[str, ...] = (
    r"\b401\b",
    r"\b402\b",
    r"\b403\b",
    r"unauthenticated",
    r"unauthorized",
    r"permission[_\s-]?denied",
    r"api[_\s-]?key[_\s-]?(not valid|invalid)",
    r"payment required",
    r"billing",
    r"insufficient",
    r"out of credit",
    r"credit exhausted",
    r"budget exceeded",
    r"spend cap",
    r"quota exceeded",
    r"prohibited[_\s-]?content",
    r"blocked by",
    r"content policy",
)


# ==================== 基础工具：进度、取消、脱敏、重试与路由 ====================


def _report_progress(progress_cb: ProgressCallback | None, pct: int, msg: str) -> None:
    if progress_cb is None:
        return
    try:
        clamped = max(0, min(100, int(pct)))
        progress_cb(clamped, str(msg))
    except Exception:
        pass


def _is_cancel_exc(exc: BaseException) -> bool:
    return isinstance(exc, InterruptedError) or exc.__class__.__name__ == "JobCancelledError"


def _raise_cancelled() -> None:
    try:
        from .jobs import JobCancelledError

        raise JobCancelledError("任务已取消")
    except ImportError:
        raise InterruptedError("任务已取消")


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    """兼容传入 `ctx.check_cancelled`（直接抛异常）与 `ctx.is_cancelled`（返回 bool）两种回调"""
    if cancel_check is None:
        return
    result = cancel_check()
    if result:
        _raise_cancelled()


def _sleep_with_cancel(seconds: float, cancel_check: CancelCheck | None) -> None:
    """支持响应取消信号的分段等待"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_cancelled(cancel_check)
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        time.sleep(min(0.25, rem))


def _resolve_cli_runtime() -> tuple[dict[str, str], dict[str, Any]]:
    """实时读取配置中的媒体模型 ID 与动态单价"""
    cfg = load_config()
    models = {
        "image": (cfg.get("CLI_IMAGE_MODEL") or DEFAULTS["CLI_IMAGE_MODEL"]).strip() or DEFAULTS["CLI_IMAGE_MODEL"],
        "video": (cfg.get("CLI_VIDEO_MODEL") or DEFAULTS["CLI_VIDEO_MODEL"]).strip() or DEFAULTS["CLI_VIDEO_MODEL"],
        "music": (cfg.get("CLI_MUSIC_MODEL") or DEFAULTS["CLI_MUSIC_MODEL"]).strip() or DEFAULTS["CLI_MUSIC_MODEL"],
        "tts": (cfg.get("CLI_TTS_MODEL") or DEFAULTS["CLI_TTS_MODEL"]).strip() or DEFAULTS["CLI_TTS_MODEL"],
    }
    pricing = get_cli_pricing(cfg)
    return models, pricing


def _resolve_media_engine(cfg: dict[str, str] | None = None) -> tuple[str, bool]:
    """
    根据 `MEDIA_ENGINE_MODE` 判定当前媒体生成主通道与是否允许回退 CLI：
    返回 `(engine, can_fallback_to_cli)`，其中 `engine` 为 `'sdk'` 或 `'cli'`。
    """
    c = cfg if cfg is not None else load_config()
    if is_cloud_collab(c):
        raise RuntimeError("协作模式已禁用本地媒体生成引擎。")

    mode = get_media_engine_mode(c)
    has_key = bool((c.get("GEMINI_API_KEY") or "").strip())
    sdk_ok = genai is not None and types is not None
    cli_ok = is_gemini_cli_available()

    if mode == "sdk":
        if not sdk_ok:
            raise RuntimeError(
                "当前环境未安装官方 google-genai Python SDK，请执行 `pip install google-genai`，或在「系统设置」将媒体引擎切换为 CLI 模式。"
            )
        if not has_key:
            raise RuntimeError("当前媒体引擎模式为强制 SDK 直连（sdk），请先在系统设置填写 Gemini API Key。")
        return "sdk", False

    if mode == "cli":
        return "cli", False

    # auto 模式：已填 Key 且装有官方 SDK 时优先 SDK 直连；否则若已装 CLI 自动走原 CLI；都未就绪给出清晰引导
    if has_key and sdk_ok:
        return "sdk", cli_ok
    if cli_ok:
        return "cli", False
    if has_key and not sdk_ok:
        raise RuntimeError(
            "已配置 Gemini API Key，但当前环境未安装 google-genai SDK 且未检测到 gemini-api CLI；"
            "请先安装 google-genai（pip install google-genai），或安装官方 gemini-api CLI。"
        )
    raise RuntimeError("请先在系统设置填写 Gemini API Key，或安装官方 gemini-api CLI")


def resolve_gemini_cli_bin() -> str:
    """跨平台定位官方 gemini-api CLI 可执行文件（兼容 macOS / Windows / Linux）"""
    found = find_gemini_cli_bin()
    if found:
        return found
    if sys.platform == "win32":
        raise RuntimeError(
            "未找到官方 gemini-api CLI，请先安装并确认其在 PATH 或 ~/.local/bin 目录中，然后执行 `gemini-api auth` 完成鉴权。"
        )
    raise RuntimeError(
        "未找到官方 gemini-api CLI（~/.local/bin/gemini-api），请先安装并执行 `gemini-api auth` 绑定系统凭据库。"
    )


def _build_cli_env() -> dict[str, str]:
    """构造 CLI 子进程环境：跨平台拼接 PATH、按需注入代理、优先走官方 CLI 自身凭据库"""
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    path_val = env.get("PATH", "")
    path_parts = path_val.split(os.pathsep) if path_val else []
    if local_bin not in path_parts:
        env["PATH"] = f"{local_bin}{os.pathsep}{path_val}" if path_val else local_bin

    # 剥离可能残留在环境里的明文密钥，引导由 gemini-api 从系统凭据库（keyring）安全读取
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)

    cfg = load_config()
    proxy = (cfg.get("GOOGLE_PROXY_URL") or "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["all_proxy"] = proxy
    return env


def sanitize_cli_error(text: str) -> str:
    """对 CLI 与 SDK 错误输出做全模式密钥脱敏，避免泄露敏感凭据"""
    s = (text or "").strip()
    s = re.sub(r"AQ\.[0-9A-Za-z_\-]{12,}", "[REDACTED_KEY]", s)
    s = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED_KEY]", s)
    s = re.sub(r"sk-[0-9A-Za-z_\-]{16,}", "[REDACTED_KEY]", s)
    s = re.sub(r"ya29\.[0-9A-Za-z_\-\.]{16,}", "[REDACTED_TOKEN]", s)
    s = re.sub(r"Bearer\s+[0-9A-Za-z_\-\.]+", "Bearer [REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(
        r"(?i)\b(x-goog-api-key|api[_-]?key|access[_-]?token|key)\s*([:=])\s*['\"]?[0-9A-Za-z_\-\.]{12,}['\"]?",
        r"\1\2[REDACTED]",
        s,
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s[:420] if len(s) > 420 else s


def _is_retryable_cli_error(err_text: str, returncode: int | None = None, *, timed_out: bool = False) -> bool:
    """判断失败是否属于可安全重试的瞬时网络/限流/5xx 错误；400 参数错、鉴权错、余额不足绝不重试"""
    if returncode in (2, 3):
        return False

    lower_err = (err_text or "").lower()
    for pat in _NON_RETRYABLE_PATTERNS:
        if re.search(pat, lower_err, flags=re.IGNORECASE):
            return False

    if timed_out:
        return True

    for pat in _RETRYABLE_PATTERNS:
        if re.search(pat, lower_err, flags=re.IGNORECASE):
            return True
    return False


def _is_transient_ssl_or_proxy_err(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            ssl.SSLError,
            ConnectionResetError,
        ),
    ):
        return True
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "unexpected_eof",
            "eof occurred",
            "connection reset",
            "broken pipe",
            "remoteprotocolerror",
            "tls handshake",
        )
    )


def _is_transient_sdk_error(exc: Exception) -> bool:
    if _is_cancel_exc(exc) or isinstance(exc, ValueError):
        return False
    if _is_transient_ssl_or_proxy_err(exc):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    return _is_retryable_cli_error(sanitize_cli_error(str(exc)))


def _is_hard_account_or_policy_error(exc: Exception) -> bool:
    msg = sanitize_cli_error(str(exc)).lower()
    return any(re.search(pat, msg, flags=re.IGNORECASE) for pat in _HARD_ACCOUNT_OR_POLICY_PATTERNS)


def _is_model_or_endpoint_compat_error(exc: Exception) -> bool:
    if _is_cancel_exc(exc) or _is_hard_account_or_policy_error(exc):
        return False
    if isinstance(exc, (AttributeError, NotImplementedError, TypeError)):
        return True
    msg = sanitize_cli_error(str(exc)).lower()
    return any(re.search(pat, msg, flags=re.IGNORECASE) for pat in _COMPAT_FALLBACK_PATTERNS)


def _run_cli_with_retry(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    max_retries: int = TRANSIENT_RETRY_MAX,
    backoff_sec: float = TRANSIENT_RETRY_BACKOFF_SEC,
    expected_path: Path | None = None,
    cancel_check: CancelCheck | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行官方 gemini-api CLI，对超时/429/5xx 瞬时故障做最多 max_retries 次退避重试"""
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        _check_cancelled(cancel_check)
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt < retries and _is_retryable_cli_error("timeout", timed_out=True):
                if expected_path is not None and expected_path.exists() and expected_path.stat().st_size == 0:
                    try:
                        expected_path.unlink()
                    except OSError:
                        pass
                _sleep_with_cancel(backoff_sec, cancel_check)
                continue
            raise RuntimeError(f"CLI 调用超时（>{timeout}s），已自动重试 {attempt} 次仍未响应。")

        if proc.returncode == 0:
            return proc

        err = sanitize_cli_error(proc.stderr or proc.stdout)
        if attempt < retries and _is_retryable_cli_error(err, returncode=proc.returncode):
            if expected_path is not None and expected_path.exists() and expected_path.stat().st_size == 0:
                try:
                    expected_path.unlink()
                except OSError:
                    pass
            _sleep_with_cancel(backoff_sec, cancel_check)
            continue
        return proc

    raise RuntimeError("CLI 调用失败（重试耗尽）")


def _make_sdk_client(
    cfg: dict[str, str],
    *,
    timeout: float = 180.0,
    use_socks: bool = False,
) -> tuple[Any, httpx.Client, str]:
    """构造带精准代理隔离的官方 `google-genai` SDK 客户端（绝不污染非 Google 流量）"""
    if genai is None or types is None:
        raise RuntimeError("当前运行环境未安装官方 google-genai SDK。")
    api_key = (cfg.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("尚未配置 Gemini API Key，请在「系统设置」填写。")
    hc = make_google_sync_client(timeout=timeout, use_socks=use_socks)
    http_opts = types.HttpOptions(api_version="v1beta", httpx_client=hc)
    client = genai.Client(api_key=api_key, http_options=http_opts)
    return client, hc, api_key


def _run_sdk_with_retry(
    fn: Callable[[Any, httpx.Client, str], Any],
    cfg: dict[str, str],
    *,
    timeout: float = 180.0,
    max_retries: int = SDK_TRANSIENT_RETRY_MAX,
    backoff_sec: float = TRANSIENT_RETRY_BACKOFF_SEC,
    cancel_check: CancelCheck | None = None,
) -> Any:
    """使用官方 SDK 客户端执行调用，内置 429/5xx/超时/代理断连退避重试与 SOCKS 自动切换"""
    has_proxy = bool((cfg.get("GOOGLE_PROXY_URL") or "").strip())
    retries = max(0, int(max_retries))
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        _check_cancelled(cancel_check)
        use_socks = bool(has_proxy and attempt >= 1)
        client, hc, api_key = _make_sdk_client(cfg, timeout=timeout, use_socks=use_socks)
        try:
            return fn(client, hc, api_key)
        except Exception as exc:
            if _is_cancel_exc(exc):
                raise
            last_err = exc
            if attempt < retries and _is_transient_sdk_error(exc):
                _sleep_with_cancel(backoff_sec * (attempt + 1), cancel_check)
                continue
            raise
        finally:
            try:
                hc.close()
            except Exception:
                pass
    raise last_err or RuntimeError("官方 Google GenAI SDK 调用失败")


def record_cli_cost(model: str, action: str, est_cost_usd: float) -> None:
    """把媒体生成估算美元花费记入 api_usage_logs，驱动月度预算预警"""
    try:
        with db_ctx() as conn:
            conn.execute(
                """
                INSERT INTO api_usage_logs (provider, model, action, prompt_tokens, completion_tokens, total_tokens, est_cost_usd, created_at)
                VALUES ('gemini', ?, ?, 0, 0, 0, ?, ?)
                """,
                (model, action, round(max(0.0, float(est_cost_usd)), 6), now_str()),
            )
    except Exception:
        pass


# ==================== 输入整理与媒体字节/封装辅助 ====================


def _append_role_hint(prompt: str, hint: str) -> str:
    p = (prompt or "").strip()
    if not p:
        return hint
    sep = "" if p[-1] in ".!?" else "."
    return f"{p}{sep} {hint}"


def _prepare_ref_images(ref_paths: list[Path], limit: int = 6) -> list[Path]:
    """去重并过滤有效且 <=14MB 的本地参考图"""
    seen: set[str] = set()
    valid: list[Path] = []
    for rp in ref_paths or []:
        if len(valid) >= limit:
            break
        try:
            resolved = rp.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists() or not resolved.is_file():
            continue
        try:
            if resolved.stat().st_size > 14 * 1024 * 1024:
                continue
        except OSError:
            continue
        seen.add(key)
        valid.append(resolved)
    return valid


def _guess_image_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def _detect_image_ext(raw_bytes: bytes, mime_type: str = "") -> str:
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return ".webp"
    if raw_bytes.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    mt = (mime_type or "").lower()
    if "png" in mt:
        return ".png"
    if "webp" in mt:
        return ".webp"
    return ".jpg"


def _detect_audio_ext(path: Path) -> str:
    """根据音频文件头判断真实后缀，确保浏览器 <audio> 无缝播放"""
    try:
        head = path.read_bytes()[:12]
    except Exception:
        return ".mp3"
    if head.startswith(b"RIFF") and b"WAVE" in head:
        return ".wav"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    return ".mp3"


def _parse_pcm_params(mime_type: str, default_rate: int = 24000, default_channels: int = 1) -> tuple[int, int]:
    rate = default_rate
    channels = default_channels
    mt = (mime_type or "").lower()
    m_rate = re.search(r"rate\s*=\s*(\d+)", mt)
    if m_rate:
        try:
            parsed_r = int(m_rate.group(1))
            if 8000 <= parsed_r <= 192000:
                rate = parsed_r
        except ValueError:
            pass
    m_ch = re.search(r"channels?\s*=\s*(\d+)", mt)
    if m_ch:
        try:
            parsed_c = int(m_ch.group(1))
            if 1 <= parsed_c <= 8:
                channels = parsed_c
        except ValueError:
            pass
    return rate, channels


def _wrap_pcm_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """使用 Python 标准库 wave 将原始线性 PCM 字节流封装为标准可播放 WAV 文件"""
    payload = pcm_bytes
    frame_size = max(1, channels * sample_width)
    rem = len(payload) % frame_size
    if rem:
        payload = payload + (b"\x00" * (frame_size - rem))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(payload)
    return buf.getvalue()


def _ensure_valid_audio_bytes(
    raw_bytes: bytes,
    *,
    mime_type: str = "",
    sample_rate: int | None = None,
    channels: int | None = None,
    assume_pcm_if_raw: bool = False,
) -> tuple[bytes, str]:
    """
    识别音频容器头并返回 `(安全可播放字节, 真实扩展名)`；
    若 SDK 返回原始 PCM（如 Gemini TTS 的 24kHz 16-bit L16 PCM），自动使用标准库 `wave` 封装为标准 WAV。
    """
    if not raw_bytes:
        raise RuntimeError("模型返回的音频内容为空。")
    head = raw_bytes[:12]
    if head.startswith(b"RIFF") and b"WAVE" in head:
        return raw_bytes, ".wav"
    if head.startswith(b"OggS"):
        return raw_bytes, ".ogg"
    if head.startswith(b"fLaC"):
        return raw_bytes, ".flac"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return raw_bytes, ".mp3"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return raw_bytes, ".m4a"

    mt = (mime_type or "").lower()
    is_pcm_mime = any(k in mt for k in ("audio/l16", "audio/pcm", "audio/raw", "linear16"))
    if is_pcm_mime or assume_pcm_if_raw:
        parsed_rate, parsed_ch = _parse_pcm_params(
            mt,
            default_rate=int(sample_rate or 24000),
            default_channels=int(channels or 1),
        )
        wav_bytes = _wrap_pcm_to_wav(raw_bytes, sample_rate=parsed_rate, channels=parsed_ch, sample_width=2)
        return wav_bytes, ".wav"

    if "mpeg" in mt or "mp3" in mt:
        return raw_bytes, ".mp3"
    if "wav" in mt:
        parsed_rate, parsed_ch = _parse_pcm_params(
            mt,
            default_rate=int(sample_rate or 24000),
            default_channels=int(channels or 1),
        )
        return _wrap_pcm_to_wav(raw_bytes, sample_rate=parsed_rate, channels=parsed_ch, sample_width=2), ".wav"
    return raw_bytes, ".mp3"


def _decode_media_data(raw_data: Any) -> bytes | None:
    if raw_data is None:
        return None
    if isinstance(raw_data, (bytes, bytearray)):
        return bytes(raw_data) if len(raw_data) > 0 else None
    if isinstance(raw_data, str):
        s = raw_data.strip()
        if not s:
            return None
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1].strip()
        try:
            decoded = base64.b64decode(s)
            return decoded if decoded else None
        except Exception:
            return None
    return None


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _download_media_uri(uri: str, client: Any, hc: httpx.Client, api_key: str) -> bytes:
    """下载 Interactions / Files API 返回的媒体 URI 或文件资源名（严格复用 Google 代理客户端）"""
    clean_uri = (uri or "").strip()
    if not clean_uri:
        raise RuntimeError("媒体 URI 为空，无法下载。")
    if clean_uri.startswith(("http://", "https://")):
        headers = {"x-goog-api-key": api_key} if api_key else {}
        resp = hc.get(clean_uri, headers=headers, follow_redirects=True)
        if resp.status_code in (401, 403) and hasattr(client, "files"):
            dl = client.files.download(file=clean_uri)
            if isinstance(dl, (bytes, bytearray)) and dl:
                return bytes(dl)
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError("下载到的媒体文件为空。")
        return resp.content

    if hasattr(client, "files"):
        dl = client.files.download(file=clean_uri)
        if isinstance(dl, (bytes, bytearray)) and dl:
            return bytes(dl)
    raise RuntimeError(f"无法下载媒体资源：{clean_uri[:120]}")


def _build_interaction_input(prompt: str, image_paths: list[Path]) -> Any:
    """按官方 v1beta/interactions 规范构造文本或多参考图输入"""
    if not image_paths:
        return prompt
    blocks: list[dict[str, Any]] = []
    for p in image_paths:
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(p.read_bytes()).decode("ascii"),
                "mime_type": _guess_image_mime(p),
            }
        )
    blocks.append({"type": "text", "text": prompt})
    return [{"type": "user_input", "content": blocks}]


def _extract_interaction_error(interaction: Any) -> str:
    errors = _get_field(interaction, "errors")
    if errors:
        parts: list[str] = []
        items = errors if isinstance(errors, list) else [errors]
        for err in items:
            msg = _get_field(err, "message") or _get_field(err, "detail") or str(err)
            code = _get_field(err, "code")
            parts.append(f"[{code}] {msg}" if code else str(msg))
        return sanitize_cli_error("; ".join(parts))
    status = str(_get_field(interaction, "status") or "unknown")
    return f"Interaction 状态异常：{status}"


def _extract_interaction_media(
    interaction: Any,
    media_kind: str,
    client: Any,
    hc: httpx.Client,
    api_key: str,
) -> tuple[bytes, str, dict[str, Any]]:
    """从 Interaction 响应提取指定媒体（image / video / audio）字节与元信息"""
    status = str(_get_field(interaction, "status") or "").lower()
    if status in ("failed", "cancelled"):
        raise RuntimeError(_extract_interaction_error(interaction))

    candidate = _get_field(interaction, f"output_{media_kind}")

    if candidate is None:
        # 兼容扫描 steps[*].content[*] 与旧版 outputs[*]
        steps = _get_field(interaction, "steps") or []
        for step in reversed(steps):
            content = _get_field(step, "content")
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if str(_get_field(block, "type") or "").lower() == media_kind:
                    candidate = block
                    break
            if candidate is not None:
                break

    if candidate is None:
        outputs = _get_field(interaction, "outputs") or []
        for block in reversed(outputs):
            if str(_get_field(block, "type") or "").lower() == media_kind:
                candidate = block
                break

    if candidate is None:
        err_text = _extract_interaction_error(interaction)
        out_text = str(_get_field(interaction, "output_text") or "").strip()
        hint = sanitize_cli_error(out_text) if out_text else err_text
        raise RuntimeError(f"模型未返回 {media_kind} 媒体数据：{hint}")

    mime_type = str(_get_field(candidate, "mime_type") or "")
    extra_meta = {
        "sample_rate": _get_field(candidate, "sample_rate"),
        "channels": _get_field(candidate, "channels"),
    }

    raw_bytes = _decode_media_data(_get_field(candidate, "data"))
    if raw_bytes is None:
        uri = str(_get_field(candidate, "uri") or _get_field(candidate, "name") or "").strip()
        if uri:
            raw_bytes = _download_media_uri(uri, client, hc, api_key)

    if not raw_bytes:
        raise RuntimeError(f"模型返回的 {media_kind} 媒体内容为空。")

    return raw_bytes, mime_type, extra_meta


def _extract_generate_content_inline(resp: Any, mime_prefix: str) -> tuple[bytes, str]:
    """从 models.generate_content 响应中提取 inline_data 媒体字节"""
    cands = getattr(resp, "candidates", None) or []
    for cand in cands:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            mime = str(getattr(inline, "mime_type", "") or "")
            raw = _decode_media_data(getattr(inline, "data", None))
            if raw and (not mime_prefix or mime.lower().startswith(mime_prefix) or not mime):
                return raw, mime
    text_hint = sanitize_cli_error(str(getattr(resp, "text", "") or ""))
    raise RuntimeError(f"模型未返回 {mime_prefix} 媒体数据{('：' + text_hint) if text_hint else '。'}")


def _poll_interaction(
    client: Any,
    interaction: Any,
    *,
    timeout_sec: int = 600,
    poll_interval: float = 5.0,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    label: str = "云端媒体渲染",
    pct_start: int = 15,
    pct_end: int = 90,
) -> Any:
    """轮询异步后台 Interaction 直到完成，支持取消回调、实时进度回报与轮询瞬时断连容错"""
    status = str(_get_field(interaction, "status") or "").lower()
    if status == "completed":
        return interaction
    if status in ("failed", "cancelled"):
        raise RuntimeError(_extract_interaction_error(interaction))

    interaction_id = str(_get_field(interaction, "id") or "").strip()
    if not interaction_id:
        return interaction

    t0 = time.monotonic()
    consecutive_errors = 0
    while True:
        elapsed = int(time.monotonic() - t0)
        if elapsed > timeout_sec:
            raise RuntimeError(f"{label}等待超时（>{timeout_sec}s），任务 ID：{interaction_id}")

        ratio = min(1.0, elapsed / max(60.0, float(timeout_sec) * 0.45))
        pct = pct_start + int((pct_end - pct_start) * ratio)
        _report_progress(progress_cb, pct, f"{label}进行中（已等待 {elapsed}s）...")

        try:
            _sleep_with_cancel(poll_interval, cancel_check)
        except Exception as cancel_exc:
            if _is_cancel_exc(cancel_exc):
                try:
                    if hasattr(client.interactions, "cancel"):
                        client.interactions.cancel(id=interaction_id)
                except Exception:
                    pass
            raise

        try:
            interaction = client.interactions.get(id=interaction_id)
            consecutive_errors = 0
        except Exception as poll_exc:
            if _is_cancel_exc(poll_exc):
                raise
            consecutive_errors += 1
            if consecutive_errors <= 4 and _is_transient_sdk_error(poll_exc):
                continue
            raise RuntimeError(f"查询{label}进度失败：{sanitize_cli_error(str(poll_exc))}")

        status = str(_get_field(interaction, "status") or "").lower()
        if status == "completed":
            return interaction
        if status in ("failed", "cancelled"):
            raise RuntimeError(_extract_interaction_error(interaction))


def _prepare_video_inputs(
    prompt: str,
    start_frame: Path | None,
    end_frame: Path | None,
    ref_paths: list[Path],
    resolution: str,
    duration_sec: int,
    vid_pricing: dict[str, float],
) -> tuple[str, Path | None, Path | None, list[Path], int, str]:
    """统一整理视频提示词、首尾帧/参考图、有效时长与分辨率（SDK 与 CLI 双通道共用）"""
    res = (resolution or "360p").strip().lower()
    if res not in vid_pricing:
        res = "360p"

    valid_sf = start_frame.expanduser().resolve() if (start_frame and start_frame.exists()) else None
    valid_ef = end_frame.expanduser().resolve() if (end_frame and end_frame.exists()) else None

    dur = max(3, min(15, int(duration_sec or 6)))
    if valid_sf and valid_ef and dur > 10:
        dur = 10

    final_prompt = (prompt or "").strip()
    lower_p = final_prompt.lower()
    input_imgs: list[Path] = []

    if valid_sf and valid_ef:
        input_imgs = [valid_sf, valid_ef]
        if "first frame" not in lower_p and "starting frame" not in lower_p:
            final_prompt = _append_role_hint(
                final_prompt,
                "Use the first image as the starting frame and the second image as the final frame.",
            )
    elif valid_sf:
        input_imgs = [valid_sf]
        if "first frame" not in lower_p and "starting frame" not in lower_p:
            final_prompt = _append_role_hint(final_prompt, "Use this image as the starting frame.")
    else:
        input_imgs = _prepare_ref_images(ref_paths, limit=6)

    return final_prompt, valid_sf, valid_ef, input_imgs, dur, res


def _find_created_output(expected: Path, started_at: float) -> Path | None:
    if expected.exists() and expected.stat().st_size > 0:
        return expected
    parent = expected.parent
    if parent.exists():
        matches = [
            p
            for p in parent.iterdir()
            if p.is_file() and p.stem.startswith(expected.stem) and p.stat().st_mtime >= started_at - 2 and p.stat().st_size > 0
        ]
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
    return None


# ==================== 原官方 `gemini-api` CLI 命令构造器（100% 保留） ====================


def build_music_cmd(prompt: str, out_path: Path, *, dry_run: bool = False) -> list[str]:
    models, _ = _resolve_cli_runtime()
    cmd = [
        resolve_gemini_cli_bin(),
        "music",
        prompt.strip(),
        "-m",
        models["music"],
        "--out",
        str(out_path),
        "-o",
        "json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def build_tts_cmd(
    text: str,
    out_path: Path,
    voice: str = "Kore",
    style_prompt: str = "",
    language: str = "zh-CN",
    *,
    dry_run: bool = False,
) -> list[str]:
    """构造官方 gemini-api tts 24kHz WAV 语音合成命令"""
    models, _ = _resolve_cli_runtime()
    clean_text = (text or "").strip()
    used_voice = (voice or "Kore").strip() or "Kore"
    clean_style = (style_prompt or "").strip()
    clean_lang = (language or "zh-CN").strip()

    flags: list[str] = [
        "-m",
        models["tts"],
        "--voice",
        used_voice,
    ]
    if clean_lang:
        flags.extend(["--language", clean_lang])
    if clean_style:
        flags.extend(["--style", clean_style])
    flags.extend(
        [
            "--out",
            str(out_path),
            "-o",
            "json",
        ]
    )
    if dry_run:
        flags.append("--dry-run")

    if clean_text.startswith("-"):
        return [resolve_gemini_cli_bin(), "tts", *flags, "--", clean_text]
    return [resolve_gemini_cli_bin(), "tts", clean_text, *flags]


def build_image_cmd(
    prompt: str,
    ref_paths: list[Path],
    aspect_ratio: str,
    image_size: str,
    out_path: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    models, pricing = _resolve_cli_runtime()
    img_pricing: dict[str, float] = pricing.get("image") or DEFAULT_CLI_PRICING["image"]
    size = image_size.upper() if image_size else "2K"
    if size not in img_pricing:
        size = "2K"
    cmd = [
        resolve_gemini_cli_bin(),
        "image",
        prompt.strip(),
        "-m",
        models["image"],
        "--aspect-ratio",
        (aspect_ratio or "9:16").strip(),
        "--image-size",
        size,
        "--output-mime-type",
        "image/jpeg",
        "--out",
        str(out_path),
        "-o",
        "json",
    ]
    for resolved in _prepare_ref_images(ref_paths, limit=6):
        cmd.extend(["-i", str(resolved)])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def build_video_cmd(
    prompt: str,
    start_frame: Path | None,
    end_frame: Path | None,
    ref_paths: list[Path],
    aspect_ratio: str,
    resolution: str,
    duration_sec: int,
    out_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[list[str], int, str]:
    """
    构造官方 gemini-api video 命令：
    - 若同时有首尾帧：仅传 `-i start -i end`，并在提示词末补齐首尾帧角色说明，时长自动约束在 3–10s；
    - 若仅有首帧：传 `-i start`，并在提示词末补齐首帧角色说明，时长 3–15s；
    - 若无首帧：最多传 6 张绑定资产图作为参考。
    """
    models, pricing = _resolve_cli_runtime()
    vid_pricing: dict[str, float] = pricing.get("video_per_sec") or DEFAULT_CLI_PRICING["video_per_sec"]
    final_prompt, _, _, input_imgs, dur, res = _prepare_video_inputs(
        prompt, start_frame, end_frame, ref_paths, resolution, duration_sec, vid_pricing
    )

    body_json = json.dumps({"response_format": {"duration": f"{dur}s"}}, ensure_ascii=False)
    cmd = [
        resolve_gemini_cli_bin(),
        "video",
        final_prompt,
        "-m",
        models["video"],
        "--aspect-ratio",
        (aspect_ratio or "9:16").strip(),
        "--resolution",
        res,
        "--body",
        body_json,
        "--poll-timeout",
        "10m",
        "--out",
        str(out_path),
        "-o",
        "json",
    ]
    for img_p in input_imgs:
        cmd.extend(["-i", str(img_p)])
    if dry_run:
        cmd.append("--dry-run")
    return cmd, dur, res


# ==================== 官方 `google-genai` SDK 直连四类媒体实现 ====================


def _sdk_generate_music_file(
    prompt: str,
    model: str,
    out_path: Path,
    cfg: dict[str, str],
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """使用官方 google-genai SDK 生成原创配乐（优先 interactions.create，回退 generate_content AUDIO）"""

    def _call(client: Any, hc: httpx.Client, api_key: str) -> tuple[bytes, str, dict[str, Any]]:
        _check_cancelled(cancel_check)
        if hasattr(client, "interactions"):
            try:
                _report_progress(progress_cb, 20, f"SDK 正在提交原创配乐任务（{model}）...")
                interaction = client.interactions.create(
                    model=model,
                    input=prompt,
                    response_format={"type": "audio"},
                    stream=False,
                )
                status = str(_get_field(interaction, "status") or "").lower()
                if status and status not in ("completed", "failed", "cancelled"):
                    interaction = _poll_interaction(
                        client,
                        interaction,
                        timeout_sec=240,
                        poll_interval=4.0,
                        progress_cb=progress_cb,
                        cancel_check=cancel_check,
                        label="原创配乐生成",
                        pct_start=30,
                        pct_end=90,
                    )
                return _extract_interaction_media(interaction, "audio", client, hc, api_key)
            except Exception as exc:
                if not _is_model_or_endpoint_compat_error(exc):
                    raise

        _report_progress(progress_cb, 35, f"SDK 正在通过 generate_content 生成配乐（{model}）...")
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=["AUDIO"]),
        )
        raw_bytes, mime = _extract_generate_content_inline(resp, "audio/")
        return raw_bytes, mime, {}

    raw_bytes, mime_type, meta = _run_sdk_with_retry(_call, cfg, timeout=240.0, cancel_check=cancel_check)
    final_bytes, real_ext = _ensure_valid_audio_bytes(
        raw_bytes,
        mime_type=mime_type,
        sample_rate=meta.get("sample_rate"),
        channels=meta.get("channels"),
        assume_pcm_if_raw=False,
    )
    actual = out_path.with_suffix(real_ext)
    actual.write_bytes(final_bytes)
    return actual


def _sdk_generate_tts_file(
    text: str,
    model: str,
    voice: str,
    style_prompt: str,
    language: str,
    out_path: Path,
    cfg: dict[str, str],
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """
    使用官方 google-genai SDK 生成中文旁白 TTS（输出标准 24kHz WAV）：
    - `gemini-3.8-flash-tts` 等优先走 `interactions.create`（带 `speech_config` 与 `speech_metadata` 语气标注），失败且为兼容性错误时平滑回退 `generate_content`；
    - `gemini-2.5-flash-preview-tts` 等标准 AI Studio TTS 模型优先走 `models.generate_content(response_modalities=["AUDIO"], speech_config=...)`；
    - 返回原始 PCM 时自动用 Python 标准库 `wave` 封装为有效 24kHz 单声道 WAV 文件。
    """
    clean_style = (style_prompt or "").strip()
    clean_lang = (language or "zh-CN").strip()
    prefer_generate_content = ("preview-tts" in model.lower()) or ("2.5-" in model.lower())

    def _run_interactions_tts(client: Any, hc: httpx.Client, api_key: str) -> tuple[bytes, str, dict[str, Any]]:
        content_item: dict[str, Any] = {"type": "text", "text": text}
        if clean_style:
            content_item["annotations"] = [{"type": "speech_metadata", "style": clean_style}]
        speech_entry: dict[str, Any] = {"voice": voice}
        if clean_lang:
            speech_entry["language"] = clean_lang
        interaction = client.interactions.create(
            model=model,
            input=[{"type": "user_input", "content": [content_item]}],
            generation_config={"speech_config": [speech_entry]},
            response_format={"type": "audio"},
            store=False,
            stream=False,
        )
        return _extract_interaction_media(interaction, "audio", client, hc, api_key)

    def _run_generate_content_tts(client: Any) -> tuple[bytes, str, dict[str, Any]]:
        tts_input = f"请用以下导演语气自然朗读下方台词（只读台词本身）：\n语气：{clean_style}\n台词：{text}" if clean_style else text
        voice_cfg = types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        speech_kwargs: dict[str, Any] = {"voice_config": voice_cfg}
        if clean_lang and "language_code" in getattr(types.SpeechConfig, "model_fields", {}):
            speech_kwargs["language_code"] = clean_lang
        try:
            resp = client.models.generate_content(
                model=model,
                contents=tts_input,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(**speech_kwargs),
                ),
            )
        except Exception as inner_exc:
            if "language_code" in speech_kwargs and "language" in str(inner_exc).lower():
                resp = client.models.generate_content(
                    model=model,
                    contents=tts_input,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(voice_config=voice_cfg),
                    ),
                )
            else:
                raise
        raw_bytes, mime = _extract_generate_content_inline(resp, "audio/")
        return raw_bytes, mime, {}

    def _call(client: Any, hc: httpx.Client, api_key: str) -> tuple[bytes, str, dict[str, Any]]:
        _check_cancelled(cancel_check)
        _report_progress(progress_cb, 25, f"SDK 正在合成旁白（{model} · {voice}）...")
        has_interactions = hasattr(client, "interactions")

        if prefer_generate_content or not has_interactions:
            try:
                return _run_generate_content_tts(client)
            except Exception as exc:
                if has_interactions and _is_model_or_endpoint_compat_error(exc):
                    return _run_interactions_tts(client, hc, api_key)
                raise

        try:
            return _run_interactions_tts(client, hc, api_key)
        except Exception as exc:
            if _is_model_or_endpoint_compat_error(exc):
                return _run_generate_content_tts(client)
            raise

    raw_bytes, mime_type, meta = _run_sdk_with_retry(_call, cfg, timeout=120.0, cancel_check=cancel_check)
    final_bytes, real_ext = _ensure_valid_audio_bytes(
        raw_bytes,
        mime_type=mime_type,
        sample_rate=meta.get("sample_rate") or 24000,
        channels=meta.get("channels") or 1,
        assume_pcm_if_raw=True,
    )
    actual = out_path.with_suffix(real_ext)
    actual.write_bytes(final_bytes)
    return actual


def _sdk_generate_single_image(
    prompt: str,
    valid_refs: list[Path],
    aspect_ratio: str,
    image_size: str,
    model: str,
    out_path: Path,
    cfg: dict[str, str],
    *,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """
    使用官方 google-genai SDK 生成单张图片（支持多参考图、宽高比与分辨率）：
    - `gemini-nano-banana-*` / `gemini-3.1-flash-lite-image` 等优先 `interactions.create`，不兼容时自动回退 `models.generate_content(response_modalities=["IMAGE"])`；
    - `gemini-2.5-flash-image*` / `gemini-3.1-flash-image*` 等标准 AI Studio 模型优先 `models.generate_content(response_modalities=["IMAGE"])`，必要时回退 `interactions.create`；
    - `imagen-*` 专用生图模型优先 `models.generate_images`。
    """
    m_low = model.lower()
    is_imagen = m_low.startswith("imagen")
    prefer_interactions = ("nano-banana" in m_low) or ("flash-lite-image" in m_low)

    def _run_interactions_image(client: Any, hc: httpx.Client, api_key: str) -> tuple[bytes, str]:
        interaction = client.interactions.create(
            model=model,
            input=_build_interaction_input(prompt, valid_refs),
            response_format={
                "type": "image",
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "mime_type": "image/jpeg",
            },
            stream=False,
        )
        raw_bytes, mime, _ = _extract_interaction_media(interaction, "image", client, hc, api_key)
        return raw_bytes, mime

    def _run_generate_content_image(client: Any) -> tuple[bytes, str]:
        parts: list[Any] = []
        for rp in valid_refs:
            parts.append(types.Part.from_bytes(data=rp.read_bytes(), mime_type=_guess_image_mime(rp)))
        parts.append(prompt)

        img_cfg_kwargs: dict[str, Any] = {"aspect_ratio": aspect_ratio}
        if image_size in ("1K", "2K", "4K") and hasattr(types, "ImageConfig"):
            if "image_size" in getattr(types.ImageConfig, "model_fields", {}):
                img_cfg_kwargs["image_size"] = image_size

        def _invoke(ic_kwargs: dict[str, Any] | None) -> Any:
            gen_kwargs: dict[str, Any] = {"response_modalities": ["IMAGE"]}
            if ic_kwargs and hasattr(types, "ImageConfig"):
                gen_kwargs["image_config"] = types.ImageConfig(**ic_kwargs)
            return client.models.generate_content(
                model=model,
                contents=parts if len(parts) > 1 else prompt,
                config=types.GenerateContentConfig(**gen_kwargs),
            )

        try:
            resp = _invoke(img_cfg_kwargs)
        except Exception as inner_exc:
            msg = str(inner_exc).lower()
            if "image_size" in img_cfg_kwargs and ("image_size" in msg or "imageconfig" in msg or "invalid" in msg):
                try:
                    resp = _invoke({"aspect_ratio": aspect_ratio})
                except Exception:
                    resp = _invoke(None)
            elif "image_config" in msg or "aspect_ratio" in msg:
                resp = _invoke(None)
            else:
                raise
        return _extract_generate_content_inline(resp, "image/")

    def _run_imagen(client: Any) -> tuple[bytes, str]:
        resp = client.models.generate_images(
            model=model,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
                output_mime_type="image/jpeg",
            ),
        )
        gen_imgs = getattr(resp, "generated_images", None) or []
        if not gen_imgs:
            raise RuntimeError("Imagen 未返回图片数据。")
        img_obj = getattr(gen_imgs[0], "image", None)
        raw = _decode_media_data(getattr(img_obj, "image_bytes", None))
        if not raw:
            raise RuntimeError("Imagen 返回的图片字节为空。")
        return raw, str(getattr(img_obj, "mime_type", "image/jpeg") or "image/jpeg")

    def _call(client: Any, hc: httpx.Client, api_key: str) -> tuple[bytes, str]:
        _check_cancelled(cancel_check)
        has_interactions = hasattr(client, "interactions")

        if is_imagen and not valid_refs:
            try:
                return _run_imagen(client)
            except Exception as exc:
                if _is_model_or_endpoint_compat_error(exc):
                    return _run_generate_content_image(client)
                raise

        if prefer_interactions and has_interactions:
            try:
                return _run_interactions_image(client, hc, api_key)
            except Exception as exc:
                if _is_model_or_endpoint_compat_error(exc):
                    return _run_generate_content_image(client)
                raise

        try:
            return _run_generate_content_image(client)
        except Exception as exc:
            if has_interactions and _is_model_or_endpoint_compat_error(exc):
                return _run_interactions_image(client, hc, api_key)
            raise

    raw_bytes, mime_type = _run_sdk_with_retry(_call, cfg, timeout=180.0, cancel_check=cancel_check)
    real_ext = _detect_image_ext(raw_bytes, mime_type)
    actual = out_path.with_suffix(real_ext)
    actual.write_bytes(raw_bytes)
    return actual


def _sdk_generate_single_video(
    prompt: str,
    start_frame: Path | None,
    end_frame: Path | None,
    ref_paths: list[Path],
    aspect_ratio: str,
    resolution: str,
    duration_sec: int,
    model: str,
    out_path: Path,
    cfg: dict[str, str],
    vid_pricing: dict[str, float],
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[Path, int, str]:
    """
    使用官方 google-genai SDK 生成单条视频：
    - `veo-*`（如 `veo-3.1-*` / `veo-2.0-*`）：走 `models.generate_videos` + `operations.get` 异步轮询 + `files.download`；
    - `gemini-omni-*` 等 Interactions 视频模型：走 `interactions.create(background=True)` + `interactions.get` 异步轮询。
    """
    final_prompt, valid_sf, valid_ef, input_imgs, used_dur, used_res = _prepare_video_inputs(
        prompt, start_frame, end_frame, ref_paths, resolution, duration_sec, vid_pricing
    )
    m_low = model.lower()
    is_veo = m_low.startswith("veo")

    def _run_veo_video(client: Any, hc: httpx.Client, api_key: str) -> Path:
        _check_cancelled(cancel_check)
        _report_progress(progress_cb, 12, f"SDK 正在提交 Veo 视频任务（{model}）...")

        veo_dur = 8 if (valid_sf and valid_ef) else max(4, min(8, used_dur))
        cfg_kwargs: dict[str, Any] = {
            "aspect_ratio": aspect_ratio,
            "duration_seconds": veo_dur,
        }
        if used_res in ("720p", "1080p", "4k"):
            cfg_kwargs["resolution"] = used_res

        first_image = types.Image.from_file(location=str(valid_sf)) if valid_sf else None
        if valid_sf and valid_ef:
            cfg_kwargs["last_frame"] = types.Image.from_file(location=str(valid_ef))
        elif not valid_sf and input_imgs and hasattr(types, "VideoGenerationReferenceImage"):
            ref_objs = [
                types.VideoGenerationReferenceImage(
                    image=types.Image.from_file(location=str(p)),
                    reference_type="asset",
                )
                for p in input_imgs[:3]
            ]
            if ref_objs:
                cfg_kwargs["reference_images"] = ref_objs

        def _submit(kwargs_dict: dict[str, Any]) -> Any:
            call_args: dict[str, Any] = {
                "model": model,
                "prompt": final_prompt,
                "config": types.GenerateVideosConfig(**kwargs_dict),
            }
            if first_image is not None:
                call_args["image"] = first_image
            return client.models.generate_videos(**call_args)

        try:
            operation = _submit(cfg_kwargs)
        except Exception as submit_exc:
            msg = str(submit_exc).lower()
            if "resolution" in cfg_kwargs and "resolution" in msg:
                retry_kwargs = dict(cfg_kwargs)
                retry_kwargs.pop("resolution", None)
                operation = _submit(retry_kwargs)
            else:
                raise

        t0 = time.monotonic()
        poll_timeout = 600
        consecutive_errors = 0
        while not getattr(operation, "done", False):
            elapsed = int(time.monotonic() - t0)
            if elapsed > poll_timeout:
                raise RuntimeError(f"Veo 视频生成轮询超时（>{poll_timeout}s）。")
            ratio = min(1.0, elapsed / 240.0)
            _report_progress(progress_cb, 15 + int(75 * ratio), f"Veo 视频云端渲染中（已等待 {elapsed}s）...")
            _sleep_with_cancel(6.0, cancel_check)
            try:
                operation = client.operations.get(operation)
                consecutive_errors = 0
            except Exception as poll_exc:
                if _is_cancel_exc(poll_exc):
                    raise
                consecutive_errors += 1
                if consecutive_errors <= 4 and _is_transient_sdk_error(poll_exc):
                    continue
                raise RuntimeError(f"查询 Veo 视频任务进度失败：{sanitize_cli_error(str(poll_exc))}")

        op_err = getattr(operation, "error", None)
        if op_err:
            raise RuntimeError(f"Veo 视频生成失败：{sanitize_cli_error(str(op_err))}")

        resp = getattr(operation, "response", None) or getattr(operation, "result", None)
        gen_videos = getattr(resp, "generated_videos", None) or []
        if not gen_videos:
            raise RuntimeError("Veo 任务已完成，但未返回任何视频文件（可能触发内容安全策略）。")

        _report_progress(progress_cb, 92, "正在下载生成的视频文件...")
        vid_obj = getattr(gen_videos[0], "video", None)
        if vid_obj is None:
            raise RuntimeError("Veo 返回的视频对象为空。")

        dl_bytes = client.files.download(file=vid_obj)
        if isinstance(dl_bytes, (bytes, bytearray)) and dl_bytes:
            out_path.write_bytes(bytes(dl_bytes))
            return out_path
        vid_bytes = _decode_media_data(getattr(vid_obj, "video_bytes", None))
        if vid_bytes:
            out_path.write_bytes(vid_bytes)
            return out_path
        if hasattr(vid_obj, "save"):
            vid_obj.save(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 0:
                return out_path
        uri = str(getattr(vid_obj, "uri", "") or "").strip()
        if uri:
            out_path.write_bytes(_download_media_uri(uri, client, hc, api_key))
            return out_path
        raise RuntimeError("Veo 视频下载失败：未获取到有效视频字节流。")

    def _run_interactions_video(client: Any, hc: httpx.Client, api_key: str) -> Path:
        _check_cancelled(cancel_check)
        if not hasattr(client, "interactions"):
            raise AttributeError("当前 google-genai SDK 版本不支持 interactions 接口")
        _report_progress(progress_cb, 12, f"SDK 正在提交视频渲染任务（{model} · {used_res} · {used_dur}s）...")
        interaction = client.interactions.create(
            model=model,
            input=_build_interaction_input(final_prompt, input_imgs),
            background=True,
            response_format={
                "type": "video",
                "aspect_ratio": aspect_ratio,
                "resolution": used_res,
                "duration": f"{used_dur}s",
            },
            stream=False,
        )
        interaction = _poll_interaction(
            client,
            interaction,
            timeout_sec=600,
            poll_interval=5.0,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            label=f"视频渲染 ({model})",
            pct_start=15,
            pct_end=90,
        )
        _report_progress(progress_cb, 92, "正在拉取生成的视频文件...")
        raw_bytes, _, _ = _extract_interaction_media(interaction, "video", client, hc, api_key)
        out_path.write_bytes(raw_bytes)
        return out_path

    def _call(client: Any, hc: httpx.Client, api_key: str) -> Path:
        if is_veo:
            return _run_veo_video(client, hc, api_key)
        return _run_interactions_video(client, hc, api_key)

    # 视频任务本身耗时较长，仅对提交阶段的瞬时异常做 1 次重试，避免重复扣费
    actual = _run_sdk_with_retry(_call, cfg, timeout=180.0, max_retries=1, cancel_check=cancel_check)
    return actual, used_dur, used_res


# ==================== 统一对外媒体生成入口（SDK 直连主通道 + CLI 完整保留） ====================


def cli_generate_music(
    prompt: str,
    out_dir: Path,
    stem: str = "lyria_bgm",
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """一键生成原创配乐（按 MEDIA_ENGINE_MODE 优先官方 google-genai SDK 直连，支持无缝回退或指定 CLI）"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("请先填写或 AI 生成本集的英文配乐提示词（bgm_prompt）。")
    _check_cancelled(cancel_check)

    cfg = load_config()
    engine, can_fallback_cli = _resolve_media_engine(cfg)
    models, pricing = _resolve_cli_runtime()
    music_model = models["music"]
    music_cost = float(pricing.get("music", DEFAULT_CLI_PRICING["music"]))

    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    expected = out_dir / f"{stem}_{ts_tag}.mp3"

    def _run_via_cli() -> Path:
        _report_progress(progress_cb, 20, f"CLI 正在生成原创配乐（{music_model}）...")
        cmd = build_music_cmd(clean_prompt, expected)
        started = time.time()
        proc = _run_cli_with_retry(cmd, env=_build_cli_env(), timeout=240, expected_path=expected, cancel_check=cancel_check)
        if proc.returncode != 0:
            err = sanitize_cli_error(proc.stderr or proc.stdout)
            raise RuntimeError(f"CLI 生成配乐失败：{err}")
        actual_p = _find_created_output(expected, started)
        if not actual_p:
            raise RuntimeError("CLI 已返回成功但未在目标目录找到生成的音频文件。")
        real_ext = _detect_audio_ext(actual_p)
        if actual_p.suffix.lower() != real_ext:
            renamed = actual_p.with_suffix(real_ext)
            actual_p.rename(renamed)
            actual_p = renamed
        return actual_p

    engine_used = engine
    if engine == "sdk":
        try:
            actual = _sdk_generate_music_file(
                clean_prompt,
                music_model,
                expected,
                cfg,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )
        except Exception as sdk_exc:
            if _is_cancel_exc(sdk_exc):
                raise
            if can_fallback_cli and _is_model_or_endpoint_compat_error(sdk_exc):
                engine_used = "cli"
                actual = _run_via_cli()
            else:
                raise RuntimeError(f"SDK 生成配乐失败：{sanitize_cli_error(str(sdk_exc))}")
    else:
        actual = _run_via_cli()

    record_cli_cost(music_model, "cli_music" if engine_used == "cli" else "sdk_music", music_cost)
    _report_progress(progress_cb, 100, "原创配乐生成完成")
    return {
        "file_path": str(actual.resolve()),
        "filename": actual.name,
        "est_cost_usd": round(music_cost, 4),
        "model": music_model,
        "engine": engine_used,
    }


def cli_generate_tts(
    text: str,
    out_dir: Path,
    voice: str = "Kore",
    style_prompt: str = "",
    stem: str = "vo",
    language: str = "zh-CN",
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """生成 24kHz 标准 WAV 旁白，探测真实时长并记入 api_usage_logs（SDK 直连主通道 + CLI 完整保留）"""
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("旁白配音文本不能为空。")
    _check_cancelled(cancel_check)

    cfg = load_config()
    engine, can_fallback_cli = _resolve_media_engine(cfg)
    models, pricing = _resolve_cli_runtime()
    tts_model = models["tts"]
    tts_cost = float(pricing.get("tts", DEFAULT_CLI_PRICING["tts"]))

    used_voice = (voice or "Kore").strip() or "Kore"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    safe_stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", (stem or "vo").strip()).strip("_") or "vo"
    expected = out_dir / f"{safe_stem}_{ts_tag}.wav"
    if expected.exists():
        expected = out_dir / f"{safe_stem}_{ts_tag}_{int(time.time() * 1000) % 1000:03d}.wav"

    def _run_via_cli() -> Path:
        _report_progress(progress_cb, 25, f"CLI 正在合成中文旁白（{tts_model} · {used_voice}）...")
        cmd = build_tts_cmd(
            clean_text,
            expected,
            voice=used_voice,
            style_prompt=style_prompt,
            language=language,
        )
        started = time.time()
        proc = _run_cli_with_retry(cmd, env=_build_cli_env(), timeout=120, expected_path=expected, cancel_check=cancel_check)
        if proc.returncode != 0:
            err = sanitize_cli_error(proc.stderr or proc.stdout)
            raise RuntimeError(f"CLI 生成旁白 TTS 失败：{err}")
        actual_p = _find_created_output(expected, started)
        if not actual_p:
            raise RuntimeError("CLI TTS 已返回成功但未在目标目录找到生成的 WAV 文件。")
        real_ext = _detect_audio_ext(actual_p)
        if actual_p.suffix.lower() != real_ext:
            renamed = actual_p.with_suffix(real_ext)
            actual_p.rename(renamed)
            actual_p = renamed
        return actual_p

    engine_used = engine
    if engine == "sdk":
        try:
            actual = _sdk_generate_tts_file(
                clean_text,
                tts_model,
                used_voice,
                style_prompt,
                language,
                expected,
                cfg,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )
        except Exception as sdk_exc:
            if _is_cancel_exc(sdk_exc):
                raise
            if can_fallback_cli and _is_model_or_endpoint_compat_error(sdk_exc):
                engine_used = "cli"
                actual = _run_via_cli()
            else:
                raise RuntimeError(f"SDK 生成旁白 TTS 失败：{sanitize_cli_error(str(sdk_exc))}")
    else:
        actual = _run_via_cli()

    meta = probe_media(actual)
    duration_sec = round(float(meta.get("duration_sec") or 0.0), 2)
    record_cli_cost(tts_model, "cli_tts" if engine_used == "cli" else "sdk_tts", tts_cost)
    _report_progress(progress_cb, 100, f"旁白 WAV 已生成（{duration_sec:.1f}s）")
    return {
        "file_path": str(actual.resolve()),
        "filename": actual.name,
        "duration_sec": duration_sec,
        "est_cost_usd": round(tts_cost, 4),
        "model": tts_model,
        "voice": used_voice,
        "engine": engine_used,
    }


def cli_generate_images(
    prompt: str,
    ref_paths: list[Path],
    aspect_ratio: str,
    image_size: str,
    count: int,
    out_dir: Path,
    prefix: str,
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """并行生成 1–4 张候选图（优先官方 google-genai SDK 直连，完整保留 CLI 通道与瞬时重试）"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("生图提示词不能为空。")
    _check_cancelled(cancel_check)

    cfg = load_config()
    engine, can_fallback_cli = _resolve_media_engine(cfg)
    models, pricing = _resolve_cli_runtime()
    image_model = models["image"]
    img_pricing: dict[str, float] = pricing.get("image") or DEFAULT_CLI_PRICING["image"]

    size = (image_size or "2K").upper()
    if size not in img_pricing:
        size = "2K"
    ratio = (aspect_ratio or "9:16").strip() or "9:16"
    n = max(1, min(4, int(count or 2)))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    valid_refs = _prepare_ref_images(ref_paths, limit=6)
    cli_env = _build_cli_env() if (engine == "cli" or can_fallback_cli) else {}

    _report_progress(
        progress_cb,
        5,
        f"正在生成 {n} 张候选图（{'SDK 直连' if engine == 'sdk' else 'CLI'} · {image_model} · {size}）...",
    )

    def _run_one_cli(idx: int) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        expected = out_dir / f"{prefix}_{ts_tag}_{idx}.jpg"
        cmd = build_image_cmd(clean_prompt, valid_refs, ratio, size, expected)
        started = time.time()
        proc = _run_cli_with_retry(cmd, env=cli_env, timeout=180, expected_path=expected, cancel_check=cancel_check)
        if proc.returncode != 0:
            raise RuntimeError(sanitize_cli_error(proc.stderr or proc.stdout))
        actual = _find_created_output(expected, started)
        if not actual:
            raise RuntimeError("CLI 未落盘图片文件")
        meta = probe_media(actual)
        return {
            "index": idx,
            "path": str(actual.resolve()),
            "filename": actual.name,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "resolution_label": meta.get("resolution_label", size),
            "_engine": "cli",
        }

    def _run_one_sdk(idx: int) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        expected = out_dir / f"{prefix}_{ts_tag}_{idx}.jpg"
        try:
            actual = _sdk_generate_single_image(
                clean_prompt,
                valid_refs,
                ratio,
                size,
                image_model,
                expected,
                cfg,
                cancel_check=cancel_check,
            )
            used_eng = "sdk"
        except Exception as sdk_exc:
            if _is_cancel_exc(sdk_exc):
                raise
            if can_fallback_cli and _is_model_or_endpoint_compat_error(sdk_exc):
                return _run_one_cli(idx)
            raise RuntimeError(sanitize_cli_error(str(sdk_exc)))

        meta = probe_media(actual)
        return {
            "index": idx,
            "path": str(actual.resolve()),
            "filename": actual.name,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "resolution_label": meta.get("resolution_label", size),
            "_engine": used_eng,
        }

    runner = _run_one_sdk if engine == "sdk" else _run_one_cli
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    cancelled_exc: BaseException | None = None
    done_count = 0

    with ThreadPoolExecutor(max_workers=min(n, 4)) as ex:
        futs = [ex.submit(runner, i) for i in range(1, n + 1)]
        for fut in as_completed(futs):
            done_count += 1
            try:
                results.append(fut.result())
            except Exception as exc:
                if _is_cancel_exc(exc):
                    cancelled_exc = exc
                else:
                    errors.append(str(exc))
            _report_progress(progress_cb, 10 + int(85 * done_count / n), f"生图进度 {done_count}/{n}（成功 {len(results)} 张）")

    if cancelled_exc is not None:
        raise cancelled_exc

    results.sort(key=lambda x: x["index"])
    if not results:
        engine_tag = "SDK" if engine == "sdk" else "CLI"
        raise RuntimeError(f"{engine_tag} 生图全部失败：{errors[0] if errors else '未知错误'}")

    engine_used = results[0].get("_engine", engine)
    for item in results:
        item.pop("_engine", None)

    unit_cost = float(img_pricing[size])
    total_cost = round(unit_cost * len(results), 4)
    action_prefix = "cli_image" if engine_used == "cli" else "sdk_image"
    record_cli_cost(image_model, f"{action_prefix}_{size}x{len(results)}", total_cost)
    _report_progress(progress_cb, 100, f"已生成 {len(results)} 张候选图")
    return {
        "candidates": results,
        "failed_count": len(errors),
        "est_cost_usd": total_cost,
        "image_size": size,
        "model": image_model,
        "engine": engine_used,
    }


# 兼容单数命名导入别名
cli_generate_image = cli_generate_images


def cli_generate_covers(
    prompt_916: str,
    prompt_34: str,
    ref_paths: list[Path],
    out_dir: Path,
    count_per_ratio: int = 2,
    image_size: str = "2K",
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """复用 cli_generate_images，分别为竖屏 9:16 与 3:4 生成候选封面图"""
    p916 = (prompt_916 or "").strip()
    p34 = (prompt_34 or "").strip()
    if not p916 and not p34:
        raise ValueError("封面提示词不能为空（9:16 与 3:4 至少填写其一）。")
    _check_cancelled(cancel_check)

    models, pricing = _resolve_cli_runtime()
    img_pricing: dict[str, float] = pricing.get("image") or DEFAULT_CLI_PRICING["image"]

    n = max(1, min(4, int(count_per_ratio or 2)))
    size = (image_size or "2K").upper()
    if size not in img_pricing:
        size = "2K"
    out_dir.mkdir(parents=True, exist_ok=True)

    covers_9_16: list[dict[str, Any]] = []
    covers_3_4: list[dict[str, Any]] = []
    total_cost = 0.0
    failed_count = 0
    engine_used = ""
    both = bool(p916 and p34)

    if p916:
        def _cb_916(pct: int, msg: str) -> None:
            scaled = int(pct * 0.5) if both else pct
            _report_progress(progress_cb, scaled, f"[9:16 封面] {msg}")

        res_916 = cli_generate_images(
            prompt=p916,
            ref_paths=ref_paths,
            aspect_ratio="9:16",
            image_size=size,
            count=n,
            out_dir=out_dir,
            prefix="cover_9x16",
            progress_cb=_cb_916 if progress_cb else None,
            cancel_check=cancel_check,
        )
        for cand in res_916.get("candidates") or []:
            item = dict(cand)
            item["aspect_ratio"] = "9:16"
            covers_9_16.append(item)
        total_cost += float(res_916.get("est_cost_usd") or 0.0)
        failed_count += int(res_916.get("failed_count") or 0)
        engine_used = str(res_916.get("engine") or engine_used)

    if p34:
        def _cb_34(pct: int, msg: str) -> None:
            scaled = (50 + int(pct * 0.5)) if both else pct
            _report_progress(progress_cb, scaled, f"[3:4 封面] {msg}")

        res_34 = cli_generate_images(
            prompt=p34,
            ref_paths=ref_paths,
            aspect_ratio="3:4",
            image_size=size,
            count=n,
            out_dir=out_dir,
            prefix="cover_3x4",
            progress_cb=_cb_34 if progress_cb else None,
            cancel_check=cancel_check,
        )
        for cand in res_34.get("candidates") or []:
            item = dict(cand)
            item["aspect_ratio"] = "3:4"
            covers_3_4.append(item)
        total_cost += float(res_34.get("est_cost_usd") or 0.0)
        failed_count += int(res_34.get("failed_count") or 0)
        engine_used = str(res_34.get("engine") or engine_used)

    _report_progress(progress_cb, 100, "双比例封面候选图已生成完成")
    return {
        "covers_9_16": covers_9_16,
        "covers_3_4": covers_3_4,
        "total_cost_usd": round(total_cost, 4),
        "failed_count": failed_count,
        "image_size": size,
        "model": models["image"],
        "engine": engine_used or "sdk",
    }


def cli_generate_video(
    prompt: str,
    start_frame: Path | None,
    end_frame: Path | None,
    ref_paths: list[Path],
    aspect_ratio: str,
    resolution: str,
    duration_sec: int,
    count: int,
    out_dir: Path,
    prefix: str,
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """生成 1–2 条视频，自动异步轮询落盘并记账（优先官方 SDK 直连，完整保留 CLI 通道与瞬时重试）"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("视频提示词不能为空。")
    _check_cancelled(cancel_check)

    cfg = load_config()
    engine, can_fallback_cli = _resolve_media_engine(cfg)
    models, pricing = _resolve_cli_runtime()
    video_model = models["video"]
    vid_pricing: dict[str, float] = pricing.get("video_per_sec") or DEFAULT_CLI_PRICING["video_per_sec"]

    ratio = (aspect_ratio or "9:16").strip() or "9:16"
    n = max(1, min(2, int(count or 1)))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    cli_env = _build_cli_env() if (engine == "cli" or can_fallback_cli) else {}

    _report_progress(
        progress_cb,
        5,
        f"准备生成 {n} 条视频（{'SDK 直连' if engine == 'sdk' else 'CLI'} · {video_model}）...",
    )

    def _run_one_cli(idx: int) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        expected = out_dir / f"{prefix}_cli_{ts_tag}_{idx}.mp4"
        cmd, used_dur, used_res = build_video_cmd(
            clean_prompt,
            start_frame,
            end_frame,
            ref_paths,
            ratio,
            resolution,
            duration_sec,
            expected,
        )
        started = time.time()
        _report_progress(progress_cb, 15, f"CLI 视频任务 #{idx} 云端渲染中（{video_model} · {used_res}）...")
        proc = _run_cli_with_retry(cmd, env=cli_env, timeout=660, expected_path=expected, cancel_check=cancel_check)
        if proc.returncode != 0:
            raise RuntimeError(sanitize_cli_error(proc.stderr or proc.stdout))
        actual = _find_created_output(expected, started)
        if not actual:
            raise RuntimeError("CLI 已结束但未找到生成的视频文件")
        meta = probe_media(actual)
        real_dur = float(meta.get("duration_sec") or used_dur)
        rate = float(vid_pricing.get(used_res, DEFAULT_CLI_PRICING["video_per_sec"].get(used_res, 0.07)))
        cost = round(max(1.0, real_dur) * rate, 4)
        return {
            "index": idx,
            "path": str(actual.resolve()),
            "filename": actual.name,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "duration_sec": round(real_dur, 2),
            "resolution_label": meta.get("resolution_label") or used_res,
            "requested_resolution": used_res,
            "est_cost_usd": cost,
            "_engine": "cli",
        }

    def _run_one_sdk(idx: int) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        expected = out_dir / f"{prefix}_sdk_{ts_tag}_{idx}.mp4"
        sub_cb: ProgressCallback | None = None
        if progress_cb is not None:
            def _cb(pct: int, msg: str) -> None:
                prefix_lbl = f"[第 {idx}/{n} 条] " if n > 1 else ""
                _report_progress(progress_cb, pct, f"{prefix_lbl}{msg}")
            sub_cb = _cb

        try:
            actual, used_dur, used_res = _sdk_generate_single_video(
                clean_prompt,
                start_frame,
                end_frame,
                ref_paths,
                ratio,
                resolution,
                duration_sec,
                video_model,
                expected,
                cfg,
                vid_pricing,
                progress_cb=sub_cb,
                cancel_check=cancel_check,
            )
            used_eng = "sdk"
        except Exception as sdk_exc:
            if _is_cancel_exc(sdk_exc):
                raise
            if can_fallback_cli and _is_model_or_endpoint_compat_error(sdk_exc):
                return _run_one_cli(idx)
            raise RuntimeError(sanitize_cli_error(str(sdk_exc)))

        meta = probe_media(actual)
        real_dur = float(meta.get("duration_sec") or used_dur)
        rate = float(vid_pricing.get(used_res, DEFAULT_CLI_PRICING["video_per_sec"].get(used_res, 0.07)))
        cost = round(max(1.0, real_dur) * rate, 4)
        return {
            "index": idx,
            "path": str(actual.resolve()),
            "filename": actual.name,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "duration_sec": round(real_dur, 2),
            "resolution_label": meta.get("resolution_label") or used_res,
            "requested_resolution": used_res,
            "est_cost_usd": cost,
            "_engine": used_eng,
        }

    runner = _run_one_sdk if engine == "sdk" else _run_one_cli
    videos: list[dict[str, Any]] = []
    errors: list[str] = []
    cancelled_exc: BaseException | None = None

    with ThreadPoolExecutor(max_workers=min(n, 2)) as ex:
        futs = [ex.submit(runner, i) for i in range(1, n + 1)]
        for fut in as_completed(futs):
            try:
                videos.append(fut.result())
            except Exception as exc:
                if _is_cancel_exc(exc):
                    cancelled_exc = exc
                else:
                    errors.append(str(exc))

    if cancelled_exc is not None:
        raise cancelled_exc

    videos.sort(key=lambda x: x["index"])
    if not videos:
        engine_tag = "SDK" if engine == "sdk" else "CLI"
        raise RuntimeError(f"{engine_tag} 生成视频失败：{errors[0] if errors else '未知错误'}")

    engine_used = videos[0].get("_engine", engine)
    for v in videos:
        v.pop("_engine", None)

    total_cost = round(sum(v["est_cost_usd"] for v in videos), 4)
    used_res = videos[0]["requested_resolution"]
    action_prefix = "cli_video" if engine_used == "cli" else "sdk_video"
    record_cli_cost(video_model, f"{action_prefix}_{used_res}x{len(videos)}", total_cost)
    _report_progress(progress_cb, 100, f"视频生成完成（共 {len(videos)} 条）")
    return {
        "videos": videos,
        "failed_count": len(errors),
        "est_cost_usd": total_cost,
        "resolution": used_res,
        "model": video_model,
        "engine": engine_used,
    }
