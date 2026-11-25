"""
发布数据复盘、半自动评论回复卡片（人工审核复制 / 一键转选题，绝不自动发评）、
四平台差异化发布文案生成、系统设置与订阅档位预设校准、三合一额度监控与可编辑 Prompt 规则模板路由。
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import get_current_actor
from ..config import (
    CLOUD_SAFE_UPDATE_KEYS,
    SUBSCRIPTION_PRESETS,
    get_public_config,
    is_cloud_collab,
    load_config,
    update_config,
)
from ..db import DEFAULT_PROMPT_TEMPLATES, db_ctx, now_str, rows_to_dicts
from ..llm import (
    draft_comment_replies_with_ai,
    generate_multi_platform_copy_with_ai,
    list_available_gemini_models,
    review_analytics_with_ai,
)
from ..net import make_cn_async_client
from ..notifier import notify_other_party
from ..worker_colab import (
    finish_colab_oauth,
    logout_colab_oauth,
    probe_colab,
    start_colab_oauth,
    stop_colab_session,
)

try:
    from ..watcher import downloads_inbox
except ImportError:  # 无 watchdog 极简环境容错降级
    downloads_inbox = None  # type: ignore[assignment]

router = APIRouter(prefix="/api", tags=["analytics_settings"])


def _brand_name() -> str:
    return (load_config().get("BRAND_NAME") or "反重力视频工坊").strip() or "反重力视频工坊"


# ==================== 发布与数据复盘 ====================


class PublishLogIn(BaseModel):
    project_id: int | None = None
    episode_id: int | None = None
    platform: str = "微信视频号"
    post_title: str
    published_at: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    favorites: int = 0
    completion_rate: float = 0.0
    moments_feedback: str = ""


class PublishLogPatch(BaseModel):
    platform: str | None = None
    post_title: str | None = None
    published_at: str | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    favorites: int | None = None
    completion_rate: float | None = None
    moments_feedback: str | None = None


class AnalyticsReviewRequest(BaseModel):
    log_ids: list[int] | None = None
    provider: str | None = None


@router.get("/publish-logs")
def list_publish_logs() -> dict[str, Any]:
    with db_ctx() as conn:
        logs = rows_to_dicts(conn.execute("SELECT * FROM publish_logs ORDER BY id DESC").fetchall())
        return {"logs": logs}


@router.post("/publish-logs")
def create_publish_log(body: PublishLogIn) -> dict[str, Any]:
    ts = now_str()
    pub_at = body.published_at.strip() or ts
    with db_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO publish_logs (
                project_id, episode_id, platform, post_title, published_at,
                views, likes, comments, shares, favorites, completion_rate,
                moments_feedback, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.project_id,
                body.episode_id,
                body.platform,
                body.post_title.strip(),
                pub_at,
                body.views,
                body.likes,
                body.comments,
                body.shares,
                body.favorites,
                body.completion_rate,
                body.moments_feedback,
                ts,
                ts,
            ),
        )
        row = dict(conn.execute("SELECT * FROM publish_logs WHERE id = ?", (cur.lastrowid,)).fetchone())
        return {"log": row}


@router.patch("/publish-logs/{log_id}")
def update_publish_log(log_id: int, body: PublishLogPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="无修改字段")
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    with db_ctx() as conn:
        conn.execute(f"UPDATE publish_logs SET {cols} WHERE id = ?", list(data.values()) + [log_id])
        row = conn.execute("SELECT * FROM publish_logs WHERE id = ?", (log_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发布记录不存在")
        return {"log": dict(row)}


@router.delete("/publish-logs/{log_id}")
def delete_publish_log(log_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM publish_logs WHERE id = ?", (log_id,))
    return {"ok": True}


@router.post("/publish-logs/ai-review")
def ai_review_publish_logs(body: AnalyticsReviewRequest) -> dict[str, Any]:
    with db_ctx() as conn:
        if body.log_ids:
            qmarks = ",".join("?" for _ in body.log_ids)
            logs = rows_to_dicts(
                conn.execute(f"SELECT * FROM publish_logs WHERE id IN ({qmarks}) ORDER BY id DESC", body.log_ids).fetchall()
            )
        else:
            logs = rows_to_dicts(conn.execute("SELECT * FROM publish_logs ORDER BY id DESC LIMIT 10").fetchall())
    if not logs:
        raise HTTPException(status_code=400, detail="请先录入至少 1 条发布数据，再生成 AI 复盘建议。")
    try:
        review_text, used_model = review_analytics_with_ai(logs, provider=body.provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with db_ctx() as conn:
        conn.execute("UPDATE publish_logs SET ai_review = ?, updated_at = ? WHERE id = ?", (review_text, now_str(), logs[0]["id"]))
    return {"used_model": used_model, "review": review_text}


# ==================== 半自动评论回复卡片（0 自动发评） ====================


class CommentDraftRequest(BaseModel):
    fan_comment: str
    platform: str = "抖音"
    episode_context: str = ""
    tone: str = "balanced"
    publish_log_id: int | None = None
    episode_id: int | None = None
    provider: str | None = None


@router.get("/comments")
def list_comment_drafts() -> dict[str, Any]:
    with db_ctx() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM comment_drafts ORDER BY id DESC LIMIT 40").fetchall())
        for r in rows:
            try:
                r["drafts"] = json.loads(r.get("drafts_json") or "[]")
            except Exception:
                r["drafts"] = []
        return {"items": rows}


@router.post("/comments/draft")
def generate_comment_draft(body: CommentDraftRequest) -> dict[str, Any]:
    comment = body.fan_comment.strip()
    if not comment:
        raise HTTPException(status_code=400, detail="请先粘贴一条观众评论")
    try:
        out, used_model = draft_comment_replies_with_ai(
            fan_comment=comment,
            platform=body.platform,
            episode_context=body.episode_context,
            tone_pref=body.tone,
            provider=body.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    drafts = out.get("drafts") or []
    topic_idea = str(out.get("topic_idea") or "").strip()
    ts = now_str()
    with db_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO comment_drafts (
                publish_log_id, episode_id, platform, fan_comment, tone,
                drafts_json, topic_idea, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.publish_log_id,
                body.episode_id,
                body.platform,
                comment,
                body.tone,
                json.dumps(drafts, ensure_ascii=False),
                topic_idea,
                ts,
            ),
        )
        row = dict(conn.execute("SELECT * FROM comment_drafts WHERE id = ?", (cur.lastrowid,)).fetchone())
        row["drafts"] = drafts
    return {"used_model": used_model, "item": row}


class CommentMarkRepliedIn(BaseModel):
    replied_text: str


@router.post("/comments/{draft_id}/mark-replied")
def mark_comment_replied(draft_id: int, body: CommentMarkRepliedIn) -> dict[str, Any]:
    """标记观众评论已人工回复，记录实际回复正文、操作人与时间并通知团队成员"""
    replied_text = (body.replied_text or "").strip()
    if not replied_text:
        raise HTTPException(status_code=400, detail="请填写实际发送的回复内容")
    actor = get_current_actor()
    replied_by = str(actor.get("display_name") or actor.get("username") or "成员")
    ts = now_str()
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM comment_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="评论草稿不存在")
        conn.execute(
            """
            UPDATE comment_drafts
            SET status = 'replied', replied_text = ?, replied_by = ?, replied_at = ?
            WHERE id = ?
            """,
            (replied_text, replied_by, ts, draft_id),
        )
        updated = dict(conn.execute("SELECT * FROM comment_drafts WHERE id = ?", (draft_id,)).fetchone())
        try:
            updated["drafts"] = json.loads(updated.get("drafts_json") or "[]")
        except Exception:
            updated["drafts"] = []

    notify_other_party(
        str(actor.get("role") or "admin"),
        title=f"{_brand_name()} · 观众评论已回复留痕",
        message=f"{replied_by} 回复了 {updated.get('platform', '')} 评论「{str(updated.get('fan_comment') or '')[:32]}」→「{replied_text[:60]}」",
        click_url="?tab=analytics",
        tags=["speech_balloon", "white_check_mark"],
        priority=2,
    )
    return {"item": updated}


@router.post("/comments/{draft_id}/to-topic")
def convert_comment_to_topic(draft_id: int) -> dict[str, Any]:
    """将评论卡片里的观众点子一键转为「热点与选题库」新选题，并同步标记 status='promoted'"""
    ts = now_str()
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM comment_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="评论草稿不存在")
        title = (row["topic_idea"] or "").strip() or f"观众点题：{row['fan_comment'][:28]}"
        cur = conn.execute(
            """
            INSERT INTO topics (
                title, source, summary, ai_pitch, score_theme, grade, status, created_at, updated_at
            ) VALUES (?, 'fan_comment', ?, ?, 0, 'UNSCORED', 'new', ?, ?)
            """,
            (
                title,
                f"来自 {row['platform']} 观众真实评论：「{row['fan_comment']}」",
                row["topic_idea"] or "",
                ts,
                ts,
            ),
        )
        topic_id = cur.lastrowid
        conn.execute(
            "UPDATE comment_drafts SET converted_topic_id = ?, status = 'promoted' WHERE id = ?",
            (topic_id, draft_id),
        )
        topic = dict(conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone())
        topic["scores"] = {}
        topic["score_theme"] = 0
    return {"topic": topic}


@router.delete("/comments/{draft_id}")
def delete_comment_draft(draft_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM comment_drafts WHERE id = ?", (draft_id,))
    return {"ok": True}


# ==================== 系统设置、订阅档位预设 & 可编辑 Prompt 规则模板 ====================


class SettingsPatch(BaseModel):
    BRAND_NAME: str | None = None
    SUBSCRIPTION_TIER: str | None = None
    ENABLE_HOT_FEEDS: bool | None = None
    ENABLE_CLOUD_COLLAB: bool | None = None
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str | None = None
    GOOGLE_PROXY_URL: str | None = None
    DEEPSEEK_ENABLED: bool | None = None
    DEEPSEEK_API_KEY: str | None = None
    DEEPSEEK_MODEL: str | None = None
    DEFAULT_PROVIDER: str | None = None
    MEDIA_ENGINE_MODE: str | None = None
    CLI_IMAGE_MODEL: str | None = None
    CLI_VIDEO_MODEL: str | None = None
    CLI_MUSIC_MODEL: str | None = None
    CLI_TTS_MODEL: str | None = None
    CLI_PRICING_JSON: str | None = None
    WATCH_DOWNLOADS_DIR: str | None = None
    DOCK_DRAG_DIR: str | None = None
    KEEP_CHINESE_COPY_ON_CLAIM: bool | None = None
    AUTO_RENAME_FLOW_DOWNLOADS: bool | None = None
    AUTO_TRASH_REJECTED_BATCH: bool | None = None
    INBOX_FILTER_MODE: str | None = None
    COLAB_API_BASE: str | None = None
    COLAB_MCP_ENDPOINT: str | None = None
    COLAB_DEFAULT_GPU: str | None = None
    COLAB_CLI_PATH: str | None = None
    COLAB_GDRIVE_ROOT: str | None = None
    CLOUD_STUDIO_URL: str | None = None
    SYNC_API_TOKEN: str | None = None
    NTFY_ENABLED: bool | None = None
    NTFY_PUBLIC_URL: str | None = None
    NTFY_TOPIC_ADMIN: str | None = None
    NTFY_TOPIC_PARTNER: str | None = None
    NTFY_TOKEN: str | None = None
    PUBLIC_STUDIO_URL: str | None = None


class ApplySubscriptionPresetIn(BaseModel):
    tier: str


class ColabOAuthFinishIn(BaseModel):
    auth_code: str
    state: str = ""


class ColabStopSessionIn(BaseModel):
    endpoint: str


class PromptTemplatePatch(BaseModel):
    content: str


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": get_public_config()}


@router.patch("/settings")
def save_settings(body: SettingsPatch) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if is_cloud_collab():
        actor = get_current_actor()
        if actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可修改远程协作端系统设置")
        forbidden = [k for k in updates if k not in CLOUD_SAFE_UPDATE_KEYS]
        if forbidden:
            raise HTTPException(status_code=400, detail="远程协作模式禁止写入本地生成或海外模型字段")
        if updates:
            update_config(updates)
        return {"settings": get_public_config()}

    if "CLI_PRICING_JSON" in updates:
        raw_pricing = str(updates["CLI_PRICING_JSON"]).strip()
        if raw_pricing:
            try:
                parsed_p = json.loads(raw_pricing)
                if not isinstance(parsed_p, dict):
                    raise ValueError("必须为 JSON 对象")
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"CLI_PRICING_JSON 格式无效：{exc}")

    if "SUBSCRIPTION_TIER" in updates:
        tier_val = str(updates["SUBSCRIPTION_TIER"]).strip().lower()
        if tier_val and tier_val not in SUBSCRIPTION_PRESETS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的订阅档位：{tier_val}，可选：{', '.join(SUBSCRIPTION_PRESETS.keys())}",
            )
        updates["SUBSCRIPTION_TIER"] = tier_val or "custom"

    if "MEDIA_ENGINE_MODE" in updates:
        engine_mode = str(updates["MEDIA_ENGINE_MODE"]).strip().lower()
        if engine_mode not in ("auto", "sdk", "cli"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的媒体引擎模式：{engine_mode}，可选：auto / sdk / cli",
            )
        updates["MEDIA_ENGINE_MODE"] = engine_mode

    old_cfg = get_public_config()
    update_config(updates)
    new_cfg = get_public_config()
    if downloads_inbox is not None:
        if new_cfg.get("watch_downloads_dir") != old_cfg.get("watch_downloads_dir"):
            downloads_inbox.start()
        elif new_cfg.get("inbox_filter_mode") != old_cfg.get("inbox_filter_mode"):
            downloads_inbox.reload_filter()
    return {"settings": new_cfg}


@router.get("/subscription-presets")
def get_subscription_presets() -> dict[str, Any]:
    """返回全部 Google 订阅与算力档位预设列表及当前选中档位，供首次引导与设置页使用"""
    pub_cfg = get_public_config()
    return {
        "current_tier": pub_cfg.get("subscription_tier", "custom"),
        "presets": list(SUBSCRIPTION_PRESETS.values()),
        "presets_by_id": SUBSCRIPTION_PRESETS,
    }


@router.post("/settings/gemini-models")
def detect_gemini_models() -> dict[str, Any]:
    """一键探测当前 Gemini Key 真实可用模型，绝不因硬编码旧版本号 404"""
    try:
        models = list_available_gemini_models()
        return {"models": models}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"探测 Gemini 模型失败：{exc}")


# ==================== 零风险四平台发布文案一键适配（纯本地人工复制，0 第三方插件） ====================


class MultiPlatformCopyRequest(BaseModel):
    episode_id: int | None = None
    project_name: str = ""
    episode_title: str = ""
    hook_0_3s: str = ""
    script_excerpt: str = ""
    ending_cta: str = ""
    extra_notes: str = ""
    provider: str | None = None


@router.get("/publish-copy/episodes")
def list_episodes_for_copy() -> dict[str, Any]:
    """返回各剧目已有分集简要信息，方便在发布页下拉一键带入剧本与钩子"""
    with db_ctx() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT e.id, e.project_id, e.ep_number, e.title, e.hook_0_3s,
                       e.ending_cta, e.script_text, p.name AS project_name
                FROM episodes e
                JOIN projects p ON p.id = e.project_id
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT 50
                """
            ).fetchall()
        )
    return {"episodes": rows}


