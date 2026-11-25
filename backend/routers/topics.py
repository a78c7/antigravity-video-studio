"""
热点抓取与通用 5 维选题库路由：
- 国内五大热榜直连（可由系统设置 ENABLE_HOT_FEEDS 开关控制，10 分钟缓存）
- 默认走 Gemini API（DeepSeek 可在设置页按需启用）做全题材 5 维打分与双轨内容分级
- 支持通用主题联网搜题（/api/topics/search-topic）与全网爆款结构对标转译（/api/topics/search-benchmarks）
- 支持一键把优质选题立项为指定剧集的新一集，以及官方流量扶持活动雷达入库与转化
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import DATA_DIR, load_config
from ..db import db_ctx, now_str, rows_to_dicts
from ..hot_feed import SUPPORTED_SOURCES, get_hot_list
from ..llm import (
    score_topics_with_ai,
    search_benchmarks_with_ai,
    search_topic_ideas,
)

router = APIRouter(prefix="/api", tags=["topics"])
CAMPAIGNS_INBOX_FILE = DATA_DIR / "campaigns_inbox.json"

TIER_PREMIUM = "【精品口碑级】"
TIER_VIRAL = "【破圈玩梗级】"


def _hot_feeds_enabled() -> bool:
    return load_config().get("ENABLE_HOT_FEEDS", "true").strip().lower() in ("1", "true", "yes", "on")


class ManualTopicIn(BaseModel):
    title: str
    source: str = "manual"
    source_url: str = ""
    summary: str = ""
    ai_pitch: str = ""
    recommended_project_id: int | None = None


class ScoreHotRequest(BaseModel):
    items: list[dict[str, Any]]
    theme: str = ""
    provider: str | None = None


class SearchTopicRequest(BaseModel):
    keyword: str = ""
    theme: str = ""
    provider: str | None = None


class SearchBenchmarksRequest(BaseModel):
    focus: str = ""
    theme: str = ""
    keyword: str = ""
    count: int = 6
    provider: str | None = None


class PromoteTopicRequest(BaseModel):
    project_id: int


@router.get("/hot-sources")
def list_hot_sources() -> dict[str, Any]:
    enabled = _hot_feeds_enabled()
    return {
        "enabled": enabled,
        "sources": [{"id": k, "label": v} for k, v in SUPPORTED_SOURCES.items()] if enabled else [],
        "message": "" if enabled else "国内热榜雷达当前已在系统设置中关闭，如需使用请开启 ENABLE_HOT_FEEDS。",
    }


@router.get("/hot/{source}")
async def fetch_hot_source(source: str, force: bool = False) -> dict[str, Any]:
    if not _hot_feeds_enabled():
        return {
            "enabled": False,
            "source": source,
            "source_label": SUPPORTED_SOURCES.get(source, source),
            "cached": False,
            "fetched_at": "",
            "latency_ms": 0,
            "items": [],
            "degraded": False,
            "warning": "",
            "message": "国内热榜雷达当前已在系统设置中关闭，可在「系统设置」开启 ENABLE_HOT_FEEDS 后重新获取。",
        }
    try:
        data = await get_hot_list(source=source, force=force)
        if isinstance(data, dict):
            payload = {"enabled": True, **data}
            payload["degraded"] = bool(payload.get("degraded", False))
            payload["warning"] = str(payload.get("warning") or "")
            return payload
        return {
            "enabled": True,
            "source": source,
            "source_label": SUPPORTED_SOURCES.get(source, source),
            "cached": False,
            "fetched_at": "",
            "latency_ms": 0,
            "items": [],
            "degraded": False,
            "warning": "",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # 优雅降级：热榜源挂掉时透传 degraded 与中文 warning，不再直接抛 502 导致页面报错
        source_label = SUPPORTED_SOURCES.get(source, source)
        return {
            "enabled": True,
            "source": source,
            "source_label": source_label,
            "cached": False,
            "fetched_at": "",
            "latency_ms": 0,
            "items": [],
            "degraded": True,
            "warning": f"{source_label} 实时源暂不可用，已自动降级展示（{exc}）。可稍后点击刷新重试或改用关键词搜题。",
        }


@router.get("/topics")
def list_topics(
    grade: str | None = None,
    status: str | None = None,
    content_tier: str | None = None,
) -> dict[str, Any]:
    with db_ctx() as conn:
        sql = "SELECT * FROM topics WHERE 1=1"
        params: list[Any] = []
        if grade and grade != "ALL":
            sql += " AND grade = ?"
            params.append(grade)
        if status and status != "ALL":
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY CASE grade WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'UNSCORED' THEN 3 ELSE 4 END, total_score DESC, id DESC"
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
        out: list[dict[str, Any]] = []
        target_tier = _normalize_content_tier(content_tier or "") if (content_tier and content_tier != "ALL") else ""
        for r in rows:
            try:
                parsed_scores = json.loads(r.get("scores_json") or "{}")
                r["scores"] = parsed_scores if isinstance(parsed_scores, dict) else {}
            except Exception:
                r["scores"] = {}
            theme_score = r.get("score_theme")
            if theme_score is None or (theme_score == 0 and r["scores"].get("theme")):
                try:
                    theme_score = int(r["scores"].get("theme") or 0)
                except (TypeError, ValueError):
                    theme_score = 0
            r["score_theme"] = int(theme_score or 0)
            tier = _resolve_content_tier(
                raw_tier=r.get("content_tier") or "",
                summary=r.get("summary") or "",
                ai_pitch=r.get("ai_pitch") or "",
                title=r.get("title") or "",
                scores=r["scores"],
            )
            r["content_tier"] = tier
            if target_tier and tier != target_tier:
                continue
            out.append(r)
        return {"topics": out}


@router.post("/topics")
def create_manual_topic(body: ManualTopicIn) -> dict[str, Any]:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="选题标题不能为空")
    ts = now_str()
    with db_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO topics (
                title, source, source_url, summary, ai_pitch,
                recommended_project_id, score_theme, grade, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'UNSCORED', 'new', ?, ?)
            """,
            (title, body.source, body.source_url, body.summary, body.ai_pitch, body.recommended_project_id, ts, ts),
        )
        row = dict(conn.execute("SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)).fetchone())
        row["scores"] = {}
        row["score_theme"] = int(row.get("score_theme") or 0)
        row["content_tier"] = _resolve_content_tier("", row.get("summary") or "", row.get("ai_pitch") or "", title, {})
        return {"topic": row}


SCORE_LIMITS: dict[str, int] = {
    "theme": 25,
    "hook": 25,
    "safe": 20,
    "viral": 15,
    "serial": 15,
}


def _normalize_content_tier(raw: Any) -> str:
    """规范化双轨分级标签为【精品口碑级】或【破圈玩梗级】"""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    if any(k in s for k in ("精品", "口碑", "质感", "体面", "官推")) or "official" in low or "premium" in low:
        return TIER_PREMIUM
    if any(k in s for k in ("破圈", "玩梗", "娱乐")) or "viral" in low or "meme" in low:
        return TIER_VIRAL
    return ""


def _resolve_content_tier(
    raw_tier: Any,
    summary: str,
    ai_pitch: str,
    title: str,
    scores: dict[str, Any],
) -> str:
    """提取或推断双轨分级标签（优先显式字段，其次文本自带标签，最后按 5 维得分稳妥兜底）"""
    explicit = _normalize_content_tier(raw_tier)
    if explicit:
        return explicit
    combined = f"{summary} {ai_pitch} {title}"
    if any(tok in combined for tok in (TIER_PREMIUM, "精品口碑级", "质感口碑级")):
        return TIER_PREMIUM
    if TIER_VIRAL in combined or "破圈玩梗级" in combined:
        return TIER_VIRAL
    if not scores:
        return ""
    try:
        safe_s = int(scores.get("safe") or 0)
        viral_s = int(scores.get("viral") or 0)
    except (TypeError, ValueError):
        safe_s, viral_s = 0, 0
    meme_hints = ("玩梗", "热梗", "整活", "魔改", "娱乐", "吐槽", "反差梗", "热搜梗")
    if any(k in combined for k in meme_hints) and safe_s < 18:
        return TIER_VIRAL
    if safe_s >= 17:
        return TIER_PREMIUM
    if viral_s >= 12:
        return TIER_VIRAL
    return TIER_PREMIUM if safe_s >= 15 else TIER_VIRAL


def _attach_tier_to_summary(summary: str, content_tier: str) -> str:
    """将双轨分级标签无重复写入 summary 开头，确保展示与持久化一致"""
    clean = (summary or "").strip()
    if not content_tier:
        return clean
    if TIER_PREMIUM in clean or TIER_VIRAL in clean:
        return clean
    return f"{content_tier} {clean}".strip()


def _normalize_and_guard_score(
    title: str,
    ai_pitch: str,
    summary: str,
    raw_scores: dict[str, Any],
    veto: int,
) -> tuple[dict[str, int], int, str, str]:
    """钳位 5 维合法区间、按真实之和重算总分；对无冲突或 AI 极难拍的选题保底压入 DROP"""
    del title, ai_pitch
    scores: dict[str, int] = {}
    for k, hi in SCORE_LIMITS.items():
        raw_val = raw_scores.get(k)
        if k == "theme" and raw_val is None:
            raw_val = raw_scores.get("score_theme")
        try:
            v = int(raw_val or 0)
        except (TypeError, ValueError):
            v = 0
        scores[k] = max(0, min(hi, v))

    notes: list[str] = []
    total = sum(scores.values())

    # 护栏：若无冲突钩子（hook<=12）或 AI 视频制片翻车风险极高（serial<=6），总分不得越过 74 线
    weak_hook = scores["hook"] <= 12
    hard_to_shoot = scores["serial"] <= 6
    if not veto and (weak_hook or hard_to_shoot) and total >= 75:
        overflow = total - 72
        for dim in ("viral", "safe", "theme", "serial", "hook"):
            if overflow <= 0:
                break
            cut = min(scores[dim], overflow)
            scores[dim] -= cut
            overflow -= cut
        total = sum(scores.values())
        if weak_hook:
            notes.append("缺少冲突钩子(hook<=12)已自动压入DROP")
        if hard_to_shoot:
            notes.append("AI 制片风险过高(serial<=6)已自动压入DROP")

    grade = "DROP" if (veto or total < 75) else ("S" if total >= 85 else "A")
    if notes:
        tag = f" [系统复检：{'；'.join(notes)}]"
        if tag not in summary:
            summary = f"{summary}{tag}".strip()
    return scores, total, grade, summary


def _save_scored_results(results: list[dict[str, Any]], source_fallback: str) -> list[dict[str, Any]]:
    ts = now_str()
    saved: list[dict[str, Any]] = []
    with db_ctx() as conn:
        for item in results:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            raw_scores = item.get("scores") or {}
            veto = 1 if item.get("veto") else 0
            ai_pitch = str(item.get("ai_pitch") or "").strip()
            summary = str(item.get("summary") or "").strip()
            benchmark_case = str(item.get("benchmark_case") or "").strip()
            if benchmark_case and benchmark_case not in summary:
                summary = f"对标爆款：「{benchmark_case}」｜{summary}".strip("｜")
            scores, total, grade, summary = _normalize_and_guard_score(
                title=title,
                ai_pitch=ai_pitch,
                summary=summary,
                raw_scores=raw_scores,
                veto=veto,
            )
            content_tier = _resolve_content_tier(
                raw_tier=item.get("content_tier") or item.get("tier") or "",
                summary=summary,
                ai_pitch=ai_pitch,
                title=title,
                scores=scores,
            )
            summary = _attach_tier_to_summary(summary, content_tier)
            rec_pid = item.get("recommended_project_id")
            if rec_pid is not None:
                exists = conn.execute("SELECT id FROM projects WHERE id = ?", (rec_pid,)).fetchone()
                if not exists:
                    rec_pid = None

            score_theme_val = int(scores.get("theme", 0))
            cur = conn.execute(
                """
                INSERT INTO topics (
                    title, source, source_url, hot_value, video_count, summary,
                    scores_json, score_theme, total_score, grade, veto, veto_reason, ai_pitch,
                    recommended_project_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    title,
                    str(item.get("source") or source_fallback),
                    str(item.get("source_url") or ""),
                    int(item.get("hot_value") or 0),
                    int(item.get("video_count") or 0),
                    summary,
                    json.dumps(scores, ensure_ascii=False),
                    score_theme_val,
                    total,
                    grade,
                    veto,
                    str(item.get("veto_reason") or ""),
                    ai_pitch,
                    rec_pid,
                    ts,
                    ts,
                ),
            )
            row = dict(conn.execute("SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)).fetchone())
            row["scores"] = scores
            row["score_theme"] = score_theme_val
            row["content_tier"] = content_tier
            saved.append(row)
    return saved


@router.post("/topics/score-hot")
def score_hot_candidates(body: ScoreHotRequest) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(status_code=400, detail="请至少勾选 1 条热点进行 AI 选题转化与 5 维打分")
    with db_ctx() as conn:
        projects = rows_to_dicts(conn.execute("SELECT id, name, track, description FROM projects ORDER BY id").fetchall())
    try:
        results, used_model = score_topics_with_ai(
            body.items[:12],
            projects,
            theme=body.theme,
            provider=body.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    saved = _save_scored_results(results, source_fallback="hot_feed")
    return {"used_model": used_model, "saved_count": len(saved), "topics": saved}


@router.post("/topics/search-topic")
def ai_search_topic_ideas(body: SearchTopicRequest) -> dict[str, Any]:
    """通用主题/关键词联网搜题，结合当前项目赛道定位输出 5 维打分选题"""
    with db_ctx() as conn:
        projects = rows_to_dicts(conn.execute("SELECT id, name, track, description FROM projects ORDER BY id").fetchall())
    try:
        results, used_model = search_topic_ideas(
            body.keyword,
            projects,
            theme=body.theme,
            provider=body.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    saved = _save_scored_results(results, source_fallback="topic_search")
    return {"used_model": used_model, "saved_count": len(saved), "topics": saved}


@router.post("/topics/search-benchmarks")
def ai_search_benchmarks(body: SearchBenchmarksRequest | None = None) -> dict[str, Any]:
    """
    全网爆款结构对标与主题转译雷达：
    - 读取可编辑规则模板 `benchmark_adapter`；
    - 优先走 Gemini Google Search Grounding 联网检索近期高完播爆款短视频与 AI 视觉奇观形式；
    - 若未显式锁定供应商且 Gemini 不可用、同时已开启 DeepSeek 备用开关，则自动回退至 DeepSeek；
    - 按 5 维打分与双轨内容分级写入选题库。
    """
    req = body or SearchBenchmarksRequest()
    target_count = max(3, min(8, int(req.count or 6)))

    with db_ctx() as conn:
        projects = rows_to_dicts(conn.execute("SELECT id, name, track, description FROM projects ORDER BY id").fetchall())

    try:
        try:
            results, used_model = search_benchmarks_with_ai(
                focus=req.focus,
                projects=projects,
                theme=req.theme,
                keyword=req.keyword,
                count=target_count,
                provider=req.provider,
            )
        except Exception as primary_exc:
            cfg = load_config()
            ds_enabled = cfg.get("DEEPSEEK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
            if req.provider is None and ds_enabled and cfg.get("DEEPSEEK_API_KEY", "").strip():
                results, used_model = search_benchmarks_with_ai(
                    focus=req.focus,
                    projects=projects,
                    theme=req.theme,
                    keyword=req.keyword,
                    count=target_count,
                    provider="deepseek",
                )
            else:
                raise primary_exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"检索全网爆款结构对标失败：{exc}")

    saved = _save_scored_results(results[:8], source_fallback="benchmark_adapter")
    return {"used_model": used_model, "saved_count": len(saved), "topics": saved}


@router.post("/topics/{topic_id}/promote")
def promote_topic_to_episode(topic_id: int, body: PromoteTopicRequest) -> dict[str, Any]:
    ts = now_str()
    with db_ctx() as conn:
        topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if not topic:
            raise HTTPException(status_code=404, detail="选题不存在")
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (body.project_id,)).fetchone()
        if not proj:
            raise HTTPException(status_code=404, detail="目标剧目不存在")
        max_ep = conn.execute(
            "SELECT COALESCE(MAX(ep_number), 0) AS m FROM episodes WHERE project_id = ?",
            (body.project_id,),
        ).fetchone()["m"]
        next_ep = int(max_ep) + 1
        cur = conn.execute(
            """
            INSERT INTO episodes (
                project_id, ep_number, title, topic_id, hook_0_3s, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (body.project_id, next_ep, topic["title"], topic_id, topic["ai_pitch"] or topic["summary"] or "", ts, ts),
        )
        conn.execute(
            "UPDATE topics SET status = 'promoted', recommended_project_id = ?, updated_at = ? WHERE id = ?",
            (body.project_id, ts, topic_id),
        )
        ep = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (cur.lastrowid,)).fetchone())
        return {"episode": ep}


@router.delete("/topics/{topic_id}")
def delete_topic(topic_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    return {"ok": True}


# ==================== 平台官方流量扶持活动雷达（支持外部定时任务自动入库） ====================


class CampaignItemIn(BaseModel):
    platform: str = "抖音"
    title: str
    hashtags: list[str] | str = []
    deadline: str = ""
    incentive: str = ""
    angle_pitch: str = ""
    source_url: str = ""
    status: str = "active"


class CampaignsIngestRequest(BaseModel):
    batch_date: str = ""
    campaigns: list[CampaignItemIn] = []


class CampaignPatch(BaseModel):
    status: str | None = None
    deadline: str | None = None
    angle_pitch: str | None = None


def _normalize_hashtags(raw: list[str] | str | None) -> list[str]:
    """规范化必带话题标签为去重后的 ['#xxx', ...] 列表"""
    if not raw:
        return []
    parts: list[str] = []
    if isinstance(raw, str):
        for seg in re.split(r"[\s,，、;/；]+", raw.strip()):
            if seg:
                parts.append(seg)
    else:
        for item in raw:
            s = str(item or "").strip()
            if s:
                parts.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        tag = p if p.startswith("#") else f"#{p}"
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _detect_expired(deadline: str, current_status: str) -> str:
    """若 deadline 含标准日期 YYYY-MM-DD 且已早于今天，自动置灰标为 expired"""
    if current_status == "archived":
        return "archived"
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", deadline or "")
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if dt.date() < datetime.now().date():
                return "expired"
        except ValueError:
            pass
    return current_status or "active"


def _upsert_campaigns(items: list[dict[str, Any]], batch_date: str = "") -> tuple[int, int]:
    """按 (platform, title) 去重写入/刷新平台流量扶持活动，避免定期推送堆积重复记录"""
    ts = now_str()
    bdate = (batch_date or "").strip() or ts[:10]
    inserted = 0
    updated = 0
    with db_ctx() as conn:
        for raw in items:
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            platform = str(raw.get("platform") or "抖音").strip() or "抖音"
            hashtags = _normalize_hashtags(raw.get("hashtags"))
            deadline = str(raw.get("deadline") or "").strip()
            incentive = str(raw.get("incentive") or "").strip()
            angle_pitch = str(raw.get("angle_pitch") or "").strip()
            source_url = str(raw.get("source_url") or "").strip()
            status = _detect_expired(deadline, str(raw.get("status") or "active").strip())

            existing = conn.execute(
                "SELECT id FROM platform_campaigns WHERE platform = ? AND title = ?",
                (platform, title),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE platform_campaigns
                    SET hashtags_json = ?, deadline = ?, incentive = ?, angle_pitch = ?,
                        source_url = ?, status = ?, batch_date = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(hashtags, ensure_ascii=False),
                        deadline,
                        incentive,
                        angle_pitch,
                        source_url,
                        status,
                        bdate,
                        ts,
                        existing["id"],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO platform_campaigns (
                        platform, title, hashtags_json, deadline, incentive,
                        angle_pitch, source_url, status, batch_date, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        title,
                        json.dumps(hashtags, ensure_ascii=False),
                        deadline,
                        incentive,
                        angle_pitch,
                        source_url,
                        status,
                        bdate,
                        ts,
                        ts,
                    ),
                )
                inserted += 1
    return inserted, updated


def absorb_offline_campaigns_inbox() -> int:
    """离线兜底自动吸收：若外部采集脚本在制片台未开机时写盘到 data/campaigns_inbox.json，启动或查询时自动同步入库"""
    if not CAMPAIGNS_INBOX_FILE.exists():
        return 0
    try:
        raw_text = CAMPAIGNS_INBOX_FILE.read_text(encoding="utf-8").strip()
        if not raw_text:
            return 0
        payload = json.loads(raw_text)
        if isinstance(payload, list):
            items, bdate = payload, ""
        elif isinstance(payload, dict):
            items = payload.get("campaigns") or []
            bdate = str(payload.get("batch_date") or "")
        else:
            return 0
        if not items:
            return 0
        ins, upd = _upsert_campaigns(items, batch_date=bdate)
        processed = DATA_DIR / "campaigns_inbox.processed.json"
        CAMPAIGNS_INBOX_FILE.replace(processed)
        return ins + upd
    except Exception:
        return 0


def _list_campaigns_from_db(include_archived: bool = False) -> list[dict[str, Any]]:
    with db_ctx() as conn:
        sql = "SELECT * FROM platform_campaigns"
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += " ORDER BY CASE status WHEN 'active' THEN 1 WHEN 'expired' THEN 2 ELSE 3 END, updated_at DESC, id DESC"
        rows = rows_to_dicts(conn.execute(sql).fetchall())
        for r in rows:
            try:
                r["hashtags"] = json.loads(r.get("hashtags_json") or "[]")
            except Exception:
                r["hashtags"] = []
            checked_status = _detect_expired(r.get("deadline") or "", r.get("status") or "active")
            if checked_status != r.get("status"):
                r["status"] = checked_status
                conn.execute("UPDATE platform_campaigns SET status = ? WHERE id = ?", (checked_status, r["id"]))
        return rows


@router.get("/campaigns")
def list_platform_campaigns(include_archived: bool = False) -> dict[str, Any]:
    """获取平台流量扶持活动列表（自动吸收离线落盘 JSON 并动态校准过期状态）"""
    absorbed = absorb_offline_campaigns_inbox()
    items = _list_campaigns_from_db(include_archived=include_archived)
    return {
        "absorbed_offline": absorbed,
        "active_count": sum(1 for x in items if x["status"] == "active"),
        "campaigns": items,
    }


@router.post("/campaigns/ingest")
def ingest_platform_campaigns(body: CampaignsIngestRequest) -> dict[str, Any]:
    """供外部定时采集任务全自动批量推送去重入库"""
    if not body.campaigns:
        raise HTTPException(status_code=400, detail="campaigns 列表不能为空")
    inserted, updated = _upsert_campaigns([c.model_dump() for c in body.campaigns], batch_date=body.batch_date)
    items = _list_campaigns_from_db(include_archived=False)
    return {
        "inserted": inserted,
        "updated": updated,
        "active_count": sum(1 for x in items if x["status"] == "active"),
        "campaigns": items,
    }


@router.post("/campaigns/{campaign_id}/to-topic")
def convert_campaign_to_topic(campaign_id: int) -> dict[str, Any]:
    """一键把平台流量扶持活动（含必带 #标签 与主题切入建议）转为选题库待拍选题"""
    ts = now_str()
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM platform_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="官方扶持活动不存在")
        tags = []
        try:
            tags = json.loads(row["hashtags_json"] or "[]")
        except Exception:
            tags = []
        tag_str = " ".join(tags)
        summary_parts = [f"【{row['platform']}官方扶持】"]
        if row["deadline"]:
            summary_parts.append(f"截止：{row['deadline']}")
        if row["incentive"]:
            summary_parts.append(f"激励：{row['incentive']}")
        if tag_str:
            summary_parts.append(f"必带标签：{tag_str}")
        summary = "｜".join(summary_parts)

        pitch = (row["angle_pitch"] or "").strip() or (
            f"围绕活动必带标签 {tag_str}，结合当前剧目定位与核心角色设定切入创作。"
            if tag_str
            else "结合活动主题与当前剧目核心角色切入创作。"
        )
        cur = conn.execute(
            """
            INSERT INTO topics (
                title, source, source_url, summary, ai_pitch, score_theme, grade, status, created_at, updated_at
            ) VALUES (?, 'official_campaign', ?, ?, ?, 0, 'UNSCORED', 'new', ?, ?)
            """,
            (
                f"借势「{row['title']}」：{row['platform']}扶持活动选题",
                row["source_url"] or "",
                summary,
                pitch,
                ts,
                ts,
            ),
        )
        topic_id = cur.lastrowid
        conn.execute(
            "UPDATE platform_campaigns SET converted_topic_id = ?, updated_at = ? WHERE id = ?",
            (topic_id, ts, campaign_id),
        )
        topic = dict(conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone())
        topic["scores"] = {}
        topic["score_theme"] = 0
        return {"topic": topic}


@router.patch("/campaigns/{campaign_id}")
def patch_platform_campaign(campaign_id: int, body: CampaignPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="无修改字段")
    data["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in data)
    with db_ctx() as conn:
        conn.execute(f"UPDATE platform_campaigns SET {cols} WHERE id = ?", list(data.values()) + [campaign_id])
        row = conn.execute("SELECT * FROM platform_campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="官方扶持活动不存在")
        d = dict(row)
        d["hashtags"] = json.loads(d.get("hashtags_json") or "[]")
        return {"campaign": d}


@router.delete("/campaigns/{campaign_id}")
def delete_platform_campaign(campaign_id: int) -> dict[str, Any]:
    with db_ctx() as conn:
        conn.execute("DELETE FROM platform_campaigns WHERE id = ?", (campaign_id,))
    return {"ok": True}
