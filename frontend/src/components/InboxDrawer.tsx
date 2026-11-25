import React, { useState } from 'react';
import { Download, Film, Image as ImageIcon, Music, RefreshCw, X, Check, Filter, Trash2, Pencil } from 'lucide-react';
import { fmtBytes, mediaUrl } from '../api';
import type { InboxItem, InboxState } from '../types';

interface Props {
  open: boolean;
  onClose: () => void;
  inbox: InboxState;
  keepChineseCopy: boolean;
  onToggleKeepChineseCopy: (val: boolean) => Promise<void>;
  onScan: () => Promise<void>;
  onSwitchMode: (mode: 'flow_only' | 'all') => Promise<void>;
  onClearAll: () => Promise<void>;
  onDismiss: (id: string) => Promise<void>;
  onTrashItem: (id: string) => Promise<void>;
  onTrashAll: () => Promise<void>;
  onRenameItem: (id: string, newName: string) => Promise<void>;
  activeShotLabel?: string;
  onClaimToShotVideo?: (item: InboxItem) => Promise<void>;
  onClaimToStartFrame?: (item: InboxItem) => Promise<void>;
  onClaimToEndFrame?: (item: InboxItem) => Promise<void>;
  onClaimToBgm?: (item: InboxItem) => Promise<void>;
}

