"""
剧集大厅、跨集资产圣经、剧本与分镜拆解、Flow 英文 Prompt 编译、
下载目录收件箱一键认领、Take 多版抽卡打星、末帧抽图、
审图对比拼图与手动 Flow 发车包路由。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..auth import get_current_actor
from ..cli_media import cli_generate_images, cli_generate_music, cli_generate_video
from ..config import DATA_DIR, MEDIA_DIR, is_cloud_collab, load_config, resolve_dock_drag_dir, update_config
from ..db import db_ctx, now_str, rows_to_dicts
from ..flow_compiler import AUDIO_STRATEGIES, FLOW_MODES, compile_shot_prompts, estimate_episode_credits
from ..llm import (
    break_script_to_shots_with_ai,
    critique_and_revise_script_with_ai,
    generate_episode_script_editorial_with_ai,
    generate_episode_script_with_ai,
)
from ..media_ops import extract_last_frame, make_export_bundle_dir, make_shot_media_dir, probe_media
from ..notifier import notify_other_party
from ..qc_ops import build_contact_sheet
from .collab import (
    EPISODE_TRACKED_FIELDS,
    SHOT_TRACKED_FIELDS,
    record_field_revisions,
    sanitize_qc_report_json,
    sanitize_rule_text,
    sanitize_take_filename,
    sanitize_take_notes,
    strip_local_abs_paths,
)

try:
    from ..watcher import downloads_inbox
except ImportError:  # 无 watchdog 极简环境容错降级
    downloads_inbox = None  # type: ignore[assignment]

router = APIRouter(prefix="/api", tags=["studio"])


def _brand_name() -> str:
    return (load_config().get("BRAND_NAME") or "反重力视频工坊").strip() or "反重力视频工坊"


def _require_inbox() -> Any:
    if downloads_inbox is None:
        raise HTTPException(status_code=503, detail="当前环境未启用本地下载目录收件箱监听（缺少 watchdog 依赖）。")
    return downloads_inbox


# ==================== 项目与大纲 ====================


class ProjectIn(BaseModel):
    name: str
    track: str = "剧情微短剧"
    visual_mode: str = "电影写实 + 3D 虚拟搭档"
    aspect_ratio: str = "9:16"
    target_duration_sec: int = 75
    global_style_prompt: str = ""
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    track: str | None = None
    visual_mode: str | None = None
    aspect_ratio: str | None = None
    target_duration_sec: int | None = None
    global_style_prompt: str | None = None
    description: str | None = None
    season_outline: list[dict[str, Any]] | None = None
    continuity_ledger: dict[str, Any] | None = None


def _slugify(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if clean:
        return clean[:32]
    return f"proj-{abs(hash(text)) % 100000}"


@router.get("/flow-meta")
def get_flow_meta() -> dict[str, Any]:
    return {"flow_modes": FLOW_MODES, "audio_strategies": AUDIO_STRATEGIES}


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    with db_ctx() as conn:
        projs = rows_to_dicts(conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall())
        for p in projs:
            p["season_outline"] = json.loads(p.get("season_outline") or "[]")
            p["continuity_ledger"] = json.loads(p.get("continuity_ledger") or "{}")
            p["episode_count"] = conn.execute(
                "SELECT COUNT(*) AS c FROM episodes WHERE project_id = ?",
                (p["id"],),
            ).fetchone()["c"]
            p["asset_count"] = conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE project_id = ?",
                (p["id"],),
            ).fetchone()["c"]
        return {"projects": projs}


@router.post("/projects")
def create_project(body: ProjectIn) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="剧目名称不能为空")
    ts = now_str()
    base_slug = _slugify(name)
    with db_ctx() as conn:
        slug = base_slug
        idx = 1
        while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{idx}"
            idx += 1
        cur = conn.execute(
            """
            INSERT INTO projects (
                name, slug, track, visual_mode, aspect_ratio, target_duration_sec,
                global_style_prompt, description, season_outline, continuity_ledger,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}', ?, ?)
            """,
            (
                name,
                slug,
                body.track,
                body.visual_mode,
                body.aspect_ratio,
                body.target_duration_sec,
                body.global_style_prompt,
                body.description,
                ts,
                ts,
            ),
        )
        p = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone())
        p["season_outline"] = []
        p["continuity_ledger"] = {}
        p["episode_count"] = 0
        p["asset_count"] = 0
        return {"project": p}


@router.get("/projects/{project_id}")
def get_project(project_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="剧目不存在")
        p = dict(row)
        p["season_outline"] = json.loads(p.get("season_outline") or "[]")
        p["continuity_ledger"] = json.loads(p.get("continuity_ledger") or "{}")
        return {"project": p}


@router.patch("/projects/{project_id}")
def update_project(project_id: int, body: ProjectPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        return get_project(project_id)
    if "season_outline" in data:
        data["season_outline"] = json.dumps(data["season_outline"], ensure_ascii=False)
    if "continuity_ledger" in data:
        data["continuity_ledger"] = json.dumps(data["continuity_ledger"], ensure_ascii=False)
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    vals = list(data.values()) + [project_id]
    with db_ctx() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE id = ?", vals)
    return get_project(project_id)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return {"ok": True}


# ==================== 跨集资产圣经 ====================


class AssetIn(BaseModel):
    asset_type: str = "character"
    name: str
    flow_tag: str = ""
    en_anchor_prompt: str = ""
    negative_guard_en: str = ""
    voice_mode: str = "flow_sfx"
    voice_en_desc: str = ""
    notes: str = ""


class AssetPatch(BaseModel):
    asset_type: str | None = None
    name: str | None = None
    flow_tag: str | None = None
    en_anchor_prompt: str | None = None
    negative_guard_en: str | None = None
    voice_mode: str | None = None
    voice_en_desc: str | None = None
    notes: str | None = None


@router.get("/projects/{project_id}/assets")
def list_assets(project_id: int) -> dict[str, Any]:
    cloud_mode = is_cloud_collab()
    with db_ctx() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM assets WHERE project_id = ? ORDER BY asset_type, id ASC",
                (project_id,),
            ).fetchall()
        )
        if cloud_mode:
            for r in rows:
                r["notes"] = sanitize_rule_text(strip_local_abs_paths(str(r.get("notes") or "")))
        return {"assets": rows}


@router.post("/projects/{project_id}/assets")
def create_asset(project_id: int, body: AssetIn) -> dict[str, Any]:
    ts = now_str()
    tag = body.flow_tag.strip() or f"@{body.name.strip()}"
    with db_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO assets (
                project_id, asset_type, name, flow_tag, en_anchor_prompt,
                negative_guard_en, voice_mode, voice_en_desc, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                body.asset_type,
                body.name.strip(),
                tag,
                body.en_anchor_prompt,
                body.negative_guard_en,
                body.voice_mode,
                body.voice_en_desc,
                body.notes,
                ts,
                ts,
            ),
        )
        row = dict(conn.execute("SELECT * FROM assets WHERE id = ?", (cur.lastrowid,)).fetchone())
        return {"asset": row}


@router.patch("/assets/{asset_id}")
def update_asset(asset_id: int, body: AssetPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="无修改字段")
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    with db_ctx() as conn:
        conn.execute(f"UPDATE assets SET {cols} WHERE id = ?", list(data.values()) + [asset_id])
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="资产不存在")
        return {"asset": dict(row)}


@router.post("/assets/{asset_id}/upload")
async def upload_asset_file(
    asset_id: int,
    slot: str = Form("clean"),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传资产参考图（slot=raw/clean）或预留声音克隆干声样本（slot=voice_sample）"""
    if slot not in ("raw", "clean", "voice_sample"):
        raise HTTPException(status_code=400, detail="slot 仅支持 raw / clean / voice_sample")
    with db_ctx() as conn:
        asset = conn.execute(
            "SELECT a.*, p.slug FROM assets a JOIN projects p ON a.project_id = p.id WHERE a.id = ?",
            (asset_id,),
        ).fetchone()
        if not asset:
            raise HTTPException(status_code=404, detail="资产不存在")

    ext = Path(file.filename or "asset.png").suffix.lower() or ".png"
    dest_dir = MEDIA_DIR / asset["slug"] / "assets" / f"asset_{asset_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slot}{ext}"
    content = await file.read()
    dest.write_bytes(content)

    col_map = {"raw": "raw_image_path", "clean": "clean_image_path", "voice_sample": "voice_clone_sample_path"}
    with db_ctx() as conn:
        conn.execute(
            f"UPDATE assets SET {col_map[slot]} = ?, updated_at = ? WHERE id = ?",
            (str(dest.resolve()), now_str(), asset_id),
        )
        row = dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())
        seq_no = _asset_seq_no(conn, row["project_id"], row.get("asset_type") or "", asset_id)
    if slot in ("clean", "raw") and _should_keep_chinese_copy(None) and downloads_inbox is not None:
        if slot == "clean" or not (row.get("clean_image_path") or "").strip():
            try:
                stem = _asset_chinese_copy_stem(row.get("asset_type") or "", row.get("name") or "", seq_no=seq_no)
                downloads_inbox.copy_to_dock_downloads(dest, stem)
            except Exception:
                pass
    return {"asset": row}


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return {"ok": True}


# ==================== 分集与 AI 编剧 ====================


class EpisodeIn(BaseModel):
    title: str
    ep_number: int | None = None
    hook_0_3s: str = ""
    script_text: str = ""


class EpisodePatch(BaseModel):
    title: str | None = None
    ep_number: int | None = None
    hook_0_3s: str | None = None
    ending_cta: str | None = None
    next_ep_hook: str | None = None
    script_text: str | None = None
    continuity_summary: str | None = None
    bgm_prompt: str | None = None
    bgm_volume: float | None = None
    vo_mode: str | None = None
    selected_cover_path: str | None = None
    status: str | None = None


class GenerateScriptRequest(BaseModel):
    topic_title: str = ""
    topic_pitch: str = ""
    extra_notes: str = ""
    editorial_mode: bool = True  # 默认启用多角色编辑部双轮精修（编剧初稿 -> 审稿主编挑刺 -> 返工定稿）
    provider: str | None = None


class CritiqueScriptRequest(BaseModel):
    extra_notes: str = ""
    provider: str | None = None


class BreakShotsRequest(BaseModel):
    provider: str | None = None
    replace_existing: bool = True


@router.get("/projects/{project_id}/episodes")
def list_episodes(project_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        eps = rows_to_dicts(
            conn.execute(
                "SELECT * FROM episodes WHERE project_id = ? ORDER BY ep_number ASC, id ASC",
                (project_id,),
            ).fetchall()
        )
        for ep in eps:
            try:
                ep["cover_candidates"] = json.loads(ep.get("cover_candidates_json") or "[]")
            except Exception:
                ep["cover_candidates"] = []
            ep["shot_count"] = conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id = ?",
                (ep["id"],),
            ).fetchone()["c"]
            ep["ready_shot_count"] = conn.execute(
                """
                SELECT COUNT(DISTINCT s.id) AS c
                FROM shots s JOIN takes t ON t.shot_id = s.id AND t.media_type='video' AND t.is_final=1
                WHERE s.episode_id = ?
                """,
                (ep["id"],),
            ).fetchone()["c"]
        return {"episodes": eps}


