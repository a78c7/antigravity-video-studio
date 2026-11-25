import React, { useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FolderOpen,
  Loader2,
  RefreshCw,
  Square,
  XCircle,
  Zap,
} from 'lucide-react';
import { api } from '../api';
import type { AsyncJob, Shot } from '../types';

interface Props {
  episodeId: number | null;
  shots: Shot[];
  refreshSignal: number;
  onActiveJobsChange?: (activeJobs: AsyncJob[]) => void;
  onJobCompleted?: (job: AsyncJob) => void;
  onRevealPath?: (path: string) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const ACTIVE_STATUSES = new Set(['pending', 'running']);

const JOB_TYPE_LABELS: Record<string, string> = {
  cli_frame: '生图（首/尾帧）',
  cli_generate_frame: '生图（首/尾帧）',
  cli_video: '生成分镜视频',
  cli_generate_video: '生成分镜视频',
  cli_bgm: '生成原创配乐',
  cli_generate_bgm: '生成原创配乐',
  cli_tts: '批量中文 TTS 旁白',
  cli_generate_tts: '批量中文 TTS 旁白',
  cli_covers: '双比例自动封面',
  cli_generate_covers: '双比例自动封面',
  local_preview: '本机 1080p 三轨渲染',
  render_local: '本机 1080p 三轨渲染',
  colab_ai_4k30: 'Colab 4K/30fps 母带',
  colab_ai_4k60: 'Colab 4K/60fps 母带',
  colab_ai_1080p60: 'Colab 1080p/60fps 补帧',
  colab_gpu_probe: 'Colab GPU 冒烟探测',
};

function jobLabel(jobType: string): string {
  return JOB_TYPE_LABELS[jobType] || jobType;
}

function statusBadge(status: string, cancelRequested?: number) {
  if (status === 'running') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-sky-100 border border-sky-200 px-2 py-0.5 text-[10px] font-semibold text-sky-800">
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        {cancelRequested ? '取消中' : '运行中'}
      </span>
    );
  }
  if (status === 'pending') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
        排队中
      </span>
    );
  }
  if (status === 'done') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
        <CheckCircle2 className="h-2.5 w-2.5" /> 已完成
      </span>
    );
  }
  if (status === 'cancelled') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 px-2 py-0.5 text-[10px] font-medium text-slate-600">
        <XCircle className="h-2.5 w-2.5" /> 已取消
      </span>
    );
  }
  if (status === 'interrupted') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-[10px] font-medium text-amber-800">
        <AlertCircle className="h-2.5 w-2.5" /> 已中断
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 border border-rose-200 px-2 py-0.5 text-[10px] font-semibold text-rose-700">
      <AlertCircle className="h-2.5 w-2.5" /> 失败
    </span>
  );
}

/**
 * 极简稳健版异步任务队列面板（SQLite 任务表 + 2 秒定时轮询，无复杂 SSE 状态机）：
 * - 页面刷新或切换分集时自动拉取 `/api/jobs?episode_id=...` 恢复进行中任务；
 * - 仅在存在 pending/running 任务时以 2 秒间隔轮询，全部结束后自动停表省资源；
 * - 检测任务状态由进行中变为终态时，通知父组件刷新分镜/时间线并弹出中文提示。
 */