@router.post("/publish-copy/generate")
def generate_publish_copy(body: MultiPlatformCopyRequest) -> dict[str, Any]:
    """一次生成微信视频号 / 抖音 / 小红书 / B站四套差异化发布文案（纯本地复制，0 封号风险）"""
    proj_name = body.project_name.strip()
    ep_title = body.episode_title.strip()
    hook = body.hook_0_3s.strip()
    excerpt = body.script_excerpt.strip()
    cta = body.ending_cta.strip()

    if body.episode_id:
        with db_ctx() as conn:
            row = conn.execute(
                """
                SELECT e.*, p.name AS project_name
                FROM episodes e JOIN projects p ON p.id = e.project_id
                WHERE e.id = ?
                """,
                (body.episode_id,),
            ).fetchone()
            if row:
                proj_name = proj_name or row["project_name"]
                ep_title = ep_title or f"第{row['ep_number']}集 {row['title']}"
                hook = hook or (row["hook_0_3s"] or "")
                excerpt = excerpt or (row["script_text"] or "")
                cta = cta or (row["ending_cta"] or "")

    if not ep_title:
        raise HTTPException(status_code=400, detail="请先选择一个分集或输入作品标题。")

    # 自动读取当下未过期的官方流量扶持活动，生成发片文案时顺手带齐必带 #话题标签
    active_campaigns: list[dict[str, Any]] = []
    try:
        with db_ctx() as conn:
            c_rows = rows_to_dicts(
                conn.execute(
                    "SELECT platform, title, hashtags_json, deadline, incentive FROM platform_campaigns WHERE status = 'active' ORDER BY updated_at DESC LIMIT 8"
                ).fetchall()
            )
            for r in c_rows:
                try:
                    r["hashtags"] = json.loads(r.get("hashtags_json") or "[]")
                except Exception:
                    r["hashtags"] = []
                active_campaigns.append(r)
    except Exception:
        active_campaigns = []

    try:
        pack, used_model = generate_multi_platform_copy_with_ai(
            project_name=proj_name,
            episode_title=ep_title,
            hook_0_3s=hook,
            script_excerpt=excerpt,
            ending_cta=cta,
            extra_notes=body.extra_notes.strip(),
            active_campaigns=active_campaigns,
            provider=body.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"生成四平台文案失败：{exc}")

    return {"used_model": used_model, "pack": pack, "linked_campaigns": len(active_campaigns)}


