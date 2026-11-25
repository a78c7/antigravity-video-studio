#!/usr/bin/env python3
"""
Google Colab GPU 真 AI 后期母带引擎（Real-ESRGAN v3 4K 超分 + 可选 RIFE v4.26 60fps + 10-bit HLG HDR + 安全区字幕）：
- 支持三大模式：
  1) `upscale_4k_30fps`（推荐默认）：仅跑 Real-ESRGAN v3 4K 超分、保持源帧率（24/30fps），保留胶片电影感且提速 2 倍；
  2) `upscale_4k_60fps`：先在原帧率上跑 Real-ESRGAN v3 4K 超分，再用 RIFE v4.26（scale=0.5 4K 模式）补至 60fps；
  3) `ai_60fps_1080p`：保持 1080p，仅跑 RIFE v4.26 60fps 补帧；
- 10-bit HLG HDR 封装（`hdr_hlg=True`）：温和电影高光曲线 + BT.2020 转换 + 10-bit HEVC HLG（优先 `hevc_nvenc`，无 NVENC 时优雅回退 `libx265`，显式写入 `bt2020 / arib-std-b67 / bt2020nc / tv` 色彩标签），4K 码率控在 ~22–26 Mbps；
- 黄金安全区中文字幕：将 `.srt` 精准转写为匹配实际 `out_w x out_h` 分辨率的 `.ass`（竖屏 `MarginV = 25% * out_h`）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

RIFE_ZIP_URL = "https://huggingface.co/hzwer/RIFE/resolve/main/RIFEv4.26_0921.zip"
ESRGAN_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth"

VALID_MODES = ("upscale_4k_30fps", "upscale_4k_60fps", "ai_60fps_1080p")

WARPLAYER_CODE = """import torch

backwarp_tenGrid = {}

def warp(tenInput, tenFlow):
    flow_device = tenFlow.device
    k = (str(flow_device), str(tenFlow.size()))
    if k not in backwarp_tenGrid:
        tenHorizontal = torch.linspace(-1.0, 1.0, tenFlow.shape[3], device=flow_device).view(
            1, 1, 1, tenFlow.shape[3]).expand(tenFlow.shape[0], -1, tenFlow.shape[2], -1)
        tenVertical = torch.linspace(-1.0, 1.0, tenFlow.shape[2], device=flow_device).view(
            1, 1, tenFlow.shape[2], 1).expand(tenFlow.shape[0], -1, -1, tenFlow.shape[3])
        backwarp_tenGrid[k] = torch.cat([tenHorizontal, tenVertical], 1).to(flow_device)

    tenFlow = torch.cat([
        tenFlow[:, 0:1, :, :] / ((tenInput.shape[3] - 1.0) / 2.0),
        tenFlow[:, 1:2, :, :] / ((tenInput.shape[2] - 1.0) / 2.0),
    ], 1)
    g = (backwarp_tenGrid[k].to(tenFlow.dtype) + tenFlow).permute(0, 2, 3, 1)
    return torch.nn.functional.grid_sample(
        input=tenInput, grid=g, mode="bilinear", padding_mode="border", align_corners=True
    )
