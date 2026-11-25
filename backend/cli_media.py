"""
Google 官方 `gemini-api` CLI 媒体生成引擎（走官方 API 赠金/账单，不消耗 Flow 积分）：
- 配乐生成：`gemini-api music`（默认模型与估算单价均从 config.py 动态读取）
- 语音旁白：`gemini-api tts`（24kHz 单声道 WAV）
- 静帧/封面/棚拍洗图：`gemini-api image`（1K/2K/4K）
- 视频生成：`gemini-api video`（360p/720p/1080p/4k）

安全红线：
- 通过 `gemini-api` 官方 CLI 自身的系统凭据库（keyring）鉴权，绝不在命令行或日志中传递/打印明文 API Key；
- 按配置动态挂接代理（`GOOGLE_PROXY_URL`，留空直连），失败信息自动脱敏；
- 内置瞬时网络故障（超时/429 限流/5xx）单次轻量自动重试（退避 3 秒），余额不足/400 参数错误/鉴权错误绝不重试。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import DEFAULTS, DEFAULT_CLI_PRICING, get_cli_pricing, load_config
from .db import db_ctx, now_str
from .media_ops import probe_media

# 默认模型常量（向后兼容导出；实际运行时均通过 _resolve_models() 从配置动态读取）
IMAGE_MODEL = DEFAULTS["CLI_IMAGE_MODEL"]
VIDEO_MODEL = DEFAULTS["CLI_VIDEO_MODEL"]
MUSIC_MODEL = DEFAULTS["CLI_MUSIC_MODEL"]
TTS_MODEL = DEFAULTS["CLI_TTS_MODEL"]

# 默认参考单价（实际计费均通过 get_cli_pricing() 合并用户 CLI_PRICING_JSON 动态读取）
IMAGE_COST_USD: dict[str, float] = dict(DEFAULT_CLI_PRICING["image"])
VIDEO_COST_PER_SEC_USD: dict[str, float] = dict(DEFAULT_CLI_PRICING["video_per_sec"])
MUSIC_COST_USD = float(DEFAULT_CLI_PRICING["music"])
TTS_COST_EST_USD = float(DEFAULT_CLI_PRICING["tts"])

# 瞬时网络异常轻量重试参数：最多重试 1 次、退避 3 秒
TRANSIENT_RETRY_MAX = 1
TRANSIENT_RETRY_BACKOFF_SEC = 3.0

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
)

# 允许自动重试 1 次的瞬时网络/限流/服务端异常特征
_RETRYABLE_PATTERNS: tuple[str, ...] = (
    r"\b429\b",
    r"too many requests",
    r"rate[_\s-]?limit",
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


def _resolve_cli_runtime() -> tuple[dict[str, str], dict[str, Any]]:
    """实时读取配置中的 CLI 模型 ID 与动态单价"""
    cfg = load_config()
    models = {
        "image": (cfg.get("CLI_IMAGE_MODEL") or DEFAULTS["CLI_IMAGE_MODEL"]).strip() or DEFAULTS["CLI_IMAGE_MODEL"],
        "video": (cfg.get("CLI_VIDEO_MODEL") or DEFAULTS["CLI_VIDEO_MODEL"]).strip() or DEFAULTS["CLI_VIDEO_MODEL"],
        "music": (cfg.get("CLI_MUSIC_MODEL") or DEFAULTS["CLI_MUSIC_MODEL"]).strip() or DEFAULTS["CLI_MUSIC_MODEL"],
        "tts": (cfg.get("CLI_TTS_MODEL") or DEFAULTS["CLI_TTS_MODEL"]).strip() or DEFAULTS["CLI_TTS_MODEL"],
    }
    pricing = get_cli_pricing(cfg)
    return models, pricing


def _is_executable(p: Path) -> bool:
    if not p.exists() or not p.is_file():
        return False
    if sys.platform == "win32":
        return True
    return os.access(p, os.X_OK)


def resolve_gemini_cli_bin() -> str:
    """跨平台定位官方 gemini-api CLI 可执行文件（兼容 macOS / Windows / Linux）"""
    if sys.platform == "win32":
        for name in ("gemini-api.exe", "gemini-api.cmd", "gemini-api.bat", "gemini-api"):
            found = shutil.which(name)
            if found:
                return found
        home = Path.home()
        local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        win_candidates = [
            home / ".local" / "bin" / "gemini-api.exe",
            home / ".local" / "bin" / "gemini-api.cmd",
            local_app / "Programs" / "gemini-api" / "gemini-api.exe",
            local_app / "bin" / "gemini-api.exe",
            home / "go" / "bin" / "gemini-api.exe",
        ]
        for p in win_candidates:
            if _is_executable(p):
                return str(p)
        raise RuntimeError(
            "未找到官方 gemini-api CLI，请先安装并确认其在 PATH 或 ~/.local/bin 目录中，然后执行 `gemini-api auth` 完成鉴权。"
        )

    candidates = [
        Path.home() / ".local" / "bin" / "gemini-api",
        Path("/usr/local/bin/gemini-api"),
        Path("/opt/homebrew/bin/gemini-api"),
    ]
    for p in candidates:
        if _is_executable(p):
            return str(p)
    found = shutil.which("gemini-api")
    if found:
        return found
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
    """对 CLI 错误输出做密钥与冗长信息脱敏，避免泄露敏感凭据"""
    s = (text or "").strip()
    s = re.sub(r"AQ\.[0-9A-Za-z_\-]{12,}", "[REDACTED_KEY]", s)
    s = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED_KEY]", s)
    s = re.sub(r"Bearer\s+[0-9A-Za-z_\-\.]+", "Bearer [REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:420] if len(s) > 420 else s


def _is_retryable_cli_error(err_text: str, returncode: int | None = None, *, timed_out: bool = False) -> bool:
    """判断 CLI 失败是否属于可安全重试的瞬时网络/限流/5xx 错误；400 参数错、鉴权错、余额不足绝不重试"""
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


def _run_cli_with_retry(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    max_retries: int = TRANSIENT_RETRY_MAX,
    backoff_sec: float = TRANSIENT_RETRY_BACKOFF_SEC,
    expected_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行官方 gemini-api CLI，对超时/429/5xx 瞬时故障做最多 max_retries 次退避重试"""
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt < retries and _is_retryable_cli_error("timeout", timed_out=True):
                if expected_path is not None and expected_path.exists() and expected_path.stat().st_size == 0:
                    try:
                        expected_path.unlink()
                    except OSError:
                        pass
                time.sleep(backoff_sec)
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
            time.sleep(backoff_sec)
            continue
        return proc

    raise RuntimeError("CLI 调用失败（重试耗尽）")


