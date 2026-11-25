import React, { Suspense, useState } from 'react';
import { Clapperboard, Film, Layers, Plus, Sparkles, Trash2, ArrowRight } from 'lucide-react';
import { api } from '../api';
import type { AppMode, AuthUser, Project } from '../types';

const LocalProjectsBanner = React.lazy(() => import('../components/LocalProjectsBanner'));

interface Props {
  projects: Project[];
  appMode: AppMode;
  user: AuthUser | null;
  onRefresh: () => Promise<void>;
  onOpenProject: (project: Project) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

export const ProjectsPage: React.FC<Props> = ({
  projects,
  appMode,
  user,
  onRefresh,
  onOpenProject,
  notify,
}) => {
  const isCloud = appMode === 'cloud_collab';
  const isAdmin = !isCloud || user?.role === 'admin';

  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [track, setTrack] = useState('剧情轻喜剧');
  const [visualMode, setVisualMode] = useState('写实电影场景 + 3D 精致虚拟搭档');
  const [aspectRatio, setAspectRatio] = useState('9:16');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const res = await api.post<{ project: Project }>('/api/projects', {
        name: name.trim(),
        track,
        visual_mode: visualMode,
        aspect_ratio: aspectRatio,
        description: description.trim(),
      });
      await onRefresh();
      setCreating(false);
      setName('');
      setDescription('');
      notify(`已创建剧目《${res.project.name}》`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (e: React.MouseEvent, p: Project) => {
    e.stopPropagation();
    if (!confirm(`确定删除剧目《${p.name}》及其全部剧集和分镜？已导出的成片不会删除。`)) return;
    try {
      await api.del(`/api/projects/${p.id}`);
      await onRefresh();
      notify(`已删除《${p.name}》`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl sm:text-2xl font-semibold tracking-tight text-slate-900">
            {isCloud ? '剧目与审片' : '项目大厅'}
          </h1>
          <p className="mt-1 text-xs sm:text-sm text-slate-500">
            {isCloud
              ? '点进任意剧目即可在线流畅审片、查看首尾帧与双比例封面投票、协同改稿留痕并查阅跨集资产圣经。'
              : '支持剧情、科幻、萌宠、美食、科普、文旅等全题材 AI 短剧制作。点进任意剧目即开启 6 步制片流水线。'}
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setCreating((v) => !v)}
            className="inline-flex items-center gap-2 rounded-xl bg-sky-600 px-4 py-2 text-xs sm:text-sm font-medium text-white transition hover:bg-sky-700 cursor-pointer shadow-xs"
          >
            <Plus className="h-4 w-4" /> 新建剧目
          </button>
        )}
      </div>

      {creating && isAdmin && (
        <form onSubmit={handleCreate} className="studio-card rounded-xl p-4 sm:p-5 space-y-4">
          <div className="text-sm font-semibold text-sky-700">新建连续短剧项目</div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div>
              <label className="mb-1 block text-xs text-slate-600">剧目名称 *</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如：星际茶馆·时空奇遇"
                className="studio-input w-full rounded-lg px-3 py-2 text-sm"
                required
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-600">赛道标签</label>
              <select
                value={track}
                onChange={(e) => setTrack(e.target.value)}
                className="studio-input w-full rounded-lg px-3 py-2 text-sm"
              >
                <option value="剧情轻喜剧">剧情轻喜剧</option>
                <option value="科幻奇观大片">科幻奇观大片</option>
                <option value="烟火治愈微剧">烟火治愈微剧</option>
                <option value="萌宠虚拟搭档">萌宠虚拟搭档</option>
                <option value="科普与城市文旅">科普与城市文旅</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-600">默认画幅</label>
              <select
                value={aspectRatio}
                onChange={(e) => setAspectRatio(e.target.value)}
                className="studio-input w-full rounded-lg px-3 py-2 text-sm"
              >
                <option value="9:16">9:16 竖屏（视频号/抖音/小红书通吃）</option>
                <option value="16:9">16:9 横屏（大屏/B站/YouTube）</option>
              </select>
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-600">视觉路线</label>
            <input
              value={visualMode}
              onChange={(e) => setVisualMode(e.target.value)}
              className="studio-input w-full rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-600">核心简介</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              placeholder="一句话说明本剧主角、核心看点与转发卖点"
              className="studio-input w-full rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setCreating(false)}
              className="rounded-lg border border-slate-200 bg-white px-4 py-1.5 text-sm text-slate-600 hover:bg-slate-50 cursor-pointer"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={busy}
              className="rounded-lg bg-sky-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-sky-700 disabled:opacity-50 cursor-pointer"
            >
              {busy ? '创建中...' : '确认创建'}
            </button>
          </div>
        </form>
      )}

      <div className="grid grid-cols-1 gap-4 sm:gap-5 lg:grid-cols-3">
        {projects.map((p) => (
          <div
            key={p.id}
            onClick={() => onOpenProject(p)}
            className="studio-card group relative flex flex-col justify-between rounded-2xl p-4 sm:p-5 transition hover:border-sky-400 hover:shadow-md cursor-pointer"
          >
            <div>
              <div className="flex items-start justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800 border border-amber-200">
                  <Sparkles className="h-3 w-3 text-amber-600" /> {p.track}
                </span>
                <div className="flex items-center gap-1.5">
                  <span className="rounded bg-slate-100 border border-slate-200 px-2 py-0.5 text-[11px] text-slate-600 font-medium">
                    {p.aspect_ratio}
                  </span>
                  {!isCloud && (
                    <button
                      onClick={(e) => handleDelete(e, p)}
                      title="删除剧目"
                      className="rounded p-1 text-slate-400 opacity-0 transition group-hover:opacity-100 hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>

              <h2 className="mt-3 text-base sm:text-lg font-semibold text-slate-900 group-hover:text-sky-700 transition">
                {p.name}
              </h2>
              <p className="mt-1 text-xs font-medium text-sky-700">{p.visual_mode}</p>
              <p className="mt-2.5 text-xs sm:text-sm leading-relaxed text-slate-600 line-clamp-3">{p.description}</p>
            </div>

            <div className="mt-5 pt-3.5 border-t border-slate-100 flex items-center justify-between gap-2 text-xs text-slate-500">
              <div className="flex flex-wrap items-center gap-3">
                <span className="inline-flex items-center gap-1">
                  <Film className="h-3.5 w-3.5 text-slate-400" /> {p.episode_count ?? 0} 集
                </span>
                <span className="inline-flex items-center gap-1">
                  <Layers className="h-3.5 w-3.5 text-slate-400" /> {p.asset_count ?? 0} 资产
                </span>
                <span className="inline-flex items-center gap-1">
                  <Clapperboard className="h-3.5 w-3.5 text-slate-400" /> 约 {p.target_duration_sec}s
                </span>
              </div>
              <span className="inline-flex items-center gap-1 font-semibold text-sky-600 group-hover:translate-x-0.5 transition shrink-0">
                {isCloud ? '进入审片' : '进入制片'} <ArrowRight className="h-3.5 w-3.5" />
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* 仅本地出片模式懒加载显示的速览提示条（云端模式既不渲染也不下载该 chunk） */}
      {!isCloud && (
        <Suspense fallback={null}>
          <LocalProjectsBanner />
        </Suspense>
      )}
    </div>
  );
};