@router.post("/projects/{project_id}/episodes")
def create_episode(project_id: int, body: EpisodeIn) -> dict[str, Any]:
    ts = now_str()
    with db_ctx() as conn:
        ep_num = body.ep_number
        if ep_num is None:
            m = conn.execute(
                "SELECT COALESCE(MAX(ep_number), 0) AS m FROM episodes WHERE project_id = ?",
                (project_id,),
            ).fetchone()["m"]
            ep_num = int(m) + 1
        cur = conn.execute(
            """
            INSERT INTO episodes (project_id, ep_number, title, hook_0_3s, script_text, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (project_id, ep_num, body.title.strip() or f"第 {ep_num} 集", body.hook_0_3s, body.script_text, ts, ts),
        )
        ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (cur.lastrowid,)).fetchone())
        ep["shot_count"] = 0
        ep["ready_shot_count"] = 0
        return {"episode": ep}


@router.patch("/episodes/{episode_id}")
def update_episode(episode_id: int, body: EpisodePatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="无更新字段")
    actor = get_current_actor()
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    with db_ctx() as conn:
        old_row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not old_row:
            raise HTTPException(status_code=404, detail="分集不存在")
        old_dict = dict(old_row)
        conn.execute(f"UPDATE episodes SET {cols} WHERE id = ?", list(data.values()) + [episode_id])
        revisions_logged = record_field_revisions(
            conn,
            entity_type="episode",
            entity_id=episode_id,
            episode_id=episode_id,
            old_row=old_dict,
            new_data=data,
            tracked_fields=EPISODE_TRACKED_FIELDS,
            actor=actor,
        )
        updated = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
        try:
            updated["cover_candidates"] = json.loads(updated.get("cover_candidates_json") or "[]")
        except Exception:
            updated["cover_candidates"] = []

    if revisions_logged:
        changed_names = "、".join(r["field_label"] for r in revisions_logged[:4])
        notify_other_party(
            actor["role"],
            title=f"{_brand_name()} · 第{updated['ep_number']}集剧本修改留痕",
            message=f"{actor['display_name']} 修改了《{updated['title'][:20]}》的{changed_names}",
            click_url=f"?ep={episode_id}",
            tags=["pencil2"],
            priority=3,
        )
    return {"episode": updated, "revisions_logged": len(revisions_logged)}


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    return {"ok": True}


@router.post("/episodes/{episode_id}/generate-script")
def ai_generate_script(episode_id: int, body: GenerateScriptRequest) -> dict[str, Any]:
    with db_ctx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
    editorial_report: dict[str, Any] | None = None
    try:
        if body.editorial_mode:
            out, editorial_report, used_model = generate_episode_script_editorial_with_ai(
                project=proj,
                ep_number=ep["ep_number"],
                topic_title=body.topic_title or ep["title"],
                topic_pitch=body.topic_pitch or ep["hook_0_3s"],
                extra_notes=body.extra_notes,
                provider=body.provider,
            )
        else:
            out, used_model = generate_episode_script_with_ai(
                project=proj,
                ep_number=ep["ep_number"],
                topic_title=body.topic_title or ep["title"],
                topic_pitch=body.topic_pitch or ep["hook_0_3s"],
                extra_notes=body.extra_notes,
                provider=body.provider,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ts = now_str()
    with db_ctx() as conn:
        conn.execute(
            """
            UPDATE episodes SET
                title = ?, hook_0_3s = ?, script_text = ?, ending_cta = ?,
                next_ep_hook = ?, bgm_prompt = ?, continuity_summary = ?,
                status = 'script_ready', updated_at = ?
            WHERE id = ?
            """,
            (
                out.get("title") or ep["title"],
                out.get("hook_0_3s") or "",
                out.get("script_text") or "",
                out.get("ending_cta") or "",
                out.get("next_ep_hook") or "",
                out.get("bgm_prompt") or "",
                out.get("continuity_summary") or "",
                ts,
                episode_id,
            ),
        )
        updated = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
    return {
        "used_model": used_model,
        "editorial_mode": body.editorial_mode,
        "editorial_report": editorial_report,
        "episode": updated,
    }


@router.post("/episodes/{episode_id}/critique-script")
def ai_critique_existing_script(episode_id: int, body: CritiqueScriptRequest) -> dict[str, Any]:
    """对当前已有或手写的剧本单独启动「审稿主编挑刺 → 编剧定向返工」精修"""
    with db_ctx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        if not (ep["script_text"] or "").strip():
            raise HTTPException(status_code=400, detail="当前剧本正文为空，请先写好或生成初稿后再交给主编审稿。")
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())

    draft_payload = {
        "title": ep["title"],
        "hook_0_3s": ep["hook_0_3s"],
        "script_text": ep["script_text"],
        "ending_cta": ep["ending_cta"],
        "next_ep_hook": ep["next_ep_hook"],
        "bgm_prompt": ep["bgm_prompt"],
        "continuity_summary": ep["continuity_summary"],
    }
    try:
        out, report, used_model = critique_and_revise_script_with_ai(
            project=proj,
            ep_number=ep["ep_number"],
            draft_script=draft_payload,
            extra_notes=body.extra_notes,
            provider=body.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ts = now_str()
    with db_ctx() as conn:
        conn.execute(
            """
            UPDATE episodes SET
                title = ?, hook_0_3s = ?, script_text = ?, ending_cta = ?,
                next_ep_hook = ?, bgm_prompt = ?, continuity_summary = ?,
                status = 'script_ready', updated_at = ?
            WHERE id = ?
            """,
            (
                out.get("title") or ep["title"],
                out.get("hook_0_3s") or ep["hook_0_3s"],
                out.get("script_text") or ep["script_text"],
                out.get("ending_cta") or ep["ending_cta"],
                out.get("next_ep_hook") or ep["next_ep_hook"],
                out.get("bgm_prompt") or ep["bgm_prompt"],
                out.get("continuity_summary") or ep["continuity_summary"],
                ts,
                episode_id,
            ),
        )
        updated = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
    return {
        "used_model": used_model,
        "editorial_report": report,
        "episode": updated,
    }


@router.post("/episodes/{episode_id}/upload-bgm")
async def upload_episode_bgm(episode_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT e.*, p.slug FROM episodes e JOIN projects p ON e.project_id = p.id WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分集不存在")
    ext = Path(file.filename or "bgm.mp3").suffix.lower() or ".mp3"
    dest_dir = MEDIA_DIR / row["slug"] / f"ep{row['ep_number']:02d}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"flow_music_bgm{ext}"
    dest.write_bytes(await file.read())
    with db_ctx() as conn:
        conn.execute(
            "UPDATE episodes SET bgm_file_path = ?, updated_at = ? WHERE id = ?",
            (str(dest.resolve()), now_str(), episode_id),
        )
        ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
    return {"episode": ep}


# ==================== 拆分镜、资产提取与 Flow 编译 ====================


class ShotIn(BaseModel):
    shot_no: int | None = None
    shot_title: str = ""
    camera_type: str = "中景"
    duration_sec: int = 6
    flow_mode: str = "omni12_i2v"
    ref_asset_ids: list[int] = []
    cn_action_desc: str = ""
    cn_dialogue: str = ""
    subtitle_text: str = ""
    frame_prompt_en: str = ""
    end_frame_prompt_en: str = ""
    video_prompt_en: str = ""
    audio_strategy: str = "keep_flow_sfx"
    upscale_target: str = "1080p_free"


class ShotPatch(BaseModel):
    shot_no: int | None = None
    shot_title: str | None = None
    camera_type: str | None = None
    duration_sec: int | None = None
    flow_mode: str | None = None
    ref_asset_ids: list[int] | None = None
    cn_action_desc: str | None = None
    cn_dialogue: str | None = None
    subtitle_text: str | None = None
    frame_prompt_en: str | None = None
    end_frame_prompt_en: str | None = None
    video_prompt_en: str | None = None
    audio_strategy: str | None = None
    upscale_target: str | None = None
    upscale_done: int | None = None
    trim_start: float | None = None
    trim_end: float | None = None
    sort_order: int | None = None


class ReorderShotsRequest(BaseModel):
    shot_ids: list[int]


def _hydrate_episode_shots(
    conn: Any,
    episode_id: int,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not ep:
        raise HTTPException(status_code=404, detail="分集不存在")
    proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
    assets = rows_to_dicts(conn.execute("SELECT * FROM assets WHERE project_id = ?", (proj["id"],)).fetchall())
    assets_map = {a["id"]: a for a in assets}
    shots = rows_to_dicts(
        conn.execute(
            "SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC, id ASC",
            (episode_id,),
        ).fetchall()
    )
    cloud_mode = is_cloud_collab()
    for s in shots:
        try:
            s["ref_asset_ids"] = json.loads(s.get("ref_asset_ids") or "[]")
        except Exception:
            s["ref_asset_ids"] = []
        if cloud_mode:
            # 远程协作模式不暴露生成平台提示词编译指引与额度消耗，只保留关联参考资产摘要
            ref_imgs = [
                {
                    "id": assets_map[aid]["id"],
                    "name": assets_map[aid].get("name") or "",
                    "asset_type": assets_map[aid].get("asset_type") or "",
                    "image_path": assets_map[aid].get("clean_image_path") or assets_map[aid].get("raw_image_path") or "",
                }
                for aid in s["ref_asset_ids"]
                if aid in assets_map
            ]
            s["compiled"] = {"ref_images": ref_imgs}
            s["qc_report_json"] = sanitize_qc_report_json(s.get("qc_report_json"))
        else:
            s["compiled"] = compile_shot_prompts(
                s,
                proj,
                assets_map,
                image_count=image_count,
                image_model=image_model,
                video_count=video_count,
            )
        takes_rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM takes WHERE shot_id = ? ORDER BY is_final DESC, rating DESC, id DESC",
                (s["id"],),
            ).fetchall()
        )
        if cloud_mode:
            for tk in takes_rows:
                res_lbl = str(tk.get("resolution_label") or "")
                tk["notes"] = sanitize_take_notes(str(tk.get("notes") or ""), res_lbl)
                tk["original_filename"] = sanitize_take_filename(
                    str(tk.get("original_filename") or ""),
                    str(tk.get("file_path") or ""),
                    res_lbl,
                )
        s["takes"] = takes_rows
    ep_dict = dict(ep)
    try:
        ep_dict["cover_candidates"] = json.loads(ep_dict.get("cover_candidates_json") or "[]")
    except Exception:
        ep_dict["cover_candidates"] = []
    budget = {} if cloud_mode else estimate_episode_credits(shots)
    return {"episode": ep_dict, "shots": shots, "budget": budget}


@router.get("/episodes/{episode_id}/shots")
def get_episode_shots(
    episode_id: int,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    with db_ctx() as conn:
        return _hydrate_episode_shots(
            conn,
            episode_id,
            image_count=image_count,
            image_model=image_model,
            video_count=video_count,
        )


@router.post("/episodes/{episode_id}/break-shots")
def ai_break_shots(episode_id: int, body: BreakShotsRequest) -> dict[str, Any]:
    with db_ctx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        if not (ep["script_text"] or "").strip():
            raise HTTPException(status_code=400, detail="请先填写或 AI 生成本集剧本，再执行拆分镜。")
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (ep["project_id"],)).fetchone())
        existing_assets = rows_to_dicts(
            conn.execute("SELECT * FROM assets WHERE project_id = ?", (proj["id"],)).fetchall()
        )

    try:
        result, used_model = break_script_to_shots_with_ai(proj, dict(ep), existing_assets, provider=body.provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ts = now_str()
    added_assets: list[dict[str, Any]] = []
    with db_ctx() as conn:
        # 1. 自动补齐剧本中提取到的新资产（角色/场景地标/道具）
        name_to_id = {a["name"]: a["id"] for a in existing_assets}
        for na in result.get("new_assets") or []:
            aname = str(na.get("name") or "").strip()
            if not aname or aname in name_to_id:
                continue
            atype = str(na.get("asset_type") or "prop")
            if atype not in ("character", "landmark", "prop", "style"):
                atype = "prop"
            cur = conn.execute(
                """
                INSERT INTO assets (
                    project_id, asset_type, name, flow_tag, en_anchor_prompt,
                    negative_guard_en, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proj["id"],
                    atype,
                    aname,
                    str(na.get("flow_tag") or f"@{aname}"),
                    str(na.get("en_anchor_prompt") or ""),
                    str(na.get("negative_guard_en") or ""),
                    ts,
                    ts,
                ),
            )
            name_to_id[aname] = cur.lastrowid
            added_assets.append(dict(conn.execute("SELECT * FROM assets WHERE id = ?", (cur.lastrowid,)).fetchone()))

        # 2. 写入分镜表
        if body.replace_existing:
            conn.execute("DELETE FROM shots WHERE episode_id = ?", (episode_id,))

        raw_shots = result.get("shots") or []
        for idx, sh in enumerate(raw_shots, start=1):
            ref_names = sh.get("ref_asset_names") or []
            ref_ids = [name_to_id[n] for n in ref_names if n in name_to_id]
            fmode = str(sh.get("flow_mode") or "omni12_i2v")
            if fmode not in FLOW_MODES:
                fmode = "omni12_i2v"
            astrat = str(sh.get("audio_strategy") or "keep_flow_sfx")
            if astrat not in AUDIO_STRATEGIES:
                astrat = "keep_flow_sfx"
            utarget = str(sh.get("upscale_target") or "1080p_free")
            if utarget not in ("1080p_free", "4k_hero"):
                utarget = "1080p_free"

            conn.execute(
                """
                INSERT INTO shots (
                    episode_id, shot_no, shot_title, camera_type, duration_sec,
                    flow_mode, ref_asset_ids, cn_action_desc, cn_dialogue, subtitle_text,
                    frame_prompt_en, end_frame_prompt_en, video_prompt_en, audio_strategy, upscale_target,
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    int(sh.get("shot_no") or idx),
                    str(sh.get("shot_title") or f"镜头{idx}"),
                    str(sh.get("camera_type") or "中景"),
                    max(4, min(15, int(sh.get("duration_sec") or 6))),
                    fmode,
                    json.dumps(ref_ids),
                    str(sh.get("cn_action_desc") or ""),
                    str(sh.get("cn_dialogue") or ""),
                    str(sh.get("subtitle_text") or ""),
                    str(sh.get("frame_prompt_en") or ""),
                    str(sh.get("end_frame_prompt_en") or ""),
                    str(sh.get("video_prompt_en") or ""),
                    astrat,
                    utarget,
                    idx,
                    ts,
                    ts,
                ),
            )
        conn.execute("UPDATE episodes SET status = 'storyboard_ready', updated_at = ? WHERE id = ?", (ts, episode_id))
        hydrated = _hydrate_episode_shots(conn, episode_id)

    hydrated["used_model"] = used_model
    hydrated["added_assets"] = added_assets
    return hydrated


@router.post("/episodes/{episode_id}/shots")
def create_shot(episode_id: int, body: ShotIn) -> dict[str, Any]:
    ts = now_str()
    with db_ctx() as conn:
        m = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM shots WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()["m"]
        next_no = body.shot_no or (int(m) + 1)
        conn.execute(
            """
            INSERT INTO shots (
                episode_id, shot_no, shot_title, camera_type, duration_sec,
                flow_mode, ref_asset_ids, cn_action_desc, cn_dialogue, subtitle_text,
                frame_prompt_en, end_frame_prompt_en, video_prompt_en, audio_strategy, upscale_target,
                sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                next_no,
                body.shot_title or f"镜头{next_no}",
                body.camera_type,
                body.duration_sec,
                body.flow_mode,
                json.dumps(body.ref_asset_ids),
                body.cn_action_desc,
                body.cn_dialogue,
                body.subtitle_text,
                body.frame_prompt_en,
                body.end_frame_prompt_en,
                body.video_prompt_en,
                body.audio_strategy,
                body.upscale_target,
                int(m) + 1,
                ts,
                ts,
            ),
        )
        return _hydrate_episode_shots(conn, episode_id)