@router.get("/settings/probe-colab")
def probe_colab_status() -> dict[str, Any]:
    """按需探测 Google Colab 官方 CLI / REST API / MCP 端点状态与真实 CU 余额"""
    return probe_colab()


@router.post("/settings/colab/oauth-start")
def colab_oauth_start() -> dict[str, Any]:
    """发起官方 Google OAuth2 授权流程，返回官方浏览器授权链接"""
    try:
        return start_colab_oauth()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"生成 Colab 官方授权链接失败：{exc}")


@router.post("/settings/colab/oauth-finish")
def colab_oauth_finish(body: ColabOAuthFinishIn) -> dict[str, Any]:
    """提交 Google 授权码换取令牌并存入本机安全凭据存储（制片台与终端 colab 命令共用）"""
    try:
        return finish_colab_oauth(body.auth_code, state=body.state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"完成 Colab 官方授权失败：{exc}")


@router.post("/settings/colab/oauth-logout")
def colab_oauth_logout() -> dict[str, Any]:
    """清除本机安全凭据存储中的 Colab OAuth2 令牌"""
    return logout_colab_oauth()


@router.post("/settings/colab/stop-session")
def colab_stop_session(body: ColabStopSessionIn) -> dict[str, Any]:
    """手动停止并释放活跃的 Colab 云端运行时，防止闲置消耗计算单元"""
    try:
        return stop_colab_session(body.endpoint)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"停止 Colab 运行时失败：{exc}")


