"""
时间线预演与多出口后期交付路由：
- 同源 Timeline 数据（定稿镜头顺序 + 粗裁秒数 + 原创配乐 + 字幕）
- 跨平台本地 1080p 三轨混音成片渲染（原生环境音 SFX + 旁白 VO + 自动闪避 BGM，可选 HLG HDR 与安全区字幕）
- Google Colab 云端 GPU 4K 超分 / 60fps 补帧 / 10-bit HLG HDR 母带派发与离线任务清单包
- 国际标准 .fcpxml + .srt 导出（剪映 / Final Cut Pro / DaVinci Resolve 原生直读）
- TTS 批量旁白配音、真人干声上传、全集音画体检、双比例封面自动生成与定稿
- AI 原创制作证明 (.md) 一键归档与本集未定稿废片一键清理
"""

from __future__ import annotations

import inspect
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..cli_media import cli_generate_covers, cli_generate_tts
from ..config import DATA_DIR, MEDIA_DIR, load_config
from ..db import db_ctx, now_str, rows_to_dicts
from ..flow_compiler import AUDIO_STRATEGIES, FLOW_MODES, compile_shot_prompts
from ..jobs import JobCancelledError, JobContext, cancel_job, get_job, list_jobs, submit_job
from ..media_ops import (
    build_master_base_mp4,
    export_fcpxml,
    export_srt,
    make_export_bundle_dir,
    make_shot_media_dir,
    probe_media,
    resolve_shot_clip_duration,
)
from ..qc_ops import build_contact_sheet, inspect_video_shot
from ..worker_colab import dispatch_episode_to_colab

router = APIRouter(prefix="/api", tags=["delivery"])


def _brand_name() -> str:
    return (load_config().get("BRAND_NAME") or "反重力视频工坊").strip() or "反重力视频工坊"


def _filter_supported_kwargs(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """仅透传目标函数签名支持的可选关键字参数（兼顾回调扩展与测试桩/旧签名）"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in sig.parameters}


def _make_job_progress_cb(ctx: JobContext) -> Any:
    """将底层媒体/渲染/Colab 回调统一适配到 JobContext.update"""

    def _cb(*args: Any, **kwargs: Any) -> None:
        pct = kwargs.get("progress_pct", kwargs.get("pct"))
        msg = kwargs.get("stage_msg", kwargs.get("msg", kwargs.get("message")))
        log_line = kwargs.get("log_line")
        provider_task_id = kwargs.get("provider_task_id")
        if len(args) >= 2:
            if isinstance(args[0], (int, float)):
                pct = int(args[0])
                msg = str(args[1])
            else:
                msg = str(args[0])
                if isinstance(args[1], (int, float)):
                    pct = int(args[1])
        elif len(args) == 1:
            if isinstance(args[0], (int, float)):
                pct = int(args[0])
            elif isinstance(args[0], str):
                msg = args[0]
        ctx.update(
            progress_pct=int(pct) if pct is not None else None,
            stage_msg=str(msg) if msg is not None else None,
            log_line=str(log_line) if log_line is not None else None,
            provider_task_id=str(provider_task_id) if provider_task_id is not None else None,
        )

    return _cb


def _normalize_vo_sync_mode(raw: str | None) -> str:
    mode = (raw or "pad").strip().lower()
    if mode not in ("pad", "speed"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 vo_sync_mode：{mode}，可选：pad（尾帧定格对齐）或 speed（微加速对齐）",
        )
    return mode


# ==================== 轻量异步任务中心 API（SQLite + 线程池 + 2s 轮询） ====================


@router.get("/jobs")
def list_async_jobs(
    episode_id: int | None = None,
    status: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """查询异步任务队列（支持按 episode_id / status 过滤，供前端 2 秒轮询）"""
    safe_limit = max(1, min(100, int(limit or 30)))
    items = list_jobs(episode_id=episode_id, status=status, limit=safe_limit)
    return {"items": items, "jobs": items}


@router.get("/jobs/{job_id}")
def get_async_job(job_id: int) -> dict[str, Any]:
    """查询单个异步任务详情（含 progress_pct / stage_msg / status / log_text / result / output_path / error_msg / cancel_requested）"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="异步任务不存在")
    return {**job, "job": job}


@router.post("/jobs/{job_id}/cancel")
def cancel_async_job(job_id: int) -> dict[str, Any]:
    """标记取消指定异步任务并返回最新状态"""
    job = cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="异步任务不存在")
    return {"ok": True, **job, "job": job}


class DispatchColabRequest(BaseModel):
    gpu_tier: str = "L4"
    task_mode: str = "upscale_4k_30fps"  # 'upscale_4k_30fps' | 'upscale_4k_60fps' | 'ai_60fps_1080p' | 'probe_gpu'
    hdr_hlg: bool = True
    burn_subtitles: bool = True
    vo_sync_mode: str = "pad"  # 'pad' | 'speed'
    async_mode: bool = False


class RenderLocalRequest(BaseModel):
    hdr_hlg: bool = False
    burn_subtitles: bool = False
    bgm_volume: float | None = None
    vo_sync_mode: str = "pad"  # 'pad' | 'speed'
    async_mode: bool = False


class CliGenerateTtsRequest(BaseModel):
    voice: str = "Kore"
    style_prompt: str = ""
    shot_ids: list[int] | None = None
    async_mode: bool = False


class QcInspectVideosRequest(BaseModel):
    resolution_label: str | None = None  # 可选 '360p' | '720p' | '1080p'，留空取最新/定稿视频
    shot_ids: list[int] | None = None
    cols: int = 1


class CliGenerateCoversRequest(BaseModel):
    prompt_916: str | None = None
    prompt_34: str | None = None
    headline: str = ""
    highlight_shot_ids: list[int] | None = None
    count_per_ratio: int = 2
    image_size: str = "2K"
    async_mode: bool = False


class SelectCoverRequest(BaseModel):
    cover_path: str | None = None
    candidate_index: int | None = None


def _collect_timeline(conn: Any, episode_id: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[int]]:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not ep:
        raise HTTPException(status_code=404, detail="分集不存在")
    proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
    shots = rows_to_dicts(
        conn.execute(
            "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC",
            (episode_id,),
        ).fetchall()
    )
    items: list[dict[str, Any]] = []
    missing_shots: list[int] = []
    for s in shots:
        take = conn.execute(
            "SELECT * FROM takes WHERE shot_id = ? AND media_type = 'video' AND is_final = 1 ORDER BY id DESC LIMIT 1",
            (s["id"],),
        ).fetchone()
        if not take or not Path(take["file_path"]).exists():
            missing_shots.append(s["shot_no"])
            continue
        take_d = dict(take)
        eff_dur = resolve_shot_clip_duration(s, take_d)
        items.append(
            {
                "shot_id": s["id"],
                "shot_no": s["shot_no"],
                "shot_title": s["shot_title"],
                "subtitle_text": s["subtitle_text"],
                "cn_dialogue": s["cn_dialogue"],
                "audio_strategy": s["audio_strategy"],
                "upscale_target": s["upscale_target"],
                "upscale_done": s["upscale_done"],
                "trim_start": float(s.get("trim_start") or 0.0),
                "trim_end": float(s.get("trim_end") or 0.0),
                "file_path": take_d["file_path"],
                "width": take_d["width"],
                "height": take_d["height"],
                "resolution_label": take_d["resolution_label"],
                "source_duration": float(take_d["duration_sec"] or s["duration_sec"] or 6.0),
                "effective_duration": eff_dur,
                "vo_file_path": s.get("vo_file_path") or "",
                "vo_source": s.get("vo_source") or "",
                "qc_strip_path": s.get("qc_strip_path") or "",
            }
        )
    return proj, dict(ep), items, missing_shots


