"""
跨平台本地媒体处理与母带封装模块（自动适配 macOS / Windows / Linux）：
1. ffprobe 秒级探针（宽高/时长/分辨率标签/音频流检测）；
2. 一键抽当前镜头末帧 PNG 给下一镜当首帧参考图防跳变；
3. 标准 UTF-8 `.srt` 字幕生成与黄金安全区中文字幕烧录（无 libass 环境下通过 Pillow 透明字幕层 + FFmpeg overlay 精准渲染）；
4. 国际标准 `.fcpxml` (v1.9) 三轨工程导出（主轨视频+原生音、lane="1" 旁白 VO 轨、lane="-1" BGM 轨，兼容剪映/Final Cut Pro/DaVinci）；
5. 真 1080p 三轨混音母带封装 `build_master_base_mp4`：
   - 保持真 1080p（竖屏 1080x1920 / 横屏 1920x1080，30fps）；
   - 自动探测最优硬件编码器并优雅回退：
     * 10-bit HLG HDR：`hevc_videotoolbox`（macOS）→ `hevc_nvenc`（NVIDIA GPU）→ `libx265`（通用软件）；
     * BT.709 SDR：`h264_videotoolbox`（macOS）→ `h264_nvenc`（NVIDIA GPU）→ `libx264`（通用软件）；
   - 标准三轨混音：轨 1 原生环境音 + 轨 2 旁白 VO + 轨 3 BGM（有 VO 时自动 sidechaincompress 闪避 + 片尾 2s 淡出 + alimiter=0.95）。
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import EXPORTS_DIR, MEDIA_DIR


def _missing_ffmpeg_guide(missing_tools: list[str]) -> str:
    tools_str = " / ".join(missing_tools)
    return (
        f"未检测到本机 {tools_str}（请先安装 FFmpeg 并确保已加入系统 PATH 后重启服务）：\n"
        "- macOS：brew install ffmpeg\n"
        "- Windows：winget install Gyan.FFmpeg（或 choco install ffmpeg）\n"
        "- Ubuntu/Debian：sudo apt update && sudo apt install -y ffmpeg"
    )


def _ensure_ffmpeg_tools(*, require_ffmpeg: bool = True, require_ffprobe: bool = True) -> tuple[str, str]:
    """检查本机 ffmpeg / ffprobe 是否可用，缺失时抛出清晰的跨平台中文安装指引"""
    ffmpeg_bin = shutil.which("ffmpeg") or ""
    ffprobe_bin = shutil.which("ffprobe") or ""
    missing: list[str] = []
    if require_ffmpeg and not ffmpeg_bin:
        missing.append("ffmpeg")
    if require_ffprobe and not ffprobe_bin:
        missing.append("ffprobe")
    if missing:
        raise RuntimeError(_missing_ffmpeg_guide(missing))
    return ffmpeg_bin, ffprobe_bin


def _resolution_label(width: int, height: int) -> str:
    short_side = min(width, height) if (width and height) else max(width, height)
    if short_side >= 2000:
        return "4K"
    if short_side >= 1000:
        return "1080p"
    if short_side >= 680:
        return "720p"
    if short_side >= 340:
        return "360p"
    return f"{width}x{height}" if width and height else ""


@functools.lru_cache(maxsize=4)
def _select_filter_complex_file_flag() -> str:
    """
    跨 FFmpeg 版本探测从文件加载 filter_complex 的标准参数（彻底规避 Windows 32KB 命令行上限）：
    - FFmpeg 4/5/6/7：支持 `-filter_complex_script <file>`；
    - FFmpeg 7/8/9+：官方移除 `-filter_complex_script`，改为通用文件参数前缀 `-/filter_complex <file>`；
    - 若均不可用则返回空字符串以回退内联 `-filter_complex`。
    """
    with tempfile.TemporaryDirectory(prefix="agvs_ff_probe_") as td:
        fp = Path(td) / "probe_fc.txt"
        fp.write_text("[0:v]null[vout]", encoding="utf-8")
        for flag in ("-filter_complex_script", "-/filter_complex"):
            try:
                res = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=64x64:d=0.05:r=24",
                        flag,
                        str(fp),
                        "-map",
                        "[vout]",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                )
                if res.returncode == 0:
                    return flag
            except Exception:
                continue
    return ""


@functools.lru_cache(maxsize=16)
def _has_ffmpeg_encoder(encoder_name: str) -> bool:
    """检查当前环境 ffmpeg 是否编入了指定编码器（带缓存）"""
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
        return encoder_name in out
    except Exception:
        return False


@functools.lru_cache(maxsize=16)
def _probe_encoder_usable(encoder_name: str, pix_fmt: str, extra_args: tuple[str, ...] = ()) -> bool:
    """对硬件编码器执行 2 帧黑屏空编码实测，确保机器真有可用硬件加速而不假阳性报错"""
    if not _has_ffmpeg_encoder(encoder_name):
        return False
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:d=0.1:r=24",
        "-vf",
        f"format={pix_fmt}",
        "-c:v",
        encoder_name,
        *extra_args,
        "-pix_fmt",
        pix_fmt,
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0)
        return res.returncode == 0
    except Exception:
        return False


def select_video_encoder(*, hdr_hlg: bool = False) -> tuple[str, str, list[str]]:
    """
    跨平台自动探测最优视频编码器（macOS VideoToolbox → NVIDIA NVENC → 软件 libx265/libx264）。
    返回 (encoder_name, target_pix_fmt, vcodec_args)。
    """
    if hdr_hlg:
        if sys.platform == "darwin" and _probe_encoder_usable("hevc_videotoolbox", "p010le", ("-profile:v", "main10")):
            return (
                "hevc_videotoolbox",
                "p010le",
                [
                    "-c:v", "hevc_videotoolbox",
                    "-profile:v", "main10",
                    "-pix_fmt", "p010le",
                    "-tag:v", "hvc1",
                    "-b:v", "10M",
                    "-color_primaries", "bt2020",
                    "-color_trc", "arib-std-b67",
                    "-colorspace", "bt2020nc",
                    "-color_range", "tv",
                ],
            )
        if _probe_encoder_usable("hevc_nvenc", "p010le", ("-profile:v", "main10")):
            return (
                "hevc_nvenc",
                "p010le",
                [
                    "-c:v", "hevc_nvenc",
                    "-preset", "p5",
                    "-profile:v", "main10",
                    "-rc", "vbr",
                    "-cq", "21",
                    "-b:v", "10M",
                    "-maxrate", "14M",
                    "-bufsize", "28M",
                    "-pix_fmt", "p010le",
                    "-tag:v", "hvc1",
                    "-color_primaries", "bt2020",
                    "-color_trc", "arib-std-b67",
                    "-colorspace", "bt2020nc",
                    "-color_range", "tv",
                ],
            )
        return (
            "libx265",
            "yuv420p10le",
            [
                "-c:v", "libx265",
                "-preset", "medium",
                "-crf", "20",
                "-profile:v", "main10",
                "-pix_fmt", "yuv420p10le",
                "-tag:v", "hvc1",
                "-x265-params", "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited",
                "-color_primaries", "bt2020",
                "-color_trc", "arib-std-b67",
                "-colorspace", "bt2020nc",
                "-color_range", "tv",
            ],
        )

    # BT.709 SDR
    if sys.platform == "darwin" and _probe_encoder_usable("h264_videotoolbox", "yuv420p"):
        return (
            "h264_videotoolbox",
            "yuv420p",
            [
                "-c:v", "h264_videotoolbox",
                "-pix_fmt", "yuv420p",
                "-b:v", "8M",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
                "-color_range", "tv",
            ],
        )
    if _probe_encoder_usable("h264_nvenc", "yuv420p"):
        return (
            "h264_nvenc",
            "yuv420p",
            [
                "-c:v", "h264_nvenc",
                "-preset", "p5",
                "-rc", "vbr",
                "-cq", "20",
                "-b:v", "8M",
                "-maxrate", "12M",
                "-bufsize", "24M",
                "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
                "-color_range", "tv",
            ],
        )
    return (
        "libx264",
        "yuv420p",
        [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "19",
            "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
        ],
    )


@functools.lru_cache(maxsize=4)
def find_cjk_font(prefer_bold: bool = True) -> tuple[str, int]:
    """
    跨平台定位本机可用的干净中文字体及 TTC 字面索引：
    - macOS：苹方 PingFang SC / 冬青黑体 / 华文黑体；
    - Windows：微软雅黑（msyhbd.ttc / msyh.ttc）/ 黑体（simhei.ttf）/ 等线（Dengb.ttf）；
    - Linux / Colab：Noto Sans CJK SC / WenQuanYi Zen Hei。
    返回 (font_path, face_index)；若找不到系统 CJK 字体则返回 ("", 0)。
    """
    candidates: list[tuple[Path, tuple[str, ...], tuple[str, ...]]] = []

    # 1. Windows 系统字体
    win_fonts_dir = Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
    if win_fonts_dir.exists():
        win_order = (
            [
                ("msyhbd.ttc", ("Microsoft YaHei", "微软雅黑"), ("Bold", "Regular")),
                ("msyh.ttc", ("Microsoft YaHei", "微软雅黑"), ("Regular",)),
                ("simhei.ttf", ("SimHei", "黑体"), ("Regular",)),
                ("Dengb.ttf", ("DengXian", "等线"), ("Bold", "Regular")),
                ("Deng.ttf", ("DengXian", "等线"), ("Regular",)),
                ("simsun.ttc", ("SimSun", "宋体"), ("Regular",)),
            ]
            if prefer_bold
            else [
                ("msyh.ttc", ("Microsoft YaHei", "微软雅黑"), ("Regular",)),
                ("msyhbd.ttc", ("Microsoft YaHei", "微软雅黑"), ("Bold", "Regular")),
                ("Deng.ttf", ("DengXian", "等线"), ("Regular",)),
                ("simhei.ttf", ("SimHei", "黑体"), ("Regular",)),
                ("simsun.ttc", ("SimSun", "宋体"), ("Regular",)),
            ]
        )
        for fname, fams, styles in win_order:
            fp = win_fonts_dir / fname
            if fp.exists():
                candidates.append((fp, fams, styles))

    # 2. macOS 苹方 PingFang SC（新版本位于 AssetsV2 动态字体目录）
    assets_v2 = Path("/System/Library/AssetsV2")
    if assets_v2.exists():
        for pf in sorted(assets_v2.glob("**/PingFang.ttc")):
            candidates.append(
                (
                    pf,
                    ("PingFang SC",),
                    ("Semibold", "Medium", "Regular") if prefer_bold else ("Medium", "Regular", "Semibold"),
                )
            )
    for pf in (Path("/System/Library/Fonts/PingFang.ttc"), Path("/Library/Fonts/PingFang.ttc")):
        if pf.exists():
            candidates.append(
                (
                    pf,
                    ("PingFang SC",),
                    ("Semibold", "Medium", "Regular") if prefer_bold else ("Medium", "Regular", "Semibold"),
                )
            )

    # 3. macOS 冬青黑体 & 华文黑体
    hiragino = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")
    if hiragino.exists():
        candidates.append(
            (
                hiragino,
                ("Hiragino Sans GB",),
                ("W6", "W3") if prefer_bold else ("W3", "W6"),
            )
        )
    st_heiti = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    if st_heiti.exists():
        candidates.append((st_heiti, ("Heiti SC", "STHeiti"), ("Medium", "Regular")))

    # 4. Linux / Colab Noto Sans CJK SC / WenQuanYi 兜底
    for noto in (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ):
        if noto.exists():
            candidates.append((noto, ("Noto Sans CJK SC", "WenQuanYi Zen Hei"), ("Bold", "Medium", "Regular")))

    # 5. Arial Unicode 最终兜底
    arial_uni = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    if arial_uni.exists():
        candidates.append((arial_uni, ("Arial Unicode MS",), ("Regular",)))

    for font_path, prefer_families, prefer_styles in candidates:
        if not font_path.exists():
            continue
        faces: list[tuple[int, str, str]] = []
        for idx in range(20):
            try:
                f = ImageFont.truetype(str(font_path), size=32, index=idx)
                fam, sty = f.getname()
                faces.append((idx, fam or "", sty or ""))
            except Exception:
                break
        if not faces:
            continue
        for sty_want in prefer_styles:
            for idx, fam, sty in faces:
                if any(fam == pf for pf in prefer_families) and sty_want.lower() in sty.lower():
                    return str(font_path), idx
        for idx, fam, _ in faces:
            if any(pf.lower() in fam.lower() for pf in prefer_families):
                return str(font_path), idx
        return str(font_path), faces[0][0]

    return "", 0


def load_cjk_font(size: int, *, prefer_bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载指定字号的干净中文字体"""
    font_path, face_idx = find_cjk_font(prefer_bold=prefer_bold)
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=max(10, int(size)), index=face_idx)
        except Exception:
            pass
    return ImageFont.load_default()


