"""
基于 SQLite `render_jobs` + `ThreadPoolExecutor(max_workers=4)` 的轻量持久化异步任务引擎：
- 零外部中间件（不依赖 Redis/Celery），单机开箱即用、刷新页面进度不丢失；
- 支持任务入队、实时进度/阶段/日志上报、协作式取消检查、结果 JSON 持久化、错误信息脱敏；
- 服务启动时自动收敛残留 `running` / `pending` 僵尸任务为 `interrupted`，避免 UI 永久假死。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .db import db_ctx, now_str

# 受控并发线程池：既允许用户多镜并发出图/出视频，又避免把本机 CPU/网络或 API 限流打爆
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agvs-job")
_DB_WRITE_LOCK = threading.Lock()
_CANCEL_LOCK = threading.Lock()
_CANCELLED_JOB_IDS: set[int] = set()

_MAX_LOG_CHARS = 32000
_TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted"}

# 错误信息敏感凭据脱敏规则（覆盖常见 API Key / Bearer Token / URL query key）
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***REDACTED***"),
    (re.compile(r"sk-[0-9A-Za-z_\-]{16,}"), "sk-***REDACTED***"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-~+/=]{12,}"), r"\1***REDACTED***"),
    (re.compile(r"(?i)([?&](?:api_?key|key|token|access_token)=)[^&\s'\"]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)((?:GEMINI_API_KEY|DEEPSEEK_API_KEY|SYNC_API_TOKEN|NTFY_TOKEN|SESSION_SECRET)\s*[:=]\s*['\"]?)[^\s'\",;]+"), r"\1***REDACTED***"),
)


class JobCancelledError(Exception):
    """任务在执行过程中检测到用户主动取消时抛出"""


def sanitize_job_error(raw: str) -> str:
    """对任务异常或日志中的潜在密钥进行脱敏并截断过长栈文本"""
    text = (raw or "").strip()
    if not text:
        return ""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    if len(text) > 1500:
        text = text[:1500] + "...(已截断)"
    return text


def _append_log(existing: str, line: str) -> str:
    clean_line = sanitize_job_error(line)
    if not clean_line:
        return existing or ""
    if not existing:
        merged = clean_line
    else:
        merged = f"{existing.rstrip()}\n{clean_line}"
    if len(merged) > _MAX_LOG_CHARS:
        merged = merged[-_MAX_LOG_CHARS:]
    return merged


def _row_to_job_dict(row: Any) -> dict[str, Any]:
    """将 SQLite render_jobs 行对象规范化为对外字典（自动反序列化 result_json -> result）"""
    d = dict(row)
    raw_result = d.get("result_json") or "{}"
    try:
        parsed_result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if not isinstance(parsed_result, dict):
            parsed_result = {"value": parsed_result}
    except Exception:
        parsed_result = {}
    d["result"] = parsed_result
    d["progress_pct"] = int(d.get("progress_pct") or 0)
    d["stage_msg"] = str(d.get("stage_msg") or "")
    d["provider_task_id"] = str(d.get("provider_task_id") or "")
    d["cancel_requested"] = bool(d.get("cancel_requested"))
    d["output_path"] = str(d.get("output_path") or "")
    d["error_msg"] = str(d.get("error_msg") or "")
    d["log_text"] = str(d.get("log_text") or "")
    return d


class JobContext:
    """传递给后台任务执行函数的上下文对象：支持上报进度/日志与轮询取消信号"""

    def __init__(self, job_id: int) -> None:
        self.job_id = int(job_id)

    def update(
        self,
        progress_pct: int | None = None,
        stage_msg: str | None = None,
        log_line: str | None = None,
        provider_task_id: str | None = None,
    ) -> None:
        """实时更新任务进度百分比、当前阶段提示、增量日志或第三方任务 ID"""
        sets: list[str] = []
        vals: list[Any] = []
        if progress_pct is not None:
            clamped = max(0, min(100, int(progress_pct)))
            sets.append("progress_pct = ?")
            vals.append(clamped)
        if stage_msg is not None:
            sets.append("stage_msg = ?")
            vals.append(sanitize_job_error(str(stage_msg))[:300])
        if provider_task_id is not None:
            sets.append("provider_task_id = ?")
            vals.append(str(provider_task_id).strip()[:200])

        with _DB_WRITE_LOCK:
            with db_ctx() as conn:
                if log_line is not None and str(log_line).strip():
                    cur_row = conn.execute(
                        "SELECT log_text FROM render_jobs WHERE id = ?",
                        (self.job_id,),
                    ).fetchone()
                    existing_log = (cur_row["log_text"] or "") if cur_row else ""
                    sets.append("log_text = ?")
                    vals.append(_append_log(existing_log, str(log_line)))
                if not sets:
                    return
                sets.append("updated_at = ?")
                vals.append(now_str())
                vals.append(self.job_id)
                conn.execute(f"UPDATE render_jobs SET {', '.join(sets)} WHERE id = ?", vals)

    def is_cancelled(self) -> bool:
        """检查该任务是否已收到取消请求（同时校验内存标记与 SQLite 持久化标记）"""
        with _CANCEL_LOCK:
            if self.job_id in _CANCELLED_JOB_IDS:
                return True
        try:
            with db_ctx() as conn:
                row = conn.execute(
                    "SELECT cancel_requested, status FROM render_jobs WHERE id = ?",
                    (self.job_id,),
                ).fetchone()
                if not row:
                    return False
                if bool(row["cancel_requested"]) or str(row["status"]) == "cancelled":
                    with _CANCEL_LOCK:
                        _CANCELLED_JOB_IDS.add(self.job_id)
                    return True
        except Exception:
            return False
        return False

    def check_cancelled(self) -> None:
        """若任务已被取消，立刻抛出 JobCancelledError 中断后续执行"""
        if self.is_cancelled():
            raise JobCancelledError("任务已被用户取消")


def _mark_job_cancelled(job_id: int, reason: str = "任务已取消") -> None:
    ts = now_str()
    safe_reason = sanitize_job_error(reason) or "任务已取消"
    with _DB_WRITE_LOCK:
        with db_ctx() as conn:
            row = conn.execute("SELECT log_text FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
            existing_log = (row["log_text"] or "") if row else ""
            conn.execute(
                """
                UPDATE render_jobs
                SET status = 'cancelled',
                    cancel_requested = 1,
                    stage_msg = ?,
                    error_msg = '',
                    log_text = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (safe_reason[:300], _append_log(existing_log, f"[已取消] {safe_reason}"), ts, job_id),
            )


