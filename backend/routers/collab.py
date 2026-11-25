"""
「反重力视频工坊」可选自托管多人协作、脱敏创作规范、全字段修改留痕、审片投票与本地↔远程同步接收路由：
- 账号登录/登出与当前身份（/api/auth/*）
- 待我协作聚合工作台（/api/collab/inbox）
- 修改留痕查询（/api/collab/revisions）
- 脱敏中文创作规范只读展示 + 修改建议闭环（/api/collab/rules*）
- 首尾帧 / 试卡 / 封面多人投票与批注（/api/episodes/{id}/votes）
- 供本地制片台同步引擎直连推拉的受控接口（/api/sync/*，X-Sync-Token 恒定时间鉴权）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from ..auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    authenticate_user,
    create_session_token,
    get_current_actor,
    is_https_request,
    require_admin_user,
    require_authenticated_user,
)
from ..config import DATA_DIR, get_app_mode, get_public_config, load_config
from ..db import db_ctx, now_str, rows_to_dicts
from ..notifier import notify_other_party, send_notification

router = APIRouter(prefix="/api", tags=["collab"])


def _brand_name() -> str:
    return (load_config().get("BRAND_NAME") or "反重力视频工坊").strip() or "反重力视频工坊"


# ==================== 全字段修改留痕工具（供 studio.py / collab.py 共用） ====================

EPISODE_TRACKED_FIELDS: dict[str, str] = {
    "title": "分集标题",
    "hook_0_3s": "0-3秒视觉钩子",
    "script_text": "分集剧本正文",
    "ending_cta": "结尾互动诱饵",
    "next_ep_hook": "下集悬念钩子",
    "continuity_summary": "跨集连续性摘要",
    "bgm_prompt": "原创配乐提示词",
    "selected_cover_path": "定稿封面",
    "status": "分集状态",
}

SHOT_TRACKED_FIELDS: dict[str, str] = {
    "shot_title": "镜头标题",
    "camera_type": "景别与运镜",
    "duration_sec": "镜头时长(秒)",
    "cn_action_desc": "中文画面动作",
    "cn_dialogue": "中文台词/旁白",
    "subtitle_text": "屏幕字幕",
    "frame_prompt_en": "首帧英文提示词",
    "end_frame_prompt_en": "尾帧英文提示词",
    "video_prompt_en": "视频英文提示词",
    "audio_strategy": "音频策略",
}

ALL_FIELD_LABELS: dict[str, str] = {**EPISODE_TRACKED_FIELDS, **SHOT_TRACKED_FIELDS}


def record_field_revisions(
    conn: Any,
    *,
    entity_type: str,
    entity_id: int,
    episode_id: int | None,
    old_row: dict[str, Any],
    new_data: dict[str, Any],
    tracked_fields: dict[str, str],
    actor: dict[str, Any],
    note: str = "",
) -> list[dict[str, Any]]:
    """逐字段比对旧值与新值，仅当发生实质变更时写入 revisions 表并返回新增留痕列表"""
    ts = now_str()
    logged: list[dict[str, Any]] = []
    actor_username = str(actor.get("username") or "system")
    actor_name = str(actor.get("display_name") or actor_username)
    actor_role = str(actor.get("role") or "admin")

    for field_name in tracked_fields:
        if field_name not in new_data:
            continue
        old_val = "" if old_row.get(field_name) is None else str(old_row.get(field_name))
        new_val = "" if new_data.get(field_name) is None else str(new_data.get(field_name))
        if old_val == new_val:
            continue
        cur = conn.execute(
            """
            INSERT INTO revisions (
                entity_type, entity_id, episode_id, field_name,
                old_value, new_value, actor_username, actor_name, actor_role, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                int(entity_id),
                episode_id,
                field_name,
                old_val,
                new_val,
                actor_username,
                actor_name,
                actor_role,
                note,
                ts,
            ),
        )
        row = dict(conn.execute("SELECT * FROM revisions WHERE id = ?", (cur.lastrowid,)).fetchone())
        row["field_label"] = tracked_fields.get(field_name, field_name)
        logged.append(row)
    return logged


# ==================== 远程协作端展示文本脱敏清洗器 ====================

_SANITIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 完整组合短语优先替换，确保语义通顺自然
    (re.compile(r"Google\s+Colab\s+Pro\+\s*官方\s*GPU\s*通道", re.IGNORECASE), "云端超分渲染通道"),
    (re.compile(r"Google\s+Colab\s+Pro\+", re.IGNORECASE), "云端超分渲染引擎"),
    (re.compile(r"Colab\s+Pro\+", re.IGNORECASE), "云端超分渲染引擎"),
    (re.compile(r"\bColab\b", re.IGNORECASE), "云端超分引擎"),
    (re.compile(r"Google\s+Flow", re.IGNORECASE), "智能视频生成引擎"),
    (re.compile(r"Nano\s+Banana(?:\s*2\.1(?:\s*Pro)?)?", re.IGNORECASE), "高清静帧引擎"),
    (re.compile(r"(?:Gemini\s+)?Omni\s+Flash(?:\s*1\.2)?", re.IGNORECASE), "角色动态引擎"),
    (re.compile(r"Veo\s*3\.1(?:\s*Lite)?", re.IGNORECASE), "电影视频引擎"),
    (re.compile(r"\bVeo\b", re.IGNORECASE), "电影视频引擎"),
    (re.compile(r"Flow\s+Music\s*\(\s*Lyria(?:\s*3\.5)?\s*\)", re.IGNORECASE), "原创配乐引擎"),
    (re.compile(r"Flow\s+Music", re.IGNORECASE), "原创配乐引擎"),
    (re.compile(r"Lyria(?:\s*3\.5)?", re.IGNORECASE), "原创配乐引擎"),
    (re.compile(r"Gemini\s+API", re.IGNORECASE), "AI 创作助手"),
    (re.compile(r"\bGemini\b", re.IGNORECASE), "AI 创作助手"),
    (re.compile(r"~/Flow[^\s\"']*素材[^\s\"']*"), "本地素材目录"),
    (re.compile(r"veo31_lite", re.IGNORECASE), "轻量空镜模式"),
    (re.compile(r"omni12_i2v", re.IGNORECASE), "角色精控模式"),
    (re.compile(r"1080p_free", re.IGNORECASE), "高清 1080p"),
    (re.compile(r"Ultra\s*仅\s*\d+\s*积分/条", re.IGNORECASE), "轻量低算力档"),
    (re.compile(r"不烧\s*\d+\s*积分/镜买\s*Flow\s*4K", re.IGNORECASE), "不盲目直出高耗算力 4K"),
    (re.compile(r"不白烧\s*\d+\s*分/镜\s*Flow\s*4K", re.IGNORECASE), "不盲目直出高耗算力 4K"),
    (re.compile(r"≤\s*10s\s*0\s*积分"), "10秒内高效直出"),
    (re.compile(r"\bUltra\b", re.IGNORECASE), "高阶档"),
    (re.compile(r"省积分"), "省算力"),
    (re.compile(r"省分"), "省算力"),
    (re.compile(r"积分"), "算力额度"),
    (re.compile(r"抽卡"), "生成"),
    (re.compile(r"Flow\s*端", re.IGNORECASE), "生成端"),
    (re.compile(r"Flow\s*卡片", re.IGNORECASE), "生成引擎素材卡"),
    (re.compile(r"Flow\s*画布", re.IGNORECASE), "生成工作台"),
    (re.compile(r"Flow\s*可拍性", re.IGNORECASE), "AI 可拍性"),
    (re.compile(r"Flow\s*实拍", re.IGNORECASE), "AI 实拍"),
    (re.compile(r"Flow\s*口型", re.IGNORECASE), "AI 口型"),
    (re.compile(r"Flow\s*4K", re.IGNORECASE), "直出 4K"),
    (re.compile(r"Flow", re.IGNORECASE), "生成引擎"),
    (re.compile(r"Omni", re.IGNORECASE), "角色动态"),
    (re.compile(r"Google", re.IGNORECASE), "生成平台"),
    (re.compile(r"\bCLI\b", re.IGNORECASE), "自动化工具"),
]