def record_cli_cost(model: str, action: str, est_cost_usd: float) -> None:
    """把 CLI 媒体生成估算美元花费记入 api_usage_logs，驱动月度预算预警"""
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


def _append_role_hint(prompt: str, hint: str) -> str:
    p = (prompt or "").strip()
    if not p:
        return hint
    sep = "" if p[-1] in ".!?" else "."
    return f"{p}{sep} {hint}"


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
    seen: set[str] = set()
    added = 0
    for rp in ref_paths:
        if added >= 6:
            break
        try:
            resolved = rp.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists() or not resolved.is_file():
            continue
        if resolved.stat().st_size > 14 * 1024 * 1024:
            continue
        seen.add(key)
        cmd.extend(["-i", key])
        added += 1
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
    input_imgs: list[str] = []

    if valid_sf and valid_ef:
        input_imgs = [str(valid_sf), str(valid_ef)]
        if "first frame" not in lower_p and "starting frame" not in lower_p:
            final_prompt = _append_role_hint(
                final_prompt,
                "Use the first image as the starting frame and the second image as the final frame.",
            )
    elif valid_sf:
        input_imgs = [str(valid_sf)]
        if "first frame" not in lower_p and "starting frame" not in lower_p:
            final_prompt = _append_role_hint(final_prompt, "Use this image as the starting frame.")
    else:
        seen: set[str] = set()
        for rp in ref_paths:
            if len(input_imgs) >= 6:
                break
            try:
                resolved = rp.expanduser().resolve()
            except Exception:
                continue
            key = str(resolved)
            if key not in seen and resolved.exists() and resolved.is_file():
                seen.add(key)
                input_imgs.append(key)

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
        cmd.extend(["-i", img_p])
    if dry_run:
        cmd.append("--dry-run")
    return cmd, dur, res


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