@router.get("/episodes/{episode_id}/timeline")
def get_episode_timeline(episode_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        proj, ep, items, missing = _collect_timeline(conn, episode_id)
        jobs = rows_to_dicts(
            conn.execute("SELECT * FROM render_jobs WHERE episode_id = ? ORDER BY id DESC LIMIT 15", (episode_id,)).fetchall()
        )
        unupscaled = [it["shot_no"] for it in items if not it["upscale_done"] and it["resolution_label"] in ("360p", "720p")]
        return {
            "project": proj,
            "episode": ep,
            "items": items,
            "total_duration_sec": round(sum(it["effective_duration"] for it in items), 2),
            "missing_shots": missing,
            "unupscaled_shots": unupscaled,
            "jobs": jobs,
        }


@router.post("/episodes/{episode_id}/export-fcpxml")
def export_episode_fcpxml(episode_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        proj, ep, items, _ = _collect_timeline(conn, episode_id)
    if not items:
        raise HTTPException(status_code=400, detail="请先至少为 1 个分镜认领并设为定稿视频，再导出剪辑工程。")

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    srt_path = export_srt(items, bundle / f"{proj['name']}_E{ep['ep_number']:02d}_{tag}.srt")
    xml_path = export_fcpxml(proj, ep, items, bundle / f"{proj['name']}_E{ep['ep_number']:02d}_{tag}.fcpxml")

    ts = now_str()
    with db_ctx() as conn:
        conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, output_path, log_text, created_at, updated_at)
            VALUES (?, 'fcpxml_export', 'local', 'done', ?, ?, ?, ?)
            """,
            (
                episode_id,
                str(xml_path.resolve()),
                f"已导出 FCPXML 工程与 SRT 字幕（剪映专业版 / Final Cut Pro 点“导入工程”可直读）\nSRT: {srt_path}",
                ts,
                ts,
            ),
        )
    return {
        "fcpxml_path": str(xml_path.resolve()),
        "srt_path": str(srt_path.resolve()),
        "bundle_dir": str(bundle.resolve()),
    }


@router.post("/episodes/{episode_id}/export-ai-proof")
def export_episode_ai_proof(episode_id: int) -> dict[str, Any]:
    """
    一键导出「AI 原创制作证明 (.md)」：
    整理本集剧目信息、剧本、全部关联资产（含定妆图路径与英文锁词）、
    全部分镜中英文 Prompt、模型选型与已落盘素材时间戳，用于各平台 AI 原创声明核验与赛事存证。
    """
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        ep = dict(ep_row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
        assets = rows_to_dicts(
            conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY asset_type, id ASC", (proj["id"],)).fetchall()
        )
        assets_map = {a["id"]: a for a in assets}
        shots = rows_to_dicts(
            conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
                (episode_id,),
            ).fetchall()
        )
        for s in shots:
            try:
                s["ref_asset_ids"] = json.loads(s.get("ref_asset_ids") or "[]")
            except Exception:
                s["ref_asset_ids"] = []
            s["compiled"] = compile_shot_prompts(s, proj, assets_map)
            s["takes"] = rows_to_dicts(
                conn.execute("SELECT * FROM takes WHERE shot_id = ? ORDER BY is_final DESC, id DESC", (s["id"],)).fetchall()
            )

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = proj["name"].replace("/", "_").replace("\\", "_").strip() or proj["slug"]
    proof_path = bundle / f"{safe_name}_E{ep['ep_number']:02d}_AI原创制作证明_{tag}.md"

    brand = _brand_name()
    total_planned_sec = sum(int(s.get("duration_sec") or 6) for s in shots)
    ts = now_str()
    asset_type_cn = {"character": "主角/角色", "landmark": "场景/地标", "prop": "关键道具", "style": "画风参考"}

    lines: list[str] = [
        f"# {brand} · AI 原创短剧制作证明｜{proj['name']} · 第 {ep['ep_number']:02d} 集《{ep['title']}》",
        "",
        f"> 本文件由本地制片台「{brand}」自动归档生成，完整留存本集原创剧本、原创资产圣经、逐镜中英文提示词（Prompt）、AI 生成模型选型及素材落盘记录，可供各内容平台 AI 原创声明核验与赛事参赛存证使用。",
        "",
        "## 一、剧目与分集基本信息",
        "",
        f"- **剧目名称**：{proj['name']}（`{proj['slug']}`）",
        f"- **分集编号与标题**：第 {ep['ep_number']:02d} 集 · {ep['title']}",
        f"- **赛道定位**：{proj.get('track') or '—'}",
        f"- **画面模式与画幅**：{proj.get('visual_mode') or '—'} · `{proj.get('aspect_ratio') or '9:16'}`",
        f"- **分镜总数 / 预计总时长**：共 {len(shots)} 镜 · 预计 {total_planned_sec} 秒（{'已达 ≥60s 短剧时长标准' if total_planned_sec >= 60 else f'距 60s 标准差 {60 - total_planned_sec} 秒'}）",
        f"- **本集当前状态**：`{ep.get('status') or 'draft'}`",
        f"- **本集创建时间 / 最近更新**：{ep.get('created_at') or '—'} / {ep.get('updated_at') or '—'}",
        f"- **证明导出时间**：{ts}",
        "",
        "### 全局画风英文锁词（Global Style Prompt）",
        "```text",
        (proj.get("global_style_prompt") or "（未填写）").strip(),
        "```",
        "",
        "## 二、本集原创剧本与结构设计",
        "",
        f"- **0–3 秒视觉钩子**：{ep.get('hook_0_3s') or '—'}",
        f"- **结尾评论区互动诱饵**：{ep.get('ending_cta') or '—'}",
        f"- **下集悬念钩子**：{ep.get('next_ep_hook') or '—'}",
        f"- **原创配乐 Prompt**：`{ep.get('bgm_prompt') or '—'}`",
        f"- **本地配乐文件**：`{ep.get('bgm_file_path') or '未上传'}`",
        "",
        "### 完整拍摄剧本正文",
        "",
        (ep.get("script_text") or "（暂无剧本正文）").strip(),
        "",
        f"## 三、跨集原创资产圣经（共 {len(assets)} 项）",
        "",
    ]

    if not assets:
        lines.append("（本剧目暂未登记独立资产）")
        lines.append("")
    else:
        for idx, a in enumerate(assets, start=1):
            atype = asset_type_cn.get(a.get("asset_type") or "", a.get("asset_type") or "其他")
            lines.extend(
                [
                    f"### {idx}. {a['name']}（{atype} · `{a.get('flow_tag') or ''}`）",
                    f"- **英文不变锁词**：`{(a.get('en_anchor_prompt') or '—').strip()}`",
                    f"- **原始参考图路径**：`{a.get('raw_image_path') or '未上传'}`",
                    f"- **清洗定妆图路径**：`{a.get('clean_image_path') or '未上传'}`",
                ]
            )
            if (a.get("notes") or "").strip():
                lines.append(f"- **制作与棚拍备注**：{a['notes'].strip()}")
            lines.append("")

    lines.extend([f"## 四、逐镜分镜与 AI 提示词完整档案（共 {len(shots)} 镜）", ""])
    if not shots:
        lines.append("（本集暂未拆解分镜）")
        lines.append("")
    else:
        for s in shots:
            fmode_key = s.get("flow_mode") or "omni12_i2v"
            fmode_label = FLOW_MODES.get(fmode_key, {}).get("label", fmode_key)
            astrat_key = s.get("audio_strategy") or "keep_flow_sfx"
            astrat_label = AUDIO_STRATEGIES.get(astrat_key, astrat_key)
            ref_tags = [
                f"{assets_map[aid].get('name')} ({assets_map[aid].get('flow_tag') or ''})"
                for aid in s.get("ref_asset_ids", [])
                if aid in assets_map
            ]
            compiled = s.get("compiled") or {}

            lines.extend(
                [
                    f"### S{int(s['shot_no']):02d} · {s.get('shot_title') or '未命名镜头'}（{s.get('camera_type') or '中景'} · {s.get('duration_sec') or 6}s）",
                    f"- **AI 模型选型**：{fmode_label}（`{fmode_key}`）",
                    f"- **音频策略**：{astrat_label}（`{astrat_key}`）",
                    f"- **绑定资产**：{', '.join(ref_tags) if ref_tags else '无'}",
                    f"- **中文画面动作**：{(s.get('cn_action_desc') or '—').strip()}",
                    f"- **中文台词 / 旁白**：{(s.get('cn_dialogue') or '—').strip()}",
                    f"- **屏幕字幕**：{(s.get('subtitle_text') or '—').strip()}",
                    f"- **分镜记录时间**：创建 `{s.get('created_at') or '—'}` · 更新 `{s.get('updated_at') or '—'}`",
                    f"- **首帧图文件**：`{s.get('start_frame_path') or '未上传'}`",
                ]
            )
            if (s.get("end_frame_path") or "").strip() or (s.get("end_frame_prompt_en") or "").strip():
                lines.append(f"- **尾帧图文件**：`{s.get('end_frame_path') or '未上传'}`")

            lines.extend(
                [
                    "- **首帧英文 Prompt（原始 / 编译后）**：",
                    "```text",
                    f"[RAW] {(s.get('frame_prompt_en') or '（空）').strip()}",
                    f"[COMPILED] {(compiled.get('compiled_frame_prompt') or '').strip()}",
                    "```",
                ]
            )

            if (s.get("end_frame_prompt_en") or "").strip():
                lines.extend(
                    [
                        "- **尾帧英文 Prompt（原始 / 编译后）**：",
                        "```text",
                        f"[RAW] {(s.get('end_frame_prompt_en') or '').strip()}",
                        f"[COMPILED] {(compiled.get('compiled_end_frame_prompt') or '').strip()}",
                        "```",
                    ]
                )

            lines.extend(
                [
                    "- **动态视频英文 Prompt（原始 / 编译后）**：",
                    "```text",
                    f"[RAW] {(s.get('video_prompt_en') or '（空）').strip()}",
                    f"[COMPILED] {(compiled.get('compiled_video_prompt') or '').strip()}",
                    "```",
                ]
            )

            takes = s.get("takes") or []
            if takes:
                lines.append(f"- **已入库生成素材（共 {len(takes)} 版）**：")
                for t in takes:
                    final_mark = "【定稿】" if t.get("is_final") else ""
                    lines.append(
                        f"  - {final_mark}`{Path(t['file_path']).name}`（原名：`{t.get('original_filename') or '—'}` · {t.get('resolution_label') or '未知'} · {t.get('duration_sec') or 0}s · 入库时间 `{t.get('created_at') or '—'}`）"
                    )
            else:
                lines.append("- **已入库生成素材**：暂未认领视频 Take")
            lines.append("")

    lines.extend(
        [
            "---",
            "",
            f"*归档落款：{brand}（Antigravity Video Studio） · 导出时间：{ts}*",
        ]
    )

    proof_path.write_text("\n".join(lines), encoding="utf-8")
    proof_abs = str(proof_path.resolve())

    with db_ctx() as conn:
        conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, output_path, log_text, created_at, updated_at)
            VALUES (?, 'ai_proof', 'local', 'done', ?, ?, ?, ?)
            """,
            (
                episode_id,
                proof_abs,
                f"已导出 AI 原创制作证明（共 {len(assets)} 项资产、{len(shots)} 个分镜 Prompt 档案，可用于平台原创合规存证）",
                ts,
                ts,
            ),
        )
        conn.execute(
            "UPDATE episodes SET ai_proof_path = ?, updated_at = ? WHERE id = ?",
            (proof_abs, ts, episode_id),
        )
    return {
        "proof_path": proof_abs,
        "ai_proof_path": proof_abs,
        "bundle_dir": str(bundle.resolve()),
        "shot_count": len(shots),
        "asset_count": len(assets),
    }


