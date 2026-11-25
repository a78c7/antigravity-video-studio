"""
Google Colab 官方 API & 官方 CLI（google-colab-cli）跨平台云端 GPU 调度模块（兼容 macOS / Windows / Linux）：
- 100% 官方通道：仅对接 Google Colab 官方 REST API（colaboratory.googleapis.com/v1beta）、
  官方托管 MCP 端点（colaboratory.googleapis.com/mcp）与官方 CLI（googlecolab/google-colab-cli），
  绝不抓取浏览器 Cookie，0 封号风险；
- 零明文密钥：OAuth2 刷新令牌复用 google-colab-cli 官方标准存于系统凭据库（keyring）；
- 精准网络隔离：按配置中的 `GOOGLE_PROXY_URL` 按需注入子进程代理（留空直连，绝不硬编码代理地址）；
- 自动释放防烧分：云端 GPU 任务结束无论成功失败均在 finally 中强制 `colab stop` 回收 VM。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import EXPORTS_DIR, load_config
from .media_ops import build_master_base_mp4

VALID_GPU_TIERS = ("CPU", "T4", "L4", "G4", "A100", "H100")

# 仅在进程内存中短期保存正在进行的 OAuth2 PKCE 会话（10 分钟过期，不落盘）
_OAUTH_LOCK = threading.Lock()
_PENDING_OAUTH: dict[str, dict[str, Any]] = {}


def _is_executable(p: Path) -> bool:
    if not p.exists() or not p.is_file():
        return False
    if sys.platform == "win32":
        return True
    return os.access(p, os.X_OK)


def _build_google_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造仅给 Google Colab 子进程使用的环境变量（跨平台拼接 PATH，按配置按需注入代理）"""
    env = dict(os.environ)
    local_bin = str(Path.home() / ".local" / "bin")
    cur_path = env.get("PATH", "")
    parts = cur_path.split(os.pathsep) if cur_path else []
    if local_bin not in parts:
        env["PATH"] = f"{local_bin}{os.pathsep}{cur_path}" if cur_path else local_bin

    proxy = load_config().get("GOOGLE_PROXY_URL", "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env.pop("ALL_PROXY", None)
        env.pop("all_proxy", None)
    if extra:
        env.update(extra)
    return env


def resolve_colab_cli() -> str:
    """跨平台按优先级解析官方 google-colab-cli 可执行文件路径（兼容 macOS / Windows / Linux）"""
    cfg_path = load_config().get("COLAB_CLI_PATH", "").strip()
    if cfg_path:
        p = Path(cfg_path).expanduser()
        if _is_executable(p):
            return str(p)

    if sys.platform == "win32":
        for name in ("colab.exe", "colab.cmd", "colab.bat", "colab"):
            found = shutil.which(name)
            if found:
                return found
        home = Path.home()
        local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        roaming_app = Path(os.environ.get("APPDATA") or str(home / "AppData" / "Roaming"))
        win_candidates = [
            home / ".local" / "bin" / "colab.exe",
            local_app / "uv" / "tools" / "google-colab-cli" / "Scripts" / "colab.exe",
            roaming_app / "uv" / "tools" / "google-colab-cli" / "Scripts" / "colab.exe",
            home / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "Scripts" / "colab.exe",
        ]
        for p in win_candidates:
            if _is_executable(p):
                return str(p)
        return ""

    for candidate in (
        shutil.which("colab"),
        str(Path.home() / ".local" / "bin" / "colab"),
        "/opt/homebrew/bin/colab",
        "/usr/local/bin/colab",
    ):
        if candidate and _is_executable(Path(candidate)):
            return str(candidate)
    return ""


def resolve_colab_python() -> str:
    """
    跨平台定位安装 google-colab-cli 时的独立 uv tool Python 解释器。
    主程序虚拟环境无需引入 google-auth-oauthlib / keyring 即可复用官方 OAuth2 与凭据存储逻辑。
    """
    home = Path.home()
    candidates: list[Path] = []

    if sys.platform == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        roaming_app = Path(os.environ.get("APPDATA") or str(home / "AppData" / "Roaming"))
        candidates.extend(
            [
                local_app / "uv" / "tools" / "google-colab-cli" / "Scripts" / "python.exe",
                roaming_app / "uv" / "tools" / "google-colab-cli" / "Scripts" / "python.exe",
                home / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "Scripts" / "python.exe",
            ]
        )
        cli = resolve_colab_cli()
        if cli:
            cli_p = Path(cli).resolve()
            sibling_py = cli_p.parent / "python.exe"
            if sibling_py.exists():
                candidates.insert(0, sibling_py)
    else:
        candidates.extend(
            [
                home / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python",
                home / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python3",
            ]
        )
        cli = resolve_colab_cli()
        if cli and not cli.lower().endswith((".exe", ".dll")):
            try:
                first_line = Path(cli).read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                if first_line.startswith("#!"):
                    interp = first_line[2:].strip().split()[0]
                    if interp and Path(interp).exists():
                        candidates.insert(0, Path(interp))
            except Exception:
                pass

    for p in candidates:
        if _is_executable(p):
            return str(p)
    return ""


def _run_colab_helper(code: str, payload: dict[str, Any] | None = None, timeout: float = 25.0) -> dict[str, Any]:
    """在 google-colab-cli 隔离环境中运行短命 JSON 辅助脚本（通过 stdin 传参，零明文落盘）"""
    py = resolve_colab_python()
    if not py:
        raise RuntimeError(
            "本机尚未安装官方 google-colab-cli，请先执行 `uv tool install google-colab-cli --with keyrings.alt`。"
        )
    res = subprocess.run(
        [py, "-c", code],
        input=json.dumps(payload or {}, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_build_google_env(),
    )
    stdout = (res.stdout or "").strip()
    if res.returncode != 0:
        err_msg = (res.stderr or stdout or f"exit {res.returncode}").strip()
        raise RuntimeError(err_msg[-600:])
    if not stdout:
        return {}
    last_line = stdout.splitlines()[-1]
    return json.loads(last_line)


def _prune_pending_oauth() -> None:
    now = time.time()
    stale = [k for k, v in _PENDING_OAUTH.items() if now - v.get("created_at", 0) > 600]
    for k in stale:
        _PENDING_OAUTH.pop(k, None)


def start_colab_oauth() -> dict[str, Any]:
    """
    生成 Google 官方 Colab OAuth2 授权链接（复用 google-colab-cli 内置公开客户端 + PKCE）。
    用户在浏览器点击允许后复制授权码粘贴回制片台，即可安全存入系统凭据库。
    """
    helper = r"""
import json, sys
from importlib import resources
from google_auth_oauthlib.flow import InstalledAppFlow
from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI

cfg_text = resources.files("colab_cli").joinpath("oauth_config.json").read_text(encoding="utf-8")
client_config = json.loads(cfg_text)
flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
flow.redirect_uri = REMOTE_REDIRECT_URI
auth_url, state = flow.authorization_url(prompt="consent", token_usage="remote", access_type="offline")
print(json.dumps({
    "auth_url": auth_url,
    "state": state,
    "code_verifier": getattr(flow, "code_verifier", None) or "",
}, ensure_ascii=False))
"""
    data = _run_colab_helper(helper, timeout=15.0)
    state = data["state"]
    with _OAUTH_LOCK:
        _prune_pending_oauth()
        _PENDING_OAUTH[state] = {
            "code_verifier": data.get("code_verifier", ""),
            "created_at": time.time(),
        }
    return {
        "auth_url": data["auth_url"],
        "state": state,
        "instructions": "请打开授权链接登录你的 Google 账号并点击允许，然后复制页面展示的授权码粘贴到下方输入框。",
    }


def finish_colab_oauth(auth_code: str, state: str = "") -> dict[str, Any]:
    """使用授权码换取 OAuth2 令牌并按 google-colab-cli 标准写入系统凭据库（制片台与终端 CLI 共用）"""
    code_clean = (auth_code or "").strip()
    if not code_clean:
        raise ValueError("授权码不能为空，请粘贴 Google 页面显示的授权码。")

    code_verifier = ""
    with _OAUTH_LOCK:
        _prune_pending_oauth()
        if state and state in _PENDING_OAUTH:
            code_verifier = _PENDING_OAUTH.pop(state).get("code_verifier", "")
        elif _PENDING_OAUTH:
            latest_k = max(_PENDING_OAUTH.keys(), key=lambda k: _PENDING_OAUTH[k]["created_at"])
            code_verifier = _PENDING_OAUTH.pop(latest_k).get("code_verifier", "")

    helper = r"""
import json, sys
from importlib import resources
from google_auth_oauthlib.flow import InstalledAppFlow
from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI, _save_token_to_keyring

req = json.loads(sys.stdin.read())
cfg_text = resources.files("colab_cli").joinpath("oauth_config.json").read_text(encoding="utf-8")
client_config = json.loads(cfg_text)
flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
flow.redirect_uri = REMOTE_REDIRECT_URI
if req.get("code_verifier"):
    flow.code_verifier = req["code_verifier"]
flow.fetch_token(code=req["code"])
creds = flow.credentials
_save_token_to_keyring(creds.to_json())
print(json.dumps({"ok": True, "has_refresh_token": bool(creds.refresh_token)}, ensure_ascii=False))
"""
    _run_colab_helper(helper, payload={"code": code_clean, "code_verifier": code_verifier}, timeout=30.0)
    return probe_colab()


def logout_colab_oauth() -> dict[str, Any]:
    """从系统凭据库清除 Colab OAuth2 令牌"""
    helper = r"""
import json, keyring
from colab_cli.auth import KEYRING_SERVICE_NAME, KEYRING_USERNAME
try:
    keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
    deleted = True
except Exception:
    deleted = False
print(json.dumps({"deleted": deleted}))
"""
    try:
        _run_colab_helper(helper, timeout=15.0)
    except Exception:
        pass
    return probe_colab()


def _check_mcp_configured(expected_endpoint: str) -> bool:
    mcp_path = Path("~/.gemini/config/mcp_config.json").expanduser()
    if not mcp_path.exists():
        return False
    try:
        raw = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", {})
        target = (expected_endpoint or "https://colaboratory.googleapis.com/mcp").rstrip("/")
        for entry in servers.values():
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or entry.get("serverUrl") or "").strip().rstrip("/")
            if url and (url == target or "colaboratory.googleapis.com/mcp" in url):
                return True
    except Exception:
        return False
    return False


def probe_colab() -> dict[str, Any]:
    """
    按需探测本机 Colab 官方 CLI、托管 MCP 端点、
    OAuth2 授权状态、官方 REST API v1beta 连通性、真实 CU 余额及当前活跃运行时。
    """
    cfg = load_config()
    api_base = cfg.get("COLAB_API_BASE", "https://colaboratory.googleapis.com/v1beta").rstrip("/")
    mcp_endpoint = cfg.get("COLAB_MCP_ENDPOINT", "https://colaboratory.googleapis.com/mcp").strip()
    cli_path = resolve_colab_cli()
    mcp_configured = _check_mcp_configured(mcp_endpoint)

    base_resp: dict[str, Any] = {
        "cli_installed": bool(cli_path),
        "cli_path": cli_path,
        "cli_version": "",
        "api_base": api_base,
        "mcp_endpoint": mcp_endpoint,
        "mcp_configured": mcp_configured,
        "authorized": False,
        "email": "",
        "ccu_balance": None,
        "ccu_rate_hourly": None,
        "assignments_count": 0,
        "active_sessions": [],
        "available_specs": [],
        "public_api_ok": False,
        "public_api_note": "",
        "message": "",
    }

    if not cli_path:
        base_resp["message"] = "未检测到官方 google-colab-cli，请先安装后再授权（不配置也可使用本机 1080p 渲染）。"
        return base_resp

    try:
        v_res = subprocess.run([cli_path, "version"], capture_output=True, text=True, timeout=5.0, env=_build_google_env())
        if v_res.returncode == 0:
            base_resp["cli_version"] = v_res.stdout.strip().replace("Version:", "").strip()
    except Exception:
        pass

    helper = r"""
import json, sys, requests
from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from colab_cli.auth import PUBLIC_SCOPES, _get_token_from_keyring, _save_token_to_keyring
from colab_cli.client import Client, Prod

req = json.loads(sys.stdin.read())
api_base = req.get("api_base", "https://colaboratory.googleapis.com/v1beta").rstrip("/")
out = {
    "authorized": False,
    "email": "",
    "ccu_balance": None,
    "ccu_rate_hourly": None,
    "assignments_count": 0,
    "active_sessions": [],
    "available_specs": [],
    "public_api_ok": False,
    "public_api_note": "",
    "error": "",
}

token_json = _get_token_from_keyring()
if not token_json:
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)

try:
    creds = Credentials.from_authorized_user_info(json.loads(token_json), PUBLIC_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token_to_keyring(creds.to_json())
        else:
            out["error"] = "本地凭据库中的 Colab 令牌已失效，请重新授权。"
            print(json.dumps(out, ensure_ascii=False))
            sys.exit(0)
except Exception as exc:
    out["error"] = f"读取或刷新 Colab 令牌失败：{exc}"
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)

out["authorized"] = True
session = AuthorizedSession(creds)

try:
    u_resp = session.get("https://www.googleapis.com/oauth2/v2/userinfo", timeout=10)
    if u_resp.ok:
        out["email"] = u_resp.json().get("email", "")
except Exception:
    pass

try:
    client = Client(Prod(), session)
    try:
        info = client.get_consumption_user_info()
        out["ccu_balance"] = round(float(info.paid_compute_units_balance), 2)
        out["ccu_rate_hourly"] = round(float(info.consumption_rate_hourly), 2)
        out["assignments_count"] = int(info.assignments_count)
    except Exception as exc:
        out["public_api_note"] = f"CU 额度读取提示：{exc}"

    try:
        assigns = client.list_assignments()
        for a in assigns:
            out["active_sessions"].append({
                "endpoint": getattr(a, "endpoint", ""),
                "accelerator": str(getattr(a, "accelerator", "") or "CPU"),
                "variant": str(getattr(a, "variant", "") or ""),
                "machine_shape": str(getattr(a, "machine_shape", "") or ""),
            })
    except Exception:
        pass
except Exception as exc:
    out["error"] = str(exc)

try:
    r_specs = session.get(f"{api_base}/runtimespecs", timeout=10)
    if r_specs.ok:
        out["public_api_ok"] = True
        data = r_specs.json()
        specs = data.get("runtimeSpecs") or data.get("runtime_specs") or []
        seen = []
        for sp in specs:
            acc = sp.get("acceleratorType") or sp.get("accelerator_type") or sp.get("accelerator") or "CPU"
            tier = sp.get("machineType") or sp.get("shape") or ""
            label = f"{acc} ({tier})" if tier else str(acc)
            if label not in seen:
                seen.append(label)
        out["available_specs"] = seen[:12]
        out["public_api_note"] = f"官方 Colab REST API ({api_base}) 连通正常"
    else:
        r_rt = session.get(f"{api_base}/runtimes", timeout=10)
        if r_rt.ok:
            out["public_api_ok"] = True
            out["public_api_note"] = f"官方 Colab REST API ({api_base}/runtimes) 连通正常"
        else:
            out["public_api_note"] = f"官方 REST API 返回 HTTP {r_specs.status_code}（核心 CLI 通道仍可正常调度 GPU）"
except Exception as exc:
    out["public_api_note"] = f"REST API 探测提示：{exc}"

print(json.dumps(out, ensure_ascii=False))
"""
    try:
        res = _run_colab_helper(helper, payload={"api_base": api_base}, timeout=30.0)
    except Exception as exc:
        base_resp["message"] = f"探测 Colab 状态异常：{exc}"
        return base_resp

    base_resp.update({k: v for k, v in res.items() if k in base_resp})
    err = res.get("error", "")
    if err:
        base_resp["message"] = err
    elif not base_resp["authorized"]:
        base_resp["message"] = (
            f"官方 colab CLI ({base_resp['cli_version'] or '已安装'}) 已就绪，"
            "点击下方「获取官方 Google 授权链接」完成登录即可解锁云端 GPU 调度（令牌安全存于系统凭据库）。"
        )
    else:
        cu_str = (
            f"剩余 {base_resp['ccu_balance']} CU（当前消耗 {base_resp['ccu_rate_hourly']}/hr）"
            if base_resp["ccu_balance"] is not None
            else "已认证"
        )
        base_resp["message"] = (
            f"Google Colab 官方通道在线（{base_resp['email'] or '已登录'} · {cu_str} · "
            f"活跃运行时 {len(base_resp['active_sessions'])} 个）"
        )
    return base_resp


def stop_colab_session(endpoint: str) -> dict[str, Any]:
    """通过官方 colab-cli Client 释放指定云端运行时，避免闲置扣计算单元"""
    ep = (endpoint or "").strip()
    if not ep:
        raise ValueError("缺少要停止的运行时 endpoint")

    helper = r"""
import json, sys
from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from colab_cli.auth import PUBLIC_SCOPES, _get_token_from_keyring, _save_token_to_keyring
from colab_cli.client import Client, Prod

req = json.loads(sys.stdin.read())
token_json = _get_token_from_keyring()
if not token_json:
    raise RuntimeError("尚未完成 Colab OAuth2 授权")
creds = Credentials.from_authorized_user_info(json.loads(token_json), PUBLIC_SCOPES)
if not creds.valid and creds.expired and creds.refresh_token:
    creds.refresh(Request())
    _save_token_to_keyring(creds.to_json())
client = Client(Prod(), AuthorizedSession(creds))
client.unassign(req["endpoint"])
print(json.dumps({"stopped": req["endpoint"]}))
"""
    _run_colab_helper(helper, payload={"endpoint": ep}, timeout=25.0)
    return probe_colab()


def _normalize_gpu_tier(gpu_tier: str) -> str:
    g = (gpu_tier or "").strip().upper()
    if g in VALID_GPU_TIERS:
        return g
    cfg_g = load_config().get("COLAB_DEFAULT_GPU", "L4").strip().upper()
    return cfg_g if cfg_g in VALID_GPU_TIERS else "L4"


def _gpu_fallback_chain(primary: str) -> list[str]:
    """抗排队自动降级链：优先分配目标卡型，高峰期缺卡时自动回退至下一档可用 GPU"""
    chains: dict[str, list[str]] = {
        "H100": ["H100", "A100", "L4", "T4"],
        "A100": ["A100", "L4", "T4"],
        "L4": ["L4", "T4"],
        "G4": ["G4", "L4", "T4"],
        "T4": ["T4"],
        "CPU": ["CPU"],
    }
    return chains.get(primary, [primary, "L4", "T4"])


def _allocate_colab_session(cli: str, session_name: str, primary_tier: str, env: dict[str, str]) -> tuple[str, list[str]]:
    """按降级链尝试分配 Colab 运行时，返回 (实际分配到的卡型, 分配日志)"""
    notes: list[str] = []
    last_err = ""
    for tier in _gpu_fallback_chain(primary_tier):
        gpu_args = ["--gpu", tier] if tier != "CPU" else []
        r_new = subprocess.run(
            [cli, "--auth=oauth2", "new", "-s", session_name, *gpu_args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        if r_new.returncode == 0:
            time.sleep(3)
            if tier != primary_tier:
                notes.append(f"提示：{primary_tier} 当前高峰紧张，已自动防排队降级至 {tier} 并分配成功")
            return tier, notes
        last_err = (r_new.stderr or r_new.stdout or f"exit {r_new.returncode}").strip()[-300:]
        notes.append(f"尝试 {tier} 未分配（{last_err[:120]}），自动尝试下一档...")
    raise RuntimeError(f"Colab GPU 分配失败（已尝试降级链 {_gpu_fallback_chain(primary_tier)}）：\n{last_err}")


def dispatch_episode_to_colab(
    project: dict[str, Any],
    episode: dict[str, Any],
    timeline_items: list[dict[str, Any]],
    srt_path: Path,
    gpu_tier: str = "L4",
    task_mode: str = "upscale_4k_30fps",
    burn_subtitles: bool = True,
    hdr_hlg: bool = True,
) -> dict[str, Any]:
    """
    通过官方 google-colab-cli 派发任务至 Google Colab 云端 GPU（默认 L4 防排队 + 自动降级）：
    - task_mode == 'probe_gpu'：租用指定 GPU 跑环境自检并自动销毁 VM；
    - task_mode == 'upscale_4k_30fps'（推荐默认）：Real-ESRGAN v3 4K 超分 + 保留源帧率电影感 + 可选 10-bit HLG HDR + 安全区字幕；
    - task_mode == 'upscale_4k_60fps'：Real-ESRGAN v3 4K 超分 + RIFE v4.26 60fps 补帧 + 可选 10-bit HLG HDR；
    - task_mode == 'ai_60fps_1080p'：RIFE v4.26 60fps 补帧（保持 1080p）；
    任务结束无论成功与否均在 finally 中强制 `colab stop` 释放 VM。
    """
    cli = resolve_colab_cli()
    if not cli:
        raise RuntimeError("未检测到官方 colab CLI，请先安装 google-colab-cli。")

    status = probe_colab()
    if not status.get("authorized"):
        raise RuntimeError("尚未完成 Google Colab 官方 OAuth2 授权。请先前往「系统设置」完成授权后再派发。")

    requested_tier = _normalize_gpu_tier(gpu_tier)
    env = _build_google_env()
    session_name = f"agvs_{uuid.uuid4().hex[:6]}"
    is_vertical = (project.get("aspect_ratio") or "9:16") == "9:16"
    target_w, target_h = (2160, 3840) if is_vertical else (3840, 2160)

    if task_mode == "probe_gpu":
        probe_script = """
import os, platform, shutil, subprocess, sys
print("=== Antigravity Video Studio · Colab Official GPU Smoke Test ===")
print("Python:", sys.version.split()[0], "| Host:", platform.node())
smi = shutil.which("nvidia-smi")
if smi:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
        text=True,
    ).strip()
    print("GPU:", out)
else:
    print("GPU: None (CPU Runtime)")
try:
    import torch
    print("PyTorch:", torch.__version__, "| CUDA:", torch.cuda.is_available())
except Exception as e:
    print("PyTorch:", e)
ff = shutil.which("ffmpeg")
print("FFmpeg:", ff or "not installed")
if ff:
    enc = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True, stderr=subprocess.DEVNULL)
    print("NVENC_H264:", "h264_nvenc" in enc, "| NVENC_HEVC:", "hevc_nvenc" in enc)
"""
        with tempfile.NamedTemporaryFile("w", suffix="_colab_probe.py", delete=False, encoding="utf-8") as tf:
            tf.write(probe_script)
            script_path = Path(tf.name)
        created = False
        try:
            actual_tier, alloc_notes = _allocate_colab_session(cli, session_name, requested_tier, env)
            created = True
            res = subprocess.run(
                [cli, "--auth=oauth2", "exec", "-s", session_name, "--timeout", "180", "-f", str(script_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=240,
                env=env,
            )
            if res.returncode != 0:
                raise RuntimeError(f"Colab GPU 冒烟测试失败：\n{(res.stderr or res.stdout)[-800:]}")
            log_lines = [*alloc_notes, res.stdout.strip() or f"Colab {actual_tier} 运行时自检通过并已自动释放 VM"]
            return {
                "mode": "probe_gpu",
                "gpu_tier": actual_tier,
                "output_path": "",
                "log": "\n".join(x for x in log_lines if x),
            }
        finally:
            if created:
                subprocess.run([cli, "--auth=oauth2", "stop", "-s", session_name], capture_output=True, text=True, timeout=45, env=env)
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

    if task_mode not in ("upscale_4k_30fps", "upscale_4k_60fps", "ai_60fps_1080p"):
        raise ValueError(f"不支持的 Colab 任务模式：{task_mode}")

    if not timeline_items:
        raise RuntimeError("当前没有已定稿的分镜视频，无法派发至 Colab 云端增强。")

    master_script = Path(__file__).resolve().parent / "colab_ai_master.py"
    if not master_script.exists():
        raise RuntimeError(f"缺少 Colab AI 引擎脚本：{master_script}")

    # 1. 本地构造真 1080p + 完整三轨音频基准片（字幕与 HLG 交由 Colab 4K 超分后原生烧录与 10-bit 封装）
    out_dir = EXPORTS_DIR / f"{project['slug']}_ep{episode['ep_number']:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_mp4 = out_dir / f"_colab_input_{tag}.mp4"
    bgm_vol = float(episode.get("bgm_volume") if episode.get("bgm_volume") is not None else 0.25)
    build_master_base_mp4(
        timeline_items,
        base_mp4,
        bgm_file_path=episode.get("bgm_file_path") or "",
        bgm_volume=bgm_vol,
        hdr_hlg=False,
        burn_srt_path=None,
    )

    mode_label_map = {
        "upscale_4k_30fps": "AI_4K30",
        "upscale_4k_60fps": "AI_4K60",
        "ai_60fps_1080p": "AI_1080p60",
    }
    mode_label = mode_label_map[task_mode] + ("_HLG" if hdr_hlg else "")
    target_fps = 30 if task_mode == "upscale_4k_30fps" else 60
    created = False
    logs: list[str] = []
    try:
        actual_tier, alloc_notes = _allocate_colab_session(cli, session_name, requested_tier, env)
        created = True
        logs.extend(alloc_notes)
        logs.append(f"[1/5] 已分配 Colab {actual_tier} 会话 ({session_name}) · 模式：{mode_label}")
        final_local_mp4 = out_dir / f"{project['name']}_E{episode['ep_number']:02d}_Colab_{actual_tier}_{mode_label}_{tag}.mp4"

        r_up_vid = subprocess.run(
            [cli, "--auth=oauth2", "upload", "-s", session_name, str(base_mp4), "/content/input_master.mp4"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if r_up_vid.returncode != 0:
            raise RuntimeError(f"上传基准成片到 Colab 失败：\n{(r_up_vid.stderr or r_up_vid.stdout)[-600:]}")

        r_up_py = subprocess.run(
            [cli, "--auth=oauth2", "upload", "-s", session_name, str(master_script), "/content/colab_ai_master.py"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if r_up_py.returncode != 0:
            raise RuntimeError(f"上传 AI 处理脚本到 Colab 失败：\n{(r_up_py.stderr or r_up_py.stdout)[-400:]}")

        remote_srt = ""
        if burn_subtitles and srt_path.exists() and srt_path.stat().st_size > 10:
            r_up_srt = subprocess.run(
                [cli, "--auth=oauth2", "upload", "-s", session_name, str(srt_path), "/content/subtitles.srt"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if r_up_srt.returncode == 0:
                remote_srt = "/content/subtitles.srt"

        logs.append(
            f"[2/5] 已上传真 1080p 三轨基准成片（{round(base_mp4.stat().st_size / (1024 * 1024), 1)} MB）与云端 AI 引擎"
            + (" + 黄金安全区中文字幕" if remote_srt else "")
        )

        runner_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "if '/content' not in sys.path:\n"
            "    sys.path.insert(0, '/content')\n"
            "import colab_ai_master\n"
            "colab_ai_master.process_video(\n"
            "    Path('/content/input_master.mp4'),\n"
            "    Path('/content/output_master.mp4'),\n"
            f"    mode={task_mode!r},\n"
            f"    target_w={target_w},\n"
            f"    target_h={target_h},\n"
            f"    target_fps={target_fps},\n"
            f"    srt_path=Path({remote_srt!r}) if {bool(remote_srt)!r} else None,\n"
            f"    hdr_hlg={bool(hdr_hlg)!r},\n"
            ")\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix="_run_ai_master.py", delete=False, encoding="utf-8") as rtf:
            rtf.write(runner_code)
            runner_path = Path(rtf.name)
        try:
            step_desc_map = {
                "upscale_4k_30fps": "Real-ESRGAN v3 4K 超分（保留原帧率电影感）",
                "upscale_4k_60fps": "Real-ESRGAN v3 4K 超分 + RIFE v4.26 60fps 补帧",
                "ai_60fps_1080p": "RIFE v4.26 60fps 补帧",
            }
            hdr_note = " + 10-bit HLG HDR 封装" if hdr_hlg else ""
            logs.append(f"[3/5] 正在云端 GPU 运行 {step_desc_map[task_mode]}{hdr_note}...")
            r_exec = subprocess.run(
                [cli, "--auth=oauth2", "exec", "-s", session_name, "--timeout", "600", "-f", str(runner_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=3600,
                env=env,
            )
        finally:
            try:
                runner_path.unlink(missing_ok=True)
            except OSError:
                pass

        exec_out = (r_exec.stdout or "").strip()
        if r_exec.returncode != 0 or "[AI Master] DONE!" not in exec_out:
            raise RuntimeError(f"Colab 云端 AI 母带处理失败：\n{(r_exec.stderr or exec_out)[-1000:]}")
        logs.append(f"[4/5] 云端 GPU AI 增强完成：\n{exec_out[-500:]}")

        r_dl = subprocess.run(
            [cli, "--auth=oauth2", "download", "-s", session_name, "/content/output_master.mp4", str(final_local_mp4)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=360,
            env=env,
        )
        if r_dl.returncode != 0 or not final_local_mp4.exists():
            raise RuntimeError(f"从 Colab 下载 AI 成片失败：\n{(r_dl.stderr or r_dl.stdout)[-600:]}")
        logs.append(f"[5/5] 已拉回母带至本机：{final_local_mp4.name}（自动释放云端 GPU 中）")

        return {
            "mode": task_mode,
            "hdr_hlg": bool(hdr_hlg),
            "gpu_tier": actual_tier,
            "output_path": str(final_local_mp4.resolve()),
            "srt_path": str(srt_path.resolve()),
            "log": "\n".join(logs),
        }
    finally:
        if created:
            subprocess.run(
                [cli, "--auth=oauth2", "stop", "-s", session_name],
                capture_output=True,
                text=True,
                timeout=45,
                env=env,
            )
        try:
            base_mp4.unlink(missing_ok=True)
        except OSError:
            pass