def has_audio_stream(path: Path) -> bool:
    """用 ffprobe 快速探测媒体文件是否包含有效音频流"""
    if not path.exists():
        return False
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
            timeout=5.0,
        )
        return bool(out.strip())
    except Exception:
        return False


def probe_media(path: Path) -> dict[str, Any]:
    """探测图片/视频/音频的分辨率、时长与是否含音轨"""
    if not path.exists():
        return {"width": 0, "height": 0, "duration_sec": 0.0, "resolution_label": "", "has_audio": False}

    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            with Image.open(path) as im:
                w, h = im.size
                return {
                    "width": w,
                    "height": h,
                    "duration_sec": 0.0,
                    "resolution_label": _resolution_label(w, h),
                    "has_audio": False,
                }
        except Exception:
            return {"width": 0, "height": 0, "duration_sec": 0.0, "resolution_label": "", "has_audio": False}

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, timeout=6.0)
        info = json.loads(out.decode("utf-8", errors="ignore"))
        dur = float((info.get("format") or {}).get("duration") or 0.0)
        w, h = 0, 0
        has_a = False
        for st in info.get("streams") or []:
            if st.get("codec_type") == "video" and w == 0:
                w = int(st.get("width") or 0)
                h = int(st.get("height") or 0)
            elif st.get("codec_type") == "audio":
                has_a = True
        return {
            "width": w,
            "height": h,
            "duration_sec": round(dur, 3),
            "resolution_label": _resolution_label(w, h),
            "has_audio": has_a,
        }
    except Exception:
        return {"width": 0, "height": 0, "duration_sec": 0.0, "resolution_label": "", "has_audio": False}