@router.post("/episodes/{episode_id}/export-colab-pack")
def export_episode_colab_pack(episode_id: int) -> dict[str, Any]:
    """
    生成 Google Colab 云端后期任务清单包：
    - 适用于全片 4K 超分 + 60fps 补帧（替代高积分 4K 直出）、固定角色专属配音克隆及训练交接；
    - 仅生成轻量 manifest JSON + SRT + rclone 上行命令，不自动耗费本机上行带宽。
    """
    with db_ctx() as conn:
        proj, ep, items, missing = _collect_timeline(conn, episode_id)
    if not items:
        raise HTTPException(status_code=400, detail="当前没有已定稿的分镜视频，请先在分镜台认领视频并设为定稿。")

    cfg = load_config()
    gdrive_root = (cfg.get("COLAB_GDRIVE_ROOT") or "AntigravityVideoStudio").strip().strip("/") or "AntigravityVideoStudio"

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_name = f"{datetime.now().strftime('%Y%m%d')}_{proj['slug']}_E{ep['ep_number']:02d}"
    srt_path = export_srt(items, bundle / f"{job_name}.srt")

    remote_job_dir = f"gdrive:{gdrive_root}/Colab_Jobs/{job_name}"
    remote_out_dir = f"gdrive:{gdrive_root}/Colab_Outputs/{job_name}"
    manifest = {
        "job_name": job_name,
        "created_at": now_str(),
        "project": {"id": proj["id"], "name": proj["name"], "slug": proj["slug"], "aspect_ratio": proj["aspect_ratio"]},
        "episode": {"id": ep["id"], "ep_number": ep["ep_number"], "title": ep["title"]},
        "recommended_colab_pipeline": {
            "gpu_tier": "Colab L4（默认省 CU 抗排队）/ A100",
            "upscale_4k": "Real-ESRGAN v3 via spandrel（替代 Flow 50分/镜 4K，全片 GPU AI 超分）",
            "frame_interp_60fps": "RIFE v4.26（带切镜防鬼影 60fps 电影丝滑补帧）",
            "voice_clone": f"CosyVoice / F5-TTS（可选固定角色声线，权重放 {gdrive_root}/Models/）",
            "models_rule": "拉取公开模型权重建议在 Colab 运行时直拉存 Google Drive，避免占用本地宽带上行",
        },
        "gdrive_paths": {
            "job_input": remote_job_dir,
            "job_output": remote_out_dir,
            "models_root": f"gdrive:{gdrive_root}/Models/",
        },
        "bgm_file_path": ep.get("bgm_file_path") or "",
        "srt_path": str(srt_path.resolve()),
        "missing_shots": missing,
        "shots": items,
    }
    manifest_path = bundle / f"{job_name}_colab_manifest_{tag}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    rclone_cmd = f"rclone copy '{bundle.resolve()}' '{remote_job_dir}' --progress"
    ts = now_str()
    with db_ctx() as conn:
        conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, output_path, log_text, created_at, updated_at)
            VALUES (?, 'colab_pack', 'colab', 'done', ?, ?, ?, ?)
            """,
            (
                episode_id,
                str(manifest_path.resolve()),
                f"已生成 Google Colab 任务清单（4K超分/60fps补帧/可选配音）\n云端落点: {remote_job_dir}\n上传命令: {rclone_cmd}",
                ts,
                ts,
            ),
        )
    return {
        "manifest_path": str(manifest_path.resolve()),
        "srt_path": str(srt_path.resolve()),
        "remote_job_dir": remote_job_dir,
        "remote_out_dir": remote_out_dir,
        "rclone_cmd": rclone_cmd,
    }


@router.post("/episodes/{episode_id}/dispatch-colab")
def dispatch_episode_colab(
    episode_id: int,
    body: DispatchColabRequest | None = None,
    async_mode: bool = False,
) -> dict[str, Any]:
    """
    通过 Google Colab 官方 API & 官方 CLI（google-colab-cli）直接派发云端 GPU 任务：
    - upscale_4k_30fps（默认）：上传真 1080p 三轨音频基准片，云端 GPU 跑 Real-ESRGAN v3 4K 超分 + 10-bit HLG HDR + 黄金安全区字幕（保源帧率电影感）；
    - upscale_4k_60fps：云端 GPU 跑 RIFE v4.26 60fps + Real-ESRGAN v3 4K 超分 + HLG HDR；
    - ai_60fps_1080p：云端 GPU 仅跑真 AI RIFE v4.26 60fps 极速补帧（保持 1080p，省 CU 极速交付）；
    - probe_gpu：租用指定 GPU（默认 L4 防排队）跑 15 秒冒烟实测并自动销毁 VM；
    - 支持 async_mode=True 走轻量异步任务队列，实时汇报进度与支持取消。
    """
    req = body or DispatchColabRequest()
    valid_modes = ("upscale_4k_30fps", "upscale_4k_60fps", "ai_60fps_1080p", "probe_gpu")
    if req.task_mode not in valid_modes:
        raise HTTPException(status_code=400, detail=f"不支持的 task_mode: {req.task_mode}，可选：{', '.join(valid_modes)}")
    vo_sync_mode = _normalize_vo_sync_mode(req.vo_sync_mode)

    with db_ctx() as conn:
        proj, ep, items, _ = _collect_timeline(conn, episode_id)
    if req.task_mode in ("upscale_4k_30fps", "upscale_4k_60fps", "ai_60fps_1080p") and not items:
        raise HTTPException(status_code=400, detail="当前没有已定稿的分镜视频，无法派发至 Colab 云端增强。")

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    job_type_map = {
        "probe_gpu": "colab_gpu_probe",
        "upscale_4k_30fps": "colab_ai_4k30",
        "upscale_4k_60fps": "colab_ai_4k60",
        "ai_60fps_1080p": "colab_ai_1080p60",
    }
    job_type = job_type_map.get(req.task_mode, "colab_ai_4k30")

    def _run_colab(ctx: JobContext | None = None, sync_job_id: int | None = None) -> dict[str, Any]:
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(
                progress_pct=5,
                stage_msg=f"正在准备 Colab {req.gpu_tier} 云端任务（{req.task_mode}）...",
            )
        srt_kwargs = _filter_supported_kwargs(export_srt, {"vo_sync_mode": vo_sync_mode})
        srt_path = export_srt(items, bundle / f"E{ep['ep_number']:02d}_colab.srt", **srt_kwargs) if items else bundle / "empty.srt"
        colab_extra: dict[str, Any] = {"vo_sync_mode": vo_sync_mode}
        if ctx is not None:
            colab_extra["progress_cb"] = _make_job_progress_cb(ctx)
            colab_extra["cancel_check"] = ctx.is_cancelled
        res = dispatch_episode_to_colab(
            proj,
            ep,
            items,
            srt_path,
            gpu_tier=req.gpu_tier,
            task_mode=req.task_mode,
            burn_subtitles=req.burn_subtitles,
            hdr_hlg=req.hdr_hlg,
            **_filter_supported_kwargs(dispatch_episode_to_colab, colab_extra),
        )
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(
                progress_pct=96,
                stage_msg="Colab 云端任务完成，正在归档母带记录...",
                log_line=res.get("log") or None,
            )

        ts2 = now_str()
        with db_ctx() as conn:
            if ctx is None and sync_job_id is not None:
                conn.execute(
                    "UPDATE render_jobs SET target_host = ?, status = 'done', output_path = ?, log_text = ?, updated_at = ? WHERE id = ?",
                    (f"colab ({res['gpu_tier']})", res.get("output_path", ""), res["log"], ts2, sync_job_id),
                )
            if res.get("output_path"):
                conn.execute(
                    "UPDATE episodes SET latest_output_path = ?, status = 'mastered', updated_at = ? WHERE id = ?",
                    (res["output_path"], ts2, episode_id),
                )
        return res

    use_async = bool(req.async_mode or async_mode)
    if use_async:
        job = submit_job(
            job_type,
            lambda ctx: _run_colab(ctx, None),
            episode_id=episode_id,
            target_host=f"colab:{req.gpu_tier}",
            stage_msg=f"已排队：等待派发 Colab {req.gpu_tier}（{req.task_mode}）",
        )
        return {"ok": True, "async": True, "job_id": job["id"], "job": job}

    ts = now_str()
    with db_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, log_text, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                episode_id,
                job_type,
                f"colab:{req.gpu_tier}",
                f"正在通过 Google Colab 官方通道派发 {req.task_mode}（GPU: {req.gpu_tier} · HLG HDR: {req.hdr_hlg} · 字幕: {req.burn_subtitles}）...",
                ts,
                ts,
            ),
        )
        job_db_id = cur.lastrowid

    try:
        return _run_colab(None, job_db_id)
    except Exception as exc:
        with db_ctx() as conn:
            conn.execute(
                "UPDATE render_jobs SET status = 'failed', log_text = ?, updated_at = ? WHERE id = ?",
                (str(exc), now_str(), job_db_id),
            )
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/episodes/{episode_id}/render-local")
def render_episode_local_preview(
    episode_id: int,
    body: RenderLocalRequest | None = None,
    async_mode: bool = False,
) -> dict[str, Any]:
    """
    本机自动探测硬编/软编合成真 1080p 三轨混音成片（原生环境音 SFX + 旁白 VO + 自动闪避 BGM）：
    - 保持无 Body 调用 100% 向后兼容；
    - 支持可选开启 10-bit BT.2020 HLG HDR（hdr_hlg=True）、底部黄金安全区字幕（burn_subtitles=True）与 VO 超长对齐策略（vo_sync_mode: pad | speed）；
    - 支持 async_mode=True 走轻量异步任务中心。
    """
    req = body or RenderLocalRequest()
    vo_sync_mode = _normalize_vo_sync_mode(req.vo_sync_mode)
    with db_ctx() as conn:
        proj, ep, items, _ = _collect_timeline(conn, episode_id)
    if not items:
        raise HTTPException(status_code=400, detail="当前没有已定稿的分镜视频，请先在分镜台认领视频并设为定稿。")

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix_tag = "Local1080p_HLG" if req.hdr_hlg else "Local1080p"
    out_mp4 = bundle / f"{proj['name']}_E{ep['ep_number']:02d}_{suffix_tag}_{tag}.mp4"
    bgm_vol = (
        float(req.bgm_volume)
        if req.bgm_volume is not None
        else float(ep.get("bgm_volume") if ep.get("bgm_volume") is not None else 0.25)
    )
    bgm_vol = max(0.0, min(1.5, bgm_vol))

    def _run_render_local(ctx: JobContext | None = None) -> dict[str, Any]:
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(progress_pct=12, stage_msg="正在导出时间线同步字幕...")
        srt_kwargs = _filter_supported_kwargs(export_srt, {"vo_sync_mode": vo_sync_mode})
        srt_path = export_srt(items, bundle / f"{proj['name']}_E{ep['ep_number']:02d}_字幕_{tag}.srt", **srt_kwargs)
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(progress_pct=25, stage_msg=f"正在本机渲染 1080p 三轨混音成片（VO 对齐：{vo_sync_mode}）...")

        render_extra: dict[str, Any] = {}
        if ctx is not None:
            render_extra["progress_cb"] = _make_job_progress_cb(ctx)
            render_extra["cancel_check"] = ctx.is_cancelled
        res = build_master_base_mp4(
            items,
            out_mp4,
            bgm_file_path=ep.get("bgm_file_path") or "",
            bgm_volume=bgm_vol,
            hdr_hlg=req.hdr_hlg,
            burn_srt_path=(srt_path if req.burn_subtitles else None),
            vo_sync_mode=vo_sync_mode,
            **_filter_supported_kwargs(build_master_base_mp4, render_extra),
        )
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(
                progress_pct=94,
                stage_msg="成片渲染完成，正在更新分集记录...",
                log_line=f"{res.get('log', '')}\nSRT: {srt_path}",
            )

        ts = now_str()
        with db_ctx() as conn:
            if ctx is None:
                conn.execute(
                    """
                    INSERT INTO render_jobs (episode_id, job_type, target_host, status, output_path, log_text, created_at, updated_at)
                    VALUES (?, 'local_preview', 'local', 'done', ?, ?, ?, ?)
                    """,
                    (episode_id, res["output_path"], f"{res['log']}\nSRT: {srt_path}", ts, ts),
                )
            conn.execute(
                "UPDATE episodes SET latest_output_path = ?, updated_at = ? WHERE id = ?",
                (res["output_path"], ts, episode_id),
            )
        payload: dict[str, Any] = {
            "output_path": res["output_path"],
            "srt_path": str(srt_path.resolve()),
            "mode": res.get("mode", "master_1080p"),
            "encoder": res.get("encoder", ""),
            "width": res.get("width", 0),
            "height": res.get("height", 0),
            "fps": res.get("fps", 30),
            "hdr_hlg": bool(res.get("hdr_hlg", req.hdr_hlg)),
            "burn_subtitles": bool(res.get("burned_subtitles", req.burn_subtitles)),
            "vo_sync_mode": res.get("vo_sync_mode", vo_sync_mode),
            "bgm_volume": bgm_vol,
            "has_bgm": bool(res.get("has_bgm", False)),
            "vo_count": int(res.get("vo_count", 0)),
            "kept_sfx_shots": int(res.get("kept_sfx_shots", 0)),
            "total_duration_sec": float(res.get("total_duration_sec", 0.0)),
            "log": res.get("log", ""),
        }
        if "av_diff_sec" in res:
            payload["av_diff_sec"] = res["av_diff_sec"]
        return payload

    use_async = bool(req.async_mode or async_mode)
    if use_async:
        job = submit_job(
            "local_preview",
            _run_render_local,
            episode_id=episode_id,
            target_host="local",
            stage_msg=f"已排队：等待本机 1080p 三轨混音渲染（VO 对齐：{vo_sync_mode}）",
        )
        return {"ok": True, "async": True, "job_id": job["id"], "job": job}

    try:
        return _run_render_local(None)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/episodes/{episode_id}/cleanup-drafts")
def cleanup_episode_drafts(episode_id: int) -> dict[str, Any]:
    """一键清理本集未设为定稿（is_final=0）的抽卡废片，保护定稿、参考图和终版成片"""
    removed_files = 0
    freed_bytes = 0
    with db_ctx() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT t.id, t.file_path
                FROM takes t JOIN shots s ON t.shot_id = s.id
                WHERE s.episode_id = ? AND t.is_final = 0
                """,
                (episode_id,),
            ).fetchall()
        )
        for r in rows:
            p = Path(r["file_path"])
            if p.exists() and p.is_file():
                try:
                    freed_bytes += p.stat().st_size
                    p.unlink()
                    removed_files += 1
                except OSError:
                    pass
            conn.execute("DELETE FROM takes WHERE id = ?", (r["id"],))
    return {
        "removed_files": removed_files,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    }