def cli_generate_music(prompt: str, out_dir: Path, stem: str = "lyria_bgm") -> dict[str, Any]:
    """调用官方 gemini-api music 一键生成原创配乐"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("请先填写或 AI 生成本集的英文配乐提示词（bgm_prompt）。")
    models, pricing = _resolve_cli_runtime()
    music_model = models["music"]
    music_cost = float(pricing.get("music", DEFAULT_CLI_PRICING["music"]))

    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    expected = out_dir / f"{stem}_{ts_tag}.mp3"
    cmd = build_music_cmd(clean_prompt, expected)
    started = time.time()

    proc = _run_cli_with_retry(cmd, env=_build_cli_env(), timeout=240, expected_path=expected)
    if proc.returncode != 0:
        err = sanitize_cli_error(proc.stderr or proc.stdout)
        raise RuntimeError(f"CLI 生成配乐失败：{err}")

    actual = _find_created_output(expected, started)
    if not actual:
        raise RuntimeError("CLI 已返回成功但未在目标目录找到生成的音频文件。")

    real_ext = _detect_audio_ext(actual)
    if actual.suffix.lower() != real_ext:
        renamed = actual.with_suffix(real_ext)
        actual.rename(renamed)
        actual = renamed

    record_cli_cost(music_model, "cli_music", music_cost)
    return {
        "file_path": str(actual.resolve()),
        "filename": actual.name,
        "est_cost_usd": round(music_cost, 4),
        "model": music_model,
    }


def cli_generate_tts(
    text: str,
    out_dir: Path,
    voice: str = "Kore",
    style_prompt: str = "",
    stem: str = "vo",
    language: str = "zh-CN",
) -> dict[str, Any]:
    """调用官方 gemini-api tts 生成 24kHz WAV 旁白，探测时长并记入 api_usage_logs"""
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("旁白配音文本不能为空。")
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

    cmd = build_tts_cmd(
        clean_text,
        expected,
        voice=used_voice,
        style_prompt=style_prompt,
        language=language,
    )
    started = time.time()
    proc = _run_cli_with_retry(cmd, env=_build_cli_env(), timeout=120, expected_path=expected)
    if proc.returncode != 0:
        err = sanitize_cli_error(proc.stderr or proc.stdout)
        raise RuntimeError(f"CLI 生成旁白 TTS 失败：{err}")

    actual = _find_created_output(expected, started)
    if not actual:
        raise RuntimeError("CLI TTS 已返回成功但未在目标目录找到生成的 WAV 文件。")

    real_ext = _detect_audio_ext(actual)
    if actual.suffix.lower() != real_ext:
        renamed = actual.with_suffix(real_ext)
        actual.rename(renamed)
        actual = renamed

    meta = probe_media(actual)
    duration_sec = round(float(meta.get("duration_sec") or 0.0), 2)
    record_cli_cost(tts_model, "cli_tts", tts_cost)
    return {
        "file_path": str(actual.resolve()),
        "filename": actual.name,
        "duration_sec": duration_sec,
        "est_cost_usd": round(tts_cost, 4),
        "model": tts_model,
        "voice": used_voice,
    }


def cli_generate_images(
    prompt: str,
    ref_paths: list[Path],
    aspect_ratio: str,
    image_size: str,
    count: int,
    out_dir: Path,
    prefix: str,
) -> dict[str, Any]:
    """并行调用官方 gemini-api image 生成 1–4 张候选图（单张内置瞬时故障 1 次重试）"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("生图提示词不能为空。")
    models, pricing = _resolve_cli_runtime()
    image_model = models["image"]
    img_pricing: dict[str, float] = pricing.get("image") or DEFAULT_CLI_PRICING["image"]

    size = (image_size or "2K").upper()
    if size not in img_pricing:
        size = "2K"
    n = max(1, min(4, int(count or 2)))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    env = _build_cli_env()

    def _run_one(idx: int) -> dict[str, Any]:
        expected = out_dir / f"{prefix}_{ts_tag}_{idx}.jpg"
        cmd = build_image_cmd(clean_prompt, ref_paths, aspect_ratio, size, expected)
        started = time.time()
        proc = _run_cli_with_retry(cmd, env=env, timeout=180, expected_path=expected)
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
        }

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(n, 4)) as ex:
        futs = [ex.submit(_run_one, i) for i in range(1, n + 1)]
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))

    results.sort(key=lambda x: x["index"])
    if not results:
        raise RuntimeError(f"CLI 生图全部失败：{errors[0] if errors else '未知错误'}")

    unit_cost = float(img_pricing[size])
    total_cost = round(unit_cost * len(results), 4)
    record_cli_cost(image_model, f"cli_image_{size}x{len(results)}", total_cost)
    return {
        "candidates": results,
        "failed_count": len(errors),
        "est_cost_usd": total_cost,
        "image_size": size,
        "model": image_model,
    }


