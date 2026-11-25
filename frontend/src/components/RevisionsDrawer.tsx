import React from 'react';
import { History, RefreshCw, X } from 'lucide-react';
import { roleLabel } from '../api';
import type { RevisionItem } from '../types';

const FIELD_LABELS: Record<string, string> = {
  title: '本集标题',
  hook_0_3s: '0–3 秒视觉钩子',
  script_text: '拍摄剧本正文',
  ending_cta: '结尾互动引导',
  next_ep_hook: '下集悬念钩子',
  continuity_summary: '跨集连续性摘要',
  bgm_prompt: '配乐方向说明',
  shot_title: '镜头标题',
  camera_type: '景别 / 运镜',
  duration_sec: '镜头时长',
  cn_action_desc: '中文画面与动作',
  cn_dialogue: '台词 / 旁白',
  subtitle_text: '屏幕字幕',
  audio_strategy: '声音策略',
  frame_prompt_en: '首帧画面描述',
  end_frame_prompt_en: '尾帧画面描述',
  video_prompt_en: '动态画面描述',
};

export function formatFieldLabel(fieldName: string): string {
  return FIELD_LABELS[fieldName] || fieldName;
}

interface Props {
  open: boolean;
  onClose: () => void;
  title?: string;
  revisions: RevisionItem[];
  loading?: boolean;
  onRefresh?: () => void;
}

export const RevisionsDrawer: React.FC<Props> = ({
  open,
  onClose,
  title = '修改留痕时间线',
  revisions,
  loading = false,
  onRefresh,
}) => {
  if (!open) return null;

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex justify-end bg-slate-900/35 backdrop-blur-xs"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg bg-white h-full shadow-2xl border-l border-slate-200 flex flex-col"
      >
        <div className="px-4 py-3.5 border-b border-slate-200 bg-slate-50/80 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <History className="h-4 w-4 text-sky-600 shrink-0" />
            <div className="min-w-0">
              <div className="text-sm font-semibold text-slate-900 truncate">{title}</div>
              <div className="text-[11px] text-slate-500">共 {revisions.length} 条改稿记录（自动按时间倒序）</div>
            </div>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {onRefresh && (
              <button
                type="button"
                onClick={onRefresh}
                disabled={loading}
                className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 hover:bg-slate-100 cursor-pointer"
                title="刷新留痕"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-200/70 hover:text-slate-900 cursor-pointer"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3 bg-[#f8fafc]">
          {revisions.map((rev) => (
            <div key={rev.id} className="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2.5 shadow-2xs">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-semibold text-slate-900">{rev.actor_name || rev.actor_username}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-medium border ${
                      rev.actor_role === 'admin'
                        ? 'bg-sky-50 text-sky-700 border-sky-200'
                        : 'bg-amber-50 text-amber-800 border-amber-200'
                    }`}
                  >
                    {roleLabel(rev.actor_role)}
                  </span>
                  <span className="rounded bg-slate-100 border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-700 font-medium">
                    {rev.entity_type === 'shot' ? `分镜 #${rev.entity_id}` : '剧本'} · {rev.field_label || formatFieldLabel(rev.field_name)}
                  </span>
                </div>
                <span className="text-[11px] text-slate-400 font-mono">{rev.created_at}</span>
              </div>

              {rev.note && (
                <div className="rounded-lg bg-sky-50/70 border border-sky-200/70 px-2.5 py-1.5 text-[11px] text-sky-900">
                  备注：{rev.note}
                </div>
              )}

              <div className="grid grid-cols-1 gap-2 text-xs">
                <div className="rounded-lg bg-rose-50/70 border border-rose-200/70 p-2.5 space-y-1">
                  <div className="text-[10px] font-semibold text-rose-700">改前原文</div>
                  <div className="whitespace-pre-wrap break-words text-rose-900/90 leading-relaxed max-h-32 overflow-y-auto">
                    {rev.old_value ? rev.old_value : '（空）'}
                  </div>
                </div>
                <div className="rounded-lg bg-emerald-50/70 border border-emerald-200/70 p-2.5 space-y-1">
                  <div className="text-[10px] font-semibold text-emerald-700">改后新文</div>
                  <div className="whitespace-pre-wrap break-words text-emerald-950 leading-relaxed max-h-36 overflow-y-auto">
                    {rev.new_value ? rev.new_value : '（清空）'}
                  </div>
                </div>
              </div>
            </div>
          ))}

          {revisions.length === 0 && !loading && (
            <div className="py-16 text-center text-xs text-slate-400 space-y-1">
              <div>暂无修改留痕记录</div>
              <div>每次保存剧本或分镜修改，系统会自动记录修改前后对比。</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