def _run_job(job_id: int, target_fn: Callable[[JobContext], dict[str, Any]]) -> None:
    ctx = JobContext(job_id)
    if ctx.is_cancelled():
        _mark_job_cancelled(job_id, "任务在排队阶段已取消")
        return

    ts_start = now_str()
    with _DB_WRITE_LOCK:
        with db_ctx() as conn:
            cur = conn.execute(
                """
                UPDATE render_jobs
                SET status = 'running',
                    progress_pct = CASE WHEN progress_pct < 1 THEN 1 ELSE progress_pct END,
                    stage_msg = CASE WHEN stage_msg = '' OR stage_msg = '任务已入队' THEN '任务开始执行...' ELSE stage_msg END,
                    updated_at = ?
                WHERE id = ? AND status = 'pending' AND cancel_requested = 0
                """,
                (ts_start, job_id),
            )
            if cur.rowcount == 0:
                row = conn.execute("SELECT status, cancel_requested FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
                if not row or row["status"] in _TERMINAL_STATUSES or row["cancel_requested"]:
                    if row and row["status"] not in _TERMINAL_STATUSES:
                        conn.execute(
                            "UPDATE render_jobs SET status = 'cancelled', stage_msg = '任务已取消', updated_at = ? WHERE id = ?",
                            (ts_start, job_id),
                        )
                    return

    try:
        raw_res = target_fn(ctx)
        if ctx.is_cancelled():
            _mark_job_cancelled(job_id, "任务已被用户取消")
            return

        res_dict: dict[str, Any] = raw_res if isinstance(raw_res, dict) else {"value": raw_res}
        output_path = str(
            res_dict.get("output_path")
            or res_dict.get("file_path")
            or res_dict.get("proof_path")
            or res_dict.get("fcpxml_path")
            or ""
        )
        res_log = str(res_dict.get("log") or "").strip()
        res_json_str = json.dumps(res_dict, ensure_ascii=False, default=str)
        ts_done = now_str()

        with _DB_WRITE_LOCK:
            with db_ctx() as conn:
                row = conn.execute("SELECT log_text FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
                existing_log = (row["log_text"] or "") if row else ""
                final_log = _append_log(existing_log, res_log) if res_log else existing_log
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET status = 'done',
                        progress_pct = 100,
                        stage_msg = '任务已完成',
                        output_path = CASE WHEN ? != '' THEN ? ELSE output_path END,
                        result_json = ?,
                        error_msg = '',
                        log_text = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (output_path, output_path, res_json_str, final_log, ts_done, job_id),
                )
    except JobCancelledError as exc:
        _mark_job_cancelled(job_id, str(exc) or "任务已被用户取消")
    except Exception as exc:
        safe_err = sanitize_job_error(str(exc)) or exc.__class__.__name__
        ts_fail = now_str()
        with _DB_WRITE_LOCK:
            with db_ctx() as conn:
                row = conn.execute("SELECT log_text FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
                existing_log = (row["log_text"] or "") if row else ""
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET status = 'failed',
                        stage_msg = '任务执行失败',
                        error_msg = ?,
                        log_text = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (safe_err, _append_log(existing_log, f"[失败] {safe_err}"), ts_fail, job_id),
                )
    finally:
        with _CANCEL_LOCK:
            _CANCELLED_JOB_IDS.discard(job_id)


def submit_job(
    job_type: str,
    target_fn: Callable[[JobContext], dict[str, Any]],
    *,
    episode_id: int,
    shot_id: int | None = None,
    target_host: str = "local",
    stage_msg: str = "任务已入队",
    provider_task_id: str = "",
) -> dict[str, Any]:
    """
    提交一个异步后台任务：
    1. 先写入 SQLite `render_jobs`（status='pending'）并读取完整初始任务对象；
    2. 投递至后台线程池执行，立即返回初始 job dict，避免阻塞 FastAPI 请求。
    """
    ts = now_str()
    clean_stage = sanitize_job_error(stage_msg or "任务已入队")[:300]
    with _DB_WRITE_LOCK:
        with db_ctx() as conn:
            cur = conn.execute(
                """
                INSERT INTO render_jobs (
                    episode_id, shot_id, job_type, target_host, status,
                    progress_pct, stage_msg, provider_task_id, cancel_requested,
                    output_path, result_json, error_msg, log_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, 0, '', '{}', '', '', ?, ?)
                """,
                (
                    int(episode_id),
                    int(shot_id) if shot_id is not None else None,
                    str(job_type),
                    str(target_host or "local"),
                    clean_stage,
                    str(provider_task_id or "").strip()[:200],
                    ts,
                    ts,
                ),
            )
            job_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
            initial_job = _row_to_job_dict(row)

    _EXECUTOR.submit(_run_job, job_id, target_fn)
    return initial_job


def get_job(job_id: int) -> dict[str, Any] | None:
    """按 ID 查询单个任务详情（自动反序列化 result_json 为 result）"""
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM render_jobs WHERE id = ?", (int(job_id),)).fetchone()
        if not row:
            return None
        return _row_to_job_dict(row)


def list_jobs(
    episode_id: int | None = None,
    status: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """按分集与状态倒序查询任务列表"""
    clamped_limit = max(1, min(200, int(limit or 30)))
    clauses: list[str] = []
    params: list[Any] = []
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(int(episode_id))
    if status is not None and str(status).strip():
        statuses = [s.strip() for s in str(status).split(",") if s.strip()]
        if len(statuses) == 1:
            clauses.append("status = ?")
            params.append(statuses[0])
        elif len(statuses) > 1:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(clamped_limit)
    with db_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM render_jobs {where_sql} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_job_dict(r) for r in rows]


def cancel_job(job_id: int) -> dict[str, Any] | None:
    """
    请求取消任务：
    - 若仍处于 `pending` 排队状态，直接标为 `cancelled`；
    - 若处于 `running` 执行状态，设置 `cancel_requested = 1` 由后台协作中止；
    - 若已处于终态（done/failed/cancelled/interrupted），保持原状直接返回。
    """
    jid = int(job_id)
    ts = now_str()
    with _CANCEL_LOCK:
        _CANCELLED_JOB_IDS.add(jid)

    with _DB_WRITE_LOCK:
        with db_ctx() as conn:
            row = conn.execute("SELECT * FROM render_jobs WHERE id = ?", (jid,)).fetchone()
            if not row:
                with _CANCEL_LOCK:
                    _CANCELLED_JOB_IDS.discard(jid)
                return None

            cur_status = str(row["status"])
            if cur_status == "pending":
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET cancel_requested = 1,
                        status = 'cancelled',
                        stage_msg = '任务已取消',
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (ts, jid),
                )
            elif cur_status == "running":
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET cancel_requested = 1,
                        stage_msg = '正在取消任务...',
                        updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (ts, jid),
                )
            updated = conn.execute("SELECT * FROM render_jobs WHERE id = ?", (jid,)).fetchone()
            return _row_to_job_dict(updated) if updated else None


def recover_stale_jobs_on_startup() -> int:
    """
    服务启动时收敛僵尸任务：
    将上次异常退出或重启遗留的 `running` / `pending` 任务统一标为 `interrupted`，
    并注明原因提示用户核对结果或重新发起。
    """
    reason = "服务重启导致任务中断，请核对已生成结果或重新发起"
    ts = now_str()
    with _DB_WRITE_LOCK:
        with db_ctx() as conn:
            stale_rows = conn.execute(
                "SELECT id, log_text FROM render_jobs WHERE status IN ('running', 'pending')"
            ).fetchall()
            if not stale_rows:
                return 0
            for r in stale_rows:
                new_log = _append_log(r["log_text"] or "", f"[中断恢复] {reason}")
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET status = 'interrupted',
                        stage_msg = ?,
                        error_msg = ?,
                        log_text = ?,
                        updated_at = ?
                    WHERE id = ? AND status IN ('running', 'pending')
                    """,
                    (reason, reason, new_log, ts, r["id"]),
                )
            return len(stale_rows)
