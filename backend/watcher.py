"""
跨平台下载目录实时防抖监听收件箱（支持 Flow 精准过滤 + 并发强去重，兼容 macOS / Windows / Linux）：
- 基于 watchdog 同时监听 on_created 与 on_moved；
- 默认启用 flow_only 清爽模式：
  * macOS：优先读取原生下载来源扩展属性 `kMDItemWhereFroms`（必要时回退 `mdls`），精准识别 Google Flow / Google AI 下载，
    自动屏蔽常见截屏、社交杂图与小图标；
  * Windows / Linux：不调用 macOS 专属接口，优雅降级为媒体扩展名 + 文件体积 + 文件名/分辨率特征过滤；
- 0.5 秒文件大小稳定防抖 + 二次持锁去重 + 已忽略记录（不重复弹窗）；
- 支持一键认领瞬移归位、中文规范重命名与跨平台安全移入系统回收站/废纸篓。
"""

from __future__ import annotations

import ctypes
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import DATA_DIR, load_config, resolve_dock_drag_dir
from .media_ops import probe_media

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
ALLOWED_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS
TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp", ".opdownload")

# Google Flow / Google AI 官方下载来源域名特征（macOS kMDItemWhereFroms）
FLOW_ORIGIN_KEYWORDS: tuple[str, ...] = (
    "flow.google.com",
    "labs.google",
    "googleusercontent.com",
    "storage.googleapis.com",
    "googleapis.com",
    "aistudio.google.com",
    "gemini.google.com",
    "deepmind.google",
)

# 常见日常杂图/截屏/手机原图文件名特征（flow_only 模式下优先拦截）
JUNK_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(截屏|屏幕快照|screenshot|screen\s*shot|clean\s*shot|snipaste)", re.IGNORECASE),
    re.compile(r"^(wechat|wx_camera|mmexport|wxid_|qq图片|tim图片|dingtalk|feishu)", re.IGNORECASE),
    re.compile(r"^(img_\d{4,}|dsc_?\d{4,}|pxl_\d{6,}|dcim|photo_\d{4,}|image\(\d+\))", re.IGNORECASE),
    re.compile(r"^(avatar|icon|logo|favicon|banner|qrcode|二维码|收款码|发票)", re.IGNORECASE),
)

# 识别典型 Flow / AI 输出命名（兼容 _8位hex、_8位hex_2K/4K/1K、标准 UUID、常见生成前缀）
FLOW_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"_[0-9a-f]{8}(?:_[124]k)?$", re.IGNORECASE),
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),
    re.compile(r"^(flow|veo|omni|lyria|imagen|nano[_-]?banana|agvs[_-]?shot)", re.IGNORECASE),
)

# 过滤极小的日常网页缩略图/表情包（单位：字节）
MIN_IMAGE_BYTES_IN_FLOW_MODE = 45 * 1024
MIN_VIDEO_BYTES_IN_FLOW_MODE = 100 * 1024


def sanitize_filename_stem(raw: str, max_len: int = 48) -> str:
    """清洗中文/英文文件名主体：去除路径分隔符与非法符号，保留直观中文可读性"""
    text = (raw or "").strip()
    lower = text.lower()
    for ext in ALLOWED_EXTS:
        if lower.endswith(ext):
            text = text[: -len(ext)]
            break
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text)
    text = re.sub(r"[\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._ ")
    if len(text) > max_len:
        text = text[:max_len].rstrip("._ ")
    return text


def classify_media_type(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def _get_xattr_bytes(path: Path, attr_name: str) -> bytes | None:
    """仅在 macOS 读取扩展属性（优先 libc.getxattr 零子进程开销）"""
    if sys.platform != "darwin":
        return None
    if hasattr(os, "getxattr"):
        try:
            return os.getxattr(str(path), attr_name)
        except OSError:
            return None
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libc.getxattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        libc.getxattr.restype = ctypes.c_ssize_t
        p_b = os.fsencode(str(path))
        a_b = attr_name.encode("utf-8")
        sz = libc.getxattr(p_b, a_b, None, 0, 0, 0)
        if sz <= 0:
            return None
        buf = ctypes.create_string_buffer(sz)
        got = libc.getxattr(p_b, a_b, buf, sz, 0, 0)
        if got <= 0:
            return None
        return buf.raw[:got]
    except Exception:
        return None


def read_macos_where_froms(path: Path) -> list[str]:
    """仅在 macOS 读取下载来源属性 kMDItemWhereFroms；Windows/Linux 直接返回空列表绝不报错"""
    if sys.platform != "darwin":
        return []
    raw = _get_xattr_bytes(path, "com.apple.metadata:kMDItemWhereFroms")
    if raw:
        try:
            parsed = plistlib.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()]
        except Exception:
            pass
    # xattr 读取不到时，在 macOS 下轻量回退一次 mdls
    try:
        res = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemWhereFroms", str(path)],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout and "(null)" not in res.stdout:
            urls = re.findall(r"https?://[^\s\",)]+", res.stdout)
            if urls:
                return urls
    except Exception:
        pass
    return []