export const InboxDrawer: React.FC<Props> = ({
  open,
  onClose,
  inbox,
  keepChineseCopy,
  onToggleKeepChineseCopy,
  onScan,
  onSwitchMode,
  onClearAll,
  onDismiss,
  onTrashItem,
  onTrashAll,
  onRenameItem,
  activeShotLabel,
  onClaimToShotVideo,
  onClaimToStartFrame,
  onClaimToEndFrame,
  onClaimToBgm,
}) => {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [renamingBusy, setRenamingBusy] = useState(false);

  if (!open) return null;

  const filterMode = inbox.filter_mode || 'flow_only';

  const startRename = (item: InboxItem) => {
    const dot = item.filename.lastIndexOf('.');
    const stem = dot > 0 ? item.filename.slice(0, dot) : item.filename;
    setRenamingId(item.id);
    setRenameDraft(stem);
  };

  const submitRename = async (itemId: string) => {
    const trimmed = renameDraft.trim();
    if (!trimmed) return;
    setRenamingBusy(true);
    try {
      await onRenameItem(itemId, trimmed);
      setRenamingId(null);
      setRenameDraft('');
    } finally {
      setRenamingBusy(false);
    }
  };

  return (
    <div className="fixed inset-y-0 right-0 z-50 w-96 max-w-[92vw] border-l border-slate-200 bg-white/98 shadow-2xl backdrop-blur flex flex-col">
      <div className="border-b border-slate-200 px-4 py-3.5 space-y-2.5 bg-slate-50/70">
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <Download className="h-4 w-4 text-sky-600" /> Flow 专属收件箱
            </div>
            <div className="text-[11px] text-slate-500 mt-0.5 truncate max-w-[240px]">
              {inbox.watch_dir || '~/Downloads'}（{inbox.status === 'running' ? '监听中' : inbox.status}）
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            {inbox.items.length > 0 && (
              <>
                <button
                  onClick={onTrashAll}
                  title="把当前列表里剩余的 Flow 废图安全移入系统废纸篓/回收站（绝不动普通文件）"
                  className="inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2 py-1 text-[10px] font-medium text-rose-700 hover:bg-rose-100 cursor-pointer"
                >
                  <Trash2 className="h-3 w-3" /> 废图进回收站
                </button>
                <button
                  onClick={onClearAll}
                  title="仅清空收件箱视图（不删磁盘原文件）"
                  className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[10px] text-slate-600 hover:bg-slate-100 cursor-pointer"
                >
                  清空视图
                </button>
              </>
            )}
            <button
              onClick={onScan}
              title="扫描近 30 分钟刚下载的素材"
              className="rounded-md border border-slate-200 bg-white p-1.5 text-slate-600 hover:bg-slate-100 cursor-pointer"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={onClose}
              className="rounded-md p-1.5 text-slate-500 hover:bg-slate-200/70 hover:text-slate-900 cursor-pointer"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 清爽模式切换条：默认仅收 Google Flow，挡掉日常截屏与杂图 */}
        <div className="flex items-center justify-between gap-2 text-[11px]">
          <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5">
            <button
              onClick={() => onSwitchMode('flow_only')}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 transition cursor-pointer ${
                filterMode === 'flow_only'
                  ? 'bg-white text-emerald-700 font-semibold shadow-xs'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Filter className="h-3 w-3" /> 仅收 Flow
            </button>
            <button
              onClick={() => onSwitchMode('all')}
              className={`rounded-md px-2 py-1 transition cursor-pointer ${
                filterMode === 'all'
                  ? 'bg-white text-sky-700 font-semibold shadow-xs'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              全部下载
            </button>
          </div>
          {filterMode === 'flow_only' && (
            <span className="text-[10px] text-slate-500">
              {inbox.filtered_skipped ? `已挡掉 ${inbox.filtered_skipped} 个日常杂图` : '自动屏蔽截屏/杂图'}
            </span>
          )}
        </div>

        {/* 素材直拖中文副本总开关 + 自动中文命名状态 */}
        <div className="space-y-1.5">
          <label
            className="flex items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50/70 px-2.5 py-1.5 text-[11px] text-sky-900 cursor-pointer"
            title="认领资产图/首尾帧归档进媒体库后，自动同步中文名副本到直拖素材目录，同批落选图自动进回收站"
          >
            <input
              type="checkbox"
              checked={keepChineseCopy}
              onChange={(e) => onToggleKeepChineseCopy(e.target.checked)}
              className="accent-sky-600"
            />
            <span>认领自动同步直拖素材目录（中文短名+清废图）</span>
          </label>
          {inbox.active_context_label && (
            <div className="rounded-md bg-emerald-50 border border-emerald-200 px-2 py-1 text-[10px] text-emerald-800 truncate">
              自动命名中：新下载的 Flow 乱码图将自动命名为 <strong className="font-mono">待选_{inbox.active_context_label}</strong>
            </div>
          )}
        </div>
      </div>

      {inbox.error && (
        <div className="mx-3 mt-3 rounded-lg bg-rose-50 border border-rose-200 p-2.5 text-xs text-rose-700">
          {inbox.error}
        </div>
      )}

      {activeShotLabel && (
        <div className="mx-3 mt-3 rounded-lg bg-sky-50 border border-sky-200 px-3 py-2 text-xs text-sky-800">
          当前激活槽位：<span className="font-semibold text-slate-900">{activeShotLabel}</span>
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-3 space-y-3 bg-[#f8fafc]">
        {inbox.items.map((item) => (
          <div key={item.id} className="rounded-xl border border-slate-200 bg-white p-3 space-y-2 shadow-2xs">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                {item.media_type === 'video' && <Film className="h-4 w-4 shrink-0 text-sky-600" />}
                {item.media_type === 'image' && <ImageIcon className="h-4 w-4 shrink-0 text-amber-600" />}
                {item.media_type === 'audio' && <Music className="h-4 w-4 shrink-0 text-emerald-600" />}
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium text-slate-900" title={item.filename}>
                    {item.filename}
                  </div>
                  <div className="text-[10px] text-slate-500 flex flex-wrap items-center gap-1 mt-0.5">
                    {item.origin_label && (
                      <span className="rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.2 text-emerald-700 font-medium">
                        {item.origin_label}
                      </span>
                    )}
                    <span>
                      {item.resolution_label || item.media_type} · {fmtBytes(item.size_bytes)}
                      {item.duration_sec > 0 ? ` · ${item.duration_sec}s` : ''} · {item.detected_at.slice(11)}
                    </span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button
                  type="button"
                  onClick={() => (renamingId === item.id ? setRenamingId(null) : startRename(item))}
                  title="原地改为中文名（直接重命名下载监听目录中的文件，方便直拖进 Flow）"
                  className={`inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5 text-[10px] font-medium cursor-pointer ${
                    renamingId === item.id
                      ? 'bg-sky-600 border-sky-600 text-white'
                      : 'bg-slate-50 border-slate-200 text-slate-600 hover:bg-sky-50 hover:border-sky-200 hover:text-sky-700'
                  }`}
                >
                  <Pencil className="h-2.5 w-2.5" /> 改中文名
                </button>
                <button
                  type="button"
                  onClick={() => onTrashItem(item.id)}
                  title="不要这张：移入系统废纸篓/回收站（随时可恢复）"
                  className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
                <button
                  onClick={() => onDismiss(item.id)}
                  title="仅从列表忽略（不删文件）"
                  className="text-slate-400 hover:text-slate-700 cursor-pointer"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            {renamingId === item.id && (
              <div className="flex items-center gap-1.5 rounded-lg bg-sky-50/70 border border-sky-200 p-1.5">
                <input
                  autoFocus
                  value={renameDraft}
                  onFocus={(e) => e.target.select()}
                  onChange={(e) => setRenameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      submitRename(item.id);
                    } else if (e.key === 'Escape') {
                      setRenamingId(null);
                    }
                  }}
                  placeholder="例：角色_主角01 或 S01首帧（≤6字不截断）"
                  className="studio-input flex-1 min-w-0 rounded px-2 py-1 text-[11px]"
                />
                <button
                  type="button"
                  disabled={renamingBusy || !renameDraft.trim()}
                  onClick={() => submitRename(item.id)}
                  className="rounded bg-sky-600 px-2 py-1 text-[10px] font-semibold text-white hover:bg-sky-700 disabled:opacity-40 cursor-pointer shrink-0"
                >
                  确定
                </button>
              </div>
            )}

            {item.media_type === 'image' && (
              <img
                src={mediaUrl(item.path)}
                alt={item.filename}
                className="max-h-36 w-full rounded-lg object-cover bg-slate-900"
              />
            )}
            {item.media_type === 'video' && (
              <video
                src={mediaUrl(item.path)}
                controls
                muted
                className="max-h-40 w-full rounded-lg bg-slate-900"
              />
            )}

            <div className="flex flex-wrap gap-1.5 pt-1">
              {item.media_type === 'video' && onClaimToShotVideo && (
                <button
                  onClick={() => onClaimToShotVideo(item)}
                  className="flex-1 inline-flex items-center justify-center gap-1 rounded-md bg-sky-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                >
                  <Check className="h-3 w-3" /> 认领为当前镜视频
                </button>
              )}
              {item.media_type === 'image' && onClaimToStartFrame && (
                <button
                  onClick={() => onClaimToStartFrame(item)}
                  className="flex-1 inline-flex items-center justify-center gap-1 rounded-md bg-amber-500 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-amber-600 cursor-pointer"
                >
                  <Check className="h-3 w-3" /> 认领为首帧图
                </button>
              )}
              {item.media_type === 'image' && onClaimToEndFrame && (
                <button
                  onClick={() => onClaimToEndFrame(item)}
                  className="flex-1 inline-flex items-center justify-center gap-1 rounded-md bg-violet-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-violet-700 cursor-pointer"
                >
                  <Check className="h-3 w-3" /> 认领为尾帧图
                </button>
              )}
              {item.media_type === 'audio' && onClaimToBgm && (
                <button
                  onClick={() => onClaimToBgm(item)}
                  className="flex-1 inline-flex items-center justify-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-emerald-700 cursor-pointer"
                >
                  <Check className="h-3 w-3" /> 认领为本集配乐
                </button>
              )}
            </div>
          </div>
        ))}
        {inbox.items.length === 0 && (
          <div className="py-16 text-center text-xs text-slate-500 space-y-2">
            <div>暂无刚下载的 Flow 素材</div>
            <div>在官方 Flow 网页点下载后，文件会立刻出现在这里，一键认领自动规范重命名。</div>
          </div>
        )}
      </div>
    </div>
  );
};