@router.patch("/shots/{shot_id}")
def update_shot(
    shot_id: int,
    body: ShotPatch,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="无修改字段")
    actor = get_current_actor()
    if "ref_asset_ids" in data:
        data["ref_asset_ids"] = json.dumps(data["ref_asset_ids"])
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    with db_ctx() as conn:
        old_row = conn.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if not old_row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        old_dict = dict(old_row)
        ep_id = int(old_dict["episode_id"])
        conn.execute(f"UPDATE shots SET {cols} WHERE id = ?", list(data.values()) + [shot_id])
        revisions_logged = record_field_revisions(
            conn,
            entity_type="shot",
            entity_id=shot_id,
            episode_id=ep_id,
            old_row=old_dict,
            new_data=data,
            tracked_fields=SHOT_TRACKED_FIELDS,
            actor=actor,
        )
        hydrated = _hydrate_episode_shots(
            conn,
            ep_id,
            image_count=image_count,
            image_model=image_model,
            video_count=video_count,
        )

    if revisions_logged:
        changed_names = "、".join(r["field_label"] for r in revisions_logged[:4])
        shot_no = int(old_dict.get("shot_no") or 0)
        ep_num = int((hydrated.get("episode") or {}).get("ep_number") or 1)
        notify_other_party(
            actor["role"],
            title=f"{_brand_name()} · 第{ep_num}集 S{shot_no:02d} 分镜修改",
            message=f"{actor['display_name']} 调整了 S{shot_no:02d} 的{changed_names}",
            click_url=f"?ep={ep_id}",
            tags=["clapper", "pencil2"],
            priority=3,
        )
    hydrated["revisions_logged"] = len(revisions_logged)
    return hydrated


@router.post("/episodes/{episode_id}/shots/reorder")
def reorder_shots(episode_id: int, body: ReorderShotsRequest) -> dict[str, Any]:
    ts = now_str()
    with db_ctx() as conn:
        for idx, sid in enumerate(body.shot_ids, start=1):
            conn.execute(
                "UPDATE shots SET sort_order = ?, shot_no = ?, updated_at = ? WHERE id = ? AND episode_id = ?",
                (idx, idx, ts, sid, episode_id),
            )
        return _hydrate_episode_shots(conn, episode_id)