@router.get("/prompt-templates")
def list_prompt_templates() -> dict[str, Any]:
    with db_ctx() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM prompt_templates ORDER BY key").fetchall())
        return {"templates": rows}


@router.patch("/prompt-templates/{key}")
def save_prompt_template(key: str, body: PromptTemplatePatch) -> dict[str, Any]:
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM prompt_templates WHERE key = ?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")
        conn.execute(
            "UPDATE prompt_templates SET content = ?, updated_at = ? WHERE key = ?",
            (body.content, now_str(), key),
        )
        updated = dict(conn.execute("SELECT * FROM prompt_templates WHERE key = ?", (key,)).fetchone())
        return {"template": updated}


@router.post("/prompt-templates/{key}/reset")
def reset_prompt_template(key: str) -> dict[str, Any]:
    if key not in DEFAULT_PROMPT_TEMPLATES:
        raise HTTPException(status_code=404, detail="无默认模板")
    d = DEFAULT_PROMPT_TEMPLATES[key]
    with db_ctx() as conn:
        conn.execute(
            "UPDATE prompt_templates SET content = ?, updated_at = ? WHERE key = ?",
            (d["content"], now_str(), key),
        )
        updated = dict(conn.execute("SELECT * FROM prompt_templates WHERE key = ?", (key,)).fetchone())
        return {"template": updated}


