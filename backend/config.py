"""
跨平台配置与安全凭据管理模块（支持 local_producer / cloud_collab 双运行模式）：
- 本地制片模式（local_producer，默认）：
  * 非敏感配置保存于项目根目录 `.env`；
  * 敏感密钥（API Key / Sync Token / Session Secret / 通知 Token）：
    - macOS 优先使用系统钥匙串 Keychain（service=antigravity-video-studio）加密存储，
      钥匙串不可用时自动平滑回退到独立受控 `.secrets.env`（权限 0o600）；
    - Windows / Linux 自动使用独立受控 `.secrets.env` 与环境变量，绝不对非 macOS 抛出异常；
- 自托管多人协作模式（cloud_collab，可选）：
  * 彻底屏蔽本机 `.env`、`.secrets.env`、Keychain 与海外模型/本地算力字段；
  * 仅从受控进程环境变量或显式密钥文件只读加载，严禁在日志或接口中输出明文密钥。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import dotenv_values, set_key
except ImportError:  # 极简环境兜底，无需强依赖 python-dotenv
    dotenv_values = None  # type: ignore[assignment]
    set_key = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
SECRETS_ENV_PATH = PROJECT_ROOT / ".secrets.env"
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR") or str(PROJECT_ROOT / "data")).expanduser().resolve()
DB_PATH = DATA_DIR / "studio.db"
MEDIA_DIR = DATA_DIR / "media"
EXPORTS_DIR = DATA_DIR / "exports"
STATIC_DIR = PROJECT_ROOT / "backend" / "static"

APP_PACKAGE_NAME = "antigravity-video-studio"
DEFAULT_BRAND_NAME = "反重力视频工坊"
KEYCHAIN_SERVICE = "antigravity-video-studio"

SECRET_KEYS = {
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "SYNC_API_TOKEN",
    "SESSION_SECRET",
    "NTFY_TOKEN",
}

# 自托管协作模式下管理员允许在线调整的脱敏安全配置项（绝不含任何密钥或海外本地算力配置）
CLOUD_SAFE_UPDATE_KEYS = {
    "BRAND_NAME",
    "DEEPSEEK_MODEL",
    "ENABLE_HOT_FEEDS",
    "NTFY_ENABLED",
    "NTFY_PUBLIC_URL",
    "NTFY_TOPIC_ADMIN",
    "NTFY_TOPIC_PARTNER",
    "PUBLIC_STUDIO_URL",
}

for _d in (DATA_DIR, MEDIA_DIR, EXPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# 官方 CLI 媒体生成默认估算单价（USD），可通过设置页 CLI_PRICING_JSON 动态覆盖
DEFAULT_CLI_PRICING: dict[str, Any] = {
    "image": {
        "1K": 0.0336,
        "2K": 0.0504,
        "4K": 0.113,
    },
    "video_per_sec": {
        "360p": 0.023,
        "720p": 0.07,
        "1080p": 0.104,
        "4k": 0.209,
    },
    "music": 0.08,
    "tts": 0.01,
}

# Google 订阅与算力档位预设（供开箱引导与设置页一键校准三油箱红线）
SUBSCRIPTION_PRESETS: dict[str, dict[str, Any]] = {
    "free": {
        "id": "free",
        "name": "免费层（AI Studio 免费 API）",
        "description": "适合零预算起步：使用 AI Studio 免费文本配额完成选题、编剧与分镜编译，本地 1080p 封装母带。",
        "gemini_daily_limit": 100,
        "gemini_monthly_credit_usd": 0.0,
        "flow_monthly_total": 0,
        "default_mode": "local_1080p",
        "media_strategy": "manual_flow",
        "recommended_gpu": "T4",
    },
    "ai_pro": {
        "id": "ai_pro",
        "name": "Google AI Pro",
        "description": "含基础月度 Flow 积分与 Colab 云端 GPU 访问能力；建议以手工 Flow 抽卡 + 免费 1080p 导出 + Colab 4K 超分为主。",
        "gemini_daily_limit": 250,
        "gemini_monthly_credit_usd": 0.0,
        "flow_monthly_total": 1000,
        "default_mode": "colab_4k_30fps",
        "media_strategy": "manual_flow",
        "recommended_gpu": "L4",
    },
    "ai_ultra": {
        "id": "ai_ultra",
        "name": "Google AI Ultra",
        "description": "含高额月度 Flow 积分（25,000）与 Colab Pro+ 高级 GPU；支持批量 Flow 视频出片 + Colab 4K/30fps HLG HDR 母带。",
        "gemini_daily_limit": 500,
        "gemini_monthly_credit_usd": 0.0,
        "flow_monthly_total": 25000,
        "default_mode": "colab_4k_30fps",
        "media_strategy": "manual_flow",
        "recommended_gpu": "L4",
    },
    "ai_ultra_dev": {
        "id": "ai_ultra_dev",
        "name": "Google AI Ultra + Developer Program 月赠金",
        "description": "三口独立油箱全开：月度 API 赠金（$100）负责 CLI 直出首尾帧/配乐/旁白/封面，Flow 25,000 积分专注高质量视频，Colab GPU 负责 4K HDR。",
        "gemini_daily_limit": 500,
        "gemini_monthly_credit_usd": 100.0,
        "flow_monthly_total": 25000,
        "default_mode": "colab_4k_30fps",
        "media_strategy": "cli_hybrid",
        "recommended_gpu": "L4",
    },
    "custom": {
        "id": "custom",
        "name": "自定义配置",
        "description": "完全手动设定每日调用上限、月度 API 预算与 Flow 积分池，按个人真实账户情况灵活校准。",
        "gemini_daily_limit": 250,
        "gemini_monthly_credit_usd": 0.0,
        "flow_monthly_total": 0,
        "default_mode": "local_1080p",
        "media_strategy": "manual_flow",
        "recommended_gpu": "L4",
    },
}


DEFAULTS: dict[str, str] = {
    # 双运行模式：local_producer（默认，本机单人全功能） | cloud_collab（可选自托管多人协作看板）
    "APP_MODE": "local_producer",
    "BRAND_NAME": DEFAULT_BRAND_NAME,
    "SUBSCRIPTION_TIER": "custom",
    "ENABLE_HOT_FEEDS": "true",
    "ENABLE_CLOUD_COLLAB": "false",
    # 可选本地 ↔ 自托管远程协作同步配置（密钥绝不落代码）
    "CLOUD_STUDIO_URL": "",
    "SYNC_API_TOKEN": "",
    "SYNC_TOKEN_FILE": "",
    # 协作端备用模型密钥文件（按需通过环境变量或受控文件提供，绝不打印明文）
    "DEEPSEEK_KEY_FILE": "",
    # 会话签名密钥（建议在自托管模式下通过受控环境变量注入）
    "SESSION_SECRET": "",
    # 可选开源通知配置（如自建 ntfy）
    "NTFY_ENABLED": "false",
    "NTFY_INTERNAL_URL": "http://127.0.0.1:2586",
    "NTFY_PUBLIC_URL": "",
    "NTFY_TOPIC_ADMIN": "",
    "NTFY_TOPIC_PARTNER": "",
    "NTFY_TOKEN": "",
    "PUBLIC_STUDIO_URL": "",
    # 以下为本地制片模式配置（cloud_collab 模式下绝不暴露给前端或调用）
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    # 默认留空走直连；若需通过代理访问 Google 服务，可手动填写代理地址（如 http://127.0.0.1:PORT）
    "GOOGLE_PROXY_URL": "",
    "DEEPSEEK_ENABLED": "false",
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "DEFAULT_PROVIDER": "gemini",
    # 媒体生成引擎模式：auto（自动优先官方 google-genai SDK 直连或已安装 CLI）| sdk（强制官方 SDK 直连）| cli（强制 gemini-api CLI）
    "MEDIA_ENGINE_MODE": "auto",
    # 官方媒体模型与动态单价配置（同时兼容 google-genai SDK 直连与 gemini-api CLI）
    "CLI_IMAGE_MODEL": "gemini-nano-banana-2.1",
    "CLI_VIDEO_MODEL": "gemini-omni-1.2-flash",
    "CLI_MUSIC_MODEL": "lyria-3.5",
    "CLI_TTS_MODEL": "gemini-3.8-flash-tts",
    "CLI_PRICING_JSON": "",
    # 本地下载目录与素材直拖托盘配置
    "WATCH_DOWNLOADS_DIR": "~/Downloads",
    "DOCK_DRAG_DIR": "",
    "KEEP_CHINESE_COPY_ON_CLAIM": "true",
    "AUTO_RENAME_FLOW_DOWNLOADS": "true",
    "AUTO_TRASH_REJECTED_BATCH": "true",
    "INBOX_FILTER_MODE": "flow_only",
    # Google Colab 官方 REST API / 托管 MCP / 官方 CLI 配置（均为非敏感地址）
    "COLAB_API_BASE": "https://colaboratory.googleapis.com/v1beta",
    "COLAB_MCP_ENDPOINT": "https://colaboratory.googleapis.com/mcp",
    "COLAB_DEFAULT_GPU": "L4",
    "COLAB_CLI_PATH": "",
    "COLAB_GDRIVE_ROOT": "AntigravityVideoStudio",
}


def _read_secret_file(path_str: str) -> str:
    """从受控密钥文件只读加载单行密钥（严禁打印明文内容）"""
    p_raw = (path_str or "").strip()
    if not p_raw:
        return ""
    try:
        p = Path(p_raw).expanduser()
        if not p.exists() or not p.is_file():
            return ""
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                # 兼容 KEY=VALUE 格式或纯密钥单行
                if "=" in s and s.split("=", 1)[0].strip().isupper():
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
                return s
    except OSError:
        return ""
    return ""


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if dotenv_values is not None:
        raw = dotenv_values(path)
        return {str(k): str(v) for k, v in raw.items() if k and v is not None}
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def _write_env_kv(path: Path, key: str, value: str, *, header_comment: str = "", restricted: bool = False) -> None:
    """安全写入单条键值对到指定 env 文件；支持删除空值并尝试收紧权限至 0o600"""
    if not path.exists():
        if not value:
            return
        init_text = f"# {header_comment}\n" if header_comment else ""
        path.write_text(init_text, encoding="utf-8")
        if restricted:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    existing = _parse_env_file(path)
    if value == "":
        existing.pop(key, None)
    else:
        existing[key] = value

    if set_key is not None and value != "":
        set_key(str(path), key, value)
    else:
        lines: list[str] = []
        if header_comment:
            lines.append(f"# {header_comment}")
        for ek, ev in existing.items():
            escaped = ev.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"{ek}='{escaped}'")
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    if restricted:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _keychain_get(account: str) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _keychain_set(account: str, value: str) -> bool:
    """尝试写入 macOS 系统钥匙串；成功返回 True，失败返回 False（供自动回退 `.secrets.env`）"""
    if sys.platform != "darwin":
        return False
    try:
        if not value:
            subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return True
        res = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", value],
            capture_output=True,
            text=True,
            timeout=4.0,
        )
        return res.returncode == 0
    except Exception:
        return False


def _secrets_file_get(account: str) -> str:
    if not SECRETS_ENV_PATH.exists():
        return ""
    vals = _parse_env_file(SECRETS_ENV_PATH)
    return (vals.get(account) or "").strip()


def _secrets_file_set(account: str, value: str) -> None:
    _write_env_kv(
        SECRETS_ENV_PATH,
        account,
        value,
        header_comment="受控本地敏感密钥文件（已被 .gitignore 严格忽略，请勿提交到代码仓库）",
        restricted=True,
    )


def get_secret(account: str) -> str:
    """跨平台读取受控密钥：macOS 优先 Keychain 再回退 `.secrets.env`，Windows/Linux 直接读 `.secrets.env`"""
    if sys.platform == "darwin":
        val = _keychain_get(account)
        if val:
            return val
    return _secrets_file_get(account)


def set_secret(account: str, value: str) -> str:
    """
    跨平台写入受控密钥（绝不对非 macOS 抛出异常）：
    - macOS 优先写入系统 Keychain，失败时自动回退 `.secrets.env`；
    - Windows / Linux 直接写入受控 `.secrets.env`（尝试 chmod 0o600）。
    返回实际使用的存储后端标识。
    """
    clean_val = (value or "").strip()
    if sys.platform == "darwin":
        if _keychain_set(account, clean_val):
            # 若清空密钥，同步清理 `.secrets.env` 避免残留旧值
            if not clean_val and SECRETS_ENV_PATH.exists():
                _secrets_file_set(account, "")
            return "macos_keychain"
    _secrets_file_set(account, clean_val)
    return "secrets_env"


def get_cli_pricing(cfg: dict[str, str] | None = None) -> dict[str, Any]:
    """合并官方默认单价与设置中自定义 `CLI_PRICING_JSON`，返回安全数值结构"""
    c = cfg if cfg is not None else load_config()
    merged: dict[str, Any] = copy.deepcopy(DEFAULT_CLI_PRICING)
    raw = (c.get("CLI_PRICING_JSON") or "").strip()
    if not raw:
        return merged
    try:
        parsed = json.loads(raw)
    except Exception:
        return merged
    if not isinstance(parsed, dict):
        return merged

    if isinstance(parsed.get("image"), dict):
        for k, v in parsed["image"].items():
            try:
                num = float(v)
                if num >= 0:
                    merged["image"][str(k).upper()] = round(num, 6)
            except (TypeError, ValueError):
                continue

    if isinstance(parsed.get("video_per_sec"), dict):
        for k, v in parsed["video_per_sec"].items():
            try:
                num = float(v)
                if num >= 0:
                    merged["video_per_sec"][str(k).lower()] = round(num, 6)
            except (TypeError, ValueError):
                continue

    for scalar_key in ("music", "tts"):
        if scalar_key in parsed:
            try:
                num = float(parsed[scalar_key])
                if num >= 0:
                    merged[scalar_key] = round(num, 6)
            except (TypeError, ValueError):
                continue

    return merged


def load_config() -> dict[str, str]:
    """合并默认值、非敏感 `.env`、环境变量、受控密钥文件与跨平台安全凭据存储"""
    cfg = dict(DEFAULTS)
    env_mode = (os.environ.get("APP_MODE") or "").strip().lower()

    # 1. 本地模式读取非敏感 `.env`（cloud_collab 模式下跳过项目根 `.env`，杜绝本地残留污染）
    if env_mode != "cloud_collab" and ENV_PATH.exists():
        file_vals = _parse_env_file(ENV_PATH)
        for k, v in file_vals.items():
            if k in cfg and k not in SECRET_KEYS and v is not None:
                cfg[k] = str(v)

    # 2. 进程环境变量优先级高于 `.env`（适配 systemd / 容器 / CLI 注入）
    for k in DEFAULTS:
        env_v = os.environ.get(k)
        if env_v is not None and env_v != "":
            cfg[k] = env_v

    app_mode = "cloud_collab" if cfg.get("APP_MODE", "").strip().lower() == "cloud_collab" else "local_producer"
    cfg["APP_MODE"] = app_mode

    # 3. 显式密钥文件回退（按需配置，绝不硬编码特定私有路径）
    if not cfg["DEEPSEEK_API_KEY"].strip():
        ds_file = cfg.get("DEEPSEEK_KEY_FILE", "").strip()
        if ds_file:
            cfg["DEEPSEEK_API_KEY"] = _read_secret_file(ds_file)

    if not cfg["SYNC_API_TOKEN"].strip():
        st_file = cfg.get("SYNC_TOKEN_FILE", "").strip()
        if st_file:
            cfg["SYNC_API_TOKEN"] = _read_secret_file(st_file)

    # 4. 自托管协作模式允许管理员在线调整的非敏感运行项（存于 DATA_DIR/cloud_settings.json）
    if app_mode == "cloud_collab":
        cloud_override_file = DATA_DIR / "cloud_settings.json"
        if cloud_override_file.exists():
            try:
                raw_ov = json.loads(cloud_override_file.read_text(encoding="utf-8"))
                if isinstance(raw_ov, dict):
                    for k, v in raw_ov.items():
                        if k in CLOUD_SAFE_UPDATE_KEYS and v is not None:
                            cfg[k] = str(v)
            except Exception:
                pass

    # 5. 本地模式下按平台读取加密凭据（macOS 优先 Keychain 再回退 `.secrets.env`，Windows/Linux 读 `.secrets.env`）
    if app_mode != "cloud_collab":
        for sk in SECRET_KEYS:
            if not os.environ.get(sk) and not cfg.get(sk, "").strip():
                sec_val = get_secret(sk)
                if sec_val:
                    cfg[sk] = sec_val

    return cfg


def get_app_mode(cfg: dict[str, str] | None = None) -> str:
    """获取当前运行模式：local_producer 或 cloud_collab"""
    c = cfg if cfg is not None else load_config()
    return "cloud_collab" if (c.get("APP_MODE") or "").strip().lower() == "cloud_collab" else "local_producer"


def is_cloud_collab(cfg: dict[str, str] | None = None) -> bool:
    """是否处于可选自托管多人协作模式"""
    return get_app_mode(cfg) == "cloud_collab"


def update_config(updates: dict[str, Any]) -> dict[str, str]:
    """非敏感项写本机 `.env`（协作模式写 `DATA_DIR/cloud_settings.json`），密钥走跨平台安全凭据存储"""
    if is_cloud_collab():
        cloud_override_file = DATA_DIR / "cloud_settings.json"
        current: dict[str, str] = {}
        if cloud_override_file.exists():
            try:
                loaded = json.loads(cloud_override_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = {str(k): str(v) for k, v in loaded.items() if k in CLOUD_SAFE_UPDATE_KEYS}
            except Exception:
                current = {}
        for k, v in updates.items():
            if k not in CLOUD_SAFE_UPDATE_KEYS or v is None:
                continue
            current[k] = ("true" if v else "false") if isinstance(v, bool) else str(v).strip()
        cloud_override_file.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return load_config()

    if not ENV_PATH.exists():
        ENV_PATH.write_text("# 仅存非敏感本机配置（敏感密钥已隔离存储于系统钥匙串或受控 .secrets.env）\n", encoding="utf-8")
        try:
            os.chmod(ENV_PATH, 0o600)
        except OSError:
            pass

    for k, v in updates.items():
        if k not in DEFAULTS or v is None:
            continue
        if isinstance(v, bool):
            sval = "true" if v else "false"
        else:
            sval = str(v).strip()

        if k in SECRET_KEYS:
            if sval == "__CLEAR__":
                set_secret(k, "")
            elif sval and "***" not in sval:
                set_secret(k, sval)
            continue

        if k == "MEDIA_ENGINE_MODE":
            sval = sval.lower()
            if sval not in VALID_MEDIA_ENGINE_MODES:
                sval = "auto"

        if set_key is not None:
            set_key(str(ENV_PATH), k, sval)
        else:
            _write_env_kv(
                ENV_PATH,
                k,
                sval,
                header_comment="仅存非敏感本机配置（敏感密钥已隔离存储于系统钥匙串或受控 .secrets.env）",
                restricted=True,
            )

    return load_config()


def mask_key(raw: str) -> str:
    """密钥脱敏显示，避免界面和日志明文泄漏"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "***"
    return f"{raw[:4]}***{raw[-4:]}"