def _probe_av_durations(path: Path) -> dict[str, float]:
    """使用 ffprobe 分别读取成片的视频流时长、音频流时长及音画时长差（秒）"""
    if not path.exists():
        return {"video_duration_sec": 0.0, "audio_duration_sec": 0.0, "av_drift_sec": 0.0}
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, timeout=8.0)
        info = json.loads(out.decode("utf-8", errors="ignore"))
        fmt_dur = float((info.get("format") or {}).get("duration") or 0.0)
        v_dur = 0.0
        a_dur = 0.0
        for st in info.get("streams") or []:
            ctype = st.get("codec_type")
            st_dur = float(st.get("duration") or 0.0)
            if ctype == "video" and v_dur == 0.0:
                v_dur = st_dur or fmt_dur
            elif ctype == "audio" and a_dur == 0.0:
                a_dur = st_dur or fmt_dur
        if v_dur <= 0.0:
            v_dur = fmt_dur
        if a_dur <= 0.0:
            a_dur = fmt_dur
        drift = round(abs(v_dur - a_dur), 3)
        return {
            "video_duration_sec": round(v_dur, 3),
            "audio_duration_sec": round(a_dur, 3),
            "av_drift_sec": drift,
        }
    except Exception:
        return {"video_duration_sec": 0.0, "audio_duration_sec": 0.0, "av_drift_sec": 0.0}


def extract_last_frame(video_path: Path, output_png: Path) -> Path:
    """从视频末尾抽取清晰末帧 PNG，用作下一镜首帧参考防画面跳变"""
    _ensure_ffmpeg_tools(require_ffmpeg=True, require_ffprobe=False)
    if not video_path.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-sseof",
        "-0.18",
        "-i",
        str(video_path),
        "-update",
        "1",
        "-frames:v",
        "1",
        str(output_png),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=12.0)
    if res.returncode != 0 or not output_png.exists():
        fallback = ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", str(output_png)]
        res2 = subprocess.run(fallback, capture_output=True, text=True, timeout=12.0)
        if res2.returncode != 0 or not output_png.exists():
            raise RuntimeError(f"抽末帧失败: {res.stderr[-200:]}")
    return output_png


def _fmt_srt_time(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    hh = ms // 3600000
    mm = (ms % 3600000) // 60000
    ss = (ms % 60000) // 1000
    rem = ms % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{rem:03d}"


def _parse_srt_time(ts: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
    if not m:
        return 0.0
    hh, mm, ss, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4).ljust(3, "0")[:3])
    return hh * 3600.0 + mm * 60.0 + ss + ms / 1000.0