def sanitize_rule_text(text: str) -> str:
    """将创作规范中的底层引擎与额度术语脱敏为通用中文表述，便于远程协作团队成员审阅"""
    out = text or ""
    for pattern, repl in _SANITIZE_PATTERNS:
        out = pattern.sub(repl, out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


_LOCAL_ABS_PATH_RE = re.compile(
    r"(?:/(?:Users|home)/[^\s\"'<>|,;]+|[A-Za-z]:\\[^\s\"'<>|,;]+)"
)
_TAKE_SENSITIVE_RE = re.compile(
    r"(gemini|omni|veo|nano\s*banana|google|flow|colab|\bcli\b|\$\s*\d|\blyria\b|/(?:users|home)/|\.gemini)",
    re.IGNORECASE,
)
_TAKE_RES_RE = re.compile(r"\b(4k|2160p|1080p|720p|360p)\b", re.IGNORECASE)


def strip_local_abs_paths(text: str) -> str:
    """将文本中的本机绝对路径安全脱敏为相对 DATA_DIR 路径或纯文件名，杜绝泄露宿主机目录结构"""
    raw = text or ""
    if not raw:
        return ""

    def _repl(match: re.Match[str]) -> str:
        val = match.group(0).replace("\\", "/")
        for marker in ("/data/media/", "/data/exports/"):
            idx = val.find(marker)
            if idx != -1:
                return val[idx + len("/data/") :]
        base = val.rstrip("/").rsplit("/", 1)[-1]
        if base.startswith(".") or base.lower().endswith((".db", ".key", ".env", ".pem")):
            return ""
        return base

    return _LOCAL_ABS_PATH_RE.sub(_repl, raw)


def sanitize_take_notes(notes: str, resolution_label: str = "") -> str:
    """脱敏清洗 takes.notes：移除底层模型标识、美元费用与本机绝对路径"""
    s = (notes or "").strip()
    if not s:
        return ""
    if _TAKE_SENSITIVE_RE.search(s):
        res_m = _TAKE_RES_RE.search(s)
        res = (res_m.group(1) if res_m else (resolution_label or "")).strip()
        if res.lower() == "4k":
            res = "4K"
        if re.match(r"^(?:CLI\s*直出|直出|自动生成)\s*[\(（].*[\)）]$", s, re.IGNORECASE):
            return f"高清成品视频 ({res})" if res else "高清成品视频"
        cleaned = re.sub(
            r"CLI\s*直出\s*[\(（][^\)）]*[\)）]",
            f"高清成品视频 ({res})" if res else "高清成品视频",
            s,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"(?:约\s*)?\$\s*\d+(?:\.\d+)?", "", cleaned)
        cleaned = sanitize_rule_text(strip_local_abs_paths(cleaned))
        cleaned = re.sub(r"[·•]\s*[·•]", "·", cleaned).strip(" ·•-")
        return cleaned or (f"高清成品视频 ({res})" if res else "高清成品视频")
    return strip_local_abs_paths(s)


def sanitize_take_filename(filename: str, fallback_path: str = "", resolution_label: str = "") -> str:
    """脱敏清洗 takes.original_filename：去除内部生成前缀与本机路径"""
    raw = (filename or "").strip()
    if not raw and fallback_path:
        raw = Path(fallback_path.replace("\\", "/")).name
    if not raw:
        return ""
    base = Path(raw.replace("\\", "/")).name
    if _TAKE_SENSITIVE_RE.search(base):
        cleaned = re.sub(r"^(?:cli_)?(?:video_|omni\d*_|veo\d*(?:_lite)?_|gemini_)+", "", base, flags=re.IGNORECASE)
        cleaned = re.sub(r"[_-]?cli(?=[_.-]|$)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:gemini|omni\d*|veo\d*|flow|google)", "clip", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"__+", "_", cleaned).strip("_")
        if cleaned and not _TAKE_SENSITIVE_RE.search(cleaned):
            return cleaned
        ext = Path(base).suffix or ".mp4"
        res = (resolution_label or "video").strip()
        return f"take_{res}{ext}"
    return base


def _sanitize_recursive_json(obj: Any, *, sanitize_terms: bool = False) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_recursive_json(v, sanitize_terms=sanitize_terms) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_recursive_json(v, sanitize_terms=sanitize_terms) for v in obj]
    if isinstance(obj, str):
        val = strip_local_abs_paths(obj)
        if sanitize_terms:
            val = sanitize_rule_text(val)
        return val
    return obj


def sanitize_qc_report_json(raw: Any) -> str:
    """脱敏清洗 shots.qc_report_json：剥离宿主机绝对路径与底层引擎词"""
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, (dict, list)):
        cleaned = _sanitize_recursive_json(raw, sanitize_terms=True)
        return json.dumps(cleaned, ensure_ascii=False)
    text = str(raw).strip()
    if not text:
        return "{}"
    try:
        parsed = json.loads(text)
        cleaned = _sanitize_recursive_json(parsed, sanitize_terms=True)
        return json.dumps(cleaned, ensure_ascii=False)
    except Exception:
        return sanitize_rule_text(strip_local_abs_paths(text))


def _sanitize_json_payload_str(raw: Any, default_empty: str = "{}") -> str:
    """仅对 JSON 字符串值做脱敏，绝不破坏 JSON 键名与结构"""
    if raw is None or raw == "":
        return default_empty
    if isinstance(raw, (dict, list)):
        return json.dumps(_sanitize_recursive_json(raw, sanitize_terms=True), ensure_ascii=False)
    text = str(raw).strip()
    if not text:
        return default_empty
    try:
        parsed = json.loads(text)
        return json.dumps(_sanitize_recursive_json(parsed, sanitize_terms=True), ensure_ascii=False)
    except Exception:
        return sanitize_rule_text(strip_local_abs_paths(text))


# ==================== 1. 登录鉴权接口（/api/auth/*） ====================


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """返回当前登录身份、运行模式、品牌名与脱敏公开配置"""
    mode = get_app_mode()
    pub = get_public_config()
    state_user = getattr(request.state, "user", None)
    if mode == "cloud_collab":
        authenticated = isinstance(state_user, dict) and bool(state_user.get("username")) and state_user.get("role") != "guest"
        user_out = state_user if authenticated else None
    else:
        authenticated = True
        user_out = state_user if (isinstance(state_user, dict) and state_user.get("username")) else get_current_actor(request)

    return {
        "authenticated": authenticated,
        "app_mode": mode,
        "brand_name": pub.get("brand_name") or _brand_name(),
        "user": user_out,
        "public_config": pub,
    }


@router.post("/auth/login")
def auth_login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    """账号密码登录并下发 7 天有效 HttpOnly 签名 Session Cookie"""
    user = authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="账号或密码错误，请核对后重试")

    token = create_session_token(user, ttl_seconds=SESSION_TTL_SECONDS)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
        path="/",
    )
    pub = get_public_config()
    return {
        "authenticated": True,
        "app_mode": get_app_mode(),
        "brand_name": pub.get("brand_name") or _brand_name(),
        "user": user,
        "public_config": pub,
    }