def _is_antigravity_colab_mcp_configured(expected_endpoint: str) -> bool:
    """检查本机全局 mcp_config.json 是否已注册官方 Colab MCP 端点"""
    mcp_path = Path("~/.gemini/config/mcp_config.json").expanduser()
    if not mcp_path.exists():
        return False
    try:
        raw = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", {})
        if not isinstance(servers, dict):
            return False
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


def resolve_dock_drag_dir(cfg: dict[str, str] | None = None) -> Path:
    """解析素材直拖目录（留空时默认跟随监听下载目录）"""
    c = cfg or load_config()
    custom = (c.get("DOCK_DRAG_DIR") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path(c.get("WATCH_DOWNLOADS_DIR") or "~/Downloads").expanduser()


VALID_MEDIA_ENGINE_MODES: tuple[str, ...] = ("auto", "sdk", "cli")


def get_media_engine_mode(cfg: dict[str, str] | None = None) -> str:
    """获取规范化后的媒体生成引擎模式：auto / sdk / cli"""
    c = cfg if cfg is not None else load_config()
    raw = (c.get("MEDIA_ENGINE_MODE") or "auto").strip().lower()
    return raw if raw in VALID_MEDIA_ENGINE_MODES else "auto"


def is_google_genai_sdk_available() -> bool:
    """探测当前 Python 运行环境是否已安装官方 google-genai SDK（不触发重型导入副作用）"""
    if "google.genai" in sys.modules and sys.modules["google.genai"] is not None:
        return True
    try:
        return importlib.util.find_spec("google.genai") is not None
    except Exception:
        return False


def _is_executable_file(p: Path) -> bool:
    if not p.exists() or not p.is_file():
        return False
    if sys.platform == "win32":
        return True
    return os.access(p, os.X_OK)


def find_gemini_cli_bin() -> str | None:
    """跨平台探测官方 gemini-api CLI 可执行文件路径，未安装时返回 None（不抛异常）"""
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
            if _is_executable_file(p):
                return str(p)
        return None

    candidates = [
        Path.home() / ".local" / "bin" / "gemini-api",
        Path("/usr/local/bin/gemini-api"),
        Path("/opt/homebrew/bin/gemini-api"),
    ]
    for p in candidates:
        if _is_executable_file(p):
            return str(p)
    return shutil.which("gemini-api")


def is_gemini_cli_available() -> bool:
    """探测本机是否已安装官方 gemini-api CLI（供 /api/health 与前端设置页共用）"""
    try:
        return find_gemini_cli_bin() is not None
    except Exception:
        return False


def _parse_bool(val: str | None, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def get_public_config() -> dict[str, Any]:
    """返回给前端的安全配置对象（cloud_collab 模式 100% 剥离海外模型与本地算力密钥字段）"""
    cfg = load_config()
    brand_name = (cfg.get("BRAND_NAME") or DEFAULT_BRAND_NAME).strip() or DEFAULT_BRAND_NAME
    ntfy_enabled = _parse_bool(cfg.get("NTFY_ENABLED"), False)
    enable_hot_feeds = _parse_bool(cfg.get("ENABLE_HOT_FEEDS"), True)
    enable_cloud_collab = _parse_bool(cfg.get("ENABLE_CLOUD_COLLAB"), False)

    if is_cloud_collab(cfg):
        return {
            "app_mode": "cloud_collab",
            "brand_name": brand_name,
            "ai_provider": "deepseek",
            "ai_ready": bool(cfg["DEEPSEEK_API_KEY"].strip()),
            "enable_hot_feeds": enable_hot_feeds,
            "enable_cloud_collab": True,
            "ntfy_enabled": ntfy_enabled,
        }

    ds_enabled = _parse_bool(cfg.get("DEEPSEEK_ENABLED"), False)
    keep_cn_copy = _parse_bool(cfg.get("KEEP_CHINESE_COPY_ON_CLAIM"), True)
    auto_rename_dl = _parse_bool(cfg.get("AUTO_RENAME_FLOW_DOWNLOADS"), True)
    auto_trash_batch = _parse_bool(cfg.get("AUTO_TRASH_REJECTED_BATCH"), True)
    colab_mcp = cfg.get("COLAB_MCP_ENDPOINT", "https://colaboratory.googleapis.com/mcp")
    watch_dir = cfg.get("WATCH_DOWNLOADS_DIR") or "~/Downloads"

    return {
        "app_mode": "local_producer",
        "brand_name": brand_name,
        "subscription_tier": (cfg.get("SUBSCRIPTION_TIER") or "custom").strip() or "custom",
        "enable_hot_feeds": enable_hot_feeds,
        "enable_cloud_collab": enable_cloud_collab,
        "cloud_studio_url": cfg.get("CLOUD_STUDIO_URL", ""),
        "sync_token_set": bool(cfg.get("SYNC_API_TOKEN", "").strip()),
        "ntfy_enabled": ntfy_enabled,
        "public_studio_url": cfg.get("PUBLIC_STUDIO_URL", ""),
        "gemini_key_set": bool(cfg["GEMINI_API_KEY"].strip()),
        "gemini_key_masked": mask_key(cfg["GEMINI_API_KEY"]),
        "gemini_model": cfg["GEMINI_MODEL"],
        "google_proxy_url": cfg["GOOGLE_PROXY_URL"],
        "deepseek_enabled": ds_enabled,
        "deepseek_key_set": bool(cfg["DEEPSEEK_API_KEY"].strip()),
        "deepseek_key_masked": mask_key(cfg["DEEPSEEK_API_KEY"]),
        "deepseek_model": cfg["DEEPSEEK_MODEL"],
        "default_provider": "deepseek" if (ds_enabled and cfg["DEFAULT_PROVIDER"] == "deepseek") else "gemini",
        "media_engine_mode": get_media_engine_mode(cfg),
        "google_genai_sdk_ok": is_google_genai_sdk_available(),
        "gemini_cli_ok": is_gemini_cli_available(),
        "cli_image_model": cfg.get("CLI_IMAGE_MODEL", DEFAULTS["CLI_IMAGE_MODEL"]),
        "cli_video_model": cfg.get("CLI_VIDEO_MODEL", DEFAULTS["CLI_VIDEO_MODEL"]),
        "cli_music_model": cfg.get("CLI_MUSIC_MODEL", DEFAULTS["CLI_MUSIC_MODEL"]),
        "cli_tts_model": cfg.get("CLI_TTS_MODEL", DEFAULTS["CLI_TTS_MODEL"]),
        "cli_pricing_json": cfg.get("CLI_PRICING_JSON", ""),
        "cli_pricing": get_cli_pricing(cfg),
        "secret_storage_backend": "macos_keychain" if sys.platform == "darwin" else "secrets_env",
        "watch_downloads_dir": watch_dir,
        "watch_downloads_resolved": str(Path(watch_dir).expanduser()),
        "dock_drag_dir": cfg.get("DOCK_DRAG_DIR", ""),
        "dock_drag_resolved": str(resolve_dock_drag_dir(cfg)),
        "keep_chinese_copy_on_claim": keep_cn_copy,
        "auto_rename_flow_downloads": auto_rename_dl,
        "auto_trash_rejected_batch": auto_trash_batch,
        "inbox_filter_mode": cfg.get("INBOX_FILTER_MODE", "flow_only"),
        "colab_api_base": cfg.get("COLAB_API_BASE", "https://colaboratory.googleapis.com/v1beta"),
        "colab_mcp_endpoint": colab_mcp,
        "colab_default_gpu": cfg.get("COLAB_DEFAULT_GPU", "L4"),
        "colab_cli_path": cfg.get("COLAB_CLI_PATH", ""),
        "colab_gdrive_root": cfg.get("COLAB_GDRIVE_ROOT", "AntigravityVideoStudio"),
        "colab_mcp_configured": _is_antigravity_colab_mcp_configured(colab_mcp),
    }
