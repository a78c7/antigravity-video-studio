#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${AGVS_PORT:-8765}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "错误：无效端口 AGVS_PORT=${PORT}（请填写 1–65535 之间的整数）。" >&2
  exit 1
fi

URL="http://127.0.0.1:${PORT}"
HEALTH_URL="${URL}/api/health"

DEPS=(
  "fastapi>=0.115.0"
  "uvicorn[standard]>=0.32.0"
  "httpx[socks]>=0.28.0"
  "google-genai>=1.0.0"
  "openai>=1.60.0"
  "pydantic>=2.10.0"
  "python-multipart>=0.0.18"
  "watchdog>=6.0.0"
  "python-dotenv>=1.0.1"
  "pillow>=11.0.0"
)

find_any_python() {
  local cand
  for cand in ".venv/bin/python" python3.13 python3.12 python3 python; do
    if [ "$cand" = ".venv/bin/python" ]; then
      if [ -x "$cand" ]; then
        echo "$cand"
        return 0
      fi
    elif command -v "$cand" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

find_python312() {
  local cand
  for cand in python3.13 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}

open_browser() {
  local target_url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$target_url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target_url" >/dev/null 2>&1 || true
  fi
}

is_port_in_use() {
  local target_port="$1"
  local py_bin
  py_bin="$(find_any_python || true)"
  if [ -n "$py_bin" ]; then
    "$py_bin" - "$target_port" <<'PY' 2>/dev/null
import socket
import sys

port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
try:
    rc = s.connect_ex(("127.0.0.1", port))
    raise SystemExit(0 if rc == 0 else 1)
finally:
    s.close()
PY
    return $?
  fi

  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$target_port" >/dev/null 2>&1
    return $?
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$target_port" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi

  return 1
}

is_agvs_healthy() {
  local url="$1"
  local py_bin
  py_bin="$(find_any_python || true)"
  if [ -n "$py_bin" ]; then
    "$py_bin" - "$url" <<'PY' 2>/dev/null
import json
import sys
import urllib.request

url = sys.argv[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(url, timeout=1.5) as resp:
        if getattr(resp, "status", 200) != 200:
            raise SystemExit(1)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
except Exception:
    raise SystemExit(1)

raise SystemExit(0 if isinstance(data, dict) and data.get("app") == "antigravity-video-studio" else 1)
PY
    return $?
  fi

  if command -v curl >/dev/null 2>&1; then
    local body
    body="$(curl --noproxy '*' -fsS --connect-timeout 1 --max-time 2 "$url" 2>/dev/null || true)"
    if [[ "$body" =~ \"app\"[[:space:]]*:[[:space:]]*\"antigravity-video-studio\" ]]; then
      return 0
    fi
  fi

  return 1
}

# 启动前先检测 127.0.0.1:$PORT 是否已被占用，避免启动失败或误弹其他软件网页
if is_port_in_use "$PORT"; then
  if is_agvs_healthy "$HEALTH_URL"; then
    echo "反重力视频工坊已在 ${URL} 运行中。"
    if [ "${AGVS_NO_BROWSER:-0}" != "1" ]; then
      open_browser "$URL"
    fi
    exit 0
  else
    echo "错误：端口 ${PORT}（127.0.0.1:${PORT}）已被其他程序占用，反重力视频工坊无法启动。" >&2
    echo "请先关闭占用该端口的程序，或改用其他端口启动，例如：" >&2
    echo "  AGVS_PORT=8766 ./start.sh" >&2
    exit 1
  fi
fi

if [ ! -x ".venv/bin/python" ] || [ ! -x ".venv/bin/uvicorn" ]; then
  echo "正在初始化 Python 3.12+ 虚拟环境并安装依赖..."
  if command -v uv >/dev/null 2>&1; then
    if ! uv venv --python 3.12 .venv 2>/dev/null; then
      PYTHON_BIN="$(find_python312 || true)"
      if [ -z "$PYTHON_BIN" ]; then
        echo "错误：未检测到 Python 3.12 及以上版本，请先安装 Python 3.12+ 后重试。" >&2
        exit 1
      fi
      uv venv --python "$PYTHON_BIN" .venv
    fi
    uv pip install --python .venv/bin/python "${DEPS[@]}"
  else
    PYTHON_BIN="$(find_python312 || true)"
    if [ -z "$PYTHON_BIN" ]; then
      echo "错误：未检测到 Python 3.12 及以上版本（或安装 uv 以自动管理 Python）。" >&2
      echo "  macOS: brew install python@3.12   Linux: 请通过系统包管理器或 uv 安装 Python 3.12+" >&2
      exit 1
    fi
    "$PYTHON_BIN" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install "${DEPS[@]}"
  fi
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "提示：未检测到 ffmpeg，本地视频剪辑、混音与音画体检功能需安装 ffmpeg。"
  echo "  macOS: brew install ffmpeg   Ubuntu/Debian: sudo apt install ffmpeg"
fi

echo "启动「反重力视频工坊」本地制片台：${URL}"
echo "按 Ctrl+C 可随时停止服务（单进程轻量模式，数据仅保存在本地 data/ 目录）"

# 后台轮询 /api/health（绕过终端代理），确认服务真正就绪后再打开浏览器
if [ "${AGVS_NO_BROWSER:-0}" != "1" ]; then
  SERVER_PID="$$"
  (
    for _ in $(seq 1 30); do
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        exit 0
      fi
      if is_agvs_healthy "$HEALTH_URL"; then
        open_browser "$URL"
        exit 0
      fi
      sleep 0.5
    done
  ) &
fi

exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