@router.post("/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    """清除会话 Cookie"""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True, "authenticated": False}


# ==================== 2. 待我协作区 & 修改留痕（/api/collab/*） ====================


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _summarize_votes_by_type(votes: list[dict[str, Any]], actor_username: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for ttype in ("frame", "video_360p", "cover"):
        subset = [v for v in votes if v.get("target_type") == ttype]
        latest_by_actor: dict[str, dict[str, Any]] = {}
        for v in subset:  # 已按 id DESC 排序，首次遇到即最新
            au = v.get("actor_username") or ""
            if au and au not in latest_by_actor:
                latest_by_actor[au] = {
                    "id": v["id"],
                    "verdict": v["verdict"],
                    "choice": v["choice"],
                    "comment": v["comment"],
                    "actor_name": v["actor_name"],
                    "actor_role": v["actor_role"],
                    "created_at": v["created_at"],
                }
        approve_cnt = sum(1 for x in latest_by_actor.values() if x["verdict"] == "approve")
        change_cnt = sum(1 for x in latest_by_actor.values() if x["verdict"] == "change")
        comment_cnt = sum(1 for x in latest_by_actor.values() if x["verdict"] == "comment")
        summary[ttype] = {
            "total": len(subset),
            "approve": approve_cnt,
            "change": change_cnt,
            "comment": comment_cnt,
            "my_vote": latest_by_actor.get(actor_username),
            "latest_by_actor": latest_by_actor,
        }
    return summary


@router.get("/collab/inbox")
def get_collab_inbox(request: Request) -> dict[str, Any]:
    """聚合返回「待我协作区」：待挑图、待审试卡、待投票封面、最新修改留痕、待处理规范建议、待回复评论"""
    actor = require_authenticated_user(request)
    actor_username = str(actor.get("username") or "")
    actor_role = str(actor.get("role") or "partner")

    with db_ctx() as conn:
        episodes = rows_to_dicts(
            conn.execute(
                """
                SELECT e.*, p.name AS project_name, p.slug AS project_slug
                FROM episodes e
                JOIN projects p ON p.id = e.project_id
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT 30
                """
            ).fetchall()
        )

        pending_frames_episodes: list[dict[str, Any]] = []
        pending_videos_episodes: list[dict[str, Any]] = []
        pending_covers_episodes: list[dict[str, Any]] = []

        for ep in episodes:
            ep_id = ep["id"]
            shot_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS shot_count,
                    SUM(CASE WHEN COALESCE(TRIM(start_frame_path), '') != '' THEN 1 ELSE 0 END) AS frames_ready
                FROM shots WHERE episode_id = ?
                """,
                (ep_id,),
            ).fetchone()
            shot_count = int(shot_stats["shot_count"] or 0)
            frames_ready = int(shot_stats["frames_ready"] or 0)

            video_ready = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT s.id) AS c
                    FROM shots s
                    JOIN takes t ON t.shot_id = s.id AND t.media_type = 'video' AND t.is_final = 1
                    WHERE s.episode_id = ?
                    """,
                    (ep_id,),
                ).fetchone()["c"]
                or 0
            )

            votes = rows_to_dicts(
                conn.execute("SELECT * FROM review_votes WHERE episode_id = ? ORDER BY id DESC", (ep_id,)).fetchall()
            )
            vsum = _summarize_votes_by_type(votes, actor_username)
            cover_cands = _parse_json_list(ep.get("cover_candidates_json"))

            base_card = {
                "episode_id": ep_id,
                "project_id": ep["project_id"],
                "project_name": ep["project_name"],
                "project_slug": ep["project_slug"],
                "ep_number": ep["ep_number"],
                "title": ep["title"],
                "status": ep["status"],
                "shot_count": shot_count,
                "frames_ready_count": frames_ready,
                "video_ready_count": video_ready,
                "updated_at": ep["updated_at"],
                "sync_updated_at": ep.get("sync_updated_at") or "",
            }

            # 待挑图剧集：已有首尾帧或首尾帧总览图，且当前用户尚未赞同通过（或有人提出修改建议）
            has_frames = bool((ep.get("frames_sheet_path") or "").strip()) or frames_ready > 0
            frame_v = vsum["frame"]
            if has_frames and ep.get("status") != "published":
                my_f = frame_v.get("my_vote")
                if not my_f or my_f.get("verdict") != "approve" or frame_v["change"] > 0:
                    pending_frames_episodes.append(
                        {
                            **base_card,
                            "frames_sheet_path": ep.get("frames_sheet_path") or "",
                            "my_vote": my_f,
                            "votes_summary": frame_v,
                        }
                    )

            # 待审试卡 / 成片剧集：已有流畅预览或成片或定稿分镜视频，且当前用户尚未赞同
            has_video = (
                bool((ep.get("web_preview_path") or "").strip())
                or bool((ep.get("latest_output_path") or "").strip())
                or video_ready > 0
            )
            video_v = vsum["video_360p"]
            if has_video and ep.get("status") != "published":
                my_v = video_v.get("my_vote")
                if not my_v or my_v.get("verdict") != "approve" or video_v["change"] > 0:
                    pending_videos_episodes.append(
                        {
                            **base_card,
                            "web_preview_path": ep.get("web_preview_path") or "",
                            "latest_output_path": ep.get("latest_output_path") or "",
                            "qc_overview_path": ep.get("qc_overview_path") or "",
                            "ai_proof_path": ep.get("ai_proof_path") or "",
                            "my_vote": my_v,
                            "votes_summary": video_v,
                        }
                    )

            # 待投票封面剧集：已有封面候选且未最终选定封面，或当前用户尚未投票赞同
            cover_v = vsum["cover"]
            if cover_cands and ep.get("status") != "published":
                my_c = cover_v.get("my_vote")
                if not (ep.get("selected_cover_path") or "").strip() or not my_c or my_c.get("verdict") != "approve":
                    pending_covers_episodes.append(
                        {
                            **base_card,
                            "cover_count": len(cover_cands),
                            "cover_candidates": cover_cands,
                            "covers_sheet_path": ep.get("covers_sheet_path") or "",
                            "selected_cover_path": ep.get("selected_cover_path") or "",
                            "my_vote": my_c,
                            "votes_summary": cover_v,
                        }
                    )

        recent_revisions = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*, e.title AS episode_title, e.ep_number AS ep_number, e.project_id AS project_id
                FROM revisions r
                LEFT JOIN episodes e ON e.id = r.episode_id
                ORDER BY r.id DESC
                LIMIT 20
                """
            ).fetchall()
        )
        for rev in recent_revisions:
            rev["field_label"] = ALL_FIELD_LABELS.get(rev["field_name"], rev["field_name"])

        open_rule_suggestions = rows_to_dicts(
            conn.execute(
                "SELECT * FROM rule_suggestions WHERE status = 'open' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        )

        pending_comments = rows_to_dicts(
            conn.execute(
                "SELECT * FROM comment_drafts WHERE COALESCE(status, 'draft') = 'draft' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        )
        for c in pending_comments:
            c["drafts"] = _parse_json_list(c.get("drafts_json"))

    other_revisions_cnt = sum(1 for r in recent_revisions if r.get("actor_username") != actor_username)
    counts = {
        "pending_frames": len(pending_frames_episodes),
        "pending_videos": len(pending_videos_episodes),
        "pending_covers": len(pending_covers_episodes),
        "recent_revisions": len(recent_revisions),
        "other_party_revisions": other_revisions_cnt,
        "open_rule_suggestions": len(open_rule_suggestions),
        "pending_comments": len(pending_comments),
    }
    if actor_role == "admin":
        counts["total_todos"] = (
            counts["pending_frames"]
            + counts["pending_videos"]
            + counts["pending_covers"]
            + counts["open_rule_suggestions"]
            + counts["pending_comments"]
        )
    else:
        counts["total_todos"] = (
            counts["pending_frames"]
            + counts["pending_videos"]
            + counts["pending_covers"]
            + counts["pending_comments"]
        )

    return {
        "actor": actor,
        "counts": counts,
        "pending_frames_episodes": pending_frames_episodes,
        "pending_videos_episodes": pending_videos_episodes,
        "pending_covers_episodes": pending_covers_episodes,
        "recent_revisions": recent_revisions,
        "open_rule_suggestions": open_rule_suggestions,
        "pending_comments": pending_comments,
    }


@router.get("/collab/revisions")
def list_revisions(
    request: Request,
    episode_id: int | None = Query(None),
    entity_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """查询剧本/分镜等全字段修改留痕（改前/改后/操作人/角色/时间）"""
    require_authenticated_user(request)
    sql = """
        SELECT r.*, e.title AS episode_title, e.ep_number AS ep_number, e.project_id AS project_id
        FROM revisions r
        LEFT JOIN episodes e ON e.id = r.episode_id
        WHERE 1=1
    """
    params: list[Any] = []
    if episode_id is not None:
        sql += " AND r.episode_id = ?"
        params.append(episode_id)
    if entity_type and entity_type != "all":
        sql += " AND r.entity_type = ?"
        params.append(entity_type)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)

    with db_ctx() as conn:
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
        for r in rows:
            r["field_label"] = ALL_FIELD_LABELS.get(r["field_name"], r["field_name"])
    return {"revisions": rows, "total": len(rows)}


# ==================== 3. 脱敏创作规范只读展示 & 提建议闭环 ====================


class RuleSuggestionIn(BaseModel):
    suggestion_text: str


class RuleSuggestionPatch(BaseModel):
    status: str  # open | adopted | dismissed
    admin_reply: str = ""


@router.get("/collab/rules")
def list_sanitized_rules(request: Request) -> dict[str, Any]:
    """返回脱敏后的中文只读创作规范，并附带每条规范关联的修改建议列表"""
    require_authenticated_user(request)
    with db_ctx() as conn:
        templates = rows_to_dicts(conn.execute("SELECT * FROM prompt_templates ORDER BY key ASC").fetchall())
        all_suggestions = rows_to_dicts(conn.execute("SELECT * FROM rule_suggestions ORDER BY id DESC").fetchall())

    sugg_by_key: dict[str, list[dict[str, Any]]] = {}
    for s in all_suggestions:
        sugg_by_key.setdefault(s["template_key"], []).append(s)

    rules: list[dict[str, Any]] = []
    for t in templates:
        key = t["key"]
        suggestions = sugg_by_key.get(key, [])
        rules.append(
            {
                "key": key,
                "title": sanitize_rule_text(t.get("title") or key),
                "description": sanitize_rule_text(t.get("description") or ""),
                "content": sanitize_rule_text(t.get("content") or ""),
                "updated_at": t.get("updated_at") or "",
                "open_suggestion_count": sum(1 for x in suggestions if x.get("status") == "open"),
                "suggestions": suggestions,
            }
        )
    return {"rules": rules}


@router.post("/collab/rules/{template_key}/suggestions")
def create_rule_suggestion(template_key: str, body: RuleSuggestionIn, request: Request) -> dict[str, Any]:
    """任何已登录团队成员提交创作规范修改建议，留痕并通知管理员"""
    actor = require_authenticated_user(request)
    text = (body.suggestion_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请填写具体的修改建议内容")

    ts = now_str()
    with db_ctx() as conn:
        tpl = conn.execute("SELECT key, title FROM prompt_templates WHERE key = ?", (template_key,)).fetchone()
        if not tpl:
            raise HTTPException(status_code=404, detail="对应的创作规范不存在")
        clean_title = sanitize_rule_text(tpl["title"] or template_key)
        cur = conn.execute(
            """
            INSERT INTO rule_suggestions (
                template_key, rule_title, suggestion_text,
                actor_username, actor_name, actor_role, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                template_key,
                clean_title,
                text,
                actor["username"],
                actor["display_name"],
                actor["role"],
                ts,
            ),
        )
        row = dict(conn.execute("SELECT * FROM rule_suggestions WHERE id = ?", (cur.lastrowid,)).fetchone())

    target = "admin" if actor["role"] != "admin" else "partner"
    send_notification(
        target_role=target,
        title=f"{_brand_name()} · 规范建议：{clean_title[:24]}",
        message=f"{actor['display_name']} 提出修改建议：{text[:140]}",
        click_url="?tab=rules",
        tags=["bulb", "memo"],
        priority=3,
    )
    return {"suggestion": row}


@router.patch("/collab/suggestions/{suggestion_id}")
def handle_rule_suggestion(suggestion_id: int, body: RuleSuggestionPatch, request: Request) -> dict[str, Any]:
    """管理员处理创作规范修改建议（标记 adopted/dismissed 并回复），触发通知推送给团队成员"""
    admin = require_admin_user(request)
    status = (body.status or "").strip().lower()
    if status not in ("open", "adopted", "dismissed"):
        raise HTTPException(status_code=400, detail="status 仅支持 open / adopted / dismissed")

    ts = now_str()
    reply = (body.admin_reply or "").strip()
    with db_ctx() as conn:
        existing = conn.execute("SELECT * FROM rule_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="该规范建议不存在")
        conn.execute(
            """
            UPDATE rule_suggestions
            SET status = ?, admin_reply = ?, handled_by = ?, handled_at = ?
            WHERE id = ?
            """,
            (status, reply, admin["display_name"], ts, suggestion_id),
        )
        updated = dict(conn.execute("SELECT * FROM rule_suggestions WHERE id = ?", (suggestion_id,)).fetchone())

    status_cn = {"adopted": "已采纳", "dismissed": "暂不采纳", "open": "重新打开"}.get(status, status)
    send_notification(
        target_role="partner",
        title=f"{_brand_name()} · 规范建议{status_cn}",
        message=f"{admin['display_name']} 处理了你在《{updated['rule_title'][:20]}》的建议：{reply or status_cn}",
        click_url="?tab=rules",
        tags=["white_check_mark" if status == "adopted" else "speech_balloon"],
        priority=3,
    )
    return {"suggestion": updated}


# ==================== 4. 首尾帧 / 试卡 / 封面多人投票 ====================


class ReviewVoteIn(BaseModel):
    target_type: str  # frame | video_360p | cover
    target_key: str = ""
    choice: str = ""
    verdict: str = "approve"  # approve | change | comment
    comment: str = ""


@router.get("/episodes/{episode_id}/votes")
def list_episode_votes(
    episode_id: int,
    request: Request,
    target_type: str | None = Query(None),
) -> dict[str, Any]:
    """查询指定分集的全部团队审片投票与汇总状态"""
    actor = require_authenticated_user(request)
    with db_ctx() as conn:
        ep = conn.execute("SELECT id, title, ep_number FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        all_votes = rows_to_dicts(
            conn.execute("SELECT * FROM review_votes WHERE episode_id = ? ORDER BY id DESC", (episode_id,)).fetchall()
        )

    filtered = [v for v in all_votes if not target_type or v["target_type"] == target_type]
    return {
        "episode_id": episode_id,
        "votes": filtered,
        "summary": _summarize_votes_by_type(all_votes, str(actor.get("username") or "")),
    }


@router.post("/episodes/{episode_id}/votes")
def submit_episode_vote(episode_id: int, body: ReviewVoteIn, request: Request) -> dict[str, Any]:
    """提交首尾帧 / 试卡 / 封面投票与文字批注，自动留痕并通知协作团队成员"""
    actor = require_authenticated_user(request)
    ttype = (body.target_type or "").strip().lower()
    if ttype not in ("frame", "video_360p", "cover"):
        raise HTTPException(status_code=400, detail="target_type 仅支持 frame / video_360p / cover")
    verdict = (body.verdict or "comment").strip().lower()
    if verdict not in ("approve", "change", "comment"):
        raise HTTPException(status_code=400, detail="verdict 仅支持 approve / change / comment")

    ts = now_str()
    with db_ctx() as conn:
        ep = conn.execute("SELECT id, ep_number, title FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not ep:
            raise HTTPException(status_code=404, detail="分集不存在")
        cur = conn.execute(
            """
            INSERT INTO review_votes (
                episode_id, target_type, target_key, choice, verdict, comment,
                actor_username, actor_name, actor_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                ttype,
                (body.target_key or "").strip(),
                (body.choice or "").strip(),
                verdict,
                (body.comment or "").strip(),
                actor["username"],
                actor["display_name"],
                actor["role"],
                ts,
            ),
        )
        vote = dict(conn.execute("SELECT * FROM review_votes WHERE id = ?", (cur.lastrowid,)).fetchone())
        all_votes = rows_to_dicts(
            conn.execute("SELECT * FROM review_votes WHERE episode_id = ? ORDER BY id DESC", (episode_id,)).fetchall()
        )

    stage_cn = {"frame": "首尾帧", "video_360p": "试卡/成片", "cover": "封面"}.get(ttype, ttype)
    verdict_cn = {"approve": "赞同通过", "change": "建议修改", "comment": "补充批注"}.get(verdict, verdict)
    choice_part = f"（选择：{vote['choice']}）" if vote["choice"] else ""
    comment_part = f"：{vote['comment'][:120]}" if vote["comment"] else ""
    notify_other_party(
        actor["role"],
        title=f"{_brand_name()} · 第{ep['ep_number']}集{stage_cn}{verdict_cn}",
        message=f"{actor['display_name']} 对《{ep['title'][:22]}》{stage_cn}投票「{verdict_cn}」{choice_part}{comment_part}",
        click_url=f"?ep={episode_id}",
        tags=["ballot_box", "white_check_mark" if verdict == "approve" else "pencil2"],
        priority=3,
    )
    return {
        "vote": vote,
        "votes": all_votes,
        "summary": _summarize_votes_by_type(all_votes, str(actor.get("username") or "")),
    }


# ==================== 5. 远程协作服务端同步接收与拉回接口（/api/sync/*） ====================

_FORBIDDEN_SYNC_FILENAMES = {"studio.db", "studio.db-wal", "studio.db-shm", ".session_secret", ".env", ".secrets.env"}


def _safe_sync_rel_path(rel_path: str) -> tuple[str, Path]:
    """严格校验同步相对路径：强制落在 DATA_DIR 内，杜绝路径穿越与敏感文件覆盖"""
    raw = (rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        raise HTTPException(status_code=400, detail="rel_path 不能为空")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise HTTPException(status_code=403, detail="非法路径：禁止目录穿越")
    if parts[0] in _FORBIDDEN_SYNC_FILENAMES or any(p.startswith(".") for p in parts):
        raise HTTPException(status_code=403, detail="禁止写入受保护系统或隐藏文件")
    if parts[-1].lower().endswith((".db", ".sqlite", ".key", ".env", ".pem")):
        raise HTTPException(status_code=403, detail="禁止同步数据库或密钥类型文件")

    norm_rel = "/".join(parts)
    data_root = DATA_DIR.resolve()
    dest = (data_root / norm_rel).resolve()
    if not dest.is_relative_to(data_root):
        raise HTTPException(status_code=403, detail="目标路径越界，已拒绝")
    return norm_rel, dest


def _normalize_synced_media_ref(raw_path: Any) -> str:
    """将本地绝对路径或相对路径安全规整为 DATA_DIR 相对路径（如 media/... 或 exports/...）"""
    s = str(raw_path or "").replace("\\", "/").strip()
    if not s:
        return ""
    for marker in ("/data/media/", "/data/exports/"):
        idx = s.find(marker)
        if idx != -1:
            s = s[idx + len("/data/") :]
            break
    s = s.lstrip("/")
    if not s or ".." in s.split("/"):
        return ""
    return s


def _normalize_cover_candidates(raw: Any) -> str:
    """规整 cover_candidates_json 中每项的 path 为服务端相对路径"""
    items = _parse_json_list(raw)
    cleaned: list[Any] = []
    for it in items:
        if isinstance(it, dict):
            cp = dict(it)
            if cp.get("path"):
                cp["path"] = _normalize_synced_media_ref(cp["path"])
            cleaned.append(cp)
        else:
            cleaned.append(it)
    return json.dumps(cleaned, ensure_ascii=False)


class SyncPushBundleIn(BaseModel):
    project: dict[str, Any]
    assets: list[dict[str, Any]] = []
    episode: dict[str, Any]
    shots: list[dict[str, Any]] = []
    takes: list[dict[str, Any]] = []
    render_jobs: list[dict[str, Any]] = []
    ai_proof_path: str = ""
    frames_sheet_path: str = ""
    covers_sheet_path: str = ""
    qc_overview_path: str = ""
    web_preview_path: str = ""
    notify_stage: str = "update"  # frames_ready | video_360p_ready | master_ready | covers_ready | update | silent
    notify_note: str = ""


@router.get("/sync/check-file")
def sync_check_file(rel_path: str = Query(..., description="相对 DATA_DIR 的文件路径")) -> dict[str, Any]:
    """供本地同步客户端检查远程协作服务端是否已有同名同大小文件，实现大文件增量跳过"""
    norm_rel, dest = _safe_sync_rel_path(rel_path)
    if dest.exists() and dest.is_file():
        return {"exists": True, "rel_path": norm_rel, "size": dest.stat().st_size}
    return {"exists": False, "rel_path": norm_rel, "size": 0}


@router.post("/sync/upload-file")
async def sync_upload_file(
    rel_path: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """流式分块接收本地制片台同步的媒体/文档并原子落盘至 DATA_DIR/<rel_path>"""
    norm_rel, dest = _safe_sync_rel_path(rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.uploading.{os.getpid()}")

    hasher = hashlib.sha256()
    total_size = 0
    try:
        with tmp_path.open("wb") as fp:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB 分块，保护低内存自托管服务端
                if not chunk:
                    break
                fp.write(chunk)
                hasher.update(chunk)
                total_size += len(chunk)
        os.replace(tmp_path, dest)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"远程协作服务端保存同步文件失败：{exc}")

    return {
        "ok": True,
        "rel_path": norm_rel,
        "size": total_size,
        "sha256": hasher.hexdigest(),
    }


@router.post("/sync/push-bundle")
def sync_push_bundle(body: SyncPushBundleIn) -> dict[str, Any]:
    """
    安全 upsert 同步数据包入远程协作服务端 SQLite：
    - 严禁覆盖远程协作服务端时间戳更新的剧本/分镜文本修改（防丢失团队成员批改）；
    - 严禁清空远程端 revisions / review_votes / rule_suggestions；
    - 严禁接收或写入任何本地密钥、quota_state 或 api_usage_logs；
    - 按 notify_stage 自动向团队成员触发可选通知推送。
    """
    ts = now_str()
    p_in = body.project or {}
    e_in = body.episode or {}

    slug = str(p_in.get("slug") or "").strip()
    proj_name = str(p_in.get("name") or slug or "未命名剧目").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="project.slug 不能为空")

    ep_number = int(e_in.get("ep_number") or 1)
    ep_title = str(e_in.get("title") or f"第 {ep_number} 集").strip()

    preserved_cloud_fields: list[str] = []
    preserved_cloud_shots: list[int] = []
    upserted_assets = 0
    upserted_shots = 0
    upserted_takes = 0

    with db_ctx() as conn:
        # 1. Upsert Project（按 slug 稳定对齐）
        existing_p = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        season_outline_str = _sanitize_json_payload_str(p_in.get("season_outline"), "[]")
        ledger_str = _sanitize_json_payload_str(p_in.get("continuity_ledger"), "{}")
        p_upd = str(p_in.get("updated_at") or ts)

        if existing_p is None:
            cur = conn.execute(
                """
                INSERT INTO projects (
                    name, slug, track, visual_mode, aspect_ratio, target_duration_sec,
                    global_style_prompt, description, season_outline, continuity_ledger,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proj_name,
                    slug,
                    str(p_in.get("track") or "剧情微短剧"),
                    str(p_in.get("visual_mode") or "电影写实 + 3D 虚拟搭档"),
                    str(p_in.get("aspect_ratio") or "9:16"),
                    int(p_in.get("target_duration_sec") or 75),
                    str(p_in.get("global_style_prompt") or ""),
                    str(p_in.get("description") or ""),
                    season_outline_str,
                    ledger_str,
                    str(p_in.get("created_at") or ts),
                    p_upd,
                ),
            )
            cloud_project_id = int(cur.lastrowid)
        else:
            cloud_project_id = int(existing_p["id"])
            conn.execute(
                """
                UPDATE projects
                SET name = ?, track = ?, visual_mode = ?, aspect_ratio = ?,
                    target_duration_sec = ?, global_style_prompt = ?, description = ?,
                    season_outline = ?, continuity_ledger = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    proj_name,
                    str(p_in.get("track") or existing_p["track"]),
                    str(p_in.get("visual_mode") or existing_p["visual_mode"]),
                    str(p_in.get("aspect_ratio") or existing_p["aspect_ratio"]),
                    int(p_in.get("target_duration_sec") or existing_p["target_duration_sec"] or 75),
                    str(p_in.get("global_style_prompt") or existing_p["global_style_prompt"] or ""),
                    str(p_in.get("description") or existing_p["description"] or ""),
                    season_outline_str,
                    ledger_str,
                    ts,
                    cloud_project_id,
                ),
            )

        # 2. Upsert Assets（按 project_id + flow_tag/name 对齐，并建立本地 ID -> 远程 ID 映射）
        local_to_cloud_asset_id: dict[int, int] = {}
        for a_in in body.assets or []:
            aname = str(a_in.get("name") or "").strip()
            if not aname:
                continue
            ftag = str(a_in.get("flow_tag") or "").strip()
            existing_a = None
            if ftag:
                existing_a = conn.execute(
                    "SELECT * FROM assets WHERE project_id = ? AND flow_tag = ?",
                    (cloud_project_id, ftag),
                ).fetchone()
            if existing_a is None:
                existing_a = conn.execute(
                    "SELECT * FROM assets WHERE project_id = ? AND name = ?",
                    (cloud_project_id, aname),
                ).fetchone()

            raw_img = _normalize_synced_media_ref(a_in.get("raw_image_path"))
            clean_img = _normalize_synced_media_ref(a_in.get("clean_image_path"))
            voice_smp = _normalize_synced_media_ref(a_in.get("voice_clone_sample_path"))
            clean_asset_notes = sanitize_rule_text(strip_local_abs_paths(str(a_in.get("notes") or "")))
            a_upd = str(a_in.get("updated_at") or ts)

            if existing_a is None:
                cur = conn.execute(
                    """
                    INSERT INTO assets (
                        project_id, asset_type, name, flow_tag, raw_image_path, clean_image_path,
                        en_anchor_prompt, negative_guard_en, voice_mode, voice_en_desc,
                        voice_clone_sample_path, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cloud_project_id,
                        str(a_in.get("asset_type") or "character"),
                        aname,
                        ftag or f"@{aname}",
                        raw_img,
                        clean_img,
                        str(a_in.get("en_anchor_prompt") or ""),
                        str(a_in.get("negative_guard_en") or ""),
                        str(a_in.get("voice_mode") or "flow_sfx"),
                        str(a_in.get("voice_en_desc") or ""),
                        voice_smp,
                        clean_asset_notes,
                        str(a_in.get("created_at") or ts),
                        a_upd,
                    ),
                )
                cloud_aid = int(cur.lastrowid)
            else:
                cloud_aid = int(existing_a["id"])
                conn.execute(
                    """
                    UPDATE assets
                    SET asset_type = ?, name = ?, flow_tag = ?,
                        raw_image_path = COALESCE(NULLIF(?, ''), raw_image_path),
                        clean_image_path = COALESCE(NULLIF(?, ''), clean_image_path),
                        en_anchor_prompt = ?, negative_guard_en = ?,
                        voice_mode = ?, voice_en_desc = ?,
                        voice_clone_sample_path = COALESCE(NULLIF(?, ''), voice_clone_sample_path),
                        notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(a_in.get("asset_type") or existing_a["asset_type"]),
                        aname,
                        ftag or existing_a["flow_tag"],
                        raw_img,
                        clean_img,
                        str(a_in.get("en_anchor_prompt") or existing_a["en_anchor_prompt"] or ""),
                        str(a_in.get("negative_guard_en") or existing_a["negative_guard_en"] or ""),
                        str(a_in.get("voice_mode") or existing_a["voice_mode"] or "flow_sfx"),
                        str(a_in.get("voice_en_desc") or existing_a["voice_en_desc"] or ""),
                        voice_smp,
                        clean_asset_notes or sanitize_rule_text(strip_local_abs_paths(str(existing_a["notes"] or ""))),
                        max(a_upd, existing_a["updated_at"] or ""),
                        cloud_aid,
                    ),
                )
            if a_in.get("id") is not None:
                try:
                    local_to_cloud_asset_id[int(a_in["id"])] = cloud_aid
                except (TypeError, ValueError):
                    pass
            upserted_assets += 1

        # 3. Upsert Episode（按 project_id + ep_number 对齐，严格保护远程端较新的文本改动）
        existing_ep = conn.execute(
            "SELECT * FROM episodes WHERE project_id = ? AND ep_number = ?",
            (cloud_project_id, ep_number),
        ).fetchone()

        in_ep_updated = str(e_in.get("updated_at") or "")
        web_preview = _normalize_synced_media_ref(body.web_preview_path or e_in.get("web_preview_path"))
        latest_out = _normalize_synced_media_ref(e_in.get("latest_output_path"))
        bgm_file = _normalize_synced_media_ref(e_in.get("bgm_file_path"))
        sel_cover = _normalize_synced_media_ref(e_in.get("selected_cover_path"))
        frames_sheet = _normalize_synced_media_ref(body.frames_sheet_path or e_in.get("frames_sheet_path"))
        covers_sheet = _normalize_synced_media_ref(body.covers_sheet_path or e_in.get("covers_sheet_path"))
        qc_overview = _normalize_synced_media_ref(body.qc_overview_path or e_in.get("qc_overview_path"))
        ai_proof = _normalize_synced_media_ref(body.ai_proof_path or e_in.get("ai_proof_path"))
        cover_cands_raw = e_in.get("cover_candidates") if "cover_candidates" in e_in else e_in.get("cover_candidates_json")
        cover_cands_json = _normalize_cover_candidates(cover_cands_raw) if cover_cands_raw is not None else "[]"

        if existing_ep is None:
            cur = conn.execute(
                """
                INSERT INTO episodes (
                    project_id, ep_number, title, topic_id, hook_0_3s, ending_cta, next_ep_hook,
                    script_text, continuity_summary, bgm_prompt, bgm_file_path, bgm_volume,
                    status, latest_output_path, web_preview_path, sync_updated_at,
                    frames_sheet_path, covers_sheet_path, qc_overview_path, ai_proof_path,
                    vo_mode, cover_candidates_json, selected_cover_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cloud_project_id,
                    ep_number,
                    ep_title,
                    e_in.get("topic_id"),
                    str(e_in.get("hook_0_3s") or ""),
                    str(e_in.get("ending_cta") or ""),
                    str(e_in.get("next_ep_hook") or ""),
                    str(e_in.get("script_text") or ""),
                    str(e_in.get("continuity_summary") or ""),
                    str(e_in.get("bgm_prompt") or ""),
                    bgm_file,
                    float(e_in.get("bgm_volume") if e_in.get("bgm_volume") is not None else 0.25),
                    str(e_in.get("status") or "draft"),
                    latest_out,
                    web_preview,
                    ts,
                    frames_sheet,
                    covers_sheet,
                    qc_overview,
                    ai_proof,
                    str(e_in.get("vo_mode") or "pending"),
                    cover_cands_json,
                    sel_cover,
                    str(e_in.get("created_at") or ts),
                    in_ep_updated or ts,
                ),
            )
            cloud_ep_id = int(cur.lastrowid)
        else:
            cloud_ep_id = int(existing_ep["id"])
            cloud_ep_updated = str(existing_ep["updated_at"] or "")
            latest_rev = (
                conn.execute(
                    "SELECT MAX(created_at) AS m FROM revisions WHERE entity_type = 'episode' AND episode_id = ?",
                    (cloud_ep_id,),
                ).fetchone()["m"]
                or ""
            )
            cloud_text_newer = bool(in_ep_updated and (cloud_ep_updated > in_ep_updated or latest_rev > in_ep_updated))

            text_fields = {
                "title": ep_title,
                "hook_0_3s": str(e_in.get("hook_0_3s") or ""),
                "ending_cta": str(e_in.get("ending_cta") or ""),
                "next_ep_hook": str(e_in.get("next_ep_hook") or ""),
                "script_text": str(e_in.get("script_text") or ""),
                "continuity_summary": str(e_in.get("continuity_summary") or ""),
                "bgm_prompt": str(e_in.get("bgm_prompt") or ""),
            }
            update_cols: dict[str, Any] = {"sync_updated_at": ts}
            if cloud_text_newer:
                for k, new_v in text_fields.items():
                    if str(existing_ep[k] or "") != new_v:
                        preserved_cloud_fields.append(k)
            else:
                update_cols.update(text_fields)
                if in_ep_updated:
                    update_cols["updated_at"] = in_ep_updated

            if latest_out:
                update_cols["latest_output_path"] = latest_out
            if web_preview:
                update_cols["web_preview_path"] = web_preview
            if bgm_file:
                update_cols["bgm_file_path"] = bgm_file
            if frames_sheet:
                update_cols["frames_sheet_path"] = frames_sheet
            if covers_sheet:
                update_cols["covers_sheet_path"] = covers_sheet
            if qc_overview:
                update_cols["qc_overview_path"] = qc_overview
            if ai_proof:
                update_cols["ai_proof_path"] = ai_proof
            if cover_cands_raw is not None and _parse_json_list(cover_cands_json):
                update_cols["cover_candidates_json"] = cover_cands_json
            if sel_cover and not cloud_text_newer:
                update_cols["selected_cover_path"] = sel_cover
            if e_in.get("vo_mode"):
                update_cols["vo_mode"] = str(e_in["vo_mode"])
            if e_in.get("bgm_volume") is not None:
                update_cols["bgm_volume"] = float(e_in["bgm_volume"])
            if e_in.get("status") and not cloud_text_newer:
                update_cols["status"] = str(e_in["status"])

            cols_sql = ", ".join(f"{k} = ?" for k in update_cols)
            conn.execute(f"UPDATE episodes SET {cols_sql} WHERE id = ?", list(update_cols.values()) + [cloud_ep_id])

        # 4. Upsert Shots（按 episode_id + shot_no 对齐，保护远程端较新文本改动）
        local_to_cloud_shot_id: dict[int, int] = {}
        shot_no_to_cloud_id: dict[int, int] = {}
        nested_takes: list[tuple[int, dict[str, Any]]] = []

        for idx, s_in in enumerate(body.shots or [], start=1):
            shot_no = int(s_in.get("shot_no") or idx)
            raw_ref_ids = _parse_json_list(s_in.get("ref_asset_ids"))
            mapped_ref_ids = [
                local_to_cloud_asset_id.get(int(aid), int(aid))
                for aid in raw_ref_ids
                if str(aid).isdigit()
            ]
            start_f = _normalize_synced_media_ref(s_in.get("start_frame_path"))
            end_f = _normalize_synced_media_ref(s_in.get("end_frame_path"))
            vo_f = _normalize_synced_media_ref(s_in.get("vo_file_path"))
            qc_strip = _normalize_synced_media_ref(s_in.get("qc_strip_path"))
            qc_rep = sanitize_qc_report_json(s_in.get("qc_report_json"))
            in_s_updated = str(s_in.get("updated_at") or "")

            existing_s = conn.execute(
                "SELECT * FROM shots WHERE episode_id = ? AND shot_no = ?",
                (cloud_ep_id, shot_no),
            ).fetchone()

            if existing_s is None:
                cur = conn.execute(
                    """
                    INSERT INTO shots (
                        episode_id, shot_no, shot_title, camera_type, duration_sec,
                        flow_mode, ref_asset_ids, cn_action_desc, cn_dialogue, subtitle_text,
                        frame_prompt_en, end_frame_prompt_en, video_prompt_en,
                        start_frame_path, end_frame_path, audio_strategy, upscale_target,
                        upscale_done, trim_start, trim_end, sort_order,
                        vo_file_path, vo_source, qc_strip_path, qc_report_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cloud_ep_id,
                        shot_no,
                        str(s_in.get("shot_title") or f"镜头{shot_no}"),
                        str(s_in.get("camera_type") or "中景"),
                        int(s_in.get("duration_sec") or 6),
                        str(s_in.get("flow_mode") or "omni12_i2v"),
                        json.dumps(mapped_ref_ids),
                        str(s_in.get("cn_action_desc") or ""),
                        str(s_in.get("cn_dialogue") or ""),
                        str(s_in.get("subtitle_text") or ""),
                        str(s_in.get("frame_prompt_en") or ""),
                        str(s_in.get("end_frame_prompt_en") or ""),
                        str(s_in.get("video_prompt_en") or ""),
                        start_f,
                        end_f,
                        str(s_in.get("audio_strategy") or "keep_flow_sfx"),
                        str(s_in.get("upscale_target") or "1080p_free"),
                        int(s_in.get("upscale_done") or 0),
                        float(s_in.get("trim_start") or 0.0),
                        float(s_in.get("trim_end") or 0.0),
                        int(s_in.get("sort_order") or idx),
                        vo_f,
                        str(s_in.get("vo_source") or ""),
                        qc_strip,
                        qc_rep,
                        str(s_in.get("created_at") or ts),
                        in_s_updated or ts,
                    ),
                )
                cloud_sid = int(cur.lastrowid)
            else:
                cloud_sid = int(existing_s["id"])
                cloud_s_updated = str(existing_s["updated_at"] or "")
                latest_s_rev = (
                    conn.execute(
                        "SELECT MAX(created_at) AS m FROM revisions WHERE entity_type = 'shot' AND entity_id = ?",
                        (cloud_sid,),
                    ).fetchone()["m"]
                    or ""
                )
                cloud_s_newer = bool(in_s_updated and (cloud_s_updated > in_s_updated or latest_s_rev > in_s_updated))

                s_upd_cols: dict[str, Any] = {
                    "ref_asset_ids": json.dumps(mapped_ref_ids),
                    "sort_order": int(s_in.get("sort_order") or existing_s["sort_order"] or idx),
                }
                if cloud_s_newer:
                    preserved_cloud_shots.append(shot_no)
                else:
                    s_upd_cols.update(
                        {
                            "shot_title": str(s_in.get("shot_title") or existing_s["shot_title"]),
                            "camera_type": str(s_in.get("camera_type") or existing_s["camera_type"]),
                            "duration_sec": int(s_in.get("duration_sec") or existing_s["duration_sec"] or 6),
                            "cn_action_desc": str(s_in.get("cn_action_desc") or ""),
                            "cn_dialogue": str(s_in.get("cn_dialogue") or ""),
                            "subtitle_text": str(s_in.get("subtitle_text") or ""),
                            "frame_prompt_en": str(s_in.get("frame_prompt_en") or ""),
                            "end_frame_prompt_en": str(s_in.get("end_frame_prompt_en") or ""),
                            "video_prompt_en": str(s_in.get("video_prompt_en") or ""),
                            "audio_strategy": str(s_in.get("audio_strategy") or existing_s["audio_strategy"]),
                            "flow_mode": str(s_in.get("flow_mode") or existing_s["flow_mode"]),
                        }
                    )
                    if in_s_updated:
                        s_upd_cols["updated_at"] = in_s_updated

                if start_f:
                    s_upd_cols["start_frame_path"] = start_f
                if end_f:
                    s_upd_cols["end_frame_path"] = end_f
                if vo_f:
                    s_upd_cols["vo_file_path"] = vo_f
                if s_in.get("vo_source"):
                    s_upd_cols["vo_source"] = str(s_in["vo_source"])
                if qc_strip:
                    s_upd_cols["qc_strip_path"] = qc_strip
                if qc_rep and qc_rep != "{}":
                    s_upd_cols["qc_report_json"] = qc_rep
                if s_in.get("trim_start") is not None:
                    s_upd_cols["trim_start"] = float(s_in["trim_start"])
                if s_in.get("trim_end") is not None:
                    s_upd_cols["trim_end"] = float(s_in["trim_end"])

                cols_sql = ", ".join(f"{k} = ?" for k in s_upd_cols)
                conn.execute(f"UPDATE shots SET {cols_sql} WHERE id = ?", list(s_upd_cols.values()) + [cloud_sid])

            if s_in.get("id") is not None:
                try:
                    local_to_cloud_shot_id[int(s_in["id"])] = cloud_sid
                except (TypeError, ValueError):
                    pass
            shot_no_to_cloud_id[shot_no] = cloud_sid
            for tk in s_in.get("takes") or []:
                if isinstance(tk, dict):
                    nested_takes.append((cloud_sid, tk))
            upserted_shots += 1

        # 5. Upsert Takes（兼容 shot 内嵌 takes 与顶层 takes 列表）
        all_takes_pairs: list[tuple[int, dict[str, Any]]] = list(nested_takes)
        for tk in body.takes or []:
            if not isinstance(tk, dict):
                continue
            target_sid = None
            if tk.get("shot_no") is not None and int(tk["shot_no"]) in shot_no_to_cloud_id:
                target_sid = shot_no_to_cloud_id[int(tk["shot_no"])]
            elif tk.get("shot_id") is not None and int(tk["shot_id"]) in local_to_cloud_shot_id:
                target_sid = local_to_cloud_shot_id[int(tk["shot_id"])]
            if target_sid is not None:
                all_takes_pairs.append((target_sid, tk))

        for cloud_sid, tk in all_takes_pairs:
            fpath = _normalize_synced_media_ref(tk.get("file_path"))
            if not fpath:
                continue
            mtype = str(tk.get("media_type") or "video")
            is_final = 1 if int(tk.get("is_final") or 0) else 0
            res_lbl = str(tk.get("resolution_label") or "")
            clean_orig_name = sanitize_take_filename(str(tk.get("original_filename") or ""), fpath, res_lbl)
            clean_notes = sanitize_take_notes(str(tk.get("notes") or ""), res_lbl)
            if is_final:
                conn.execute(
                    "UPDATE takes SET is_final = 0 WHERE shot_id = ? AND media_type = ? AND file_path != ?",
                    (cloud_sid, mtype, fpath),
                )
            existing_t = conn.execute(
                "SELECT id FROM takes WHERE shot_id = ? AND file_path = ?",
                (cloud_sid, fpath),
            ).fetchone()
            if existing_t is None:
                conn.execute(
                    """
                    INSERT INTO takes (
                        shot_id, media_type, file_path, original_filename,
                        width, height, duration_sec, resolution_label, rating, is_final, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cloud_sid,
                        mtype,
                        fpath,
                        clean_orig_name,
                        int(tk.get("width") or 0),
                        int(tk.get("height") or 0),
                        float(tk.get("duration_sec") or 0.0),
                        res_lbl,
                        int(tk.get("rating") or 0),
                        is_final,
                        clean_notes,
                        str(tk.get("created_at") or ts),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE takes
                    SET media_type = ?, original_filename = ?, width = ?, height = ?,
                        duration_sec = ?, resolution_label = ?, rating = ?, is_final = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        mtype,
                        clean_orig_name,
                        int(tk.get("width") or 0),
                        int(tk.get("height") or 0),
                        float(tk.get("duration_sec") or 0.0),
                        res_lbl,
                        int(tk.get("rating") or 0),
                        is_final,
                        clean_notes,
                        existing_t["id"],
                    ),
                )
            upserted_takes += 1

        # 6. 记录 AI 原创证明 / 渲染产出记录
        jobs_to_upsert = list(body.render_jobs or [])
        if ai_proof:
            jobs_to_upsert.append(
                {
                    "job_type": "ai_proof",
                    "target_host": "cloud_sync",
                    "status": "done",
                    "output_path": ai_proof,
                    "log_text": "已由本地制片台同步 AI 原创制作证明",
                }
            )
        for job in jobs_to_upsert:
            j_out = _normalize_synced_media_ref(job.get("output_path"))
            j_type = str(job.get("job_type") or "sync_artifact")
            clean_j_log = sanitize_rule_text(strip_local_abs_paths(str(job.get("log_text") or "")))
            if not j_out:
                continue
            ex_j = conn.execute(
                "SELECT id FROM render_jobs WHERE episode_id = ? AND job_type = ? AND output_path = ?",
                (cloud_ep_id, j_type, j_out),
            ).fetchone()
            if ex_j is None:
                conn.execute(
                    """
                    INSERT INTO render_jobs (
                        episode_id, job_type, target_host, status, output_path, log_text, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cloud_ep_id,
                        j_type,
                        str(job.get("target_host") or "cloud_sync"),
                        str(job.get("status") or "done"),
                        j_out,
                        clean_j_log,
                        str(job.get("created_at") or ts),
                        ts,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE render_jobs SET log_text = ?, updated_at = ? WHERE id = ?",
                    (clean_j_log, ts, ex_j["id"]),
                )

    # 7. 按节点类型触发可选通知推送给团队成员
    brand = _brand_name()
    stage = (body.notify_stage or "update").strip().lower()
    stage_meta: dict[str, tuple[str, str, list[str]]] = {
        "frames_ready": (
            f"{brand} · 第{ep_number}集首尾帧已就绪",
            f"《{ep_title}》首尾帧总览图已同步至远程协作服务端，点开即可挑图投票。",
            ["framed_picture", "clapper"],
        ),
        "video_360p_ready": (
            f"{brand} · 第{ep_number}集试卡已出",
            f"《{ep_title}》动态试卡已同步，邀团队成员在线审片并提修改意见。",
            ["film_strip", "eyes"],
        ),
        "covers_ready": (
            f"{brand} · 第{ep_number}集封面候选已出",
            f"《{ep_title}》双比例封面候选已同步，欢迎投票选出主封面。",
            ["art", "ballot_box"],
        ),
        "master_ready": (
            f"{brand} · 第{ep_number}集终版成片已同步",
            f"《{ep_title}》流畅预览版、高清终版与 AI 原创证明已上线，随时审片下载。",
            ["movie_camera", "tada"],
        ),
        "update": (
            f"{brand} · 第{ep_number}集内容已更新",
            f"《{ep_title}》最新进度已同步到远程协作看板。",
            ["bell"],
        ),
    }

    notified = False
    if stage not in ("silent", "none", ""):
        nt_title, nt_msg, nt_tags = stage_meta.get(stage, stage_meta["update"])
        if body.notify_note.strip():
            nt_msg = f"{nt_msg} 备注：{body.notify_note.strip()[:120]}"
        notified = send_notification(
            target_role="partner",
            title=nt_title,
            message=nt_msg,
            click_url=f"?ep={cloud_ep_id}",
            tags=nt_tags,
            priority=3,
        )

    return {
        "ok": True,
        "project_id": cloud_project_id,
        "episode_id": cloud_ep_id,
        "upserted_assets": upserted_assets,
        "upserted_shots": upserted_shots,
        "upserted_takes": upserted_takes,
        "preserved_cloud_fields": preserved_cloud_fields,
        "preserved_cloud_shots": preserved_cloud_shots,
        "notified": notified,
        "sync_updated_at": ts,
    }


@router.get("/sync/pull-changes")
def sync_pull_changes(since: str = Query("", description="增量起始时间 YYYY-MM-DD HH:MM:SS")) -> dict[str, Any]:
    """供本地制片台一键拉回远程协作服务端最新剧本/分镜修改、留痕、投票、规范建议、新立项选题与评论回复留痕"""
    since_clean = (since or "").strip()
    server_time = now_str()

    with db_ctx() as conn:
        if since_clean:
            episodes = rows_to_dicts(
                conn.execute(
                    """
                    SELECT e.id, e.project_id, p.slug AS project_slug, p.name AS project_name,
                           e.ep_number, e.title, e.hook_0_3s, e.ending_cta, e.next_ep_hook,
                           e.script_text, e.continuity_summary, e.bgm_prompt,
                           e.selected_cover_path, e.status, e.updated_at, e.sync_updated_at
                    FROM episodes e
                    JOIN projects p ON p.id = e.project_id
                    WHERE e.updated_at > ?
                    ORDER BY e.updated_at ASC, e.id ASC
                    """,
                    (since_clean,),
                ).fetchall()
            )
            shots = rows_to_dicts(
                conn.execute(
                    """
                    SELECT s.id, s.episode_id, p.slug AS project_slug, e.ep_number,
                           s.shot_no, s.shot_title, s.camera_type, s.duration_sec,
                           s.cn_action_desc, s.cn_dialogue, s.subtitle_text,
                           s.frame_prompt_en, s.end_frame_prompt_en, s.video_prompt_en,
                           s.audio_strategy, s.updated_at
                    FROM shots s
                    JOIN episodes e ON e.id = s.episode_id
                    JOIN projects p ON p.id = e.project_id
                    WHERE s.updated_at > ?
                    ORDER BY s.updated_at ASC, s.id ASC
                    """,
                    (since_clean,),
                ).fetchall()
            )
            revisions = rows_to_dicts(
                conn.execute(
                    """
                    SELECT r.*, e.ep_number, e.title AS episode_title, p.slug AS project_slug
                    FROM revisions r
                    LEFT JOIN episodes e ON e.id = r.episode_id
                    LEFT JOIN projects p ON p.id = e.project_id
                    WHERE r.created_at > ?
                    ORDER BY r.id ASC
                    """,
                    (since_clean,),
                ).fetchall()
            )
            review_votes = rows_to_dicts(
                conn.execute(
                    """
                    SELECT v.*, e.ep_number, e.title AS episode_title, p.slug AS project_slug
                    FROM review_votes v
                    JOIN episodes e ON e.id = v.episode_id
                    JOIN projects p ON p.id = e.project_id
                    WHERE v.created_at > ?
                    ORDER BY v.id ASC
                    """,
                    (since_clean,),
                ).fetchall()
            )
            rule_suggestions = rows_to_dicts(
                conn.execute(
                    """
                    SELECT * FROM rule_suggestions
                    WHERE created_at > ? OR (handled_at != '' AND handled_at > ?)
                    ORDER BY id ASC
                    """,
                    (since_clean, since_clean),
                ).fetchall()
            )
            topics = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM topics WHERE updated_at > ? ORDER BY id ASC",
                    (since_clean,),
                ).fetchall()
            )
            comment_drafts = rows_to_dicts(
                conn.execute(
                    """
                    SELECT * FROM comment_drafts
                    WHERE created_at > ? OR (replied_at != '' AND replied_at > ?)
                    ORDER BY id ASC
                    """,
                    (since_clean, since_clean),
                ).fetchall()
            )
            publish_logs = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM publish_logs WHERE updated_at > ? ORDER BY id ASC",
                    (since_clean,),
                ).fetchall()
            )
        else:
            episodes = rows_to_dicts(
                conn.execute(
                    """
                    SELECT e.id, e.project_id, p.slug AS project_slug, p.name AS project_name,
                           e.ep_number, e.title, e.hook_0_3s, e.ending_cta, e.next_ep_hook,
                           e.script_text, e.continuity_summary, e.bgm_prompt,
                           e.selected_cover_path, e.status, e.updated_at, e.sync_updated_at
                    FROM episodes e
                    JOIN projects p ON p.id = e.project_id
                    ORDER BY e.updated_at DESC, e.id DESC
                    LIMIT 50
                    """
                ).fetchall()
            )
            shots = rows_to_dicts(
                conn.execute(
                    """
                    SELECT s.id, s.episode_id, p.slug AS project_slug, e.ep_number,
                           s.shot_no, s.shot_title, s.camera_type, s.duration_sec,
                           s.cn_action_desc, s.cn_dialogue, s.subtitle_text,
                           s.frame_prompt_en, s.end_frame_prompt_en, s.video_prompt_en,
                           s.audio_strategy, s.updated_at
                    FROM shots s
                    JOIN episodes e ON e.id = s.episode_id
                    JOIN projects p ON p.id = e.project_id
                    ORDER BY s.updated_at DESC, s.id DESC
                    LIMIT 200
                    """
                ).fetchall()
            )
            revisions = rows_to_dicts(
                conn.execute(
                    """
                    SELECT r.*, e.ep_number, e.title AS episode_title, p.slug AS project_slug
                    FROM revisions r
                    LEFT JOIN episodes e ON e.id = r.episode_id
                    LEFT JOIN projects p ON p.id = e.project_id
                    ORDER BY r.id DESC
                    LIMIT 100
                    """
                ).fetchall()
            )
            review_votes = rows_to_dicts(
                conn.execute(
                    """
                    SELECT v.*, e.ep_number, e.title AS episode_title, p.slug AS project_slug
                    FROM review_votes v
                    JOIN episodes e ON e.id = v.episode_id
                    JOIN projects p ON p.id = e.project_id
                    ORDER BY v.id DESC
                    LIMIT 100
                    """
                ).fetchall()
            )
            rule_suggestions = rows_to_dicts(
                conn.execute("SELECT * FROM rule_suggestions ORDER BY id DESC LIMIT 100").fetchall()
            )
            topics = rows_to_dicts(
                conn.execute("SELECT * FROM topics ORDER BY id DESC LIMIT 100").fetchall()
            )
            comment_drafts = rows_to_dicts(
                conn.execute("SELECT * FROM comment_drafts ORDER BY id DESC LIMIT 100").fetchall()
            )
            publish_logs = rows_to_dicts(
                conn.execute("SELECT * FROM publish_logs ORDER BY id DESC LIMIT 100").fetchall()
            )

    for rev in revisions:
        rev["field_label"] = ALL_FIELD_LABELS.get(rev["field_name"], rev["field_name"])

    return {
        "server_time": server_time,
        "since": since_clean,
        "episodes": episodes,
        "shots": shots,
        "revisions": revisions,
        "review_votes": review_votes,
        "rule_suggestions": rule_suggestions,
        "topics": topics,
        "comment_drafts": comment_drafts,
        "publish_logs": publish_logs,
    }