# ==================== 左下角三合一额度监控（Gemini API / Google Flow / DeepSeek） ====================


class QuotaCalibrateIn(BaseModel):
    gemini_daily_limit: int | None = None
    gemini_monthly_credit_usd: float | None = None
    flow_monthly_total: int | None = None
    flow_remaining: int | None = None


def _ensure_quota_row(conn: Any) -> dict[str, Any]:
    now = datetime.now()
    cur_month = now.strftime("%Y-%m")
    row = conn.execute("SELECT * FROM quota_state WHERE id = 1").fetchone()
    if not row:
        ts = now_str()
        conn.execute(
            """
            INSERT INTO quota_state (
                id, gemini_daily_limit, gemini_monthly_credit_usd, flow_monthly_total, flow_remaining,
                flow_month, flow_calibrated_at, flow_estimated_spent_since_cal, updated_at
            ) VALUES (1, 100, 0.0, 0, 0, ?, '', 0, ?)
            """,
            (cur_month, ts),
        )
        row = conn.execute("SELECT * FROM quota_state WHERE id = 1").fetchone()

    d = dict(row)
    # 跨月自动重置 Flow 月度池（月度积分月底清零不结转）
    if d.get("flow_month") != cur_month:
        ts = now_str()
        conn.execute(
            """
            UPDATE quota_state
            SET flow_month = ?, flow_remaining = flow_monthly_total,
                flow_estimated_spent_since_cal = 0, updated_at = ?
            WHERE id = 1
            """,
            (cur_month, ts),
        )
        d = dict(conn.execute("SELECT * FROM quota_state WHERE id = 1").fetchone())
    return d