# ==================== TTS 配音、真人干声上传、视频音画体检与自动双比例封面 ====================


def _parse_bool_flag(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")


@router.post("/episodes/{episode_id}/cli-generate-tts")
def cli_generate_episode_tts(
    episode_id: int,
    body: CliGenerateTtsRequest | None = None,
    async_mode: bool = False,
) -> dict[str, Any]:
    """
    批量并发（受控线程池 max_workers=3）调用媒体引擎为本集所有含台词/字幕的镜头生成旁白 WAV，
    写入 shots.vo_file_path / shots.vo_source='tts' 与 episodes.vo_mode='tts'；
    支持 async_mode=True 走轻量异步任务中心并实时汇报完成镜数百分比。
    """
    req = body or CliGenerateTtsRequest()
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        ep = dict(ep_row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
        shots = rows_to_dicts(
            conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
                (episode_id,),
            ).fetchall()
        )

    shot_id_filter = set(req.shot_ids) if req.shot_ids else None
    targets: list[tuple[dict[str, Any], str]] = []
    skipped_shots: list[int] = []
    for s in shots:
        if shot_id_filter is not None and s["id"] not in shot_id_filter:
            continue
        text = (s.get("cn_dialogue") or s.get("subtitle_text") or "").strip()
        if not text:
            skipped_shots.append(int(s["shot_no"]))
            continue
        targets.append((s, text))

    if not targets:
        raise HTTPException(status_code=400, detail="本集所选分镜均无台词或字幕文本，无需生成 TTS 旁白。")

    voice = (req.voice or "Kore").strip() or "Kore"
    style_prompt = (req.style_prompt or "").strip()

    def _run_tts(ctx: JobContext | None = None) -> dict[str, Any]:
        total = len(targets)
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(progress_pct=5, stage_msg=f"正在并发生成 {total} 镜中文旁白 TTS（音色：{voice}）...")

        generated_indexed: list[tuple[int, dict[str, Any]]] = []
        errors: list[dict[str, Any]] = []
        total_cost = 0.0
        completed = 0

        def _gen_one(order_idx: int, shot_item: dict[str, Any], tts_text: str) -> tuple[int, dict[str, Any], str, dict[str, Any]]:
            if ctx is not None and ctx.is_cancelled():
                raise JobCancelledError("批量 TTS 任务已取消")
            sno = int(shot_item["shot_no"])
            out_dir = make_shot_media_dir(proj["slug"], ep["ep_number"], sno)
            stem = f"E{ep['ep_number']:02d}_S{sno:02d}_tts"
            extra_cb: dict[str, Any] = {}
            if ctx is not None:
                extra_cb["cancel_check"] = ctx.is_cancelled
            res = cli_generate_tts(
                text=tts_text,
                out_dir=out_dir,
                voice=voice,
                style_prompt=style_prompt,
                stem=stem,
                **_filter_supported_kwargs(cli_generate_tts, extra_cb),
            )
            return order_idx, shot_item, tts_text, res

        max_workers = min(3, total)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            fut_map = {
                executor.submit(_gen_one, idx, s, text): (idx, s)
                for idx, (s, text) in enumerate(targets)
            }
            for fut in as_completed(fut_map):
                order_idx, s = fut_map[fut]
                sno = int(s["shot_no"])
                completed += 1
                try:
                    _, shot_item, tts_text, res = fut.result()
                    ts = now_str()
                    with db_ctx() as conn:
                        conn.execute(
                            "UPDATE shots SET vo_file_path = ?, vo_source = 'tts', updated_at = ? WHERE id = ?",
                            (res["file_path"], ts, shot_item["id"]),
                        )
                    cost = float(res.get("est_cost_usd") or 0.0)
                    total_cost += cost
                    generated_indexed.append(
                        (
                            order_idx,
                            {
                                "shot_id": shot_item["id"],
                                "shot_no": sno,
                                "shot_title": shot_item.get("shot_title") or "",
                                "text": tts_text,
                                "file_path": res["file_path"],
                                "filename": res.get("filename") or Path(res["file_path"]).name,
                                "duration_sec": float(res.get("duration_sec") or 0.0),
                                "est_cost_usd": cost,
                                "model": res.get("model") or "",
                                "voice": res.get("voice") or voice,
                            },
                        )
                    )
                    if ctx is not None:
                        pct = min(95, 5 + int(90 * completed / total))
                        ctx.update(
                            progress_pct=pct,
                            stage_msg=f"旁白 TTS 生成中：{completed}/{total} 镜（已完成 S{sno:02d}）",
                            log_line=f"S{sno:02d} 旁白生成完成：{Path(res['file_path']).name}",
                        )
                except JobCancelledError:
                    for pending in fut_map:
                        pending.cancel()
                    raise
                except Exception as exc:
                    errors.append({"shot_id": s["id"], "shot_no": sno, "error": str(exc)})
                    if ctx is not None:
                        pct = min(95, 5 + int(90 * completed / total))
                        ctx.update(
                            progress_pct=pct,
                            stage_msg=f"旁白 TTS 生成中：{completed}/{total} 镜（S{sno:02d} 失败）",
                            log_line=f"S{sno:02d} 旁白生成失败：{exc}",
                        )

                if ctx is not None and ctx.is_cancelled():
                    for pending in fut_map:
                        pending.cancel()
                    raise JobCancelledError("批量 TTS 任务已取消")

        generated_indexed.sort(key=lambda pair: pair[0])
        generated = [item for _, item in generated_indexed]
        if not generated:
            raise RuntimeError(f"批量生成 TTS 旁白全部失败：{errors[0]['error'] if errors else '未知错误'}")

        with db_ctx() as conn:
            conn.execute("UPDATE episodes SET vo_mode = 'tts', updated_at = ? WHERE id = ?", (now_str(), episode_id))

        total_cost_rounded = round(total_cost, 4)
        return {
            "episode_id": episode_id,
            "vo_mode": "tts",
            "voice": voice,
            "generated_count": len(generated),
            "failed_count": len(errors),
            "skipped_shots": skipped_shots,
            "total_cost_usd": total_cost_rounded,
            "est_cost_usd": total_cost_rounded,
            "shots": generated,
            "errors": errors,
        }

    use_async = bool(req.async_mode or async_mode)
    if use_async:
        job = submit_job(
            "cli_tts",
            _run_tts,
            episode_id=episode_id,
            target_host="local",
            stage_msg=f"已排队：等待并发生成 {len(targets)} 镜中文旁白 TTS",
        )
        return {"ok": True, "async": True, "job_id": job["id"], "job": job}

    try:
        return _run_tts(None)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/shots/{shot_id}/upload-vo")
async def upload_shot_vo(
    shot_id: int,
    request: Request,
    clear: bool = False,
    vo_source: str = "human",
) -> dict[str, Any]:
    """
    上传本镜本地真人/克隆干声 WAV/MP3（multipart/form-data 字段 `file`），
    或传 `clear=true`（支持 Form、Query 或 JSON `{"clear": true}`）切为纯字幕无旁白，
    同步更新 shots.vo_file_path、shots.vo_source 与 episodes.vo_mode。
    """
    with db_ctx() as conn:
        shot_row = conn.execute(
            """
            SELECT s.*, e.ep_number, e.project_id, p.slug
            FROM shots s
            JOIN episodes e ON s.episode_id = e.id
            JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not shot_row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        shot = dict(shot_row)

    is_clear = bool(clear)
    src_label = (vo_source or "human").strip().lower()
    uploaded_file: Any = None
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            if "clear" in payload:
                is_clear = _parse_bool_flag(payload.get("clear"))
            if payload.get("vo_source"):
                src_label = str(payload.get("vo_source")).strip().lower()
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        if "clear" in form:
            is_clear = _parse_bool_flag(form.get("clear"))
        if form.get("vo_source"):
            src_label = str(form.get("vo_source")).strip().lower()
        cand_file = form.get("file")
        if cand_file is not None and hasattr(cand_file, "read"):
            uploaded_file = cand_file

    ts = now_str()
    if is_clear:
        with db_ctx() as conn:
            conn.execute(
                "UPDATE shots SET vo_file_path = '', vo_source = 'none', updated_at = ? WHERE id = ?",
                (ts, shot_id),
            )
            rem_vo = conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id = ? AND COALESCE(vo_file_path, '') != ''",
                (shot["episode_id"],),
            ).fetchone()["c"]
            if rem_vo == 0:
                conn.execute(
                    "UPDATE episodes SET vo_mode = 'none', updated_at = ? WHERE id = ?",
                    (ts, shot["episode_id"]),
                )
            updated = dict(conn.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone())
        return {
            "shot": updated,
            "vo_file_path": "",
            "vo_source": "none",
            "duration_sec": 0.0,
            "cleared": True,
        }

    if uploaded_file is None:
        raise HTTPException(status_code=400, detail="请上传音频文件（字段名 file，支持 WAV/MP3/M4A），或传 clear=true 切为纯字幕无旁白。")

    if src_label not in ("human", "tts", "none"):
        src_label = "human"
    orig_name = getattr(uploaded_file, "filename", None) or "vo.wav"
    ext = Path(orig_name).suffix.lower()
    if ext not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        ext = ".wav"

    dest_dir = make_shot_media_dir(shot["slug"], shot["ep_number"], shot["shot_no"])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"E{shot['ep_number']:02d}_S{shot['shot_no']:02d}_vo_{src_label}_{tag}{ext}"
    data_bytes = await uploaded_file.read()
    if not data_bytes:
        raise HTTPException(status_code=400, detail="上传的配音音频文件为空。")
    dest.write_bytes(data_bytes)
    meta = probe_media(dest)
    dest_abs = str(dest.resolve())

    with db_ctx() as conn:
        conn.execute(
            "UPDATE shots SET vo_file_path = ?, vo_source = ?, updated_at = ? WHERE id = ?",
            (dest_abs, src_label, ts, shot_id),
        )
        conn.execute(
            "UPDATE episodes SET vo_mode = ?, updated_at = ? WHERE id = ?",
            (src_label, ts, shot["episode_id"]),
        )
        updated = dict(conn.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone())

    return {
        "shot": updated,
        "file_path": dest_abs,
        "vo_file_path": dest_abs,
        "filename": dest.name,
        "vo_source": src_label,
        "duration_sec": float(meta.get("duration_sec") or 0.0),
        "cleared": False,
    }


@router.post("/episodes/{episode_id}/qc-inspect-videos")
def qc_inspect_episode_videos(
    episode_id: int,
    body: QcInspectVideosRequest | None = None,
) -> dict[str, Any]:
    """
    对本集所有镜头的最新（或指定 resolution_label 如 360p / 1080p）视频执行 3 帧画面抽图 + 音轨健康体检，
    写入 shots.qc_strip_path / shots.qc_report_json，并自动拼一张全集体检总览图返回结构化汇总。
    """
    req = body or QcInspectVideosRequest()
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        ep = dict(ep_row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
        shots = rows_to_dicts(
            conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
                (episode_id,),
            ).fetchall()
        )
        shot_id_filter = set(req.shot_ids) if req.shot_ids else None
        res_filter = (req.resolution_label or "").strip()

        shot_takes: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for s in shots:
            if shot_id_filter is not None and s["id"] not in shot_id_filter:
                continue
            if res_filter:
                trow = conn.execute(
                    """
                    SELECT * FROM takes
                    WHERE shot_id = ? AND media_type = 'video' AND LOWER(resolution_label) = LOWER(?)
                    ORDER BY is_final DESC, id DESC LIMIT 1
                    """,
                    (s["id"], res_filter),
                ).fetchone()
            else:
                trow = conn.execute(
                    """
                    SELECT * FROM takes
                    WHERE shot_id = ? AND media_type = 'video'
                    ORDER BY is_final DESC, id DESC LIMIT 1
                    """,
                    (s["id"],),
                ).fetchone()
            shot_takes.append((s, dict(trow) if trow else None))

    if not shot_takes:
        raise HTTPException(status_code=400, detail="当前分集没有可体检的分镜。")

    inspected: list[dict[str, Any]] = []
    missing_shots: list[int] = []
    strip_entries: list[dict[str, Any]] = []
    warning_count = 0

    for s, take in shot_takes:
        sno = int(s["shot_no"])
        if not take or not Path(take["file_path"]).exists():
            missing_shots.append(sno)
            continue

        video_path = Path(take["file_path"])
        out_dir = make_shot_media_dir(proj["slug"], ep["ep_number"], sno)
        res_tag = (take.get("resolution_label") or "vid").replace("/", "_")
        out_strip = out_dir / f"E{ep['ep_number']:02d}_S{sno:02d}_qc_{res_tag}.jpg"
        expect_dialogue = (s.get("audio_strategy") or "") == "keep_flow_dialogue"

        try:
            report = inspect_video_shot(video_path, out_strip, expect_dialogue=expect_dialogue)
        except Exception as exc:
            report = {
                "status": "warn",
                "warnings": [f"体检执行失败：{exc}"],
                "summary": f"体检执行失败：{exc}",
                "error": str(exc),
                "strip_path": "",
            }

        strip_path_str = str(Path(report.get("strip_path") or report.get("output_path") or out_strip).resolve()) if out_strip.exists() else ""
        warnings = report.get("warnings") or report.get("alerts") or []
        has_warning = bool(warnings) or (report.get("status") == "warn")
        if has_warning:
            warning_count += 1

        ts = now_str()
        with db_ctx() as conn:
            conn.execute(
                "UPDATE shots SET qc_strip_path = ?, qc_report_json = ?, updated_at = ? WHERE id = ?",
                (strip_path_str, json.dumps(report, ensure_ascii=False), ts, s["id"]),
            )

        if strip_path_str and Path(strip_path_str).exists():
            res_lbl = take.get("resolution_label") or ""
            label_parts = [f"#{len(strip_entries) + 1}", f"S{sno:02d}"]
            if (s.get("shot_title") or "").strip():
                label_parts.append(s["shot_title"].strip()[:12])
            if res_lbl:
                label_parts.append(f"({res_lbl})")
            strip_entries.append(
                {
                    "label": " ".join(label_parts),
                    "path": strip_path_str,
                    "badge": "异常需复听" if has_warning else "正常",
                    "caption": str(report.get("summary") or "").strip(),
                }
            )

        inspected.append(
            {
                "shot_id": s["id"],
                "shot_no": sno,
                "shot_title": s.get("shot_title") or "",
                "take_id": take["id"],
                "resolution_label": take.get("resolution_label") or "",
                "video_path": str(video_path.resolve()),
                "qc_strip_path": strip_path_str,
                "expect_dialogue": expect_dialogue,
                "has_warning": has_warning,
                "warnings": warnings,
                "report": report,
            }
        )

    if not inspected:
        raise HTTPException(
            status_code=400,
            detail=f"未找到可体检的视频文件{f'（筛选清晰度：{res_filter}）' if res_filter else ''}，请先生成或认领分镜视频。",
        )

    overview_sheet_path = ""
    if strip_entries:
        bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
        qc_dir = bundle / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        overview_jpg = qc_dir / f"E{ep['ep_number']:02d}_qc_overview_{tag}.jpg"
        title = f"{proj['name']} · 第{ep['ep_number']:02d}集 音画体检总览（{len(inspected)}镜 · 异常{warning_count}镜）"
        try:
            sheet_res = build_contact_sheet(strip_entries, overview_jpg, title=title, cols=max(1, min(2, int(req.cols or 1))))
            overview_sheet_path = str(Path(sheet_res.get("output_path") or overview_jpg).resolve())
        except Exception:
            overview_sheet_path = ""

    if overview_sheet_path:
        with db_ctx() as conn:
            conn.execute(
                "UPDATE episodes SET qc_overview_path = ?, updated_at = ? WHERE id = ?",
                (overview_sheet_path, now_str(), episode_id),
            )

    return {
        "episode_id": episode_id,
        "resolution_filter": res_filter or "latest",
        "inspected_count": len(inspected),
        "passed_count": len(inspected) - warning_count,
        "warning_count": warning_count,
        "missing_shots": missing_shots,
        "overview_sheet_path": overview_sheet_path,
        "qc_overview_path": overview_sheet_path,
        "shots": inspected,
    }


def _collect_cover_refs_and_prompts(
    proj: dict[str, Any],
    ep: dict[str, Any],
    shots: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    req: CliGenerateCoversRequest,
) -> tuple[list[Path], str, str]:
    """挑选本集高光首帧 + 核心绑定资产作为参考图，并拼接带防穿帮护盾的无字双比例封面提示词"""
    ref_paths: list[Path] = []
    seen_paths: set[str] = set()

    def _add_ref(path_str: str) -> None:
        if len(ref_paths) >= 6:
            return
        s = (path_str or "").strip()
        if not s:
            return
        p = Path(s).expanduser().resolve()
        key = str(p)
        if key not in seen_paths and p.exists() and p.is_file():
            seen_paths.add(key)
            ref_paths.append(p)

    # 1. 优先选用户指定的高光镜，否则挑开场镜 + 中段高光镜 + 收尾镜
    if req.highlight_shot_ids:
        id_set = set(req.highlight_shot_ids)
        chosen_shots = [s for s in shots if s["id"] in id_set]
    else:
        valid_shots = [s for s in shots if (s.get("start_frame_path") or "").strip() and Path(s["start_frame_path"]).exists()]
        chosen_shots = []
        if valid_shots:
            chosen_shots.append(valid_shots[0])
            mid_hero = next((s for s in valid_shots[1:-1] if s.get("flow_mode") in ("frames_to_video", "omni12_i2v")), None)
            if mid_hero:
                chosen_shots.append(mid_hero)
            elif len(valid_shots) >= 3:
                chosen_shots.append(valid_shots[len(valid_shots) // 2])
            if len(valid_shots) >= 2 and valid_shots[-1] not in chosen_shots:
                chosen_shots.append(valid_shots[-1])

    for s in chosen_shots[:3]:
        _add_ref(s.get("start_frame_path") or "")

    # 2. 补充主角与关键场景资产图（并汇总其英文锁词与永久防穿帮护盾）
    sorted_assets = sorted(
        assets,
        key=lambda a: (0 if a.get("asset_type") == "character" else (1 if a.get("asset_type") == "landmark" else 2), a["id"]),
    )
    anchor_bits: list[str] = []
    guard_bits: list[str] = []
    for a in sorted_assets:
        _add_ref(a.get("clean_image_path") or a.get("raw_image_path") or "")
        anc = (a.get("en_anchor_prompt") or "").strip()
        if anc and len(anchor_bits) < 3:
            anchor_bits.append(f"{a.get('flow_tag') or a['name']}: {anc}")
        grd = (a.get("negative_guard_en") or "").strip()
        if grd and grd not in guard_bits:
            guard_bits.append(grd)

    global_style = (
        proj.get("global_style_prompt")
        or "Cinematic live-action film, warm golden-hour lighting, photorealistic 35mm cinema"
    ).strip()
    anchor_text = "; ".join(anchor_bits)
    guard_text = "; ".join(guard_bits)
    theme_hint = (req.headline or ep.get("hook_0_3s") or ep.get("title") or "").strip()

    common_tail_parts = [
        anchor_text,
        global_style,
        f"Critical visual integrity guards: {guard_text}." if guard_text else "",
        "Clean dramatic poster composition with open breathing space in the upper third for later title typography, "
        "ultra-sharp focal subject, rich depth of field, strictly NO text, NO Chinese characters, NO English letters, NO logos, NO watermarks.",
    ]
    common_tail = " ".join(p for p in common_tail_parts if p)

    prompt_916 = (req.prompt_916 or "").strip() or (
        f"Vertical 9:16 theatrical streaming short-drama key-art cover poster inspired by the reference frames"
        f"{f' (story highlight: {theme_hint})' if theme_hint else ''}. "
        "Hero subject prominently framed in the center-lower two-thirds with dynamic golden-hour rim light and atmospheric depth, "
        f"eye-catching thumbnail contrast for mobile vertical feeds. {common_tail}"
    )
    prompt_34 = (req.prompt_34 or "").strip() or (
        f"Vertical 3:4 editorial visual cover poster inspired by the reference frames"
        f"{f' (story highlight: {theme_hint})' if theme_hint else ''}. "
        "Gallery-grade aesthetic composition, balanced foreground storytelling prop and atmospheric cinematic scene backdrop, "
        f"inviting warm film color palette designed for high save rate. {common_tail}"
    )
    return ref_paths, prompt_916, prompt_34


@router.post("/episodes/{episode_id}/cli-generate-covers")
def cli_generate_episode_covers(
    episode_id: int,
    body: CliGenerateCoversRequest | None = None,
    async_mode: bool = False,
) -> dict[str, Any]:
    """
    基于本集高光首帧与绑定资产，调媒体引擎生成 9:16（短视频竖屏）与 3:4（图文/小红书）各 2 张候选封面，
    拼成编号对比总览图并写入 episodes.cover_candidates_json 与 episodes.covers_sheet_path；
    支持 async_mode=True 走轻量异步任务中心。
    """
    req = body or CliGenerateCoversRequest()
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        ep = dict(ep_row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
        assets = rows_to_dicts(
            conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY asset_type, id ASC", (proj["id"],)).fetchall()
        )
        shots = rows_to_dicts(
            conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
                (episode_id,),
            ).fetchall()
        )

    ref_paths, prompt_916, prompt_34 = _collect_cover_refs_and_prompts(proj, ep, shots, assets, req)
    cover_dir = MEDIA_DIR / proj["slug"] / f"ep{ep['ep_number']:02d}" / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True)
    count_per = max(1, min(4, int(req.count_per_ratio or 2)))

    def _run_covers(ctx: JobContext | None = None) -> dict[str, Any]:
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(progress_pct=10, stage_msg=f"正在生成 9:16 与 3:4 双比例候选封面（各 {count_per} 张）...")
        extra_cb: dict[str, Any] = {}
        if ctx is not None:
            extra_cb["progress_cb"] = _make_job_progress_cb(ctx)
            extra_cb["cancel_check"] = ctx.is_cancelled

        res = cli_generate_covers(
            prompt_916=prompt_916,
            prompt_34=prompt_34,
            ref_paths=ref_paths,
            out_dir=cover_dir,
            count_per_ratio=count_per,
            image_size=req.image_size,
            **_filter_supported_kwargs(cli_generate_covers, extra_cb),
        )
        if ctx is not None:
            ctx.check_cancelled()
            ctx.update(progress_pct=82, stage_msg="双比例候选封面已落盘，正在拼接编号对比总览图...")

        covers_9_16 = res.get("covers_9_16") or []
        covers_3_4 = res.get("covers_3_4") or []
        all_candidates: list[dict[str, Any]] = []
        sheet_entries: list[dict[str, Any]] = []

        idx = 1
        for j, c in enumerate(covers_9_16, start=1):
            code = chr(64 + j) if j <= 26 else str(j)
            lbl = f"#{idx} 抖音/视频号 {code}"
            item = {
                "index": idx,
                "ratio": "9:16",
                "platform": "抖音/视频号",
                "label": lbl,
                "path": c["path"],
                "filename": c.get("filename") or Path(c["path"]).name,
                "width": c.get("width", 0),
                "height": c.get("height", 0),
            }
            all_candidates.append(item)
            sheet_entries.append(
                {
                    "label": lbl,
                    "path": c["path"],
                    "badge": "9:16",
                    "caption": f"抖音/视频号 9:16 · {c.get('width', 0)}x{c.get('height', 0)}",
                }
            )
            idx += 1

        for j, c in enumerate(covers_3_4, start=1):
            code = chr(64 + j) if j <= 26 else str(j)
            lbl = f"#{idx} 小红书 {code}"
            item = {
                "index": idx,
                "ratio": "3:4",
                "platform": "小红书",
                "label": lbl,
                "path": c["path"],
                "filename": c.get("filename") or Path(c["path"]).name,
                "width": c.get("width", 0),
                "height": c.get("height", 0),
            }
            all_candidates.append(item)
            sheet_entries.append(
                {
                    "label": lbl,
                    "path": c["path"],
                    "badge": "3:4",
                    "caption": f"小红书 3:4 · {c.get('width', 0)}x{c.get('height', 0)}",
                }
            )
            idx += 1

        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        sheet_jpg = cover_dir / f"E{ep['ep_number']:02d}_covers_sheet_{tag}.jpg"
        sheet_path = ""
        if sheet_entries:
            try:
                sheet_res = build_contact_sheet(
                    sheet_entries,
                    sheet_jpg,
                    title=f"{proj['name']} · 第{ep['ep_number']:02d}集 双比例封面候选对比",
                    cols=2,
                )
                sheet_path = str(Path(sheet_res.get("output_path") or sheet_jpg).resolve())
            except Exception:
                sheet_path = ""

        ts = now_str()
        with db_ctx() as conn:
            if sheet_path:
                conn.execute(
                    "UPDATE episodes SET cover_candidates_json = ?, covers_sheet_path = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(all_candidates, ensure_ascii=False), sheet_path, ts, episode_id),
                )
            else:
                conn.execute(
                    "UPDATE episodes SET cover_candidates_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(all_candidates, ensure_ascii=False), ts, episode_id),
                )
            updated_ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())

        total_cost = round(float(res.get("total_cost_usd") or 0.0), 4)
        return {
            "episode_id": episode_id,
            "sheet_path": sheet_path,
            "covers_sheet_path": sheet_path,
            "output_path": sheet_path,
            "covers_9_16": covers_9_16,
            "covers_3_4": covers_3_4,
            "candidates": all_candidates,
            "ref_count": len(ref_paths),
            "total_cost_usd": total_cost,
            "est_cost_usd": total_cost,
            "episode": updated_ep,
        }

    use_async = bool(req.async_mode or async_mode)
    if use_async:
        job = submit_job(
            "cli_covers",
            _run_covers,
            episode_id=episode_id,
            target_host="local",
            stage_msg=f"已排队：等待生成第 {ep['ep_number']:02d} 集双比例封面候选",
        )
        return {"ok": True, "async": True, "job_id": job["id"], "job": job}

    try:
        return _run_covers(None)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成双比例封面失败：{exc}")


@router.post("/episodes/{episode_id}/select-cover")
def select_episode_cover(episode_id: int, body: SelectCoverRequest) -> dict[str, Any]:
    """锁定本集最终选定封面路径（支持直接传 cover_path 或传候选 1-based 编号 candidate_index）"""
    with db_ctx() as conn:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        ep = dict(ep_row)

    target_path_str = (body.cover_path or "").strip()
    if not target_path_str and body.candidate_index is not None:
        try:
            cands = json.loads(ep.get("cover_candidates_json") or "[]")
        except Exception:
            cands = []
        matched = next((c for c in cands if int(c.get("index") or 0) == int(body.candidate_index)), None)
        if not matched and isinstance(cands, list) and 1 <= int(body.candidate_index) <= len(cands):
            matched = cands[int(body.candidate_index) - 1]
        if not matched or not matched.get("path"):
            raise HTTPException(status_code=404, detail=f"未找到编号 #{body.candidate_index} 的候选封面")
        target_path_str = str(matched["path"])

    if not target_path_str:
        raise HTTPException(status_code=400, detail="请提供 cover_path 或 candidate_index。")

    resolved = Path(target_path_str).expanduser().resolve()
    data_root = DATA_DIR.resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="仅允许选用本制片台受控目录内的封面文件")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="指定的封面文件不存在")

    ts = now_str()
    resolved_str = str(resolved)
    with db_ctx() as conn:
        conn.execute(
            "UPDATE episodes SET selected_cover_path = ?, updated_at = ? WHERE id = ?",
            (resolved_str, ts, episode_id),
        )
        updated_ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())

    return {
        "episode_id": episode_id,
        "selected_cover_path": resolved_str,
        "episode": updated_ep,
    }