"""


def run_cmd(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        s = line.strip()
        if s:
            print(s, flush=True)
    rc = proc.wait()
    if check and rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}): {' '.join(cmd)}\n{''.join(lines[-20:])}")
    return subprocess.CompletedProcess(cmd, rc, "".join(lines), "")


def ensure_system_tools(need_cjk_font: bool) -> None:
    need_pkgs: list[str] = []
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        need_pkgs.append("ffmpeg")
    if need_cjk_font and not Path("/usr/share/fonts/opentype/noto").exists() and not Path("/usr/share/fonts/truetype/noto").exists():
        need_pkgs.append("fonts-noto-cjk")
    if need_pkgs:
        print(f"[Setup] Installing system packages: {', '.join(need_pkgs)}", flush=True)
        run_cmd(["apt-get", "update", "-qq"])
        run_cmd(["apt-get", "install", "-y", "-qq", *need_pkgs])


def ensure_spandrel() -> None:
    if importlib.util.find_spec("spandrel") is None:
        print("[Setup] Installing spandrel (zero-conflict PyTorch AI upscaler loader)...", flush=True)
        run_cmd([sys.executable, "-m", "pip", "install", "-q", "spandrel"])


def download_file(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 100_000:
        return dest
    print(f"[Weights] Downloading {dest.name} ...", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    print(f"[Weights] Saved {dest.name} ({len(data) / (1024 * 1024):.1f} MB)", flush=True)
    return dest


def probe_video(path: Path) -> tuple[int, int, float, float, bool, str]:
    """返回 (width, height, fps, duration, has_audio, audio_codec)"""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        text=True,
    )
    info = json.loads(out)
    v_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v_stream:
        raise RuntimeError(f"输入文件不包含视频轨: {path}")
    a_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    has_audio = a_stream is not None
    audio_codec = str((a_stream or {}).get("codec_name") or "")
    w = int(v_stream["width"])
    h = int(v_stream["height"])
    fps_raw = v_stream.get("avg_frame_rate") or v_stream.get("r_frame_rate") or "30/1"
    if "/" in fps_raw:
        num, den = fps_raw.split("/", 1)
        fps = float(num) / max(1.0, float(den))
    else:
        fps = float(fps_raw)
    if fps <= 1.0 or fps > 240.0:
        fps = 30.0
    dur = float(v_stream.get("duration") or info.get("format", {}).get("duration") or 0.0)
    return w, h, fps, dur, has_audio, audio_codec


def _probe_encoder_works(codec: str, pix_fmt: str, extra_args: list[str] | None = None) -> bool:
    """实际跑 2 帧黑屏空编码探测 NVENC 是否在当前 GPU 上真可用"""
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
        codec,
        *(extra_args or []),
        "-pix_fmt",
        pix_fmt,
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)
        return res.returncode == 0
    except Exception:
        return False


def _srt_ts_to_ass_ts(ts: str) -> str:
    """将 SRT 时间戳 HH:MM:SS,mmm 转为 ASS 格式 H:MM:SS.cc"""
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
    if not m:
        return "0:00:00.00"
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ms = int(m.group(4).ljust(3, "0")[:3])
    cs = min(99, int(round(ms / 10.0)))
    return f"{hh}:{mm:02d}:{ss:02d}.{cs:02d}"


def _wrap_ass_cn_line(text: str, max_chars: int = 15) -> str:
    """对长中文句子按自然标点折行（\\N），避免竖屏字幕过宽冲进右侧按钮列"""
    out_parts: list[str] = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if len(s) <= max_chars:
            out_parts.append(s)
            continue
        if len(s) <= max_chars * 2:
            mid = len(s) // 2
            split_at = -1
            best_dist = 999
            for idx, ch in enumerate(s):
                if ch in "，。！？；、,.;!?" and 3 <= idx <= len(s) - 4:
                    dist = abs(idx - mid)
                    if dist < best_dist:
                        best_dist = dist
                        split_at = idx + 1
            if split_at > 0:
                out_parts.append(s[:split_at].strip() + r"\N" + s[split_at:].strip())
                continue
        chunks = [s[i : i + max_chars] for i in range(0, len(s), max_chars)]
        out_parts.append(r"\N".join(chunks))
    return r"\N".join(out_parts)


def convert_srt_to_safe_ass(
    srt_path: Path,
    ass_path: Path,
    out_w: int,
    out_h: int,
    *,
    font_name: str = "Noto Sans CJK SC",
    hdr_hlg: bool = True,
) -> Path:
    """将 .srt 转换为绑定真实分辨率 PlayResX/PlayResY 的黄金安全区 .ass 字幕文件"""
    is_vertical = out_h > out_w
    margin_v = int(round(out_h * (0.25 if is_vertical else 0.09)))
    margin_lr = int(round(out_w * (0.11 if is_vertical else 0.08)))
    font_size = max(34, int(round(out_w * 0.043))) if is_vertical else max(34, int(round(out_h * 0.045)))
    outline = max(2, int(round(font_size * 0.085)))
    shadow = max(1, int(round(font_size * 0.045)))
    primary_color = "&H00EAEAEA" if hdr_hlg else "&H00FFFFFF"

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {out_w}",
            f"PlayResY: {out_h}",
            "ScaledBorderAndShadow: yes",
            "WrapStyle: 2",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font_name},{font_size},{primary_color},&H000000FF,&H00121010,&H90000000,"
            f"-1,0,0,0,100,100,1,0,1,{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )

    raw = srt_path.read_text(encoding="utf-8-sig", errors="ignore").replace("\r\n", "\n")
    blocks = re.split(r"\n{2,}", raw.strip())
    events: list[str] = []
    max_chars = 15 if is_vertical else 26
    for blk in blocks:
        lines = [ln.strip() for ln in blk.split("\n") if ln.strip()]
        t_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if t_idx < 0 or t_idx + 1 >= len(lines):
            continue
        parts = lines[t_idx].split("-->")
        if len(parts) != 2:
            continue
        st = _srt_ts_to_ass_ts(parts[0])
        et = _srt_ts_to_ass_ts(parts[1])
        txt = _wrap_ass_cn_line("\n".join(lines[t_idx + 1 :]), max_chars=max_chars)
        txt = txt.replace("{", r"\{").replace("}", r"\}")
        if txt:
            events.append(f"Dialogue: 0,{st},{et},Default,,0,0,0,,{txt}")

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(header + "\n" + "\n".join(events) + "\n", encoding="utf-8")
    return ass_path


def load_rife_model(cache_dir: Path, device):
    import torch

    rife_dir = cache_dir / "rife_v426"
    model_pkg = rife_dir / "model"
    model_pkg.mkdir(parents=True, exist_ok=True)
    (model_pkg / "__init__.py").write_text("", encoding="utf-8")
    (model_pkg / "warplayer.py").write_text(WARPLAYER_CODE, encoding="utf-8")

    flownet_pkl = rife_dir / "flownet.pkl"
    ifnet_py = rife_dir / "IFNet_HDv3.py"
    if not flownet_pkl.exists() or not ifnet_py.exists():
        zip_path = download_file(RIFE_ZIP_URL, cache_dir / "RIFEv4.26_0921.zip")
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if member.startswith("__MACOSX/") or member.endswith("/"):
                    continue
                base = Path(member).name
                if base in ("flownet.pkl", "IFNet_HDv3.py"):
                    (rife_dir / base).write_bytes(zf.read(member))

    if str(rife_dir) not in sys.path:
        sys.path.insert(0, str(rife_dir))

    from IFNet_HDv3 import IFNet  # type: ignore

    net = IFNet()
    state = torch.load(str(flownet_pkl), map_location="cpu", weights_only=False)
    cleaned = {k.replace("module.", ""): v for k, v in state.items()}
    net.load_state_dict(cleaned, strict=False)
    net.to(device).eval()
    return net


def load_esrgan_model(cache_dir: Path, device):
    import spandrel

    pth = download_file(ESRGAN_URL, cache_dir / "realesr-general-x4v3.pth")
    desc = spandrel.ModelLoader(device=device).load_from_file(str(pth))
    model = desc.model.to(device).eval()
    if device.type == "cuda":
        model = model.half()
    return model, int(getattr(desc, "scale", 4) or 4)


def run_rife_frame(rife_net, f0, f1, timestep: float, device, *, scale: float = 1.0):
    """官方 RIFE v4.26 任意时间步补帧；4K 帧使用 scale=0.5 需 128 对齐"""
    import torch
    import torch.nn.functional as F

    _, _, h, w = f0.shape
    align = int(round(64 / scale))
    ph = ((h - 1) // align + 1) * align
    pw = ((w - 1) // align + 1) * align
    pad = (0, pw - w, 0, ph - h)
    p0 = F.pad(f0, pad, mode="replicate") if (ph != h or pw != w) else f0
    p1 = F.pad(f1, pad, mode="replicate") if (ph != h or pw != w) else f1
    inp = torch.cat((p0, p1), dim=1)
    scale_list = [16 / scale, 8 / scale, 4 / scale, 2 / scale, 1 / scale]
    with torch.inference_mode(), torch.amp.autocast("cuda", enabled=(device.type == "cuda")):
        _, _, merged = rife_net(inp, timestep=float(timestep), scale_list=scale_list)
        out = merged[-1][:, :, :h, :w].clamp(0.0, 1.0)
    return out


def upscale_tiled_4k(model, scale: int, img_bchw, target_h: int, target_w: int, device, tile: int = 1120, overlap: int = 24):
    """纯 FP16 重叠分块 GPU 超分 + 电影感保真混合（保留 12% 原图细节避免塑料磨皮），再 GPU 双三次缩放到目标 4K"""
    import torch
    import torch.nn.functional as F

    _, c, h, w = img_bchw.shape
    use_cuda = device.type == "cuda"
    inp_dtype = torch.float16 if use_cuda else torch.float32
    x = img_bchw.to(dtype=inp_dtype)

    if h <= tile and w <= tile:
        with torch.inference_mode():
            sr = model(x).clamp(0.0, 1.0)
    else:
        out_h, out_w = h * scale, w * scale
        output = torch.empty((1, c, out_h, out_w), device=device, dtype=inp_dtype)
        tiles_y = math.ceil(h / tile)
        tiles_x = math.ceil(w / tile)
        with torch.inference_mode():
            for ty in range(tiles_y):
                for tx in range(tiles_x):
                    y0 = ty * tile
                    x0 = tx * tile
                    y1 = min(y0 + tile, h)
                    x1 = min(x0 + tile, w)

                    iy0 = max(y0 - overlap, 0)
                    ix0 = max(x0 - overlap, 0)
                    iy1 = min(y1 + overlap, h)
                    ix1 = min(x1 + overlap, w)

                    patch = x[:, :, iy0:iy1, ix0:ix1]
                    sr_patch = model(patch).clamp(0.0, 1.0)

                    oy0 = (y0 - iy0) * scale
                    ox0 = (x0 - ix0) * scale
                    oy1 = oy0 + (y1 - y0) * scale
                    ox1 = ox0 + (x1 - x0) * scale
                    output[:, :, y0 * scale : y1 * scale, x0 * scale : x1 * scale] = sr_patch[:, :, oy0:oy1, ox0:ox1]
        sr = output

    if sr.shape[2] != target_h or sr.shape[3] != target_w:
        sr_target = F.interpolate(sr.float(), size=(target_h, target_w), mode="bicubic", align_corners=False, antialias=True)
    else:
        sr_target = sr.float()

    bicubic_ref = F.interpolate(img_bchw.float(), size=(target_h, target_w), mode="bicubic", align_corners=False, antialias=True)
    blended = (0.88 * sr_target + 0.12 * bicubic_ref).clamp(0.0, 1.0)
    return blended.to(dtype=inp_dtype)


def build_encode_command(
    *,
    input_path: Path,
    output_path: Path,
    out_w: int,
    out_h: int,
    out_fps: float,
    has_audio: bool,
    audio_codec: str,
    do_upscale: bool,
    hdr_hlg: bool,
    ass_path: Path | None,
    use_hevc_nvenc: bool,
    use_h264_nvenc: bool,
) -> tuple[list[str], str]:
    """构造 FFmpeg 编码命令与编码器名称"""
    fps_str = f"{out_fps:.3f}".rstrip("0").rstrip(".")
    enc_cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{out_w}x{out_h}",
        "-r",
        fps_str,
        "-i",
        "pipe:0",
    ]
    if has_audio:
        audio_args = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        enc_cmd.extend(["-i", str(input_path), "-map", "0:v:0", "-map", "1:a:0", *audio_args])
    else:
        enc_cmd.extend(["-map", "0:v:0"])

    vf_parts: list[str] = []
    if ass_path and ass_path.exists():
        esc_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")
        vf_parts.append(f"ass='{esc_ass}'")

    if hdr_hlg:
        target_pix = "p010le" if use_hevc_nvenc else "yuv420p10le"
        vf_parts.extend(
            [
                "curves=m='0/0 0.20/0.21 0.50/0.54 0.78/0.83 1/1'",
                "colorspace=all=bt2020:iall=bt709:range=tv:format=yuv420p10",
                "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc:range=tv",
                f"format={target_pix}",
            ]
        )
        if vf_parts:
            enc_cmd.extend(["-vf", ",".join(vf_parts)])

        avg_br, max_br, buf_br, cq, crf = ("24M", "26M", "52M", "23", "20") if do_upscale else ("10M", "14M", "28M", "21", "19")
        if use_hevc_nvenc:
            encoder_label = "hevc_nvenc (10-bit HLG)"
            enc_cmd.extend(
                [
                    "-c:v", "hevc_nvenc",
                    "-preset", "p5",
                    "-profile:v", "main10",
                    "-rc", "vbr",
                    "-cq", cq,
                    "-b:v", avg_br,
                    "-maxrate", max_br,
                    "-bufsize", buf_br,
                    "-pix_fmt", "p010le",
                    "-tag:v", "hvc1",
                ]
            )
        else:
            encoder_label = "libx265 (10-bit HLG)"
            enc_cmd.extend(
                [
                    "-c:v", "libx265",
                    "-preset", "medium",
                    "-profile:v", "main10",
                    "-crf", crf,
                    "-maxrate", max_br,
                    "-bufsize", buf_br,
                    "-pix_fmt", "yuv420p10le",
                    "-tag:v", "hvc1",
                    "-x265-params",
                    "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited",
                ]
            )
        enc_cmd.extend(
            [
                "-color_primaries", "bt2020",
                "-color_trc", "arib-std-b67",
                "-colorspace", "bt2020nc",
                "-color_range", "tv",
            ]
        )
    else:
        if vf_parts:
            enc_cmd.extend(["-vf", ",".join(vf_parts)])
        avg_br, max_br, buf_br, cq = ("24M", "26M", "52M", "22") if do_upscale else ("12M", "16M", "32M", "20")
        if use_h264_nvenc:
            encoder_label = "h264_nvenc (SDR)"
            enc_cmd.extend(
                [
                    "-c:v", "h264_nvenc",
                    "-preset", "p5",
                    "-rc", "vbr",
                    "-cq", cq,
                    "-b:v", avg_br,
                    "-maxrate", max_br,
                    "-bufsize", buf_br,
                ]
            )
        else:
            encoder_label = "libx264 (SDR)"
            enc_cmd.extend(
                [
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "18",
                    "-maxrate", max_br,
                    "-bufsize", buf_br,
                ]
            )
        enc_cmd.extend(
            [
                "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
                "-color_range", "tv",
            ]
        )

    enc_cmd.extend(["-movflags", "+faststart", str(output_path)])
    return enc_cmd, encoder_label


def process_video(
    input_path: Path,
    output_path: Path,
    *,
    mode: str = "upscale_4k_30fps",
    target_w: int = 2160,
    target_h: int = 3840,
    target_fps: int = 30,
    srt_path: Path | None = None,
    hdr_hlg: bool = True,
) -> None:
    import numpy as np
    import torch

    if mode not in VALID_MODES:
        raise ValueError(f"不支持的处理模式：{mode}（可选：{', '.join(VALID_MODES)}）")

    t_start = time.time()
    do_upscale = mode in ("upscale_4k_30fps", "upscale_4k_60fps")
    do_rife = mode in ("upscale_4k_60fps", "ai_60fps_1080p")
    need_sub = bool(srt_path and srt_path.exists() and srt_path.stat().st_size > 10)
    ensure_system_tools(need_cjk_font=need_sub)
    if do_upscale:
        ensure_spandrel()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    vram_gb = (torch.cuda.get_device_properties(0).total_memory / (1024**3)) if device.type == "cuda" else 0.0
    gpu_name = torch.cuda.get_device_name(0) if device.type == "cuda" else "CPU"
    tile_size = 1120 if vram_gb >= 20.0 else 640

    src_w, src_h, src_fps, src_dur, has_audio, audio_codec = probe_video(input_path)
    out_w, out_h = (target_w, target_h) if do_upscale else (src_w, src_h)
    out_fps = float(target_fps) if do_rife else round(src_fps, 3)
    rife_scale = 0.5 if max(out_w, out_h) >= 2400 else 1.0

    print(
        f"[AI Master] Device={gpu_name} ({vram_gb:.1f}GB) | Mode={mode} | HDR_HLG={hdr_hlg} | "
        f"Source={src_w}x{src_h}@{src_fps:.3f}fps ({src_dur:.2f}s) -> Target={out_w}x{out_h}@{out_fps}fps | tile={tile_size}",
        flush=True,
    )

    cache_dir = Path("/content/.agvs_ai_cache")
    rife_net = load_rife_model(cache_dir, device) if do_rife else None
    esrgan_model, esrgan_scale = (load_esrgan_model(cache_dir, device) if do_upscale else (None, 1))

    use_hevc_nvenc = bool(
        hdr_hlg and device.type == "cuda" and _probe_encoder_works("hevc_nvenc", "p010le", ["-profile:v", "main10"])
    )
    use_h264_nvenc = bool(
        (not hdr_hlg) and device.type == "cuda" and _probe_encoder_works("h264_nvenc", "yuv420p")
    )

    with tempfile.TemporaryDirectory(prefix="agvs_colab_sub_") as sub_tmp:
        ass_path: Path | None = None
        if need_sub and srt_path:
            ass_path = convert_srt_to_safe_ass(
                srt_path,
                Path(sub_tmp) / "safe_zone.ass",
                out_w=out_w,
                out_h=out_h,
                font_name="Noto Sans CJK SC",
                hdr_hlg=hdr_hlg,
            )

        enc_cmd, encoder_label = build_encode_command(
            input_path=input_path,
            output_path=output_path,
            out_w=out_w,
            out_h=out_h,
            out_fps=out_fps,
            has_audio=has_audio,
            audio_codec=audio_codec,
            do_upscale=do_upscale,
            hdr_hlg=hdr_hlg,
            ass_path=ass_path,
            use_hevc_nvenc=use_hevc_nvenc,
            use_h264_nvenc=use_h264_nvenc,
        )
        print(
            f"[AI Master] Models ready in {time.time() - t_start:.1f}s | Encoder={encoder_label} | SafeSub={bool(ass_path)}",
            flush=True,
        )

        dec_cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(input_path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        dec_proc = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc_proc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        frame_bytes = src_w * src_h * 3
        assert dec_proc.stdout is not None and enc_proc.stdin is not None

        def read_next_prepared_tensor():
            raw = dec_proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                return None
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((src_h, src_w, 3)).copy()
            t = torch.from_numpy(arr).to(device, non_blocking=True).permute(2, 0, 1).unsqueeze(0).float().div_(255.0)
            if do_upscale and esrgan_model is not None:
                return upscale_tiled_4k(esrgan_model, esrgan_scale, t, out_h, out_w, device, tile=tile_size)
            return t.half() if device.type == "cuda" else t

        def emit_tensor(frame_bchw) -> None:
            out_u8 = (
                frame_bchw.float()
                .squeeze(0)
                .permute(1, 2, 0)
                .mul(255.0)
                .round()
                .clamp(0, 255)
                .to(torch.uint8)
                .cpu()
                .numpy()
            )
            enc_proc.stdin.write(out_u8.tobytes())

        out_idx = 0
        scene_cuts = 0
        t_loop = time.time()

        try:
            if not do_rife:
                while True:
                    frame = read_next_prepared_tensor()
                    if frame is None:
                        break
                    emit_tensor(frame)
                    out_idx += 1
                    if out_idx % 30 == 0:
                        elapsed = max(0.1, time.time() - t_loop)
                        print(
                            f"[AI Master] Upscaled {out_idx} frames ({out_idx / elapsed:.1f} fps)",
                            flush=True,
                        )
            else:
                f_prev = read_next_prepared_tensor()
                if f_prev is None:
                    raise RuntimeError("无法从输入视频读取任何帧")
                f_curr = read_next_prepared_tensor()
                prev_idx = 0

                while True:
                    src_pos = (out_idx / float(out_fps)) * src_fps
                    need_idx = int(math.floor(src_pos))
                    frac = src_pos - need_idx

                    while prev_idx < need_idx and f_curr is not None:
                        f_prev = f_curr
                        prev_idx += 1
                        f_curr = read_next_prepared_tensor()

                    if f_curr is None:
                        if need_idx <= prev_idx:
                            emit_tensor(f_prev)
                            out_idx += 1
                        break

                    if frac <= 0.02:
                        emit_tensor(f_prev)
                    elif frac >= 0.98:
                        emit_tensor(f_curr)
                    else:
                        diff = (f_prev.float() - f_curr.float()).abs().mean().item()
                        if diff > 0.24:
                            scene_cuts += 1
                            emit_tensor(f_prev if frac < 0.5 else f_curr)
                        else:
                            mid = run_rife_frame(rife_net, f_prev, f_curr, frac, device, scale=rife_scale)
                            emit_tensor(mid)

                    out_idx += 1
                    if out_idx % 30 == 0:
                        elapsed = max(0.1, time.time() - t_loop)
                        print(
                            f"[AI Master] Rendered {out_idx} frames ({out_idx / elapsed:.1f} fps, cuts_protected={scene_cuts})",
                            flush=True,
                        )
        finally:
            try:
                enc_proc.stdin.close()
            except Exception:
                pass
            try:
                dec_proc.stdout.close()
            except Exception:
                pass

        dec_rc = dec_proc.wait()
        enc_rc = enc_proc.wait()
        if dec_rc != 0:
            err = dec_proc.stderr.read().decode("utf-8", errors="ignore") if dec_proc.stderr else ""
            raise RuntimeError(f"FFmpeg 解码失败 (rc={dec_rc}): {err[-400:]}")
        if enc_rc != 0:
            err = enc_proc.stderr.read().decode("utf-8", errors="ignore") if enc_proc.stderr else ""
            raise RuntimeError(f"FFmpeg 编码封装失败 (rc={enc_rc}): {err[-400:]}")

    total_sec = time.time() - t_start
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(
        f"[AI Master] DONE! {out_idx} frames -> {out_w}x{out_h}@{out_fps}fps | {encoder_label} | "
        f"{size_mb:.2f} MB | cuts_protected={scene_cuts} | elapsed={total_sec:.1f}s",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mode", default="upscale_4k_30fps", choices=list(VALID_MODES))
    ap.add_argument("--width", type=int, default=2160)
    ap.add_argument("--height", type=int, default=3840)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--srt", default="")
    ap.add_argument("--hdr-hlg", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()
    srt_p = Path(args.srt) if args.srt else None
    process_video(
        Path(args.input),
        Path(args.output),
        mode=args.mode,
        target_w=args.width,
        target_h=args.height,
        target_fps=args.fps,
        srt_path=srt_p,
        hdr_hlg=bool(args.hdr_hlg),
    )


if __name__ == "__main__":
    main()