def _parse_srt(srt_path: Path) -> list[tuple[float, float, str]]:
    """解析标准 UTF-8 .srt 字幕文件，返回 [(start_sec, end_sec, text), ...]"""
    if not srt_path or not srt_path.exists():
        return []
    raw = srt_path.read_text(encoding="utf-8-sig", errors="ignore").replace("\r\n", "\n")
    blocks = re.split(r"\n{2,}", raw.strip())
    entries: list[tuple[float, float, str]] = []
    for blk in blocks:
        lines = [ln.strip() for ln in blk.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        time_idx = -1
        for i, ln in enumerate(lines):
            if "-->" in ln:
                time_idx = i
                break
        if time_idx < 0 or time_idx + 1 >= len(lines):
            continue
        parts = lines[time_idx].split("-->")
        if len(parts) != 2:
            continue
        start_s = _parse_srt_time(parts[0])
        end_s = _parse_srt_time(parts[1])
        text = "\n".join(lines[time_idx + 1 :]).strip()
        if text and end_s > start_s:
            entries.append((start_s, end_s, text))
    return entries


def _wrap_cjk_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    """按像素宽度自然折行中文字幕，尽量避免标点落到行首并保持多行视觉均衡"""
    dummy = ImageDraw.Draw(Image.new("RGBA", (16, 16)))

    def text_w(s: str) -> float:
        box = dummy.textbbox((0, 0), s, font=font)
        return float(box[2] - box[0])

    out_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if text_w(line) <= max_width:
            out_lines.append(line)
            continue
        if text_w(line) <= max_width * 1.92:
            mid = len(line) // 2
            best_split = -1
            best_dist = 999
            for idx, ch in enumerate(line):
                if ch in "，。！？；、,.;!?" and 3 <= idx <= len(line) - 4:
                    left_s = line[: idx + 1].strip()
                    right_s = line[idx + 1 :].strip()
                    if text_w(left_s) <= max_width and text_w(right_s) <= max_width:
                        dist = abs(idx - mid)
                        if dist < best_dist:
                            best_dist = dist
                            best_split = idx + 1
            if best_split > 0:
                out_lines.append(line[:best_split].strip())
                out_lines.append(line[best_split:].strip())
                continue

        cur = ""
        for ch in line:
            nxt = cur + ch
            if cur and text_w(nxt) > max_width:
                if ch in "，。！？；、,.!?;）】》”’" and len(cur) > 1:
                    out_lines.append(cur + ch)
                    cur = ""
                else:
                    out_lines.append(cur)
                    cur = ch
            else:
                cur = nxt
        if cur:
            out_lines.append(cur)
    return out_lines or [text]


def _render_subtitle_strip_png(
    text: str,
    out_png: Path,
    video_w: int,
    video_h: int,
    *,
    hdr_hlg: bool = False,
) -> tuple[int, int, int]:
    """
    用 Pillow 渲染电影级抗锯齿中文字幕透明条带 PNG，精确锚定黄金安全区：
    - 竖屏 9:16（video_h > video_w）：字幕底部严格抬高至画面底部往上 25% 处（避开主流平台底栏与右侧互动按钮列）；
    - 横屏 16:9：字幕底部位于画面底部往上 10% 处。
    返回 (strip_w, strip_h, overlay_y)。
    """
    is_vertical = video_h > video_w
    font_size = max(32, int(round(video_w * 0.043))) if is_vertical else max(34, int(round(video_h * 0.045)))
    font = load_cjk_font(font_size, prefer_bold=True)
    max_text_w = int(round(video_w * (0.78 if is_vertical else 0.84)))
    lines = _wrap_cjk_text(text, font, max_text_w)

    stroke_w = max(2, int(round(font_size * 0.085)))
    line_gap = max(8, int(round(font_size * 0.28)))
    pad_y = max(18, stroke_w * 4)

    probe_draw = ImageDraw.Draw(Image.new("RGBA", (16, 16)))
    line_metrics: list[tuple[str, int, int, int]] = []
    total_text_h = 0
    for ln in lines:
        bbox = probe_draw.textbbox((0, 0), ln, font=font, stroke_width=stroke_w)
        lw = int(bbox[2] - bbox[0])
        lh = int(bbox[3] - bbox[1])
        line_metrics.append((ln, lw, lh, int(bbox[1])))
        total_text_h += lh
    total_text_h += line_gap * max(0, len(line_metrics) - 1)

    strip_w = video_w
    strip_h = total_text_h + pad_y * 2
    if strip_h % 2 == 1:
        strip_h += 1

    shadow_layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_layer)
    text_layer = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    t_draw = ImageDraw.Draw(text_layer)

    fill_rgb = (236, 236, 236, 255) if hdr_hlg else (255, 255, 255, 255)
    stroke_rgba = (12, 14, 18, 240)
    shadow_rgba = (0, 0, 0, 175)

    cur_y = pad_y
    for ln, lw, lh, top_off in line_metrics:
        x = (strip_w - lw) // 2
        y = cur_y - top_off
        s_draw.text((x, y + max(2, stroke_w)), ln, font=font, fill=shadow_rgba, stroke_width=stroke_w, stroke_fill=shadow_rgba)
        t_draw.text((x, y), ln, font=font, fill=fill_rgb, stroke_width=stroke_w, stroke_fill=stroke_rgba)
        cur_y += lh + line_gap

    shadow_blur = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(2, stroke_w)))
    composed = Image.alpha_composite(shadow_blur, text_layer)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    composed.save(out_png, format="PNG")

    safe_bottom_ratio = 0.25 if is_vertical else 0.10
    text_bottom_y = int(round(video_h * (1.0 - safe_bottom_ratio)))
    overlay_y = max(0, text_bottom_y - (strip_h - pad_y))
    return strip_w, strip_h, overlay_y


def resolve_shot_clip_duration(shot: dict[str, Any], final_take: dict[str, Any] | None) -> float:
    """计算单镜在时间线上的真实有效时长（考虑粗裁起止点）"""
    base_dur = float(final_take["duration_sec"]) if (final_take and final_take.get("duration_sec")) else float(shot.get("duration_sec") or 6.0)
    ts = float(shot.get("trim_start") or 0.0)
    te = float(shot.get("trim_end") or 0.0)
    if te > ts and te <= base_dur + 0.1:
        return round(max(0.5, te - ts), 2)
    if ts > 0 and ts < base_dur:
        return round(max(0.5, base_dur - ts), 2)
    return round(max(0.5, base_dur), 2)


def normalize_vo_sync_mode(vo_sync_mode: str | None) -> str:
    """规范化 VO 与镜头时长对齐模式：'pad'（尾帧定格延展，默认）或 'speed'（≤1.2x 微加速 + 兜底定格）"""
    mode = str(vo_sync_mode or "pad").strip().lower()
    return "speed" if mode == "speed" else "pad"


def _resolve_item_vo_duration(item: dict[str, Any]) -> tuple[Path | None, float]:
    """解析镜头关联 VO 音频路径与真实时长（存在物理文件优先用 ffprobe 探测，否则回退显式 vo_duration_sec）"""
    p_str = str(item.get("vo_file_path") or "").strip()
    vo_path: Path | None = None
    vo_dur = 0.0
    if p_str:
        cand = Path(p_str)
        if cand.exists() and cand.is_file():
            vo_path = cand.resolve()
            meta = probe_media(vo_path)
            vo_dur = float(meta.get("duration_sec") or 0.0)
    if vo_dur <= 0.0 and item.get("vo_duration_sec"):
        try:
            vo_dur = max(0.0, float(item["vo_duration_sec"]))
        except (TypeError, ValueError):
            vo_dur = 0.0
    return vo_path, round(vo_dur, 3)