def check_flow_origin(path: Path, mtype: str, filter_mode: str, is_dedicated_dir: bool) -> tuple[bool, str]:
    """
    判断文件是否应进入素材收件箱，返回 (是否放行, 来源标签)：
    - 若切换为 all 模式，或专门指向独立子目录（非公共 ~/Downloads），全部放行；
    - macOS 默认 flow_only 模式：优先按 kMDItemWhereFroms + 文件名特征筛选；
    - Windows / Linux 默认 flow_only 模式：优雅降级为排除杂图特征 + 体积/命名/视频分辨率特征过滤。
    """
    if sys.platform == "darwin":
        where_froms = read_macos_where_froms(path)
        joined_origins = " ".join(where_froms).lower()
        if any(kw in joined_origins for kw in FLOW_ORIGIN_KEYWORDS):
            return True, "Google Flow"
    else:
        where_froms = []

    if filter_mode == "all" or is_dedicated_dir:
        return True, "本地目录" if is_dedicated_dir else "全部下载"

    # flow_only 模式且位于公共 ~/Downloads：
    if where_froms:
        return False, ""

    stem = path.stem.strip()
    for pat in JUNK_NAME_PATTERNS:
        if pat.search(stem):
            return False, ""

    try:
        size_bytes = path.stat().st_size
    except OSError:
        return False, ""

    if mtype == "image" and size_bytes < MIN_IMAGE_BYTES_IN_FLOW_MODE:
        return False, ""
    if mtype == "video" and size_bytes < MIN_VIDEO_BYTES_IN_FLOW_MODE:
        return False, ""

    for pat in FLOW_FILENAME_PATTERNS:
        if pat.search(stem):
            return True, "Flow 命名匹配"

    # Windows / Linux 缺少下载来源元数据时，按常见生成视频/音频尺寸特征宽松放行，避免漏收 Flow 文件
    if sys.platform != "darwin":
        if mtype == "audio":
            return True, "本地音频匹配"
        if mtype == "video":
            meta = probe_media(path)
            w, h = int(meta.get("width") or 0), int(meta.get("height") or 0)
            dur = float(meta.get("duration_sec") or 0.0)
            if max(w, h) >= 640 and 1.0 <= dur <= 30.0:
                return True, "短视频特征匹配"

    return False, ""


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _win_move_to_recycle_bin(src: Path) -> bool:
    """Windows 下调用 Shell32 SHFileOperationW 安全移入系统回收站"""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400

        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = str(src) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not src.exists()
    except Exception:
        return False


def move_file_to_trash(path: Path) -> Path:
    """跨平台将文件安全移入系统回收站/废纸篓（绝不做不可恢复的强制删除，自动防重名）"""
    src = path.expanduser().resolve()
    if sys.platform == "win32":
        if _win_move_to_recycle_bin(src):
            return src
        trash_dir = DATA_DIR / ".trash"
    elif sys.platform == "darwin":
        trash_dir = Path("~/.Trash").expanduser()
    else:
        trash_dir = Path("~/.local/share/Trash/files").expanduser()

    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / src.name
    idx = 1
    while dest.exists():
        dest = trash_dir / f"{src.stem}_{idx}{src.suffix}"
        idx += 1
    shutil.move(str(src), str(dest))
    return dest