@router.get("/quota/summary")
def get_quota_summary() -> dict[str, Any]:
    """返回左下角三合一额度监控：Gemini API（真实本地调用记账）、Google Flow（月度账本+清零倒计时）、DeepSeek（备用状态+直连余额）"""
    now = datetime.now()
    today_prefix = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left_in_month = max(0, days_in_month - now.day)

    pub_cfg = get_public_config()
    with db_ctx() as conn:
        qs = _ensure_quota_row(conn)
        g_today = conn.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(est_cost_usd), 0.0) AS cost_usd
            FROM api_usage_logs WHERE provider = 'gemini' AND created_at LIKE ?
            """,
            (f"{today_prefix}%",),
        ).fetchone()
        g_month = conn.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(est_cost_usd), 0.0) AS cost_usd
            FROM api_usage_logs WHERE provider = 'gemini' AND created_at LIKE ?
            """,
            (f"{month_prefix}%",),
        ).fetchone()
        ds_month = conn.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
            FROM api_usage_logs WHERE provider = 'deepseek' AND created_at LIKE ?
            """,
            (f"{month_prefix}%",),
        ).fetchone()

    flow_est_remaining = max(0, int(qs["flow_remaining"]) - int(qs["flow_estimated_spent_since_cal"]))
    raw_credit = qs.get("gemini_monthly_credit_usd")
    monthly_credit = round(float(raw_credit if raw_credit is not None else 0.0), 2)
    month_cost = round(float(g_month["cost_usd"] or 0.0), 2)
    today_cost = round(float(g_today["cost_usd"] or 0.0), 2)
    rem_credit = round(max(0.0, monthly_credit - month_cost), 2)

    return {
        "subscription_tier": pub_cfg.get("subscription_tier", "custom"),
        "gemini": {
            "key_set": pub_cfg["gemini_key_set"],
            "model": pub_cfg["gemini_model"],
            "today_calls": int(g_today["calls"]),
            "today_tokens": int(g_today["tokens"]),
            "today_prompt_tokens": int(g_today["prompt_tokens"]),
            "today_completion_tokens": int(g_today["completion_tokens"]),
            "today_cost_usd": today_cost,
            "month_calls": int(g_month["calls"]),
            "month_tokens": int(g_month["tokens"]),
            "month_cost_usd": month_cost,
            "monthly_credit_usd": monthly_credit,
            "remaining_credit_usd": rem_credit,
            "daily_limit": int(qs["gemini_daily_limit"]),
        },
        "flow": {
            "monthly_total": int(qs["flow_monthly_total"]),
            "calibrated_remaining": int(qs["flow_remaining"]),
            "estimated_spent": int(qs["flow_estimated_spent_since_cal"]),
            "estimated_remaining": flow_est_remaining,
            "month": qs["flow_month"],
            "days_left": days_left_in_month,
            "calibrated_at": qs["flow_calibrated_at"],
        },
        "deepseek": {
            "enabled": pub_cfg["deepseek_enabled"],
            "key_set": pub_cfg["deepseek_key_set"],
            "month_calls": int(ds_month["calls"]),
            "month_tokens": int(ds_month["tokens"]),
            "balance_text": qs["deepseek_balance_text"],
            "checked_at": qs["deepseek_checked_at"],
        },
    }


@router.post("/quota/apply-preset")
def apply_subscription_preset(body: ApplySubscriptionPresetIn) -> dict[str, Any]:
    """
    一键应用指定 Google 订阅档位预设：
    - 写入非敏感配置 SUBSCRIPTION_TIER；
    - 同步校准 quota_state 推荐的每日调用上限、月度 API 预算与月度 Flow 积分池；
    - 返回更新后的 quota/summary 与最新公开配置 config。
    """
    tier = (body.tier or "").strip().lower()
    if tier not in SUBSCRIPTION_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的订阅档位：{tier}，可选：{', '.join(SUBSCRIPTION_PRESETS.keys())}",
        )
    preset = SUBSCRIPTION_PRESETS[tier]
    update_config({"SUBSCRIPTION_TIER": tier})

    ts = now_str()
    cur_month = datetime.now().strftime("%Y-%m")
    with db_ctx() as conn:
        _ensure_quota_row(conn)
        if tier != "custom":
            flow_total = max(0, int(preset.get("flow_monthly_total") or 0))
            daily_limit = max(10, int(preset.get("gemini_daily_limit") or 100))
            monthly_credit = round(max(0.0, float(preset.get("gemini_monthly_credit_usd") or 0.0)), 2)
            conn.execute(
                """
                UPDATE quota_state
                SET gemini_daily_limit = ?,
                    gemini_monthly_credit_usd = ?,
                    flow_monthly_total = ?,
                    flow_remaining = ?,
                    flow_month = ?,
                    flow_calibrated_at = ?,
                    flow_estimated_spent_since_cal = 0,
                    updated_at = ?
                WHERE id = 1
                """,
                (daily_limit, monthly_credit, flow_total, flow_total, cur_month, ts, ts),
            )
        else:
            conn.execute("UPDATE quota_state SET updated_at = ? WHERE id = 1", (ts,))

    quota_summary = get_quota_summary()
    pub_cfg = get_public_config()
    return {
        "ok": True,
        "tier": tier,
        "preset": preset,
        "quota": quota_summary,
        "config": pub_cfg,
        "settings": pub_cfg,
        **quota_summary,
    }


@router.post("/quota/calibrate")
def calibrate_quota(body: QuotaCalibrateIn) -> dict[str, Any]:
    """手动校准 Flow 真实剩余积分 / 月度池，或 Gemini 每日参考限额与月度预算"""
    ts = now_str()
    with db_ctx() as conn:
        _ensure_quota_row(conn)
        if body.gemini_daily_limit is not None:
            conn.execute(
                "UPDATE quota_state SET gemini_daily_limit = ?, updated_at = ? WHERE id = 1",
                (max(10, int(body.gemini_daily_limit)), ts),
            )
        if body.gemini_monthly_credit_usd is not None:
            conn.execute(
                "UPDATE quota_state SET gemini_monthly_credit_usd = ?, updated_at = ? WHERE id = 1",
                (round(max(0.0, float(body.gemini_monthly_credit_usd)), 2), ts),
            )
        if body.flow_monthly_total is not None:
            conn.execute(
                "UPDATE quota_state SET flow_monthly_total = ?, updated_at = ? WHERE id = 1",
                (max(0, int(body.flow_monthly_total)), ts),
            )
        if body.flow_remaining is not None:
            conn.execute(
                """
                UPDATE quota_state
                SET flow_remaining = ?, flow_estimated_spent_since_cal = 0,
                    flow_calibrated_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (max(0, int(body.flow_remaining)), ts, ts),
            )
    return get_quota_summary()