def plan_timeline_with_vo_sync(
    timeline_items: list[dict[str, Any]],
    *,
    vo_sync_mode: str = "pad",
    vo_offset_sec: float = 0.10,
) -> dict[str, Any]:
    """
    统一计算全集每个镜头与 VO 旁白对齐后的精准时间轴（供 build_master_base_mp4、export_srt、export_fcpxml 同源复用）：
    - 'pad' 模式：当 vo_dur > base_dur - 0.15 时，计算 pad_sec = round(vo_dur + 0.20 - base_dur, 3) 丝滑定格尾帧，
      将本镜有效时长延展至 eff_dur = base_dur + pad_sec，并同步顺延后续镜头时间轴 cursor；
    - 'speed' 模式：当 vo_dur / (base_dur - 0.15) <= 1.20 时用 atempo 微加速人声贴合原镜；
      超过 1.20x 则限速 atempo=1.20 并对剩余差额做尾帧 tpad 兜底，绝不变花栗鼠也不截断尾音；
    - 每条 VO 均计算严格截断上限 vo_max_len，确保人声绝对锁定在本镜窗口内，杜绝跨镜叠音打架。
    """
    mode = normalize_vo_sync_mode(vo_sync_mode)
    planned: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    cursor = 0.0

    for idx, item in enumerate(timeline_items):
        base_dur = round(max(0.5, float(item.get("effective_duration") or item.get("duration") or 6.0)), 3)
        vo_path, vo_dur = _resolve_item_vo_duration(item)
        has_vo = vo_dur > 0.0 and (vo_path is not None or bool(item.get("vo_duration_sec")))

        pad_sec = 0.0
        vo_tempo = 1.0
        eff_dur = base_dur

        if has_vo and vo_dur > base_dur - 0.15:
            if mode == "speed":
                avail = max(0.25, base_dur - 0.15)
                ratio = vo_dur / avail
                if ratio <= 1.20:
                    vo_tempo = round(max(1.0, ratio), 4)
                    pad_sec = 0.0
                    eff_dur = base_dur
                else:
                    vo_tempo = 1.20
                    scaled_vo = vo_dur / vo_tempo
                    pad_sec = round(max(0.0, scaled_vo + 0.20 - base_dur), 3)
                    eff_dur = round(base_dur + pad_sec, 3)
            else:
                pad_sec = round(max(0.0, vo_dur + 0.20 - base_dur), 3)
                eff_dur = round(base_dur + pad_sec, 3)

        played_vo_dur = round(vo_dur / vo_tempo, 3) if has_vo else 0.0
        if has_vo:
            max_safe_offset = max(0.0, eff_dur - played_vo_dur - 0.04)
            vo_offset = round(min(vo_offset_sec, max_safe_offset), 3)
        else:
            vo_offset = round(min(vo_offset_sec, base_dur * 0.10), 3)

        # 严格限制单镜 VO 音频有效长度在本镜时间窗内（绝不串入下一镜）
        vo_max_len = round(max(0.1, min(eff_dur - 0.08, eff_dur - vo_offset)), 3)

        # 字幕展示起止时间：有 VO 时优先贴合 min(eff_dur, vo_offset + played_vo_dur + 0.15)
        if has_vo:
            sub_start = round(cursor + vo_offset, 3)
            ideal_sub_end = min(eff_dur, vo_offset + played_vo_dur + 0.15)
            sub_end = round(cursor + max(min(eff_dur, vo_offset + 0.35), ideal_sub_end), 3)
        else:
            sub_start = round(cursor + 0.10, 3)
            sub_end = round(cursor + max(0.40, eff_dur - 0.10), 3)
        sub_end = min(round(cursor + eff_dur, 3), max(round(sub_start + 0.20, 3), sub_end))

        entry = {
            "index": idx,
            "shot_no": int(item.get("shot_no") or (idx + 1)),
            "shot_title": item.get("shot_title") or "",
            "item": item,
            "cursor_start": round(cursor, 3),
            "cursor_end": round(cursor + eff_dur, 3),
            "base_duration": base_dur,
            "effective_duration": eff_dur,
            "pad_sec": pad_sec,
            "has_vo": has_vo,
            "vo_path": vo_path,
            "vo_duration_sec": vo_dur,
            "vo_tempo": vo_tempo,
            "played_vo_duration_sec": played_vo_dur,
            "vo_offset_sec": vo_offset,
            "vo_start_sec": round(cursor + vo_offset, 3),
            "vo_max_len": vo_max_len,
            "sub_start_sec": sub_start,
            "sub_end_sec": sub_end,
        }
        planned.append(entry)

        if pad_sec > 0.0 or vo_tempo > 1.001:
            adjustments.append(
                {
                    "shot_no": entry["shot_no"],
                    "mode": mode,
                    "base_duration_sec": base_dur,
                    "vo_duration_sec": vo_dur,
                    "pad_sec": pad_sec,
                    "vo_tempo": vo_tempo,
                    "effective_duration_sec": eff_dur,
                }
            )
        cursor = round(cursor + eff_dur, 3)

    return {
        "vo_sync_mode": mode,
        "items": planned,
        "total_duration_sec": round(cursor, 3),
        "adjustments": adjustments,
    }


def export_srt(
    timeline_items: list[dict[str, Any]],
    output_srt: Path,
    *,
    vo_sync_mode: str = "pad",
) -> Path:
    """生成标准 UTF-8 .srt 字幕文件（与 VO 对齐时间轴同源一致，有 VO 时紧贴人声结束时间）"""
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    plan = plan_timeline_with_vo_sync(timeline_items, vo_sync_mode=vo_sync_mode)
    lines: list[str] = []
    idx = 1
    for p in plan["items"]:
        item = p["item"]
        text = (item.get("subtitle_text") or item.get("cn_dialogue") or "").strip()
        if text:
            lines.append(
                f"{idx}\n{_fmt_srt_time(p['sub_start_sec'])} --> {_fmt_srt_time(p['sub_end_sec'])}\n{text}\n"
            )
            idx += 1
    output_srt.write_text("\n".join(lines), encoding="utf-8")
    return output_srt


def _file_uri(p: Path) -> str:
    """跨平台生成标准 RFC 8089 file:// URI（自动兼容 macOS / Linux / Windows 盘符）"""
    return p.resolve().as_uri()