@router.delete("/shots/{shot_id}")
def delete_shot(shot_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute("SELECT episode_id FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        ep_id = row["episode_id"]
        conn.execute("DELETE FROM shots WHERE id = ?", (shot_id,))
        return _hydrate_episode_shots(conn, ep_id)


# ==================== 下载目录收件箱认领 & 多版 Take 管理 ====================


class ClaimRequest(BaseModel):
    inbox_item_id: str
    target_type: str  # shot_video | shot_start_frame | shot_end_frame | episode_bgm | asset_clean
    target_id: int
    keep_chinese_copy: bool | None = None


class RenameInboxRequest(BaseModel):
    new_name: str


class InboxContextRequest(BaseModel):
    label: str = ""


class SyncDockTrayRequest(BaseModel):
    clear_existing: bool = True


class CopyToDownloadsRequest(BaseModel):
    path: str
    chinese_stem: str


class TakePatch(BaseModel):
    rating: int | None = None
    is_final: int | None = None
    notes: str | None = None


class RevealFinderRequest(BaseModel):
    path: str


class InboxModePatch(BaseModel):
    mode: str  # flow_only | all


@router.get("/inbox")
def get_downloads_inbox() -> dict[str, Any]:
    return _require_inbox().list_items()


@router.post("/inbox/scan")
def scan_downloads_inbox(minutes: int = 30, reset_dismissed: bool = False) -> dict[str, Any]:
    inbox = _require_inbox()
    added = inbox.scan_recent(minutes=minutes, reset_dismissed=reset_dismissed)
    data = inbox.list_items()
    data["scanned_added"] = added
    return data


@router.post("/inbox/mode")
def switch_inbox_filter_mode(body: InboxModePatch) -> dict[str, Any]:
    """在「仅收 Flow 素材（默认）」与「全部下载媒体」之间一键切换"""
    inbox = _require_inbox()
    mode = "all" if body.mode.strip().lower() == "all" else "flow_only"
    update_config({"INBOX_FILTER_MODE": mode})
    inbox.reload_filter()
    return inbox.list_items()


@router.post("/inbox/clear")
def clear_downloads_inbox() -> dict[str, Any]:
    """一键清空当前收件箱展示（绝不删除磁盘原文件）"""
    inbox = _require_inbox()
    inbox.clear_all()
    return inbox.list_items()


@router.delete("/inbox/{item_id}")
def dismiss_inbox_item(item_id: str) -> dict[str, Any]:
    inbox = _require_inbox()
    inbox.dismiss_item(item_id)
    return inbox.list_items()


@router.post("/inbox/context")
def update_inbox_active_context(body: InboxContextRequest) -> dict[str, Any]:
    """记录当前正在操作的分镜/资产中文名，新落盘的 Flow 乱码图自动以该中文名命名"""
    inbox = _require_inbox()
    clean = inbox.set_active_context(body.label)
    return {"active_context_label": clean}


@router.post("/inbox/{item_id}/trash")
def trash_inbox_item(item_id: str) -> dict[str, Any]:
    """将不要的 Flow 候选图安全移入系统回收站/废纸篓（绝不误删非 Flow 文件）"""
    inbox = _require_inbox()
    try:
        removed = inbox.trash_item(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"trashed": removed["filename"], "inbox": inbox.list_items()}


@router.post("/inbox/trash-all")
def trash_all_inbox_items() -> dict[str, Any]:
    """一键将当前 Flow 收件箱里所有未认领的 Flow 废图安全移入系统回收站/废纸篓"""
    inbox = _require_inbox()
    trashed = inbox.trash_all_items()
    return {"trashed_count": len(trashed), "trashed": trashed, "inbox": inbox.list_items()}


@router.post("/inbox/{item_id}/rename")
def rename_inbox_item(item_id: str, body: RenameInboxRequest) -> dict[str, Any]:
    """原地将下载目录里乱码文件改为直观中文名（方便直接从本地快捷目录拖入 Flow）"""
    inbox = _require_inbox()
    try:
        updated = inbox.rename_item(item_id, body.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"item": updated, "inbox": inbox.list_items()}


def _should_keep_chinese_copy(flag: bool | None) -> bool:
    if flag is not None:
        return bool(flag)
    return load_config().get("KEEP_CHINESE_COPY_ON_CLAIM", "true").lower() in ("1", "true", "yes", "on")


_ASSET_DECOR_PREFIXES: tuple[str, ...] = (
    "高清",
    "标准",
    "核心",
    "通用",
    "主场景",
    "全景",
    "写实",
    "电影",
)
_ASSET_TAIL_SUFFIXES: tuple[str, ...] = ("场景", "背景", "全景", "特写", "参考", "道具")


def _asset_seq_no(conn: Any, project_id: int, asset_type: str, asset_id: int) -> int:
    """获取同一剧目同分类下资产的稳定序号（1 起步，用于生成 01/02 短编号）"""
    rows = conn.execute(
        "SELECT id FROM assets WHERE project_id = ? AND asset_type = ? ORDER BY id ASC",
        (project_id, asset_type),
    ).fetchall()
    for idx, r in enumerate(rows, start=1):
        if r["id"] == asset_id:
            return idx
    return 1


def _asset_chinese_copy_stem(asset_type: str, name: str, seq_no: int | None = None) -> str:
    """
    生成 Google Flow 卡片底部一字不截的极简资产短名（严格控在 6–8 字内）：
    格式：「类别_简名NN」，如 角色_搭档01、场景_街角01、道具_怀表01。
    """
    prefix_map = {"character": "角色", "landmark": "场景", "prop": "道具", "style": "画风"}
    prefix = prefix_map.get(asset_type or "", "资产")
    raw = re.sub(r"[\\/:*?\"<>|，。！？、；：“”‘’（）()【】\[\]\s_]+", "", (name or "").strip())

    # 提取名称末尾已带的数字编号（如 001 -> 01），否则按同分类序号补 2 位编号
    num_match = re.search(r"(\d+)$", raw)
    if num_match:
        num_str = f"{int(num_match.group(1)):02d}"
        core = raw[: num_match.start()]
    else:
        num_str = f"{int(seq_no):02d}" if seq_no else ""
        core = raw

    # 先按连词切取主核心词
    core = re.split(r"[与和及的·]", core)[0] or core

    # 剥离冗长修饰前缀（前提是剩余核心词仍 >= 2 字）
    changed = True
    while changed and len(core) > 3:
        changed = False
        for dp in _ASSET_DECOR_PREFIXES:
            if core.startswith(dp) and len(core) - len(dp) >= 2:
                core = core[len(dp) :]
                changed = True
                break

    # 若名称较长且带泛化尾缀，去掉尾缀保留核心主体
    if len(core) > 3:
        for suf in _ASSET_TAIL_SUFFIXES:
            if core.endswith(suf) and len(core) - len(suf) >= 2:
                core = core[: -len(suf)]
                break

    # 最终压到 2–3 字：角色保留末尾 3 字（主体名词在后），场景/道具保留前 3 字
    if len(core) > 3:
        core = core[-3:] if asset_type == "character" else core[:3]

    core = core or "素材"
    return f"{prefix}_{core}{num_str}"


def _shot_frame_chinese_copy_stem(ep_number: int, shot_no: int, slot_label: str, shot_title: str = "") -> str:
    """
    生成 Google Flow 卡片底部零截断的分镜极简短名：
    压缩为 S01首帧 / S08首帧 / S08尾帧（仅 5 字符，Flow 预览一字不截）。
    """
    _ = (ep_number, shot_title)
    return f"S{int(shot_no):02d}{slot_label}"


def _should_auto_trash_rejected_batch() -> bool:
    return load_config().get("AUTO_TRASH_REJECTED_BATCH", "true").lower() in ("1", "true", "yes", "on")


@router.post("/inbox/claim")
def claim_inbox_media(
    body: ClaimRequest,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    """将下载目录里的 Flow 随机文件名素材一键认领归位并规范重命名，可选保留中文直拖副本并自动清理同批落选废图"""
    inbox = _require_inbox()
    ts = now_str()
    keep_cn_copy = _should_keep_chinese_copy(body.keep_chinese_copy)
    pre_item = inbox.get_item(body.inbox_item_id)
    batch_prefix = (pre_item or {}).get("batch_prefix") or ""
    with db_ctx() as conn:
        if body.target_type in ("shot_video", "shot_start_frame", "shot_end_frame"):
            row = conn.execute(
                """
                SELECT s.*, e.ep_number, e.project_id, p.slug
                FROM shots s
                JOIN episodes e ON s.episode_id = e.id
                JOIN projects p ON e.project_id = p.id
                WHERE s.id = ?
                """,
                (body.target_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="分镜不存在")
            dest_dir = make_shot_media_dir(row["slug"], row["ep_number"], row["shot_no"])
            take_cnt = conn.execute("SELECT COUNT(*) AS c FROM takes WHERE shot_id = ?", (body.target_id,)).fetchone()["c"]
            stem_prefix = f"E{row['ep_number']:02d}_S{row['shot_no']:02d}"
            if body.target_type == "shot_start_frame":
                stem = f"{stem_prefix}_start_frame"
            elif body.target_type == "shot_end_frame":
                stem = f"{stem_prefix}_end_frame"
            else:
                stem = f"{stem_prefix}_take{take_cnt + 1:02d}"

            try:
                claimed = inbox.claim_file(body.inbox_item_id, dest_dir, stem)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc))

            dock_copy_path = ""
            dock_copy_name = ""
            if body.target_type == "shot_start_frame":
                conn.execute(
                    "UPDATE shots SET start_frame_path = ?, updated_at = ? WHERE id = ?",
                    (claimed["path"], ts, body.target_id),
                )
                if keep_cn_copy:
                    try:
                        cn_stem = _shot_frame_chinese_copy_stem(row["ep_number"], row["shot_no"], "首帧", row["shot_title"])
                        cp = inbox.copy_to_dock_downloads(Path(claimed["path"]), cn_stem)
                        dock_copy_path, dock_copy_name = str(cp), cp.name
                    except Exception:
                        pass
            elif body.target_type == "shot_end_frame":
                conn.execute(
                    "UPDATE shots SET end_frame_path = ?, updated_at = ? WHERE id = ?",
                    (claimed["path"], ts, body.target_id),
                )
                if keep_cn_copy:
                    try:
                        cn_stem = _shot_frame_chinese_copy_stem(row["ep_number"], row["shot_no"], "尾帧", row["shot_title"])
                        cp = inbox.copy_to_dock_downloads(Path(claimed["path"]), cn_stem)
                        dock_copy_path, dock_copy_name = str(cp), cp.name
                    except Exception:
                        pass
            else:
                # 若尚无定稿视频，自动把首条认领的视频标为定稿
                has_final = conn.execute(
                    "SELECT 1 FROM takes WHERE shot_id = ? AND media_type = 'video' AND is_final = 1",
                    (body.target_id,),
                ).fetchone()
                is_final = 1 if (claimed["media_type"] == "video" and not has_final) else 0
                conn.execute(
                    """
                    INSERT INTO takes (
                        shot_id, media_type, file_path, original_filename,
                        width, height, duration_sec, resolution_label, rating, is_final, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.target_id,
                        claimed["media_type"],
                        claimed["path"],
                        claimed["filename"],
                        claimed["width"],
                        claimed["height"],
                        claimed["duration_sec"],
                        claimed["resolution_label"],
                        4 if is_final else 0,
                        is_final,
                        ts,
                    ),
                )
                # 认领视频 Take 时按标准费率自动记录 Flow 积分估算消耗
                if claimed["media_type"] == "video":
                    dur_s = max(4, int(round(claimed.get("duration_sec") or row["duration_sec"] or 6)))
                    res_lbl = claimed.get("resolution_label") or ""
                    if row["flow_mode"] == "veo31_lite":
                        est_cost = 5
                    elif res_lbl == "360p":
                        est_cost = dur_s * (2 if dur_s > 10 else 1)
                    else:
                        est_cost = dur_s * (6 if dur_s > 10 else 3)
                    if res_lbl == "4K":
                        est_cost += 50
                    conn.execute(
                        "UPDATE quota_state SET flow_estimated_spent_since_cal = flow_estimated_spent_since_cal + ?, updated_at = ? WHERE id = 1",
                        (est_cost, ts),
                    )
            trashed_siblings: list[str] = []
            if batch_prefix and _should_auto_trash_rejected_batch():
                trashed_siblings = inbox.trash_batch_siblings(batch_prefix, exclude_item_id=body.inbox_item_id)
            return {
                "claimed": claimed,
                "dock_copy_path": dock_copy_path,
                "dock_copy_name": dock_copy_name,
                "trashed_siblings": trashed_siblings,
                "inbox": inbox.list_items(),
                "episode_data": _hydrate_episode_shots(
                    conn,
                    row["episode_id"],
                    image_count=image_count,
                    image_model=image_model,
                    video_count=video_count,
                ),
            }

        if body.target_type == "episode_bgm":
            row = conn.execute(
                "SELECT e.*, p.slug FROM episodes e JOIN projects p ON e.project_id = p.id WHERE e.id = ?",
                (body.target_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="分集不存在")
            dest_dir = MEDIA_DIR / row["slug"] / f"ep{row['ep_number']:02d}"
            claimed = inbox.claim_file(body.inbox_item_id, dest_dir, f"E{row['ep_number']:02d}_flow_music")
            conn.execute("UPDATE episodes SET bgm_file_path = ?, updated_at = ? WHERE id = ?", (claimed["path"], ts, body.target_id))
            return {"claimed": claimed, "inbox": inbox.list_items()}

        if body.target_type == "asset_clean":
            row = conn.execute(
                "SELECT a.*, p.slug FROM assets a JOIN projects p ON a.project_id = p.id WHERE a.id = ?",
                (body.target_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="资产不存在")
            dest_dir = MEDIA_DIR / row["slug"] / "assets" / f"asset_{body.target_id}"
            claimed = inbox.claim_file(body.inbox_item_id, dest_dir, "clean_ref")
            conn.execute(
                "UPDATE assets SET clean_image_path = ?, updated_at = ? WHERE id = ?",
                (claimed["path"], ts, body.target_id),
            )
            dock_copy_path = ""
            dock_copy_name = ""
            if keep_cn_copy:
                try:
                    seq_no = _asset_seq_no(conn, row["project_id"], row["asset_type"], body.target_id)
                    cn_stem = _asset_chinese_copy_stem(row["asset_type"], row["name"], seq_no=seq_no)
                    cp = inbox.copy_to_dock_downloads(Path(claimed["path"]), cn_stem)
                    dock_copy_path, dock_copy_name = str(cp), cp.name
                except Exception:
                    pass
            trashed_siblings: list[str] = []
            if batch_prefix and _should_auto_trash_rejected_batch():
                trashed_siblings = inbox.trash_batch_siblings(batch_prefix, exclude_item_id=body.inbox_item_id)
            return {
                "claimed": claimed,
                "dock_copy_path": dock_copy_path,
                "dock_copy_name": dock_copy_name,
                "trashed_siblings": trashed_siblings,
                "inbox": inbox.list_items(),
            }

        raise HTTPException(status_code=400, detail=f"不支持的 target_type: {body.target_type}")


@router.post("/episodes/{episode_id}/sync-dock-tray")
def sync_episode_to_dock_tray(episode_id: int, body: SyncDockTrayRequest | None = None) -> dict[str, Any]:
    """
    一键把当前剧目全部已上传资产定妆图 + 本集全部首/尾帧，按制片台里的直观中文名
    同步到本机快捷直拖文件夹（由 DOCK_DRAG_DIR 配置），方便直接拖进官方 Flow。
    """
    inbox = _require_inbox()
    clear_existing = True if body is None else bool(body.clear_existing)
    cleared = inbox.clear_dock_tray_dir() if clear_existing else 0
    synced: list[str] = []

    with db_ctx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        assets = rows_to_dicts(
            conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY asset_type, id ASC", (ep["project_id"],)).fetchall()
        )
        shots = rows_to_dicts(
            conn.execute("SELECT * FROM shots WHERE episode_id = ? ORDER BY sort_order ASC, shot_no ASC", (episode_id,)).fetchall()
        )

    type_counters: dict[str, int] = {}
    for a in assets:
        atype = a.get("asset_type") or ""
        type_counters[atype] = type_counters.get(atype, 0) + 1
        img = (a.get("clean_image_path") or a.get("raw_image_path") or "").strip()
        if not img or not Path(img).exists():
            continue
        stem = _asset_chinese_copy_stem(atype, a.get("name") or "", seq_no=type_counters[atype])
        dest = inbox.copy_to_dock_downloads(Path(img), stem)
        synced.append(dest.name)

    for s in shots:
        sf = (s.get("start_frame_path") or "").strip()
        if sf and Path(sf).exists():
            stem = _shot_frame_chinese_copy_stem(ep["ep_number"], s["shot_no"], "首帧", s.get("shot_title") or "")
            dest = inbox.copy_to_dock_downloads(Path(sf), stem)
            synced.append(dest.name)
        ef = (s.get("end_frame_path") or "").strip()
        if ef and Path(ef).exists():
            stem = _shot_frame_chinese_copy_stem(ep["ep_number"], s["shot_no"], "尾帧", s.get("shot_title") or "")
            dest = inbox.copy_to_dock_downloads(Path(ef), stem)
            synced.append(dest.name)

    tray_dir = resolve_dock_drag_dir().resolve()
    return {
        "tray_dir": str(tray_dir),
        "cleared_count": cleared,
        "synced_count": len(synced),
        "files": synced,
    }


@router.post("/shots/{shot_id}/upload-media")
async def upload_shot_media(
    shot_id: int,
    slot: str = Form("video"),  # video | start_frame | end_frame
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """手动拖拽上传首帧/尾帧/抽卡视频"""
    ts = now_str()
    with db_ctx() as conn:
        row = conn.execute(
            """
            SELECT s.*, e.ep_number, p.slug
            FROM shots s JOIN episodes e ON s.episode_id = e.id JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        dest_dir = make_shot_media_dir(row["slug"], row["ep_number"], row["shot_no"])
        ext = Path(file.filename or "clip.mp4").suffix.lower() or ".mp4"
        take_cnt = conn.execute("SELECT COUNT(*) AS c FROM takes WHERE shot_id = ?", (shot_id,)).fetchone()["c"]
        if slot == "start_frame":
            dest = dest_dir / f"E{row['ep_number']:02d}_S{row['shot_no']:02d}_start_frame{ext}"
        elif slot == "end_frame":
            dest = dest_dir / f"E{row['ep_number']:02d}_S{row['shot_no']:02d}_end_frame{ext}"
        else:
            dest = dest_dir / f"E{row['ep_number']:02d}_S{row['shot_no']:02d}_take{take_cnt + 1:02d}{ext}"
        dest.write_bytes(await file.read())
        meta = probe_media(dest)

        if slot == "start_frame":
            conn.execute("UPDATE shots SET start_frame_path = ?, updated_at = ? WHERE id = ?", (str(dest.resolve()), ts, shot_id))
            if _should_keep_chinese_copy(None) and downloads_inbox is not None:
                try:
                    cn_stem = _shot_frame_chinese_copy_stem(row["ep_number"], row["shot_no"], "首帧", row["shot_title"])
                    downloads_inbox.copy_to_dock_downloads(dest, cn_stem)
                except Exception:
                    pass
        elif slot == "end_frame":
            conn.execute("UPDATE shots SET end_frame_path = ?, updated_at = ? WHERE id = ?", (str(dest.resolve()), ts, shot_id))
            if _should_keep_chinese_copy(None) and downloads_inbox is not None:
                try:
                    cn_stem = _shot_frame_chinese_copy_stem(row["ep_number"], row["shot_no"], "尾帧", row["shot_title"])
                    downloads_inbox.copy_to_dock_downloads(dest, cn_stem)
                except Exception:
                    pass
        else:
            has_final = conn.execute(
                "SELECT 1 FROM takes WHERE shot_id = ? AND media_type = 'video' AND is_final = 1",
                (shot_id,),
            ).fetchone()
            is_final = 1 if not has_final else 0
            conn.execute(
                """
                INSERT INTO takes (
                    shot_id, media_type, file_path, original_filename,
                    width, height, duration_sec, resolution_label, rating, is_final, created_at
                ) VALUES (?, 'video', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shot_id,
                    str(dest.resolve()),
                    file.filename or dest.name,
                    meta["width"],
                    meta["height"],
                    meta["duration_sec"],
                    meta["resolution_label"],
                    4 if is_final else 0,
                    is_final,
                    ts,
                ),
            )
        return _hydrate_episode_shots(conn, row["episode_id"])


@router.patch("/takes/{take_id}")
def update_take(take_id: int, body: TakePatch) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT t.*, s.episode_id FROM takes t JOIN shots s ON t.shot_id = s.id WHERE t.id = ?",
            (take_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="版本记录不存在")
        if body.is_final == 1:
            conn.execute("UPDATE takes SET is_final = 0 WHERE shot_id = ? AND media_type = ?", (row["shot_id"], row["media_type"]))
        data = body.model_dump(exclude_none=True)
        if data:
            cols = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE takes SET {cols} WHERE id = ?", list(data.values()) + [take_id])
        return _hydrate_episode_shots(conn, row["episode_id"])


@router.delete("/takes/{take_id}")
def delete_take(take_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT t.*, s.episode_id FROM takes t JOIN shots s ON t.shot_id = s.id WHERE t.id = ?",
            (take_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="版本记录不存在")
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        conn.execute("DELETE FROM takes WHERE id = ?", (take_id,))
        return _hydrate_episode_shots(conn, row["episode_id"])


@router.post("/shots/{shot_id}/extract-last-frame")
def extract_shot_last_frame_to_next(shot_id: int, apply_to_next: bool = True) -> dict[str, Any]:
    """一键抽取当前定稿镜头末帧 PNG，并可选直接塞进下一镜首帧参考图防跳变"""
    ts = now_str()
    with db_ctx() as conn:
        shot = conn.execute(
            """
            SELECT s.*, e.ep_number, p.slug
            FROM shots s JOIN episodes e ON s.episode_id = e.id JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not shot:
            raise HTTPException(status_code=404, detail="分镜不存在")
        take = conn.execute(
            "SELECT * FROM takes WHERE shot_id = ? AND media_type = 'video' ORDER BY is_final DESC, id DESC LIMIT 1",
            (shot_id,),
        ).fetchone()
        if not take:
            raise HTTPException(status_code=400, detail="当前镜头还没有认领视频，无法抽取末帧")

        dest_dir = make_shot_media_dir(shot["slug"], shot["ep_number"], shot["shot_no"])
        out_png = dest_dir / f"E{shot['ep_number']:02d}_S{shot['shot_no']:02d}_last_frame.png"
        try:
            extract_last_frame(Path(take["file_path"]), out_png)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        out_str = str(out_png.resolve())
        conn.execute("UPDATE shots SET end_frame_path = ?, updated_at = ? WHERE id = ?", (out_str, ts, shot_id))

        next_shot_id = None
        if apply_to_next:
            nxt = conn.execute(
                "SELECT id FROM shots WHERE episode_id = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                (shot["episode_id"], shot["sort_order"]),
            ).fetchone()
            if nxt:
                next_shot_id = nxt["id"]
                conn.execute("UPDATE shots SET start_frame_path = ?, updated_at = ? WHERE id = ?", (out_str, ts, next_shot_id))

        data = _hydrate_episode_shots(conn, shot["episode_id"])
        data["extracted_frame_path"] = out_str
        data["applied_next_shot_id"] = next_shot_id
        return data


def _open_path_in_os(p: Path) -> None:
    """跨平台在系统文件管理器中定位并打开文件或目录（适配 macOS / Windows / Linux）"""
    resolved = p.resolve()
    if sys.platform == "darwin":
        if resolved.is_file():
            subprocess.Popen(["open", "-R", str(resolved)])
        else:
            subprocess.Popen(["open", str(resolved)])
        return

    if sys.platform.startswith("win"):
        if resolved.is_file():
            try:
                subprocess.Popen(["explorer", f"/select,{str(resolved)}"])
                return
            except Exception:
                os.startfile(str(resolved.parent))  # type: ignore[attr-defined]
                return
        os.startfile(str(resolved))  # type: ignore[attr-defined]
        return

    # Linux 及其他类 Unix 桌面环境
    target = resolved.parent if resolved.is_file() else resolved
    subprocess.Popen(["xdg-open", str(target)])


@router.post("/system/copy-to-downloads")
def copy_media_to_downloads(body: CopyToDownloadsRequest) -> dict[str, Any]:
    """一键将制片台已归档的资产图/首尾帧复制一份直观中文名副本到快捷直拖目录"""
    inbox = _require_inbox()
    src = Path(body.path).expanduser().resolve()
    data_root = DATA_DIR.resolve()
    try:
        src.relative_to(data_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="仅允许复制本制片台媒体库内的归档文件")
    try:
        dest = inbox.copy_to_dock_downloads(src, body.chinese_stem)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "copy_path": str(dest), "filename": dest.name, "dir": str(dest.parent)}


@router.post("/system/reveal-finder")
@router.post("/system/open-path")
def reveal_in_file_manager(body: RevealFinderRequest) -> dict[str, Any]:
    """跨平台在系统文件管理器（Finder / 资源管理器 / 文件管理器）中定位并打开文件或目录"""
    p = Path(body.path).expanduser()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{p}")
    try:
        _open_path_in_os(p)
        return {"ok": True, "path": str(p.resolve()), "platform": sys.platform}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法打开系统文件管理器：{exc}")


# ==================== 官方 gemini-api CLI 一键直出（走 Gemini API 额度） ====================


class CliGenerateBgmRequest(BaseModel):
    prompt: str | None = None


class CliGenerateAssetCleanRequest(BaseModel):
    image_size: str = "2K"
    count: int = 2
    mode: str = "portrait"  # portrait | full


class SelectAssetCandidateRequest(BaseModel):
    candidate_path: str


class CliGenerateFrameRequest(BaseModel):
    slot: str = "start_frame"  # start_frame | end_frame
    image_size: str = "2K"
    count: int = 2


class SelectFrameCandidateRequest(BaseModel):
    slot: str = "start_frame"  # start_frame | end_frame
    candidate_path: str


class CliGenerateVideoRequest(BaseModel):
    resolution: str = "360p"  # 360p | 720p | 1080p
    count: int = 1
    duration_sec: int | None = None


def _validate_media_candidate(candidate_path: str) -> Path:
    src = Path(candidate_path).expanduser().resolve()
    media_root = MEDIA_DIR.resolve()
    try:
        src.relative_to(media_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="仅允许选用本制片台媒体库内生成的候选图")
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="所选候选图不存在或已被移除")
    return src


@router.post("/episodes/{episode_id}/cli-generate-bgm")
def cli_generate_episode_bgm(episode_id: int, body: CliGenerateBgmRequest | None = None) -> dict[str, Any]:
    """调用官方 gemini-api music 一键生成原创配乐并绑定到本集"""
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT e.*, p.slug FROM episodes e JOIN projects p ON e.project_id = p.id WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分集不存在")
        prompt = ((body.prompt if body else None) or row["bgm_prompt"] or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="请先填写或 AI 生成本集的英文配乐提示词（bgm_prompt）。")
        dest_dir = MEDIA_DIR / row["slug"] / f"ep{row['ep_number']:02d}"
        ep_num = row["ep_number"]

    try:
        res = cli_generate_music(prompt, dest_dir, stem=f"E{ep_num:02d}_lyria_bgm")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    ts = now_str()
    with db_ctx() as conn:
        conn.execute(
            "UPDATE episodes SET bgm_prompt = ?, bgm_file_path = ?, updated_at = ? WHERE id = ?",
            (prompt, res["file_path"], ts, episode_id),
        )
        ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
    return {
        "episode": ep,
        "file_path": res["file_path"],
        "filename": res["filename"],
        "est_cost_usd": res["est_cost_usd"],
        "model": res["model"],
    }


@router.post("/assets/{asset_id}/cli-generate-clean")
def cli_generate_asset_clean(asset_id: int, body: CliGenerateAssetCleanRequest | None = None) -> dict[str, Any]:
    """调用官方 gemini-api image 一键棚拍洗图（自动挂载原图参考，支持 1/2/4 候选图挑选）"""
    req = body or CliGenerateAssetCleanRequest()
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT a.*, p.slug, p.aspect_ratio FROM assets a JOIN projects p ON a.project_id = p.id WHERE a.id = ?",
            (asset_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="资产不存在")
        asset = dict(row)
        seq_no = _asset_seq_no(conn, asset["project_id"], asset.get("asset_type") or "", asset_id)

    desc = (asset.get("en_anchor_prompt") or asset.get("name") or "").strip()
    if not desc:
        raise HTTPException(status_code=400, detail="请先填写该资产的英文锁词或名称")

    if req.mode == "full":
        prompt = (
            f"Full-view complete silhouette reference of the exact same subject ({desc}), "
            "showing full head-to-tail and top-to-bottom proportions, structure, surface materials and signature details clearly, "
            "centered in a clean dark neutral matte studio background, soft even cinematic studio lighting, "
            "photorealistic 35mm film quality, no extra unrequested clothing or props, no text or watermarks."
        )
    else:
        prompt = (
            f"{desc}. Clean dark neutral matte studio background, remove outdoor clutter and busy reflections, "
            "soft even cinematic studio key light, standard front three-quarter close-up reference sheet locking exact "
            "surface texture, materials, silhouette and signature accessories, photorealistic 35mm film quality, "
            "no extra unrequested human clothing, no text or watermarks."
        )

    ref_paths: list[Path] = []
    for k in ("raw_image_path", "clean_image_path"):
        p_str = (asset.get(k) or "").strip()
        if p_str and Path(p_str).exists():
            ref_paths.append(Path(p_str))
            break

    cand_dir = MEDIA_DIR / asset["slug"] / "assets" / f"asset_{asset_id}" / "cli_candidates"
    try:
        res = cli_generate_images(
            prompt=prompt,
            ref_paths=ref_paths,
            aspect_ratio=asset.get("aspect_ratio") or "9:16",
            image_size=req.image_size,
            count=req.count,
            out_dir=cand_dir,
            prefix=f"asset{asset_id}_clean",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    auto_bound = False
    dock_copy_name = ""
    updated_asset = asset
    if len(res["candidates"]) == 1:
        winner = Path(res["candidates"][0]["path"])
        dest_dir = MEDIA_DIR / asset["slug"] / "assets" / f"asset_{asset_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"clean_cli_{now_str().replace(' ', '_').replace(':', '').replace('-', '')}{winner.suffix or '.jpg'}"
        shutil.copy2(winner, dest)
        with db_ctx() as conn:
            conn.execute(
                "UPDATE assets SET clean_image_path = ?, updated_at = ? WHERE id = ?",
                (str(dest.resolve()), now_str(), asset_id),
            )
            updated_asset = dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())
        auto_bound = True
        if _should_keep_chinese_copy(None) and downloads_inbox is not None:
            try:
                stem = _asset_chinese_copy_stem(asset.get("asset_type") or "", asset.get("name") or "", seq_no=seq_no)
                cp = downloads_inbox.copy_to_dock_downloads(dest, stem)
                dock_copy_name = cp.name
            except Exception:
                pass

    return {
        "asset": updated_asset,
        "auto_bound": auto_bound,
        "dock_copy_name": dock_copy_name,
        "candidates": res["candidates"],
        "est_cost_usd": res["est_cost_usd"],
        "image_size": res["image_size"],
        "model": res["model"],
    }


@router.post("/assets/{asset_id}/select-clean-candidate")
def select_asset_clean_candidate(asset_id: int, body: SelectAssetCandidateRequest) -> dict[str, Any]:
    """从 CLI 多张棚拍候选图中点选 1 张绑定为正式定妆图，并同步短名到快捷直拖目录"""
    src = _validate_media_candidate(body.candidate_path)
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT a.*, p.slug FROM assets a JOIN projects p ON a.project_id = p.id WHERE a.id = ?",
            (asset_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="资产不存在")
        dest_dir = MEDIA_DIR / row["slug"] / "assets" / f"asset_{asset_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"clean_cli_{now_str().replace(' ', '_').replace(':', '').replace('-', '')}{src.suffix or '.jpg'}"
        shutil.copy2(src, dest)
        conn.execute(
            "UPDATE assets SET clean_image_path = ?, updated_at = ? WHERE id = ?",
            (str(dest.resolve()), now_str(), asset_id),
        )
        updated = dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())
        seq_no = _asset_seq_no(conn, updated["project_id"], updated.get("asset_type") or "", asset_id)

    dock_copy_name = ""
    if _should_keep_chinese_copy(None) and downloads_inbox is not None:
        try:
            stem = _asset_chinese_copy_stem(updated.get("asset_type") or "", updated.get("name") or "", seq_no=seq_no)
            cp = downloads_inbox.copy_to_dock_downloads(dest, stem)
            dock_copy_name = cp.name
        except Exception:
            pass
    return {"asset": updated, "dock_copy_name": dock_copy_name}


@router.post("/shots/{shot_id}/cli-generate-frame")
def cli_generate_shot_frame(
    shot_id: int,
    body: CliGenerateFrameRequest | None = None,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    """调用官方 gemini-api image 一键生成分镜首帧或尾帧（自动挂载本镜绑定资产参考图）"""
    req = body or CliGenerateFrameRequest()
    slot = "end_frame" if req.slot == "end_frame" else "start_frame"

    with db_ctx() as conn:
        row = conn.execute(
            """
            SELECT s.*, e.ep_number, e.project_id, p.slug, p.aspect_ratio, p.global_style_prompt
            FROM shots s
            JOIN episodes e ON s.episode_id = e.id
            JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        shot = dict(row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (shot["project_id"],)).fetchone())
        assets = rows_to_dicts(conn.execute("SELECT * FROM assets WHERE project_id = ?", (shot["project_id"],)).fetchall())
        assets_map = {a["id"]: a for a in assets}

    compiled = compile_shot_prompts(
        shot,
        proj,
        assets_map,
        image_count=image_count,
        image_model=image_model,
        video_count=video_count,
    )

    ref_paths: list[Path] = []
    if slot == "end_frame":
        prompt = (compiled.get("cli_end_frame_prompt") or shot.get("end_frame_prompt_en") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="请先点「编辑本镜」填写本镜的尾帧英文提示词（end_frame_prompt_en）。")
        # 尾帧生图优先把本镜首帧放在第 1 位参考，确保机位、光线与主体连续不变
        sf_str = (shot.get("start_frame_path") or "").strip()
        if sf_str and Path(sf_str).exists():
            ref_paths.append(Path(sf_str))
            prompt = (
                f"{prompt} Maintain exact camera angle, lighting, environment, and subject identity "
                "from the first reference image while transitioning to the target ending state."
            )
    else:
        prompt = (compiled.get("cli_frame_prompt") or "").strip()

    for r in compiled.get("ref_images") or []:
        ip = (r.get("image_path") or "").strip()
        if ip and Path(ip).exists():
            ref_paths.append(Path(ip))

    dest_dir = make_shot_media_dir(shot["slug"], shot["ep_number"], shot["shot_no"])
    cand_dir = dest_dir / "cli_candidates"
    try:
        res = cli_generate_images(
            prompt=prompt,
            ref_paths=ref_paths,
            aspect_ratio=proj.get("aspect_ratio") or "9:16",
            image_size=req.image_size,
            count=req.count,
            out_dir=cand_dir,
            prefix=f"E{shot['ep_number']:02d}_S{shot['shot_no']:02d}_{slot}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    auto_bound = False
    dock_copy_name = ""
    episode_data = None
    if len(res["candidates"]) == 1:
        winner = Path(res["candidates"][0]["path"])
        final_dest = dest_dir / f"E{shot['ep_number']:02d}_S{shot['shot_no']:02d}_{slot}{winner.suffix or '.jpg'}"
        shutil.copy2(winner, final_dest)
        col = "end_frame_path" if slot == "end_frame" else "start_frame_path"
        with db_ctx() as conn:
            conn.execute(
                f"UPDATE shots SET {col} = ?, updated_at = ? WHERE id = ?",
                (str(final_dest.resolve()), now_str(), shot_id),
            )
            episode_data = _hydrate_episode_shots(
                conn,
                shot["episode_id"],
                image_count=image_count,
                image_model=image_model,
                video_count=video_count,
            )
        auto_bound = True
        if _should_keep_chinese_copy(None) and downloads_inbox is not None:
            try:
                slot_cn = "尾帧" if slot == "end_frame" else "首帧"
                cn_stem = _shot_frame_chinese_copy_stem(shot["ep_number"], shot["shot_no"], slot_cn, shot.get("shot_title") or "")
                cp = downloads_inbox.copy_to_dock_downloads(final_dest, cn_stem)
                dock_copy_name = cp.name
            except Exception:
                pass

    return {
        "slot": slot,
        "auto_bound": auto_bound,
        "dock_copy_name": dock_copy_name,
        "candidates": res["candidates"],
        "est_cost_usd": res["est_cost_usd"],
        "image_size": res["image_size"],
        "model": res["model"],
        "episode_data": episode_data,
    }


@router.post("/shots/{shot_id}/select-frame-candidate")
def select_shot_frame_candidate(
    shot_id: int,
    body: SelectFrameCandidateRequest,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    """从 CLI 多张首/尾帧候选图中点选 1 张绑定为本镜定稿帧，并同步短名到快捷直拖目录"""
    src = _validate_media_candidate(body.candidate_path)
    slot = "end_frame" if body.slot == "end_frame" else "start_frame"
    col = "end_frame_path" if slot == "end_frame" else "start_frame_path"

    with db_ctx() as conn:
        row = conn.execute(
            """
            SELECT s.*, e.ep_number, p.slug
            FROM shots s JOIN episodes e ON s.episode_id = e.id JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        dest_dir = make_shot_media_dir(row["slug"], row["ep_number"], row["shot_no"])
        final_dest = dest_dir / f"E{row['ep_number']:02d}_S{row['shot_no']:02d}_{slot}{src.suffix or '.jpg'}"
        shutil.copy2(src, final_dest)
        conn.execute(
            f"UPDATE shots SET {col} = ?, updated_at = ? WHERE id = ?",
            (str(final_dest.resolve()), now_str(), shot_id),
        )
        episode_data = _hydrate_episode_shots(
            conn,
            row["episode_id"],
            image_count=image_count,
            image_model=image_model,
            video_count=video_count,
        )

    dock_copy_name = ""
    if _should_keep_chinese_copy(None) and downloads_inbox is not None:
        try:
            slot_cn = "尾帧" if slot == "end_frame" else "首帧"
            cn_stem = _shot_frame_chinese_copy_stem(row["ep_number"], row["shot_no"], slot_cn, row["shot_title"])
            cp = downloads_inbox.copy_to_dock_downloads(final_dest, cn_stem)
            dock_copy_name = cp.name
        except Exception:
            pass
    return {"slot": slot, "dock_copy_name": dock_copy_name, "episode_data": episode_data}


@router.post("/shots/{shot_id}/cli-generate-video")
def cli_generate_shot_video(
    shot_id: int,
    body: CliGenerateVideoRequest | None = None,
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    """调用官方 gemini-api video 一键生成 1–2 条视频，自动轮询落盘并写入 takes"""
    req = body or CliGenerateVideoRequest()
    with db_ctx() as conn:
        row = conn.execute(
            """
            SELECT s.*, e.ep_number, e.project_id, p.slug, p.aspect_ratio, p.global_style_prompt
            FROM shots s
            JOIN episodes e ON s.episode_id = e.id
            JOIN projects p ON e.project_id = p.id
            WHERE s.id = ?
            """,
            (shot_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分镜不存在")
        shot = dict(row)
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (shot["project_id"],)).fetchone())
        assets = rows_to_dicts(conn.execute("SELECT * FROM assets WHERE project_id = ?", (shot["project_id"],)).fetchall())
        assets_map = {a["id"]: a for a in assets}

    compiled = compile_shot_prompts(
        shot,
        proj,
        assets_map,
        image_count=image_count,
        image_model=image_model,
        video_count=video_count,
    )
    prompt = (compiled.get("cli_video_prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="本镜缺少视频英文 Prompt，请先编辑补充。")

    sf_str = (shot.get("start_frame_path") or "").strip()
    ef_str = (shot.get("end_frame_path") or "").strip()
    start_frame = Path(sf_str) if (sf_str and Path(sf_str).exists()) else None
    end_frame = Path(ef_str) if (ef_str and Path(ef_str).exists() and (shot.get("flow_mode") == "frames_to_video" or ef_str)) else None
    ref_paths = [
        Path(r["image_path"])
        for r in (compiled.get("ref_images") or [])
        if (r.get("image_path") or "").strip() and Path(r["image_path"]).exists()
    ]

    dur = int(req.duration_sec or shot.get("duration_sec") or 6)
    dest_dir = make_shot_media_dir(shot["slug"], shot["ep_number"], shot["shot_no"])
    try:
        res = cli_generate_video(
            prompt=prompt,
            start_frame=start_frame,
            end_frame=end_frame,
            ref_paths=ref_paths,
            aspect_ratio=proj.get("aspect_ratio") or "9:16",
            resolution=req.resolution,
            duration_sec=dur,
            count=req.count,
            out_dir=dest_dir,
            prefix=f"E{shot['ep_number']:02d}_S{shot['shot_no']:02d}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    ts = now_str()
    with db_ctx() as conn:
        has_final = bool(
            conn.execute(
                "SELECT 1 FROM takes WHERE shot_id = ? AND media_type = 'video' AND is_final = 1",
                (shot_id,),
            ).fetchone()
        )
        for v in res["videos"]:
            mark_final = 1 if not has_final else 0
            if mark_final:
                has_final = True
            conn.execute(
                """
                INSERT INTO takes (
                    shot_id, media_type, file_path, original_filename,
                    width, height, duration_sec, resolution_label, rating, is_final, notes, created_at
                ) VALUES (?, 'video', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shot_id,
                    v["path"],
                    f"cli_video_{v['requested_resolution']}_{v['filename']}",
                    v["width"],
                    v["height"],
                    v["duration_sec"],
                    v["resolution_label"],
                    4 if mark_final else 0,
                    mark_final,
                    f"CLI 直出 ({res['model']} · {v['requested_resolution']} · 约 ${v['est_cost_usd']:.3f})",
                    ts,
                ),
            )
        episode_data = _hydrate_episode_shots(
            conn,
            shot["episode_id"],
            image_count=image_count,
            image_model=image_model,
            video_count=video_count,
        )

    return {
        "created_count": len(res["videos"]),
        "est_cost_usd": res["est_cost_usd"],
        "resolution": res["resolution"],
        "model": res["model"],
        "episode_data": episode_data,
    }


# ==================== 门禁 1 编号审图总览拼图 & 手动 Flow 发车包 ====================


class QcFramesSheetRequest(BaseModel):
    shot_id: int | None = None
    shot_no: int | None = None
    slot: str | None = None  # 'start_frame' | 'end_frame' | None
    candidate_paths: list[str] | None = None
    labels: list[str] | None = None
    badges: list[str] | None = None
    include_candidates: bool = False
    cols: int = 2
    title: str = ""


class PrepareManualFlowPackRequest(BaseModel):
    clear_existing: bool = True
    include_assets: bool = True
    image_count: int = 4
    image_model: str = "Nano Banana 2.1 Pro"
    video_count: int = 2


def _clean_pack_title(text: str, max_len: int = 10) -> str:
    """清理镜头中文标题用于发车包文件名，去除路径非法字符与冗余标点"""
    cleaned = re.sub(r"[\\/:*?\"<>|，。！？、；：“”‘’（）()【】\[\]\s]+", "", (text or "").strip())
    return cleaned[:max_len] if cleaned else "镜头"


def _validate_data_file(path_str: str) -> Path:
    src = Path(path_str).expanduser().resolve()
    data_root = DATA_DIR.resolve()
    try:
        src.relative_to(data_root)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"仅允许访问本制片台数据目录内的图片：{path_str}")
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail=f"图片文件不存在：{path_str}")
    return src


@router.post("/episodes/{episode_id}/qc-frames-sheet")
def build_episode_qc_frames_sheet(
    episode_id: int,
    body: QcFramesSheetRequest | None = None,
) -> dict[str, Any]:
    """
    一键把本集全部定稿首尾帧（或指定某镜的多张候选图）拼成带高对比度大号编号的总览对比 JPG，
    供 AI 导演对话直接审图、弹窗拍板与看板集中展示。
    """
    req = body or QcFramesSheetRequest()
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

    entries: list[dict[str, Any]] = []
    sheet_tag = "frames"

    if req.candidate_paths:
        # 场景 A：显式传入一组候选图路径拼编号对比图
        sheet_tag = "candidates"
        for idx, raw_p in enumerate(req.candidate_paths, start=1):
            p = _validate_data_file(raw_p)
            custom_lbl = req.labels[idx - 1].strip() if (req.labels and idx - 1 < len(req.labels) and req.labels[idx - 1]) else ""
            custom_badge = req.badges[idx - 1].strip() if (req.badges and idx - 1 < len(req.badges) and req.badges[idx - 1]) else ""
            entry: dict[str, Any] = {
                "label": custom_lbl or f"#{idx} 候选{chr(64 + idx) if idx <= 26 else idx} ({p.stem})",
                "path": str(p),
            }
            if custom_badge:
                entry["badge"] = custom_badge
            entries.append(entry)
    elif req.shot_id is not None or req.shot_no is not None:
        # 场景 B：指定某镜，优先收集其 cli_candidates/ 多张候选图，若无则退回其已定稿首尾帧
        target = next(
            (
                s
                for s in shots
                if (req.shot_id is not None and s["id"] == req.shot_id)
                or (req.shot_no is not None and s["shot_no"] == req.shot_no)
            ),
            None,
        )
        if not target:
            raise HTTPException(status_code=404, detail="指定分镜不存在")
        sheet_tag = f"S{int(target['shot_no']):02d}"
        cand_dir = make_shot_media_dir(proj["slug"], ep["ep_number"], target["shot_no"]) / "cli_candidates"
        img_exts = {".jpg", ".jpeg", ".png", ".webp"}
        cand_files: list[Path] = []
        if cand_dir.exists():
            all_imgs = [p for p in cand_dir.iterdir() if p.is_file() and p.suffix.lower() in img_exts]
            if req.slot in ("start_frame", "end_frame"):
                all_imgs = [p for p in all_imgs if req.slot in p.name]
            all_imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            cand_files = sorted(all_imgs[:8], key=lambda p: p.name)

        if cand_files and (req.include_candidates or not (target.get("start_frame_path") or "").strip()):
            for idx, cf in enumerate(cand_files, start=1):
                custom_badge = req.badges[idx - 1].strip() if (req.badges and idx - 1 < len(req.badges) and req.badges[idx - 1]) else ""
                entry = {
                    "label": f"#{idx} S{int(target['shot_no']):02d} 候选{chr(64 + idx) if idx <= 26 else idx}",
                    "path": str(cf.resolve()),
                }
                if custom_badge:
                    entry["badge"] = custom_badge
                entries.append(entry)
        else:
            sf = (target.get("start_frame_path") or "").strip()
            ef = (target.get("end_frame_path") or "").strip()
            if sf and Path(sf).exists() and req.slot != "end_frame":
                entries.append(
                    {
                        "label": f"#1 S{int(target['shot_no']):02d} 首帧 · {target.get('shot_title') or ''}".strip(" ·"),
                        "path": str(Path(sf).resolve()),
                    }
                )
            if ef and Path(ef).exists() and req.slot != "start_frame":
                entries.append(
                    {
                        "label": f"#{len(entries) + 1} S{int(target['shot_no']):02d} 尾帧 · {target.get('shot_title') or ''}".strip(" ·"),
                        "path": str(Path(ef).resolve()),
                        "badge": "尾帧",
                    }
                )
    else:
        # 场景 C（默认）：拼本集所有镜头的已定稿首帧与尾帧总览对比图
        idx = 1
        for s in shots:
            sno = int(s["shot_no"])
            stitle = (s.get("shot_title") or "").strip()
            sf = (s.get("start_frame_path") or "").strip()
            ef = (s.get("end_frame_path") or "").strip()
            has_ef = bool(ef and Path(ef).exists())
            if sf and Path(sf).exists() and req.slot != "end_frame":
                entry = {
                    "label": f"#{idx} S{sno:02d} 首帧",
                    "path": str(Path(sf).resolve()),
                    "caption": f"S{sno:02d} 首帧 · {stitle}" if stitle else f"S{sno:02d} 首帧",
                }
                if has_ef:
                    entry["badge"] = "首尾双帧"
                entries.append(entry)
                idx += 1
            if has_ef and req.slot != "start_frame":
                entries.append(
                    {
                        "label": f"#{idx} S{sno:02d} 尾帧",
                        "path": str(Path(ef).resolve()),
                        "badge": "尾帧",
                        "caption": f"S{sno:02d} 尾帧 · {stitle}" if stitle else f"S{sno:02d} 尾帧",
                    }
                )
                idx += 1

    if not entries:
        raise HTTPException(status_code=400, detail="当前没有可供拼图的首尾帧或候选图，请先生成或上传静帧。")

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    qc_dir = bundle / "qc"
    qc_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_jpg = qc_dir / f"E{ep['ep_number']:02d}_{sheet_tag}_sheet_{tag}.jpg"
    cols = max(1, min(4, int(req.cols or 2)))
    title = req.title.strip() or f"{proj['name']} · 第{ep['ep_number']:02d}集 首尾帧总览对比（共 {len(entries)} 张）"

    try:
        res = build_contact_sheet(entries, out_jpg, title=title, cols=cols)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成首尾帧对比总览图失败：{exc}")

    sheet_abs = str(Path(res.get("output_path") or out_jpg).resolve())
    return {
        "episode_id": episode_id,
        "sheet_path": sheet_abs,
        "output_path": sheet_abs,
        "image_count": len(entries),
        "cols": cols,
        "title": title,
        "width": res.get("width", 0),
        "height": res.get("height", 0),
        "entries": entries,
    }


@router.post("/episodes/{episode_id}/prepare-manual-flow-pack")
def prepare_episode_manual_flow_pack(
    episode_id: int,
    body: PrepareManualFlowPackRequest | None = None,
) -> dict[str, Any]:
    """
    手动 Flow 模式一键发车包：
    在 `data/exports/<slug>_epXX/manual_flow_pack/` 下按镜清晰复制并编号全部定稿首尾帧
    （如 `S01_首帧_xxx.jpg`、`S08_首帧_xxx.jpg`、`S08_尾帧_xxx.jpg`），
    并生成干净好复制的 `逐镜Flow提示词清单.md`（含每镜模型、时长、参考图编号与完整英文词）。
    """
    req = body or PrepareManualFlowPackRequest()
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

    if not shots:
        raise HTTPException(status_code=400, detail="当前分集尚无分镜，无法打包手动 Flow 发车素材。")

    bundle = make_export_bundle_dir(proj["slug"], ep["ep_number"])
    pack_dir = bundle / "manual_flow_pack"
    if req.clear_existing and pack_dir.exists():
        for child in pack_dir.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                pass
    pack_dir.mkdir(parents=True, exist_ok=True)

    # 1. 可选复制参考资产图到子目录「参考资产/」，保持根目录纯粹只有首尾帧与提示词清单
    copied_assets: list[dict[str, Any]] = []
    if req.include_assets and assets:
        assets_dir = pack_dir / "参考资产"
        type_counters: dict[str, int] = {}
        for idx, a in enumerate(assets, start=1):
            atype = a.get("asset_type") or ""
            type_counters[atype] = type_counters.get(atype, 0) + 1
            img_str = (a.get("clean_image_path") or a.get("raw_image_path") or "").strip()
            if not img_str or not Path(img_str).exists():
                continue
            assets_dir.mkdir(parents=True, exist_ok=True)
            src = Path(img_str)
            stem = _asset_chinese_copy_stem(atype, a.get("name") or "", seq_no=type_counters[atype])
            dest = assets_dir / f"A{idx:02d}_{stem}{src.suffix.lower() or '.jpg'}"
            shutil.copy2(src, dest)
            copied_assets.append(
                {
                    "asset_id": a["id"],
                    "name": a["name"],
                    "flow_tag": a.get("flow_tag") or "",
                    "filename": dest.name,
                    "path": str(dest.resolve()),
                }
            )

    # 2. 按镜复制全部定稿首尾帧到 manual_flow_pack/ 根目录，并编译每镜 Flow 提示词
    copied_frames: list[dict[str, Any]] = []
    missing_frames: list[int] = []
    shot_pack_rows: list[dict[str, Any]] = []

    for s in shots:
        sno = int(s["shot_no"])
        clean_title = _clean_pack_title(s.get("shot_title") or "")
        sf_str = (s.get("start_frame_path") or "").strip()
        ef_str = (s.get("end_frame_path") or "").strip()

        start_filename = ""
        end_filename = ""
        if sf_str and Path(sf_str).exists():
            src = Path(sf_str)
            start_filename = f"S{sno:02d}_首帧_{clean_title}{src.suffix.lower() or '.jpg'}"
            dest = pack_dir / start_filename
            shutil.copy2(src, dest)
            copied_frames.append(
                {
                    "shot_id": s["id"],
                    "shot_no": sno,
                    "slot": "start_frame",
                    "filename": start_filename,
                    "path": str(dest.resolve()),
                }
            )
        else:
            missing_frames.append(sno)

        if ef_str and Path(ef_str).exists():
            src = Path(ef_str)
            end_filename = f"S{sno:02d}_尾帧_{clean_title}{src.suffix.lower() or '.jpg'}"
            dest = pack_dir / end_filename
            shutil.copy2(src, dest)
            copied_frames.append(
                {
                    "shot_id": s["id"],
                    "shot_no": sno,
                    "slot": "end_frame",
                    "filename": end_filename,
                    "path": str(dest.resolve()),
                }
            )

        compiled = compile_shot_prompts(
            s,
            proj,
            assets_map,
            image_count=req.image_count,
            image_model=req.image_model,
            video_count=req.video_count,
        )
        shot_pack_rows.append(
            {
                "shot": s,
                "compiled": compiled,
                "start_filename": start_filename,
                "end_filename": end_filename,
            }
        )

    # 3. 生成干净好复制的《逐镜Flow提示词清单.md》
    total_planned_sec = sum(int(s.get("duration_sec") or 6) for s in shots)
    lines: list[str] = [
        f"# 手动 Flow 发车清单｜{proj['name']} · 第 {ep['ep_number']:02d} 集《{ep['title']}》",
        "",
        f"- **生成时间**：{now_str()}",
        f"- **画幅与总时长**：`{proj.get('aspect_ratio') or '9:16'}` · 共 {len(shots)} 镜 · 预计 {total_planned_sec} 秒",
        f"- **发车目录**：`{pack_dir.resolve()}`",
        "- **操作铁律**：逐镜「一镜一清」→ 拖入对应首/尾帧 → 粘贴下方英文词生成 2 条 → 挑最佳下 **免费 1080p**（绝不点 Flow 4K 烧 50 分）→ 下载到本机下载目录后自动归档并跑 3 帧+音轨体检。",
        "",
        "## 一、全集速查总表",
        "",
        "| 镜号 | 镜头标题 | 推荐 Flow 模型 | 时长 | 本镜参考图文件 | 音频策略 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for row in shot_pack_rows:
        s = row["shot"]
        sno = int(s["shot_no"])
        fmode = s.get("flow_mode") or "omni12_i2v"
        fmode_label = FLOW_MODES.get(fmode, {}).get("label", fmode)
        astrat = AUDIO_STRATEGIES.get(s.get("audio_strategy") or "keep_flow_sfx", s.get("audio_strategy") or "")
        files_col = " / ".join(x for x in [row["start_filename"], row["end_filename"]] if x) or "⚠️ 暂缺首帧"
        lines.append(
            f"| S{sno:02d} | {s.get('shot_title') or '未命名'} | {fmode_label} | {s.get('duration_sec') or 6}s | `{files_col}` | {astrat} |"
        )

    lines.extend(["", "## 二、逐镜参考图与一键复制英文词", ""])
    for row in shot_pack_rows:
        s = row["shot"]
        compiled = row["compiled"]
        sno = int(s["shot_no"])
        fmode = s.get("flow_mode") or "omni12_i2v"
        fmode_label = FLOW_MODES.get(fmode, {}).get("label", fmode)
        astrat_key = s.get("audio_strategy") or "keep_flow_sfx"
        astrat_label = AUDIO_STRATEGIES.get(astrat_key, astrat_key)

        ref_files_desc: list[str] = []
        if row["start_filename"]:
            ref_files_desc.append(f"首帧：`{row['start_filename']}`")
        else:
            ref_files_desc.append("首帧：⚠️ 尚未生成/上传")
        if row["end_filename"]:
            ref_files_desc.append(f"尾帧：`{row['end_filename']}`")

        ref_asset_tags = [
            f"{r.get('name')} ({r.get('flow_tag')})" for r in (compiled.get("ref_images") or [])
        ]
        active_guards = compiled.get("active_guards") or []

        lines.extend(
            [
                f"### S{sno:02d} · {s.get('shot_title') or '未命名镜头'}（{s.get('camera_type') or '中景'} · {s.get('duration_sec') or 6}s）",
                f"- **推荐模型**：{fmode_label}（`{fmode}`）",
                f"- **时长 / 音频策略**：`{s.get('duration_sec') or 6}s` · {astrat_label}",
                f"- **发车参考图**：{' ｜ '.join(ref_files_desc)}",
                f"- **关联资产**：{', '.join(ref_asset_tags) if ref_asset_tags else '无'}",
            ]
        )
        if active_guards:
            lines.append(f"- **已自动拼入防穿帮护盾**：`{' ; '.join(active_guards)}`")
        if (s.get("cn_action_desc") or "").strip():
            lines.append(f"- **中文动作与运镜**：{s['cn_action_desc'].strip()}")
        if (s.get("cn_dialogue") or "").strip():
            lines.append(f"- **台词 / 旁白**：{s['cn_dialogue'].strip()}")
        if (s.get("subtitle_text") or "").strip() and (s.get("subtitle_text") or "").strip() != (s.get("cn_dialogue") or "").strip():
            lines.append(f"- **屏幕字幕**：{s['subtitle_text'].strip()}")
        if compiled.get("flow_entry_guide"):
            lines.append(f"- **实操指引**：{compiled['flow_entry_guide']}")
        for warn in compiled.get("warnings") or []:
            lines.append(f"- ⚠️ **注意**：{warn}")

        lines.extend(
            [
                "",
                "**完整 Flow 视频英文提示词（直接复制粘贴）：**",
                "```text",
                (compiled.get("compiled_video_prompt") or "").strip(),
                "```",
                "",
            ]
        )

    checklist_path = pack_dir / "逐镜Flow提示词清单.md"
    checklist_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "episode_id": episode_id,
        "pack_dir": str(pack_dir.resolve()),
        "checklist_path": str(checklist_path.resolve()),
        "prompt_md_path": str(checklist_path.resolve()),
        "shot_count": len(shots),
        "frame_count": len(copied_frames),
        "asset_count": len(copied_assets),
        "copied_frames": copied_frames,
        "copied_assets": copied_assets,
        "missing_frames": missing_frames,
    }