@router.post("/quota/refresh-deepseek")
async def refresh_deepseek_balance() -> dict[str, Any]:
    """若已开启 DeepSeek 备用开关且已填 Key，直连查询官方真实余额"""
    cfg = load_config()
    ds_enabled = cfg["DEEPSEEK_ENABLED"].lower() in ("1", "true", "yes", "on")
    if not ds_enabled:
        raise HTTPException(status_code=400, detail="DeepSeek 备用开关目前是关闭的，无需查询余额。")
    key = cfg["DEEPSEEK_API_KEY"].strip()
    if not key:
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key，请先在「系统设置与规则」填入。")

    try:
        async with make_cn_async_client(timeout=10.0) as client:
            r = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {key}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查询 DeepSeek 余额失败：{exc}")

    infos = data.get("balance_infos") or []
    parts = [f"{x.get('currency', 'CNY')} {x.get('total_balance', '0')}" for x in infos]
    bal_text = " / ".join(parts) if parts else ("可用" if data.get("is_available") else "余额不足")
    ts = now_str()
    with db_ctx() as conn:
        _ensure_quota_row(conn)
        conn.execute(
            "UPDATE quota_state SET deepseek_balance_text = ?, deepseek_checked_at = ?, updated_at = ? WHERE id = 1",
            (bal_text, ts, ts),
        )
    return get_quota_summary()