export const AsyncJobsPanel: React.FC<Props> = ({
  episodeId,
  shots,
  refreshSignal,
  onActiveJobsChange,
  onJobCompleted,
  onRevealPath,
  notify,
}) => {
  const [jobs, setJobs] = useState<AsyncJob[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [cancellingIds, setCancellingIds] = useState<Set<number>>(new Set());

  const prevStatusMapRef = useRef<Map<number, string>>(new Map());
  const initializedEpRef = useRef<number | null>(null);
  const onActiveChangeRef = useRef(onActiveJobsChange);
  const onJobDoneRef = useRef(onJobCompleted);
  const notifyRef = useRef(notify);

  useEffect(() => {
    onActiveChangeRef.current = onActiveJobsChange;
  }, [onActiveJobsChange]);

  useEffect(() => {
    onJobDoneRef.current = onJobCompleted;
  }, [onJobCompleted]);

  useEffect(() => {
    notifyRef.current = notify;
  }, [notify]);

  const fetchJobs = async (silent = true) => {
    if (!episodeId) {
      setJobs([]);
      onActiveChangeRef.current?.([]);
      return;
    }
    if (!silent) setLoading(true);
    try {
      const res = await api.get<{ items?: AsyncJob[]; jobs?: AsyncJob[] }>(
        `/api/jobs?episode_id=${episodeId}&limit=16`
      );
      const list = Array.isArray(res.items) ? res.items : Array.isArray(res.jobs) ? res.jobs : [];
      setJobs(list);

      const activeList = list.filter((j) => ACTIVE_STATUSES.has(j.status));
      onActiveChangeRef.current?.(activeList);

      // 首次有进行中任务时自动展开面板，方便直观盯进度
      if (activeList.length > 0 && initializedEpRef.current !== episodeId) {
        setExpanded(true);
      }

      // 检测从 pending/running 跃迁到终态的任务
      if (initializedEpRef.current === episodeId) {
        for (const j of list) {
          const prev = prevStatusMapRef.current.get(j.id);
          if (prev && ACTIVE_STATUSES.has(prev) && !ACTIVE_STATUSES.has(j.status)) {
            if (j.status === 'done') {
              notifyRef.current(`${jobLabel(j.job_type)}已完成！`);
              onJobDoneRef.current?.(j);
            } else if (j.status === 'failed') {
              notifyRef.current(
                `${jobLabel(j.job_type)}失败：${j.error_msg || j.stage_msg || '请展开任务队列查看详情'}`,
                'err'
              );
              onJobDoneRef.current?.(j);
            } else if (j.status === 'cancelled') {
              notifyRef.current(`${jobLabel(j.job_type)}已取消`);
              onJobDoneRef.current?.(j);
            }
          }
        }
      }

      const nextMap = new Map<number, string>();
      for (const j of list) nextMap.set(j.id, j.status);
      prevStatusMapRef.current = nextMap;
      initializedEpRef.current = episodeId;
    } catch {
      // 轮询偶发失败静默忽略，不打断导演创作
    } finally {
      if (!silent) setLoading(false);
    }
  };

  // 分集切换或父组件提交新任务时立即拉取
  useEffect(() => {
    if (initializedEpRef.current !== episodeId) {
      prevStatusMapRef.current = new Map();
    }
    fetchJobs(true);
  }, [episodeId, refreshSignal]);

  // 有活跃任务时自动展开 + 启动 2 秒简单轮询
  const activeCount = jobs.filter((j) => ACTIVE_STATUSES.has(j.status)).length;
  useEffect(() => {
    if (refreshSignal > 0) {
      setExpanded(true);
    }
  }, [refreshSignal]);

  useEffect(() => {
    if (!episodeId || activeCount === 0) return;
    const timer = window.setInterval(() => {
      fetchJobs(true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [episodeId, activeCount]);

  const handleCancelJob = async (jobId: number) => {
    setCancellingIds((prev) => {
      const next = new Set(prev);
      next.add(jobId);
      return next;
    });
    try {
      await api.post(`/api/jobs/${jobId}/cancel`, {});
      notify('已发送取消指令，任务将在当前安全节点停止');
      await fetchJobs(true);
    } catch (err: any) {
      notify(err?.message || '取消任务失败', 'err');
    } finally {
      setCancellingIds((prev) => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    }
  };

  if (!episodeId || jobs.length === 0) return null;

  const doneCount = jobs.filter((j) => j.status === 'done').length;
  const failCount = jobs.filter((j) => j.status === 'failed' || j.status === 'interrupted').length;

  const shotNoMap = new Map<number, number>();
  for (const s of shots) shotNoMap.set(s.id, s.shot_no);

  return (
    <div
      className={`studio-card rounded-2xl overflow-hidden transition ${
        activeCount > 0 ? 'border-sky-300 ring-1 ring-sky-100' : 'border-slate-200'
      }`}
    >
      <div
        onClick={() => setExpanded((v) => !v)}
        className={`px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 cursor-pointer select-none ${
          activeCount > 0 ? 'bg-sky-50/75' : 'bg-slate-50/70'
        }`}
      >
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="inline-flex items-center gap-1.5 font-semibold text-slate-900">
            {activeCount > 0 ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-600" />
            ) : (
              <Zap className="h-3.5 w-3.5 text-slate-500" />
            )}
            后台异步任务队列
          </span>
          {activeCount > 0 && (
            <span className="rounded-full bg-sky-600 px-2 py-0.5 text-[10px] font-bold text-white">
              进行中 {activeCount}
            </span>
          )}
          {doneCount > 0 && (
            <span className="rounded-full bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
              已完成 {doneCount}
            </span>
          )}
          {failCount > 0 && (
            <span className="rounded-full bg-rose-50 border border-rose-200 px-2 py-0.5 text-[10px] font-medium text-rose-700">
              异常 {failCount}
            </span>
          )}
          <span className="text-[11px] text-slate-500 hidden sm:inline">
            支持多镜并发提交，可自由切换步骤与镜头，刷新页面自动恢复进度
          </span>
        </div>

        <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            onClick={() => fetchJobs(false)}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100 cursor-pointer"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </button>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 cursor-pointer"
          >
            {expanded ? (
              <>
                收起 <ChevronUp className="h-3 w-3" />
              </>
            ) : (
              <>
                展开 ({jobs.length}) <ChevronDown className="h-3 w-3" />
              </>
            )}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="divide-y divide-slate-100 max-h-64 overflow-y-auto bg-white">
          {jobs.map((j) => {
            const isActive = ACTIVE_STATUSES.has(j.status);
            const pct =
              typeof j.progress_pct === 'number'
                ? Math.max(0, Math.min(100, Math.round(j.progress_pct)))
                : j.status === 'done'
                  ? 100
                  : 0;
            const sno = j.shot_id ? shotNoMap.get(j.shot_id) : undefined;
            const cancelling = cancellingIds.has(j.id) || Boolean(j.cancel_requested);

            return (
              <div key={j.id} className="px-4 py-2.5 space-y-1.5 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5 min-w-0">
                    <span className="font-mono text-[10px] text-slate-400">#{j.id}</span>
                    {sno !== undefined && (
                      <span className="rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-bold text-sky-700">
                        S{String(sno).padStart(2, '0')}
                      </span>
                    )}
                    <span className="font-semibold text-slate-900">{jobLabel(j.job_type)}</span>
                    {statusBadge(j.status, j.cancel_requested)}
                    <span className="text-[10px] text-slate-400">{j.created_at}</span>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {isActive && (
                      <span className="font-mono text-[11px] font-semibold text-sky-700">{pct}%</span>
                    )}
                    {isActive && (
                      <button
                        type="button"
                        disabled={cancelling}
                        onClick={() => handleCancelJob(j.id)}
                        className="inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-45 cursor-pointer"
                      >
                        <Square className="h-2.5 w-2.5 fill-current" />
                        {cancelling ? '取消中...' : '取消'}
                      </button>
                    )}
                    {j.status === 'done' && j.output_path && onRevealPath && (
                      <button
                        type="button"
                        onClick={() => onRevealPath(j.output_path!)}
                        className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 cursor-pointer"
                      >
                        <FolderOpen className="h-3 w-3" /> 打开位置
                      </button>
                    )}
                  </div>
                </div>

                {/* 进度条 */}
                {(isActive || pct > 0) && j.status !== 'failed' && j.status !== 'cancelled' && (
                  <div className="h-1.5 w-full rounded-full bg-slate-100 overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all duration-300 ${
                        j.status === 'done' ? 'bg-emerald-500' : 'bg-sky-500'
                      }`}
                      style={{ width: `${Math.max(isActive ? 6 : 0, pct)}%` }}
                    />
                  </div>
                )}

                {j.stage_msg && (
                  <div className="text-[11px] text-slate-600 leading-snug">{j.stage_msg}</div>
                )}
                {j.error_msg && (
                  <div className="rounded-lg border border-rose-200 bg-rose-50/70 px-2.5 py-1 text-[11px] text-rose-800 break-words">
                    {j.error_msg}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