def export_fcpxml(
    project: dict[str, Any],
    episode: dict[str, Any],
    timeline_items: list[dict[str, Any]],
    output_fcpxml: Path,
    *,
    vo_sync_mode: str = "pad",
) -> Path:
    """
    导出国际标准 FCPXML 1.9 三轨工程（兼容主流非线性剪辑软件原生导入）：
    - 主轨（Spine）：视频 + 每镜原生环境音（仅 mute_for_local_vo 设为静音）；
    - lane="1"：每镜独立旁白人声 VO 轨（若有 vo_file_path，已与对齐后时间轴同步）；
    - lane="-1"：全集完整 BGM 配乐轨。
    """
    output_fcpxml.parent.mkdir(parents=True, exist_ok=True)
    fps = 30
    is_vertical = (project.get("aspect_ratio") or "9:16") == "9:16"
    width, height = (1080, 1920) if is_vertical else (1920, 1080)
    plan = plan_timeline_with_vo_sync(timeline_items, vo_sync_mode=vo_sync_mode)

    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        name=f"FFVideoFormat{height}p{fps}",
        frameDuration=f"1/{fps}s",
        width=str(width),
        height=str(height),
    )

    asset_entries: list[tuple[str, str, int, float, dict[str, Any]]] = []
    total_frames = 0
    for idx, p in enumerate(plan["items"], start=1):
        item = p["item"]
        eff_dur = float(p["effective_duration"])
        clip_path = Path(item["file_path"])
        rid = f"r_clip_{idx}"
        clip_frames = max(1, int(round(eff_dur * fps)))
        src_frames = max(clip_frames, int(round(float(item.get("source_duration") or eff_dur) * fps)))
        ET.SubElement(
            resources,
            "asset",
            id=rid,
            name=clip_path.name,
            src=_file_uri(clip_path),
            start="0s",
            duration=f"{src_frames}/{fps}s",
            hasVideo="1",
            hasAudio="1" if item.get("audio_strategy") != "mute_for_local_vo" else "0",
            format="r1",
        )

        vo_rid = ""
        vo_frames = 0
        vo_path = p["vo_path"]
        if vo_path is not None and vo_path.exists():
            vo_dur = float(p["vo_duration_sec"] or eff_dur)
            vo_frames = max(1, int(round(min(vo_dur, eff_dur) * fps)))
            vo_rid = f"r_vo_{idx}"
            ET.SubElement(
                resources,
                "asset",
                id=vo_rid,
                name=vo_path.name,
                src=_file_uri(vo_path),
                start="0s",
                duration=f"{vo_frames}/{fps}s",
                hasVideo="0",
                hasAudio="1",
            )

        asset_entries.append((rid, vo_rid, vo_frames, eff_dur, item))
        total_frames += clip_frames

    bgm_rid = ""
    bgm_path_str = (episode.get("bgm_file_path") or "").strip()
    if bgm_path_str and Path(bgm_path_str).exists():
        bgm_path = Path(bgm_path_str)
        bgm_meta = probe_media(bgm_path)
        bgm_dur = max(1, int(round((bgm_meta.get("duration_sec") or total_frames / fps) * fps)))
        bgm_rid = "r_bgm"
        ET.SubElement(
            resources,
            "asset",
            id=bgm_rid,
            name=bgm_path.name,
            src=_file_uri(bgm_path),
            start="0s",
            duration=f"{bgm_dur}/{fps}s",
            hasVideo="0",
            hasAudio="1",
        )

    lib = ET.SubElement(fcpxml, "library")
    evt = ET.SubElement(lib, "event", name=f"{project['name']}_E{episode['ep_number']:02d}")
    proj_el = ET.SubElement(evt, "project", name=f"E{episode['ep_number']:02d}_{episode['title']}")
    seq = ET.SubElement(
        proj_el,
        "sequence",
        format="r1",
        duration=f"{max(1, total_frames)}/{fps}s",
        tcStart="0s",
        tcFormat="NDF",
    )
    spine = ET.SubElement(seq, "spine")

    offset_frames = 0
    for rid, vo_rid, vo_frames, eff_dur, item in asset_entries:
        dur_frames = max(1, int(round(eff_dur * fps)))
        start_frames = max(0, int(round(float(item.get("trim_start") or 0.0) * fps)))
        clip_el = ET.SubElement(
            spine,
            "asset-clip",
            name=f"S{item['shot_no']:02d}_{item.get('shot_title') or '镜头'}",
            ref=rid,
            offset=f"{offset_frames}/{fps}s",
            start=f"{start_frames}/{fps}s",
            duration=f"{dur_frames}/{fps}s",
        )
        if vo_rid:
            ET.SubElement(
                clip_el,
                "asset-clip",
                name=f"S{item['shot_no']:02d}_VO旁白",
                ref=vo_rid,
                lane="1",
                offset=f"{start_frames}/{fps}s",
                start="0s",
                duration=f"{vo_frames}/{fps}s",
            )
        if offset_frames == 0 and bgm_rid:
            ET.SubElement(
                clip_el,
                "asset-clip",
                name="Original_Score_BGM",
                ref=bgm_rid,
                lane="-1",
                offset=f"{start_frames}/{fps}s",
                start="0s",
                duration=f"{max(1, total_frames)}/{fps}s",
            )
        offset_frames += dur_frames

    ET.indent(fcpxml, space="  ")
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(fcpxml, encoding="utf-8")
    output_fcpxml.write_bytes(xml_bytes)
    return output_fcpxml


def _resolve_target_dimensions(timeline_items: list[dict[str, Any]]) -> tuple[int, int]:
    """根据素材宽高比确定真 1080p 目标画幅（竖屏 1080x1920 / 横屏 1920x1080）"""
    for item in timeline_items:
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        if w > 0 and h > 0:
            return (1920, 1080) if w > h else (1080, 1920)
    first_p = Path(timeline_items[0]["file_path"])
    meta = probe_media(first_p)
    if meta["width"] > meta["height"] and meta["width"] > 0:
        return (1920, 1080)
    return (1080, 1920)