class DownloadsInbox:
    """线程安全的 Flow 素材收件箱（强去重 + 智能挡杂图 + 按当前镜自动中文化 + 废图进回收站）"""

    def __init__(self, max_items: int = 35) -> None:
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._in_progress: set[str] = set()
        self._dismissed_paths: set[str] = set()
        self._suppressed_paths: dict[str, float] = {}
        self._active_context_label: str = ""
        self._active_context_at: float = 0.0
        self._filtered_count: int = 0
        self._observer: Observer | None = None
        self._watch_dir: Path | None = None
        self._status: str = "stopped"
        self._error: str = ""

    def set_active_context(self, label: str) -> str:
        """记录当前正在操作的分镜/资产简短中文名，新落盘的乱码素材自动套用该名称"""
        clean = sanitize_filename_stem(label, max_len=16)
        with self._lock:
            self._active_context_label = clean
            self._active_context_at = time.time() if clean else 0.0
        return clean

    def _get_fresh_context_label(self, ttl_sec: float = 1800.0) -> str:
        with self._lock:
            if self._active_context_label and (time.time() - self._active_context_at) <= ttl_sec:
                return self._active_context_label
            return ""

    def _current_filter_mode(self) -> str:
        mode = (load_config().get("INBOX_FILTER_MODE") or "flow_only").strip().lower()
        return "all" if mode == "all" else "flow_only"

    def _is_dedicated_dir(self, watch_dir: Path) -> bool:
        """若用户将监听目录设成独立子目录（非公共 ~/Downloads），视为专属目录默认全放行"""
        try:
            default_dl = Path("~/Downloads").expanduser().resolve()
            return watch_dir.resolve() != default_dl
        except OSError:
            return False

    def _wait_stable(self, path: Path, checks: int = 4, interval: float = 0.35) -> bool:
        last_size = -1
        for _ in range(checks):
            if not path.exists() or not path.is_file():
                return False
            try:
                sz = path.stat().st_size
            except OSError:
                return False
            if sz > 0 and sz == last_size:
                return True
            last_size = sz
            time.sleep(interval)
        return path.exists() and path.stat().st_size > 0

    def register_path(self, path: Path, debounce: bool = True, ignore_dismissed: bool = False) -> bool:
        name = path.name
        if name.startswith(".") or name.lower().endswith(TEMP_SUFFIXES):
            return False
        mtype = classify_media_type(path)
        if not mtype:
            return False
        if debounce and not self._wait_stable(path):
            return False
        if not path.exists() or not path.is_file():
            return False

        orig_abs_str = str(path.resolve())
        abs_str = orig_abs_str
        now_ts = time.time()
        with self._lock:
            expired = [k for k, exp in self._suppressed_paths.items() if exp <= now_ts]
            for k in expired:
                self._suppressed_paths.pop(k, None)
            if abs_str in self._suppressed_paths:
                return False
            if abs_str in self._in_progress:
                return False
            if not ignore_dismissed and abs_str in self._dismissed_paths:
                return False
            if any(existing["path"] == abs_str for existing in self._items):
                return False
            self._in_progress.add(orig_abs_str)

        try:
            cfg = load_config()
            watch_dir = (self._watch_dir or Path(cfg.get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()).resolve()
            ok, origin_label = check_flow_origin(
                path=path,
                mtype=mtype,
                filter_mode=self._current_filter_mode(),
                is_dedicated_dir=self._is_dedicated_dir(watch_dir),
            )
            if not ok:
                with self._lock:
                    self._filtered_count += 1
                return False

            batch_prefix = ""
            auto_rename = cfg.get("AUTO_RENAME_FLOW_DOWNLOADS", "true").lower() in ("1", "true", "yes", "on")
            is_flow_candidate = origin_label in ("Google Flow", "Flow 命名匹配") or any(
                pat.search(path.stem) for pat in FLOW_FILENAME_PATTERNS
            )
            if debounce and auto_rename and is_flow_candidate and not _has_cjk(path.stem):
                ctx_label = self._get_fresh_context_label()
                if ctx_label and path.resolve().parent == watch_dir:
                    prefix = f"待选_{ctx_label}"
                    ext = path.suffix.lower()
                    dest = (watch_dir / f"{prefix}{ext}").resolve()
                    idx = 1
                    while dest.exists():
                        dest = (watch_dir / f"{prefix}_{idx}{ext}").resolve()
                        idx += 1
                    try:
                        self.suppress_path(dest, ttl_sec=8.0, also_dismiss=False)
                        path.rename(dest)
                        path = dest
                        abs_str = str(dest)
                        batch_prefix = prefix
                    except OSError:
                        pass

            if not batch_prefix and path.stem.startswith("待选_"):
                batch_prefix = re.sub(r"_\d+$", "", path.stem)

            meta = probe_media(path) if mtype in ("video", "audio", "image") else {}
            stat = path.stat()
            item = {
                "id": uuid.uuid4().hex[:12],
                "filename": path.name,
                "path": abs_str,
                "media_type": mtype,
                "origin_label": origin_label,
                "batch_prefix": batch_prefix,
                "size_bytes": stat.st_size,
                "width": meta.get("width", 0),
                "height": meta.get("height", 0),
                "duration_sec": meta.get("duration_sec", 0.0),
                "resolution_label": meta.get("resolution_label", ""),
                "detected_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            with self._lock:
                if any(existing["path"] == abs_str for existing in self._items):
                    return False
                self._items.appendleft(item)
                return True
        finally:
            with self._lock:
                self._in_progress.discard(orig_abs_str)

    def scan_recent(self, minutes: int = 30, reset_dismissed: bool = False) -> int:
        """扫描监听目录最近 N 分钟落盘的素材"""
        watch_dir = Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()
        self._watch_dir = watch_dir
        if not watch_dir.exists():
            return 0
        if reset_dismissed:
            with self._lock:
                self._dismissed_paths.clear()

        cutoff = time.time() - minutes * 60
        candidates: list[tuple[float, Path]] = []
        try:
            for p in watch_dir.iterdir():
                if not p.is_file():
                    continue
                if classify_media_type(p) is None:
                    continue
                mtime = p.stat().st_mtime
                if mtime >= cutoff:
                    candidates.append((mtime, p))
        except PermissionError as exc:
            hint = "请在系统隐私设置中允许访问 Downloads 目录" if sys.platform == "darwin" else "请检查下载目录读取权限"
            self._error = f"无权访问下载目录（{hint}）：{exc}"
            return 0

        candidates.sort(key=lambda x: x[0])
        with self._lock:
            self._filtered_count = 0
        added = 0
        for _, p in candidates[-30:]:
            if self.register_path(p, debounce=False, ignore_dismissed=reset_dismissed):
                added += 1
        return added

    def reload_filter(self) -> int:
        """切换过滤模式后立刻按新模式重建当前列表"""
        with self._lock:
            self._items.clear()
            self._filtered_count = 0
        return self.scan_recent(minutes=60, reset_dismissed=False)

    def clear_all(self) -> int:
        """一键清空收件箱视图（不删除磁盘原文件，仅记入忽略名单）"""
        with self._lock:
            cnt = len(self._items)
            for x in self._items:
                self._dismissed_paths.add(x["path"])
            self._items.clear()
            return cnt

    def start(self) -> None:
        self.stop()
        watch_dir = Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()
        self._watch_dir = watch_dir
        if not watch_dir.exists():
            self._status = "dir_missing"
            self._error = f"监听目录不存在: {watch_dir}"
            return

        inbox = self

        class _Handler(FileSystemEventHandler):
            def _dispatch(self, raw_path: str) -> None:
                p = Path(raw_path)
                if p.name.startswith(".") or p.name.lower().endswith(TEMP_SUFFIXES):
                    return
                if classify_media_type(p) is None:
                    return
                threading.Thread(target=inbox.register_path, args=(p, True), daemon=True).start()

            def on_created(self, event: FileSystemEvent) -> None:
                if not event.is_directory:
                    self._dispatch(str(event.src_path))

            def on_moved(self, event: FileSystemEvent) -> None:
                if not event.is_directory and getattr(event, "dest_path", None):
                    self._dispatch(str(event.dest_path))

        try:
            obs = Observer()
            obs.schedule(_Handler(), str(watch_dir), recursive=False)
            obs.start()
            self._observer = obs
            self._status = "running"
            self._error = ""
            threading.Thread(target=self.scan_recent, args=(30,), daemon=True).start()
        except Exception as exc:
            self._status = "error"
            self._error = str(exc)

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        self._status = "stopped"

    def list_items(self) -> dict[str, Any]:
        filter_mode = self._current_filter_mode()
        watch_dir = self._watch_dir or Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()
        is_dedicated = self._is_dedicated_dir(watch_dir)
        with self._lock:
            seen: set[str] = set()
            alive: list[dict[str, Any]] = []
            for x in self._items:
                if x["path"] in seen or not Path(x["path"]).exists():
                    continue
                seen.add(x["path"])
                alive.append(x)
            self._items = deque(alive, maxlen=self._items.maxlen)
            items = list(self._items)
            filtered_count = self._filtered_count
        cfg = load_config()
        return {
            "status": self._status,
            "watch_dir": str(watch_dir),
            "dock_drag_dir": str(resolve_dock_drag_dir(cfg)),
            "active_context_label": self._get_fresh_context_label(),
            "auto_rename_flow_downloads": cfg.get("AUTO_RENAME_FLOW_DOWNLOADS", "true").lower() in ("1", "true", "yes", "on"),
            "auto_trash_rejected_batch": cfg.get("AUTO_TRASH_REJECTED_BATCH", "true").lower() in ("1", "true", "yes", "on"),
            "filter_mode": filter_mode,
            "is_dedicated_dir": is_dedicated,
            "filtered_skipped": filtered_count,
            "error": self._error,
            "items": items,
        }

    def dismiss_item(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            kept: list[dict[str, Any]] = []
            for x in self._items:
                if x["id"] == item_id:
                    self._dismissed_paths.add(x["path"])
                else:
                    kept.append(x)
            self._items = deque(kept, maxlen=self._items.maxlen)
            return len(self._items) < before

    def suppress_path(self, path: Path, ttl_sec: float = 8.0, also_dismiss: bool = False) -> None:
        """短时屏蔽 watchdog 对指定路径的回声事件"""
        try:
            abs_str = str(path.resolve())
        except OSError:
            abs_str = str(path)
        with self._lock:
            self._suppressed_paths[abs_str] = time.time() + ttl_sec
            if also_dismiss:
                self._dismissed_paths.add(abs_str)

    def rename_item(self, item_id: str, new_name: str) -> dict[str, Any]:
        """原地将监听目录中的乱码文件改为直观中文名"""
        clean_stem = sanitize_filename_stem(new_name, max_len=60)
        if not clean_stem:
            raise ValueError("请输入有效的新文件名（支持中文、字母、数字和下划线）")

        watch_dir = (self._watch_dir or Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()).resolve()

        with self._lock:
            target = next((dict(x) for x in self._items if x["id"] == item_id), None)
        if not target:
            raise FileNotFoundError("收件箱里找不到该文件，可能已被认领或移除。")

        src = Path(target["path"]).resolve()
        if not src.exists() or not src.is_file():
            self.dismiss_item(item_id)
            raise FileNotFoundError(f"源文件已不存在: {src}")
        if src.parent != watch_dir:
            raise PermissionError("仅允许原地重命名当前监听下载目录下的媒体文件。")
        if classify_media_type(src) is None:
            raise PermissionError("仅允许重命名图片、视频或音频媒体文件。")

        ext = src.suffix.lower()
        dest = (watch_dir / f"{clean_stem}{ext}").resolve()
        if dest.parent != watch_dir:
            raise PermissionError("文件名包含非法路径字符。")

        if dest != src:
            idx = 1
            while dest.exists():
                dest = (watch_dir / f"{clean_stem}_{idx}{ext}").resolve()
                idx += 1

            self.suppress_path(dest, ttl_sec=8.0, also_dismiss=False)
            src.rename(dest)
            try:
                os.utime(dest, None)
            except OSError:
                pass

        dest_str = str(dest)
        now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._dismissed_paths.discard(dest_str)
            updated_item: dict[str, Any] | None = None
            for x in self._items:
                if x["id"] == item_id:
                    x["path"] = dest_str
                    x["filename"] = dest.name
                    x["detected_at"] = now_label
                    updated_item = dict(x)
                    break
        return updated_item or {**target, "path": dest_str, "filename": dest.name, "detected_at": now_label}

    def copy_to_dock_downloads(self, src_path: Path, chinese_stem: str) -> Path:
        """将已归档的资产图/首尾帧复制一份极简中文短名副本到直拖目录，刷新修改时间并屏蔽回声"""
        src = src_path.expanduser().resolve()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"源文件不存在：{src}")
        if classify_media_type(src) is None:
            raise ValueError("仅支持同步图片、视频或音频媒体文件到直拖目录")

        clean_stem = sanitize_filename_stem(chinese_stem, max_len=18) or sanitize_filename_stem(src.stem, max_len=18) or "Flow图"
        drag_dir = resolve_dock_drag_dir().resolve()
        drag_dir.mkdir(parents=True, exist_ok=True)
        dest = (drag_dir / f"{clean_stem}{src.suffix.lower()}").resolve()
        if dest.parent != drag_dir:
            raise PermissionError("目标文件名非法")

        self.suppress_path(dest, ttl_sec=10.0, also_dismiss=True)
        if src != dest:
            shutil.copy2(str(src), str(dest))
        try:
            os.utime(dest, None)
        except OSError:
            pass
        return dest

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((dict(x) for x in self._items if x["id"] == item_id), None)

    def trash_item(self, item_id: str) -> dict[str, Any]:
        """将收件箱中指定废弃媒体移入系统回收站/废纸篓"""
        watch_dir = (self._watch_dir or Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()).resolve()
        target = self.get_item(item_id)
        if not target:
            raise FileNotFoundError("收件箱里找不到该文件。")
        src = Path(target["path"]).resolve()
        if src.parent != watch_dir or classify_media_type(src) is None:
            raise PermissionError("仅允许将当前监听下载目录下的媒体废图移入回收站。")
        if src.exists() and src.is_file():
            move_file_to_trash(src)
        self.dismiss_item(item_id)
        return target

    def trash_batch_siblings(self, batch_prefix: str, exclude_item_id: str = "") -> list[str]:
        """认领某批 4 选 1 里的最佳图后，自动将同批落选的其余待选图移入回收站"""
        if not batch_prefix or not batch_prefix.startswith("待选_"):
            return []
        watch_dir = (self._watch_dir or Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()).resolve()
        with self._lock:
            siblings = [
                dict(x)
                for x in self._items
                if x["id"] != exclude_item_id
                and (x.get("batch_prefix") == batch_prefix or Path(x["filename"]).stem == batch_prefix or Path(x["filename"]).stem.startswith(f"{batch_prefix}_"))
            ]
        trashed: list[str] = []
        for sib in siblings:
            src = Path(sib["path"]).resolve()
            if src.parent == watch_dir and classify_media_type(src) is not None and src.exists() and src.is_file():
                try:
                    move_file_to_trash(src)
                    trashed.append(sib["filename"])
                except OSError:
                    pass
            self.dismiss_item(sib["id"])
        return trashed

    def trash_all_items(self) -> list[str]:
        """将当前收件箱里剩余的未认领媒体移入回收站"""
        watch_dir = (self._watch_dir or Path(load_config().get("WATCH_DOWNLOADS_DIR", "~/Downloads")).expanduser()).resolve()
        with self._lock:
            snapshot = [dict(x) for x in self._items]
        trashed: list[str] = []
        for it in snapshot:
            src = Path(it["path"]).resolve()
            if src.parent == watch_dir and classify_media_type(src) is not None and src.exists() and src.is_file():
                try:
                    move_file_to_trash(src)
                    trashed.append(it["filename"])
                except OSError:
                    pass
            self.dismiss_item(it["id"])
        return trashed

    def clear_dock_tray_dir(self) -> int:
        """安全清空独立直拖托盘内的旧媒体副本（硬保护：若配置为 ~/Downloads 等系统目录则拒绝清空）"""
        drag_dir = resolve_dock_drag_dir().resolve()
        protected = {
            Path.home().resolve(),
            Path("~/Downloads").expanduser().resolve(),
            Path("~/Desktop").expanduser().resolve(),
            Path("~/Documents").expanduser().resolve(),
        }
        if drag_dir in protected or not drag_dir.exists():
            return 0
        removed = 0
        for p in drag_dir.iterdir():
            if p.is_file() and classify_media_type(p) is not None:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def claim_file(self, item_id: str, dest_dir: Path, new_stem: str) -> dict[str, Any]:
        """将收件箱中的指定媒体文件瞬移至目标目录并规范重命名"""
        target: dict[str, Any] | None = None
        with self._lock:
            for x in self._items:
                if x["id"] == item_id:
                    target = dict(x)
                    break
        if not target:
            raise FileNotFoundError("收件箱里找不到该文件，可能已被认领或删除。")

        src = Path(target["path"])
        if not src.exists():
            self.dismiss_item(item_id)
            raise FileNotFoundError(f"源文件已不存在: {src}")

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{new_stem}{src.suffix.lower()}"
        idx = 1
        while dest.exists():
            dest = dest_dir / f"{new_stem}_{idx}{src.suffix.lower()}"
            idx += 1

        shutil.move(str(src), str(dest))
        self.dismiss_item(item_id)
        target["path"] = str(dest.resolve())
        target["dest_filename"] = dest.name
        return target


# 全局单例收件箱
downloads_inbox = DownloadsInbox()