def cli_generate_covers(
    prompt_916: str,
    prompt_34: str,
    ref_paths: list[Path],
    out_dir: Path,
    count_per_ratio: int = 2,
    image_size: str = "2K",
) -> dict[str, Any]:
    """复用 cli_generate_images，分别为竖屏 9:16 与 3:4 生成候选封面图"""
    p916 = (prompt_916 or "").strip()
    p34 = (prompt_34 or "").strip()
    if not p916 and not p34:
        raise ValueError("封面提示词不能为空（9:16 与 3:4 至少填写其一）。")

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

    if p916:
        res_916 = cli_generate_images(
            prompt=p916,
            ref_paths=ref_paths,
            aspect_ratio="9:16",
            image_size=size,
            count=n,
            out_dir=out_dir,
            prefix="cover_9x16",
        )
        for cand in res_916.get("candidates") or []:
            item = dict(cand)
            item["aspect_ratio"] = "9:16"
            covers_9_16.append(item)
        total_cost += float(res_916.get("est_cost_usd") or 0.0)
        failed_count += int(res_916.get("failed_count") or 0)

    if p34:
        res_34 = cli_generate_images(
            prompt=p34,
            ref_paths=ref_paths,
            aspect_ratio="3:4",
            image_size=size,
            count=n,
            out_dir=out_dir,
            prefix="cover_3x4",
        )
        for cand in res_34.get("candidates") or []:
            item = dict(cand)
            item["aspect_ratio"] = "3:4"
            covers_3_4.append(item)
        total_cost += float(res_34.get("est_cost_usd") or 0.0)
        failed_count += int(res_34.get("failed_count") or 0)

    return {
        "covers_9_16": covers_9_16,
        "covers_3_4": covers_3_4,
        "total_cost_usd": round(total_cost, 4),
        "failed_count": failed_count,
        "image_size": size,
        "model": models["image"],
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
) -> dict[str, Any]:
    """调用官方 gemini-api video 生成 1–2 条视频，自动轮询落盘并记账（单条内置瞬时故障 1 次重试）"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("视频提示词不能为空。")
    models, pricing = _resolve_cli_runtime()
    video_model = models["video"]
    vid_pricing: dict[str, float] = pricing.get("video_per_sec") or DEFAULT_CLI_PRICING["video_per_sec"]

    n = max(1, min(2, int(count or 1)))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    env = _build_cli_env()

    def _run_one(idx: int) -> dict[str, Any]:
        expected = out_dir / f"{prefix}_cli_{ts_tag}_{idx}.mp4"
        cmd, used_dur, used_res = build_video_cmd(
            clean_prompt,
            start_frame,
            end_frame,
            ref_paths,
            aspect_ratio,
            resolution,
            duration_sec,
            expected,
        )
        started = time.time()
        proc = _run_cli_with_retry(cmd, env=env, timeout=660, expected_path=expected)
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
        }

    videos: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(n, 2)) as ex:
        futs = [ex.submit(_run_one, i) for i in range(1, n + 1)]
        for fut in as_completed(futs):
            try:
                videos.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))

    videos.sort(key=lambda x: x["index"])
    if not videos:
        raise RuntimeError(f"CLI 生成视频失败：{errors[0] if errors else '未知错误'}")

    total_cost = round(sum(v["est_cost_usd"] for v in videos), 4)
    used_res = videos[0]["requested_resolution"]
    record_cli_cost(video_model, f"cli_video_{used_res}x{len(videos)}", total_cost)
    return {
        "videos": videos,
        "failed_count": len(errors),
        "est_cost_usd": total_cost,
        "resolution": used_res,
        "model": video_model,
    }