def _collect_vo_tracks(
    timeline_items: list[dict[str, Any]],
    vo_tracks: list[dict[str, Any]] | None,
    *,
    vo_sync_mode: str = "pad",
    plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """统一解析旁白人声轨（含精准起播时间、最大时间窗截断长度与可选 atempo 系数）"""
    resolved: list[dict[str, Any]] = []
    if vo_tracks is not None:
        for entry in vo_tracks:
            p_str = str(entry.get("file_path") or entry.get("path") or "").strip()
            if not p_str or not Path(p_str).exists():
                continue
            start_sec = float(entry.get("start_sec") if entry.get("start_sec") is not None else (entry.get("offset_sec") or 0.0))
            vol = float(entry.get("volume") if entry.get("volume") is not None else 1.15)
            max_dur = entry.get("max_duration_sec")
            tempo = float(entry.get("tempo") or 1.0)
            resolved.append(
                {
                    "path": Path(p_str).resolve(),
                    "start_sec": max(0.0, start_sec),
                    "volume": vol,
                    "max_duration_sec": float(max_dur) if max_dur is not None else None,
                    "tempo": max(0.5, min(2.0, tempo)),
                }
            )
        return resolved

    tl_plan = plan if plan is not None else plan_timeline_with_vo_sync(timeline_items, vo_sync_mode=vo_sync_mode)
    for p in tl_plan["items"]:
        vo_path = p.get("vo_path")
        if vo_path is not None and Path(vo_path).exists():
            resolved.append(
                {
                    "path": Path(vo_path).resolve(),
                    "start_sec": max(0.0, float(p["vo_start_sec"])),
                    "volume": 1.15,
                    "max_duration_sec": float(p["vo_max_len"]),
                    "tempo": float(p["vo_tempo"]),
                    "shot_no": p["shot_no"],
                }
            )
    return resolved


def build_master_base_mp4(
    timeline_items: list[dict[str, Any]],
    output_mp4: Path,
    bgm_file_path: str = "",
    bgm_volume: float = 0.25,
    vo_tracks: list[dict[str, Any]] | None = None,
    hdr_hlg: bool = False,
    burn_srt_path: Path | None = None,
    vo_sync_mode: str = "pad",
) -> dict[str, Any]:
    """
    跨平台真 1080p 三轨混音母带封装（自动适配 VideoToolbox / NVENC / libx265 / libx264）：
    - 画幅：保持真 1080p（竖屏 1080x1920 / 横屏 1920x1080，30fps）；
    - VO 音画精准对齐（vo_sync_mode='pad' 尾帧定格延展 | 'speed' ≤1.2x 微加速 + 兜底定格）并严防跨镜叠音；
    - 三轨混音：轨 1 原生环境音 + 轨 2 旁白 VO + 轨 3 BGM（有 VO 时自动 sidechaincompress 闪避 + 片尾 2s 淡出 + alimiter=0.95）；
    - 安全区字幕：传入 burn_srt_path 时，使用 Pillow 渲染抗锯齿中文字幕透明层并 overlay 至黄金安全区；
    - 防 Windows 32KB 命令行溢出：filter_complex 统一落盘临时脚本文件，通过 `-filter_complex_script` 传给 FFmpeg。
    """
    if not timeline_items:
        raise ValueError("当前剧集还没有定稿视频镜头，无法封装母带。")

    _ensure_ffmpeg_tools(require_ffmpeg=True, require_ffprobe=True)

    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    out_w, out_h = _resolve_target_dimensions(timeline_items)
    fps = 30

    norm_sync_mode = normalize_vo_sync_mode(vo_sync_mode)
    plan = plan_timeline_with_vo_sync(timeline_items, vo_sync_mode=norm_sync_mode)
    resolved_vos = _collect_vo_tracks(timeline_items, vo_tracks, vo_sync_mode=norm_sync_mode, plan=plan)
    has_vo = len(resolved_vos) > 0
    has_bgm = bool(bgm_file_path and Path(bgm_file_path).exists())
    bgm_vol = max(0.0, min(2.0, float(bgm_volume if bgm_volume is not None else 0.25)))

    srt_entries = _parse_srt(Path(burn_srt_path)) if burn_srt_path else []
    burned_subtitles = False

    with tempfile.TemporaryDirectory(prefix="agvs_master_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        inputs: list[str] = []
        filters: list[str] = []
        concat_pairs: list[str] = []

        total_dur = 0.0
        kept_sfx_count = 0
        muted_or_silent_count = 0

        for idx, planned_item in enumerate(plan["items"]):
            item = planned_item["item"]
            clip_path = Path(item["file_path"]).resolve()
            if not clip_path.exists():
                raise FileNotFoundError(f"镜头视频文件不存在：{clip_path}")
            ts = max(0.0, float(item.get("trim_start") or 0.0))
            base_dur = float(planned_item["base_duration"])
            eff_dur = float(planned_item["effective_duration"])
            pad_sec = float(planned_item["pad_sec"])
            total_dur = round(total_dur + eff_dur, 3)

            if ts > 0:
                inputs.extend(["-ss", f"{ts:.3f}"])
            inputs.extend(["-t", f"{base_dur:.3f}", "-i", str(clip_path)])

            v_chain = (
                f"[{idx}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps},trim=0:{base_dur:.3f},setpts=PTS-STARTPTS"
            )
            if pad_sec > 0.001:
                v_chain += f",tpad=stop_mode=clone:stop_duration={pad_sec:.3f},trim=0:{eff_dur:.3f},setpts=PTS-STARTPTS"
            v_chain += f",format=yuv420p[v{idx}]"
            filters.append(v_chain)

            mute = item.get("audio_strategy") == "mute_for_local_vo"
            if not mute and has_audio_stream(clip_path):
                filters.append(
                    f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"atrim=0:{base_dur:.3f},apad=whole_dur={eff_dur:.3f},atrim=0:{eff_dur:.3f},asetpts=PTS-STARTPTS[a{idx}]"
                )
                kept_sfx_count += 1
            else:
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=0:{eff_dur:.3f},asetpts=PTS-STARTPTS[a{idx}]"
                )
                muted_or_silent_count += 1

            concat_pairs.append(f"[v{idx}][a{idx}]")

        next_input_idx = len(plan["items"])
        filters.append(f"{''.join(concat_pairs)}concat=n={len(plan['items'])}:v=1:a=1[vcat][sfx_raw]")
        v_cur = "[vcat]"

        # 烧录黄金安全区中文字幕
        if srt_entries:
            for sub_i, (s_start, s_end, s_text) in enumerate(srt_entries):
                s0 = max(0.0, min(s_start, total_dur))
                s1 = max(s0 + 0.1, min(s_end, total_dur))
                if s0 >= total_dur:
                    continue
                sub_png = tmp_dir / f"sub_{sub_i:03d}.png"
                _, _, overlay_y = _render_subtitle_strip_png(
                    s_text, sub_png, out_w, out_h, hdr_hlg=hdr_hlg
                )
                inputs.extend(["-i", str(sub_png)])
                sub_in = next_input_idx
                next_input_idx += 1
                v_next = f"[vsub{sub_i}]"
                filters.append(
                    f"{v_cur}[{sub_in}:v]overlay=x=(W-w)/2:y={overlay_y}:eof_action=repeat:"
                    f"enable='between(t,{s0:.3f},{s1:.3f})'{v_next}"
                )
                v_cur = v_next
                burned_subtitles = True

        # 跨平台色彩管线与硬件/软件编码器自动选择
        encoder_name, target_pix, vcodec_args = select_video_encoder(hdr_hlg=hdr_hlg)
        if hdr_hlg:
            filters.append(
                f"{v_cur}curves=m='0/0 0.20/0.21 0.50/0.54 0.78/0.83 1/1',"
                f"colorspace=all=bt2020:iall=bt709:range=tv:format=yuv420p10,"
                f"setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc:range=tv,"
                f"format={target_pix}[vout]"
            )
            v_cur = "[vout]"
        else:
            filters.append(
                f"{v_cur}setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p[vout]"
            )
            v_cur = "[vout]"

        # 轨 2 旁白人声 VO 汇聚（按镜头窗口严格 atrim 防止跨镜叠音，支持 speed 模式微加速 atempo）
        if has_vo:
            vo_labels: list[str] = []
            for k, vo in enumerate(resolved_vos):
                inputs.extend(["-i", str(vo["path"])])
                vo_in = next_input_idx
                next_input_idx += 1
                delay_ms = int(round(vo["start_sec"] * 1000))
                vo_vol = max(0.1, min(3.0, float(vo["volume"])))
                vo_tempo = float(vo.get("tempo") or 1.0)
                vo_max_len = vo.get("max_duration_sec")

                vo_chain_parts = [
                    f"[{vo_in}:a]aresample=48000",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                    "highpass=f=80",
                ]
                if vo_tempo > 1.001 or vo_tempo < 0.999:
                    vo_chain_parts.append(f"atempo={vo_tempo:.4f}")
                if vo_max_len is not None and float(vo_max_len) > 0:
                    vo_chain_parts.append(f"atrim=0:{max(0.1, float(vo_max_len)):.3f}")
                    vo_chain_parts.append("asetpts=PTS-STARTPTS")
                vo_chain_parts.extend(
                    [
                        f"volume={vo_vol:.2f}",
                        f"adelay={delay_ms}|{delay_ms}",
                        f"apad=whole_dur={total_dur:.3f}",
                        f"atrim=0:{total_dur:.3f}",
                        f"asetpts=PTS-STARTPTS[vo{k}]",
                    ]
                )
                filters.append(",".join(vo_chain_parts))
                vo_labels.append(f"[vo{k}]")
            if len(vo_labels) == 1:
                if has_bgm:
                    filters.append(f"{vo_labels[0]}asplit=2[vo_mix][vo_sc]")
                else:
                    filters.append(f"{vo_labels[0]}anull[vo_mix]")
            else:
                if has_bgm:
                    filters.append(
                        f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=first:normalize=0,"
                        f"asplit=2[vo_mix][vo_sc]"
                    )
                else:
                    filters.append(
                        f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=first:normalize=0[vo_mix]"
                    )

        # 轨 3 BGM（循环对齐 + 片尾 2 秒自然淡出 + 有 VO 时 sidechaincompress 闪避）
        if has_bgm:
            inputs.extend(["-stream_loop", "-1", "-i", str(Path(bgm_file_path).resolve())])
            bgm_in = next_input_idx
            next_input_idx += 1
            fade_dur = min(2.0, max(0.3, total_dur * 0.25))
            fade_st = max(0.0, total_dur - fade_dur)
            filters.append(
                f"[{bgm_in}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
                f"volume={bgm_vol:.3f},afade=t=out:st={fade_st:.3f}:d={fade_dur:.3f}[bgm_pre]"
            )
            if has_vo:
                filters.append(
                    "[bgm_pre][vo_sc]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=350:makeup=1[bgm_final]"
                )
            else:
                filters.append("[bgm_pre]anull[bgm_final]")

        # 三轨总线汇合 + 真峰值限幅器 alimiter=0.95
        if has_vo and has_bgm:
            filters.append(
                "[sfx_raw][vo_mix][bgm_final]amix=inputs=3:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        elif has_vo:
            filters.append(
                "[sfx_raw][vo_mix]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        elif has_bgm:
            filters.append(
                "[sfx_raw][bgm_final]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        else:
            filters.append("[sfx_raw]alimiter=limit=0.95[aout]")

        # 统一写入 filter_complex 脚本文件，彻底规避 Windows 32KB 命令行长度上限（兼容 FFmpeg 4–7 `-filter_complex_script` 与 FFmpeg 8/9+ `-/filter_complex`）
        filter_graph_text = ";\n".join(filters)
        filter_script_path = tmp_dir / "filter_complex.txt"
        filter_script_path.write_text(filter_graph_text, encoding="utf-8")
        fc_file_flag = _select_filter_complex_file_flag()
        fc_args = [fc_file_flag, str(filter_script_path)] if fc_file_flag else ["-filter_complex", filter_graph_text]

        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            *inputs,
            *fc_args,
            "-map",
            v_cur,
            "-map",
            "[aout]",
            *vcodec_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300.0)
        if res.returncode != 0 or not output_mp4.exists():
            raise RuntimeError(f"母带封装失败（{encoder_name}）：{res.stderr[-500:]}")

    av_probe = _probe_av_durations(output_mp4)
    adjustments = plan["adjustments"]
    hdr_tag = "10-bit HEVC BT.2020 HLG HDR" if hdr_hlg else "BT.709 SDR"
    audio_parts = [f"原生环境音({kept_sfx_count}镜)"]
    if has_vo:
        sync_label = "尾帧定格对齐" if norm_sync_mode == "pad" else "人声微加速+兜底定格"
        adj_note = f"/{len(adjustments)}镜自动延展或变速" if adjustments else ""
        audio_parts.append(f"旁白VO({len(resolved_vos)}条·{sync_label}{adj_note})")
    if has_bgm:
        audio_parts.append(f"BGM(vol={bgm_vol:.2f}" + ("+侧链闪避" if has_vo else "") + "+片尾2s淡出)")
    sub_note = " + 黄金安全区中文字幕" if burned_subtitles else ""
    drift_note = f" · 音画差 {av_probe['av_drift_sec']:.3f}s"
    return {
        "mode": "local_hdr_hlg" if hdr_hlg else "local_master_1080p",
        "encoder": encoder_name,
        "output_path": str(output_mp4.resolve()),
        "width": out_w,
        "height": out_h,
        "fps": fps,
        "hdr_hlg": bool(hdr_hlg),
        "has_bgm": has_bgm,
        "vo_count": len(resolved_vos),
        "vo_sync_mode": norm_sync_mode,
        "vo_sync_adjustments": adjustments,
        "kept_sfx_shots": kept_sfx_count,
        "muted_shots": muted_or_silent_count,
        "burned_subtitles": burned_subtitles,
        "total_duration_sec": round(total_dur, 2),
        "video_duration_sec": av_probe["video_duration_sec"],
        "audio_duration_sec": av_probe["audio_duration_sec"],
        "av_drift_sec": av_probe["av_drift_sec"],
        "log": (
            f"本地 {encoder_name} 封装完成：{out_w}x{out_h}@{fps}fps · {hdr_tag} · "
            f"三轨混音[{' + '.join(audio_parts)}]{sub_note}{drift_note}"
        ),
    }


def build_local_preview_mp4(
    timeline_items: list[dict[str, Any]],
    output_mp4: Path,
    bgm_file_path: str = "",
    bgm_volume: float = 0.25,
    vo_tracks: list[dict[str, Any]] | None = None,
    hdr_hlg: bool = False,
    burn_srt_path: Path | None = None,
    vo_sync_mode: str = "pad",
) -> dict[str, Any]:
    """向后兼容的本地拼片入口：输出真 1080p + 三轨音频（原生环境音 + 旁白 VO + 闪避 BGM）"""
    return build_master_base_mp4(
        timeline_items=timeline_items,
        output_mp4=output_mp4,
        bgm_file_path=bgm_file_path,
        bgm_volume=bgm_volume,
        vo_tracks=vo_tracks,
        hdr_hlg=hdr_hlg,
        burn_srt_path=burn_srt_path,
        vo_sync_mode=vo_sync_mode,
    )


def make_export_bundle_dir(project_slug: str, ep_number: int) -> Path:
    d = EXPORTS_DIR / f"{project_slug}_ep{ep_number:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_shot_media_dir(project_slug: str, ep_number: int, shot_no: int) -> Path:
    d = MEDIA_DIR / project_slug / f"ep{ep_number:02d}" / f"shot{shot_no:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d
