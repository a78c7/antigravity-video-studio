"""
轻量化音画质检与高对比度编号拼图模块（低负载秒级体检，适配主流笔记本与桌面主机）：
1. `build_contact_sheet`：将多张候选首尾帧、封面图或分镜体检条带拼成宽约 1500px 的高清总览对比 JPG，
   每格自带大号红底白字高对比度编号标签与可选状态角标（如 AI推荐 / 需复听）；
2. `inspect_video_shot`：对单镜视频抽取头(0.3s)/中(50%)/尾(-0.25s) 3 帧拼横排质检图，
   并运行 ffmpeg `volumedetect` + `silencedetect` + 人声中频段(300–3400Hz)能量探测，
   自动标记疑似静音、疑似爆音、过长静音占比与非台词镜疑似 AI 幻觉乱说话风险。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps, ImageStat

from .media_ops import load_cjk_font, probe_media


def _badge_color(badge: str) -> tuple[int, int, int]:
    """根据角标语义自动匹配高对比度背景色"""
    b = (badge or "").strip()
    if any(k in b for k in ("异常", "爆音", "静音", "风险", "报错", "失败", "复听", "warn", "error")):
        return (196, 44, 44)
    if any(k in b for k in ("推荐", "正常", "通过", "定稿", "ok", "OK", "优选")):
        return (22, 152, 74)
    if any(k in b for k in ("试卡", "待审", "360p", "注意")):
        return (212, 128, 18)
    return (37, 99, 235)


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: Any,
    bg_rgb: tuple[int, int, int],
    fg_rgb: tuple[int, int, int] = (255, 255, 255),
    pad_x: int = 16,
    pad_y: int = 8,
    anchor_right: bool = False,
) -> tuple[int, int, int, int]:
    """绘制带细描边的高对比度圆角胶囊标签"""
    tb = draw.textbbox((0, 0), text, font=font)
    tw = int(tb[2] - tb[0])
    th = int(tb[3] - tb[1])
    box_w = tw + pad_x * 2
    box_h = th + pad_y * 2
    x0 = (xy[0] - box_w) if anchor_right else xy[0]
    y0 = xy[1]
    x1 = x0 + box_w
    y1 = y0 + box_h
    radius = min(14, box_h // 3)
    draw.rounded_rectangle(
        (x0, y0, x1, y1),
        radius=radius,
        fill=(*bg_rgb, 242),
        outline=(255, 255, 255, 220),
        width=2,
    )
    tx = x0 + pad_x - int(tb[0])
    ty = y0 + pad_y - int(tb[1])
    draw.text((tx, ty), text, font=font, fill=(*fg_rgb, 255))
    return (x0, y0, x1, y1)


def build_contact_sheet(
    image_entries: list[dict[str, Any]],
    output_jpg: Path,
    title: str = "",
    cols: int = 2,
) -> dict[str, Any]:
    """
    将多张候选图/质检图拼成一张宽约 1500px、带大号红底白字高对比度编号的总览对比 JPG。
    image_entries 每项支持：
      - label: str（如 "#1 S01 候选A"）
      - path: str | Path（本地图片路径）
      - badge: str（可选，如 "AI推荐" / "正常" / "需复听"）
      - caption: str（可选，底部补充说明）
    """
    output_jpg = Path(output_jpg)
    output_jpg.parent.mkdir(parents=True, exist_ok=True)
    if not image_entries:
        raise ValueError("image_entries 为空，无法生成编号对比总览图。")

    eff_cols = max(1, min(int(cols or 2), len(image_entries)))
    canvas_w = 1520
    outer_pad = 24
    gutter = 20
    cell_w = (canvas_w - outer_pad * 2 - gutter * (eff_cols - 1)) // eff_cols
    max_cell_img_h = int(round(cell_w * 1.85))
    min_cell_img_h = 220

    title_font = load_cjk_font(34, prefer_bold=True)
    label_font = load_cjk_font(30 if eff_cols <= 2 else 24, prefer_bold=True)
    badge_font = load_cjk_font(24 if eff_cols <= 2 else 20, prefer_bold=True)
    caption_font = load_cjk_font(20, prefer_bold=False)

    prepared: list[dict[str, Any]] = []
    for idx, entry in enumerate(image_entries, start=1):
        label = str(entry.get("label") or f"#{idx}").strip()
        badge = str(entry.get("badge") or "").strip()
        caption = str(entry.get("caption") or "").strip()
        img_path = Path(str(entry.get("path") or ""))

        img_rgb: Image.Image | None = None
        if img_path.exists() and img_path.is_file():
            try:
                with Image.open(img_path) as raw_im:
                    im = ImageOps.exif_transpose(raw_im).convert("RGB")
                    iw, ih = im.size
                    if iw > 0 and ih > 0:
                        scale = min(cell_w / iw, max_cell_img_h / ih)
                        nw = max(1, int(round(iw * scale)))
                        nh = max(1, int(round(ih * scale)))
                        img_rgb = im.resize((nw, nh), Image.Resampling.LANCZOS)
            except Exception:
                img_rgb = None

        if img_rgb is None:
            ph = Image.new("RGB", (cell_w, 320), (34, 38, 48))
            pdraw = ImageDraw.Draw(ph)
            pdraw.text((24, 140), f"图片缺失或无法读取\n{img_path.name}", font=caption_font, fill=(220, 120, 120))
            img_rgb = ph

        prepared.append(
            {
                "label": label,
                "badge": badge,
                "caption": caption,
                "image": img_rgb,
                "img_w": img_rgb.width,
                "img_h": img_rgb.height,
            }
        )

    rows: list[list[dict[str, Any]]] = [
        prepared[i : i + eff_cols] for i in range(0, len(prepared), eff_cols)
    ]
    header_h = 84 if title.strip() else outer_pad
    card_top_bar_h = 62
    caption_bar_h = 40
    row_heights: list[int] = []
    for row in rows:
        row_img_h = max(min_cell_img_h, max(item["img_h"] for item in row))
        has_cap = any(bool(item["caption"]) for item in row)
        row_heights.append(card_top_bar_h + row_img_h + (caption_bar_h if has_cap else 0))

    canvas_h = header_h + sum(row_heights) + gutter * max(0, len(rows) - 1) + outer_pad
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (18, 21, 27, 255))
    draw = ImageDraw.Draw(canvas)

    if title.strip():
        draw.rectangle((0, 0, canvas_w, header_h - 8), fill=(26, 30, 40, 255))
        draw.line((0, header_h - 8, canvas_w, header_h - 8), fill=(58, 66, 84, 255), width=2)
        draw.text((outer_pad, 20), title.strip(), font=title_font, fill=(246, 248, 252, 255))
        count_txt = f"共 {len(prepared)} 项"
        tb = draw.textbbox((0, 0), count_txt, font=badge_font)
        draw.text((canvas_w - outer_pad - (tb[2] - tb[0]), 28), count_txt, font=badge_font, fill=(165, 178, 204, 255))

    cur_y = header_h
    for r_idx, row in enumerate(rows):
        rh = row_heights[r_idx]
        has_cap = any(bool(item["caption"]) for item in row)
        img_box_h = rh - card_top_bar_h - (caption_bar_h if has_cap else 0)

        for c_idx, item in enumerate(row):
            cx = outer_pad + c_idx * (cell_w + gutter)
            cy = cur_y
            draw.rounded_rectangle(
                (cx, cy, cx + cell_w, cy + rh),
                radius=12,
                fill=(28, 32, 42, 255),
                outline=(72, 82, 102, 255),
                width=2,
            )
            draw.rectangle((cx + 2, cy + 2, cx + cell_w - 2, cy + card_top_bar_h), fill=(20, 24, 32, 255))
            draw.line((cx + 2, cy + card_top_bar_h, cx + cell_w - 2, cy + card_top_bar_h), fill=(52, 60, 76, 255), width=1)

            _draw_pill(
                draw,
                (cx + 12, cy + 9),
                item["label"],
                label_font,
                bg_rgb=(217, 34, 34),
                fg_rgb=(255, 255, 255),
                pad_x=16,
                pad_y=7,
            )

            if item["badge"]:
                _draw_pill(
                    draw,
                    (cx + cell_w - 12, cy + 11),
                    item["badge"],
                    badge_font,
                    bg_rgb=_badge_color(item["badge"]),
                    fg_rgb=(255, 255, 255),
                    pad_x=14,
                    pad_y=7,
                    anchor_right=True,
                )

            im = item["image"]
            px = cx + (cell_w - im.width) // 2
            py = cy + card_top_bar_h + (img_box_h - im.height) // 2
            canvas.paste(im.convert("RGBA"), (px, py))

            if has_cap:
                cap_y = cy + card_top_bar_h + img_box_h
                draw.rectangle((cx + 2, cap_y, cx + cell_w - 2, cy + rh - 2), fill=(22, 25, 33, 255))
                if item["caption"]:
                    cap_txt = item["caption"]
                    if len(cap_txt) > 54:
                        cap_txt = cap_txt[:52] + "…"
                    draw.text((cx + 14, cap_y + 8), cap_txt, font=caption_font, fill=(215, 222, 235, 255))

        cur_y += rh + gutter

    canvas.convert("RGB").save(output_jpg, format="JPEG", quality=88, optimize=True)
    return {
        "output_path": str(output_jpg.resolve()),
        "width": canvas_w,
        "height": canvas_h,
        "count": len(prepared),
        "cols": eff_cols,
        "title": title,
    }


def _extract_frame_at(video_path: Path, timestamp_sec: float, out_jpg: Path) -> bool:
    """用 ffmpeg 快速在指定时间戳抽取单帧"""
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, timestamp_sec):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out_jpg),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)
    if res.returncode == 0 and out_jpg.exists() and out_jpg.stat().st_size > 100:
        return True
    fallback = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-sseof",
        "-0.25",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out_jpg),
    ]
    res2 = subprocess.run(fallback, capture_output=True, text=True, timeout=15.0)
    return res2.returncode == 0 and out_jpg.exists() and out_jpg.stat().st_size > 100


def _probe_audio_metrics(video_path: Path, duration_sec: float) -> dict[str, Any]:
    """运行 volumedetect、silencedetect 与 300–3400Hz 中频人声段能量探测"""
    p1 = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-af",
            "volumedetect,silencedetect=n=-45dB:d=0.4",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=25.0,
    )
    err1 = p1.stderr or ""
    m_mean = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", err1)
    m_max = re.search(r"max_volume:\s*([-\d.]+)\s*dB", err1)
    mean_db = round(float(m_mean.group(1)), 2) if m_mean else None
    max_db = round(float(m_max.group(1)), 2) if m_max else None

    silence_durs = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", err1)]
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err1)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err1)]
    total_sil = sum(silence_durs)
    if len(starts) > len(ends) and duration_sec > starts[-1]:
        total_sil += max(0.0, duration_sec - starts[-1])
    silence_ratio = round(min(1.0, total_sil / max(0.1, duration_sec)), 3)

    p2 = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-af",
            "highpass=f=300,lowpass=f=3400,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=25.0,
    )
    err2 = p2.stderr or ""
    vm = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", err2)
    vx = re.search(r"max_volume:\s*([-\d.]+)\s*dB", err2)
    voice_mean_db = round(float(vm.group(1)), 2) if vm else None
    voice_max_db = round(float(vx.group(1)), 2) if vx else None

    return {
        "mean_volume_db": mean_db,
        "max_volume_db": max_db,
        "silence_duration_sec": round(total_sil, 2),
        "silence_ratio": silence_ratio,
        "voice_band_mean_db": voice_mean_db,
        "voice_band_max_db": voice_max_db,
    }


def inspect_video_shot(
    video_path: Path,
    out_strip_jpg: Path,
    expect_dialogue: bool = False,
) -> dict[str, Any]:
    """
    对单个镜头视频做秒级音画双检：
    1. 抽取头(0.3s) / 中(50%) / 尾(-0.25s) 3 帧拼成横排质检条带图 `out_strip_jpg`；
    2. 用 ffmpeg `volumedetect` + `silencedetect` + 中频人声段能量分析原生音轨健康度；
    3. 返回结构化体检报告（含峰值/均值 dB、静音占比、异常告警与中文摘要）。
    """
    video_path = Path(video_path)
    out_strip_jpg = Path(out_strip_jpg)
    if not video_path.exists():
        raise FileNotFoundError(f"待体检视频不存在：{video_path}")
    out_strip_jpg.parent.mkdir(parents=True, exist_ok=True)

    meta = probe_media(video_path)
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    dur = float(meta.get("duration_sec") or 0.0)
    has_audio = bool(meta.get("has_audio"))

    t_head = min(0.30, max(0.0, dur * 0.08))
    t_mid = max(0.0, dur * 0.50)
    t_tail = max(0.0, dur - 0.25)
    frame_specs = [
        ("头", t_head),
        ("中", t_mid),
        ("尾", t_tail),
    ]

    warnings: list[str] = []
    frame_lumas: list[float] = []

    with tempfile.TemporaryDirectory(prefix="agvs_qc_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        panels: list[tuple[str, Image.Image]] = []
        target_panel_w = 460
        for idx, (tag, ts) in enumerate(frame_specs):
            fp = tmp_dir / f"f{idx}.jpg"
            ok = _extract_frame_at(video_path, ts, fp)
            if ok:
                with Image.open(fp) as raw:
                    im = raw.convert("RGB")
                    luma = float(ImageStat.Stat(im.convert("L")).mean[0])
                    frame_lumas.append(round(luma, 1))
                    if luma < 8.0:
                        warnings.append(f"{tag}帧({ts:.2f}s)疑似全黑画面(均值亮度={luma:.1f})")
                    elif luma > 248.0:
                        warnings.append(f"{tag}帧({ts:.2f}s)疑似过曝纯白(均值亮度={luma:.1f})")
                    scale = target_panel_w / max(1, im.width)
                    nh = max(120, int(round(im.height * scale)))
                    im_resized = im.resize((target_panel_w, nh), Image.Resampling.LANCZOS)
                    panels.append((f"{tag} {ts:.2f}s", im_resized))
            else:
                ph = Image.new("RGB", (target_panel_w, 460), (40, 32, 36))
                panels.append((f"{tag} 抽帧失败", ph))
                warnings.append(f"{tag}帧({ts:.2f}s)抽取失败")

        audio_metrics: dict[str, Any] = {
            "mean_volume_db": None,
            "max_volume_db": None,
            "silence_duration_sec": 0.0,
            "silence_ratio": 1.0,
            "voice_band_mean_db": None,
            "voice_band_max_db": None,
        }
        if not has_audio:
            warnings.append("源视频无音轨（混音时将自动补静音）")
        else:
            audio_metrics = _probe_audio_metrics(video_path, dur)
            mean_db = audio_metrics["mean_volume_db"]
            max_db = audio_metrics["max_volume_db"]
            sil_ratio = audio_metrics["silence_ratio"]
            v_mean = audio_metrics["voice_band_mean_db"]
            v_max = audio_metrics["voice_band_max_db"]

            if mean_db is None:
                warnings.append("无法解析音轨响度（可能为无效空音轨）")
            else:
                if mean_db < -48.0:
                    warnings.append(f"疑似静音或声量极低（均值 {mean_db:.1f} dB < -48 dB）")
                if max_db is not None and max_db > -0.3:
                    warnings.append(f"疑似爆音削波（峰值 {max_db:.1f} dB > -0.3 dB）")
                if sil_ratio >= 0.85:
                    warnings.append(f"静音占比过高（{int(round(sil_ratio * 100))}%）")
                if (
                    not expect_dialogue
                    and v_mean is not None
                    and v_max is not None
                    and v_mean > -25.0
                    and (v_mean - mean_db) >= -2.0
                    and v_max > -10.0
                ):
                    warnings.append(
                        f"非台词镜中频人声段(300-3400Hz)能量异常集中（均值 {v_mean:.1f} dB / 峰值 {v_max:.1f} dB），"
                        "建议人工复听排查 AI 幻觉乱说话"
                    )

        pad = 16
        gap = 14
        header_h = 64
        footer_h = 46
        panel_h = max(im.height for _, im in panels)
        strip_w = pad * 2 + target_panel_w * 3 + gap * 2
        strip_h = header_h + panel_h + footer_h + pad

        strip = Image.new("RGBA", (strip_w, strip_h), (18, 21, 27, 255))
        draw = ImageDraw.Draw(strip)
        h_font = load_cjk_font(24, prefer_bold=True)
        tag_font = load_cjk_font(24, prefer_bold=True)
        sub_font = load_cjk_font(20, prefer_bold=False)

        status = "warn" if warnings else "ok"
        badge_txt = "需复核" if warnings else "音画正常"
        draw.rectangle((0, 0, strip_w, header_h), fill=(26, 31, 42, 255))
        title_str = f"{video_path.name}  |  {w}x{h} · {dur:.2f}s"
        draw.text((pad, 18), title_str, font=h_font, fill=(244, 247, 252, 255))
        _draw_pill(
            draw,
            (strip_w - pad, 12),
            badge_txt,
            tag_font,
            bg_rgb=_badge_color(badge_txt),
            pad_x=14,
            pad_y=6,
            anchor_right=True,
        )

        for idx, (tag_label, im) in enumerate(panels):
            px = pad + idx * (target_panel_w + gap)
            py = header_h + (panel_h - im.height) // 2
            strip.paste(im.convert("RGBA"), (px, py))
            _draw_pill(
                draw,
                (px + 10, header_h + 10),
                tag_label,
                tag_font,
                bg_rgb=(217, 34, 34),
                pad_x=12,
                pad_y=5,
            )

        foot_y = header_h + panel_h + 10
        if has_audio and audio_metrics["mean_volume_db"] is not None:
            audio_line = (
                f"原声均值 {audio_metrics['mean_volume_db']:.1f} dB  |  "
                f"峰值 {audio_metrics['max_volume_db']:.1f} dB  |  "
                f"静音占比 {int(round(audio_metrics['silence_ratio'] * 100))}%  |  "
                f"中频人声段 {audio_metrics['voice_band_mean_db']} dB"
            )
        else:
            audio_line = "原声状态：无音轨"
        if warnings:
            audio_line += f"  |  提示：{warnings[0]}"
        draw.text(
            (pad, foot_y),
            audio_line[:92],
            font=sub_font,
            fill=(252, 182, 110, 255) if warnings else (152, 224, 178, 255),
        )

        strip.convert("RGB").save(out_strip_jpg, format="JPEG", quality=88, optimize=True)

    summary_cn = (
        f"正常（均值 {audio_metrics['mean_volume_db']} dB，峰值 {audio_metrics['max_volume_db']} dB）"
        if not warnings
        else "；".join(warnings)
    )
    return {
        "video_path": str(video_path.resolve()),
        "strip_path": str(out_strip_jpg.resolve()),
        "width": w,
        "height": h,
        "duration_sec": round(dur, 2),
        "has_audio": has_audio,
        "mean_volume_db": audio_metrics["mean_volume_db"],
        "max_volume_db": audio_metrics["max_volume_db"],
        "voice_band_mean_db": audio_metrics["voice_band_mean_db"],
        "voice_band_max_db": audio_metrics["voice_band_max_db"],
        "silence_duration_sec": audio_metrics["silence_duration_sec"],
        "silence_ratio": audio_metrics["silence_ratio"],
        "frame_lumas": frame_lumas,
        "expect_dialogue": bool(expect_dialogue),
        "status": status,
        "warnings": warnings,
        "summary": summary_cn,
    }
