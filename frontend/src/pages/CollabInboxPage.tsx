import React, { useEffect, useState } from 'react';
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Film,
  History,
  Image as ImageIcon,
  MessageSquareText,
  RefreshCw,
  Sparkles,
  Vote,
} from 'lucide-react';
import { api, roleLabel } from '../api';
import { formatFieldLabel } from '../components/RevisionsDrawer';
import type { AuthUser, CollabInboxResponse, PendingEpisodeCard } from '../types';

export type JumpSection = 'review_frames' | 'review_video' | 'review_covers' | 'script' | 'shots';

interface Props {
  user: AuthUser | null;
  onJumpToEpisode: (projectId: number, episodeId: number, section: JumpSection, openRevisions?: boolean) => void;
  onJumpToRules: () => void;
  onJumpToComments: () => void;
  onInboxLoaded?: (pendingTotal: number) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

export const CollabInboxPage: React.FC<Props> = ({
  user,
  onJumpToEpisode,
  onJumpToRules,
  onJumpToComments,
  onInboxLoaded,
  notify,
}) => {
  const [data, setData] = useState<CollabInboxResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const loadInbox = async (showToast = false) => {
    setLoading(true);
    try {
      const res = await api.get<CollabInboxResponse>('/api/collab/inbox');
      setData(res);
      const total =
        res.counts?.total_todos ??
        res.counts?.total ??
        (res.pending_frames_episodes?.length || 0) +
          (res.pending_videos_episodes?.length || 0) +
          (res.pending_covers_episodes?.length || 0) +
          (res.open_rule_suggestions?.length || 0) +
          (res.pending_comments?.length || 0);
      onInboxLoaded?.(total);
      if (showToast) notify('已刷新最新协作待办');
    } catch (err: any) {
      notify(err?.message || '加载协作待办失败', 'err');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInbox(false);
  }, []);

  const epIdOf = (card: PendingEpisodeCard): number => Number(card.episode_id ?? card.id ?? 0);

  const framesList = data?.pending_frames_episodes || [];
  const videosList = data?.pending_videos_episodes || [];
  const coversList = data?.pending_covers_episodes || [];
  const revisionsList = data?.recent_revisions || [];
  const suggestionsList = data?.open_rule_suggestions || [];
  const commentsList = data?.pending_comments || [];

  const totalPending =
    framesList.length + videosList.length + coversList.length + suggestionsList.length + commentsList.length;

  return (
    <div className="space-y-5">
      {/* 顶栏欢迎与刷新 */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl sm:text-2xl font-semibold tracking-tight text-slate-900">待我协作</h1>
            {user && (
              <span className="rounded-full bg-sky-50 border border-sky-200 px-2.5 py-0.5 text-[11px] font-medium text-sky-700">
                {user.display_name || user.username} · {roleLabel(user.role)}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs sm:text-sm text-slate-500">
            集中汇总待挑首尾帧、待审试卡与成片、待投票封面、团队最新改稿留痕、规则建议与待回粉丝评论，点任意一条直达现场。
          </p>
        </div>
        <button
          type="button"
          onClick={() => loadInbox(true)}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 cursor-pointer shadow-2xs"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin text-sky-600' : ''}`} />
          刷新待办
        </button>
      </div>

      {/* 顶部快速计数卡（适配 390px 2 列、桌面 5 列） */}
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
        {[
          { label: '待挑首尾帧', val: framesList.length, color: 'text-amber-700 bg-amber-50 border-amber-200' },
          { label: '待审试卡/成片', val: videosList.length, color: 'text-sky-700 bg-sky-50 border-sky-200' },
          { label: '待投票封面', val: coversList.length, color: 'text-violet-700 bg-violet-50 border-violet-200' },
          { label: '待处理规范建议', val: suggestionsList.length, color: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
          { label: '待回复粉丝评论', val: commentsList.length, color: 'text-rose-700 bg-rose-50 border-rose-200' },
        ].map((item) => (
          <div key={item.label} className={`rounded-2xl border p-3.5 ${item.val > 0 ? item.color : 'bg-white border-slate-200 text-slate-600'}`}>
            <div className="text-[11px] font-medium opacity-85">{item.label}</div>
            <div className="mt-1 text-2xl font-bold tracking-tight">{loading && !data ? '—' : item.val}</div>
          </div>
        ))}
      </div>

      {!loading && totalPending === 0 && revisionsList.length === 0 && (
        <div className="studio-card rounded-2xl p-8 text-center space-y-2">
          <CheckCircle2 className="mx-auto h-9 w-9 text-emerald-500" />
          <div className="text-sm font-semibold text-slate-800">目前全部待办已清零</div>
          <div className="text-xs text-slate-500">可前往「剧目与审片」查看历史成片，或去「热点与选题雷达」挖掘下一集选题。</div>
        </div>
      )}

      {/* 三大审片待办卡片区 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 1. 待挑图剧集 */}
        <section className="studio-card rounded-2xl p-4 space-y-3 flex flex-col">
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
              <ImageIcon className="h-4 w-4 text-amber-600" /> 待挑首尾帧 ({framesList.length})
            </div>
            <span className="text-[11px] text-slate-400">看图投票</span>
          </div>
          <div className="space-y-2 flex-1">
            {framesList.map((ep) => {
              const epId = epIdOf(ep);
              return (
                <button
                  key={`frame-${epId}`}
                  type="button"
                  onClick={() => onJumpToEpisode(ep.project_id, epId, 'review_frames')}
                  className="w-full text-left rounded-xl border border-amber-200/80 bg-amber-50/45 hover:bg-amber-50 p-3 transition cursor-pointer group"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-amber-800 truncate">{ep.project_name || '剧目'}</span>
                    <ArrowRight className="h-3.5 w-3.5 text-amber-600 shrink-0 group-hover:translate-x-0.5 transition" />
                  </div>
                  <div className="mt-1 text-xs font-semibold text-slate-900 truncate">
                    第 {ep.ep_number} 集 · {ep.title}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    {(ep.frames_ready_count ?? ep.frame_count) !== undefined && (
                      <span>已出 {ep.frames_ready_count ?? ep.frame_count} 帧</span>
                    )}
                    {ep.shot_count !== undefined && <span>共 {ep.shot_count} 镜</span>}
                    {!ep.my_vote && (
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">待我表态</span>
                    )}
                    {ep.votes_summary && (ep.votes_summary.change ?? 0) > 0 && (
                      <span className="rounded bg-rose-50 border border-rose-200 px-1.5 py-0.5 text-[10px] font-medium text-rose-700">
                        有修改建议
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
            {framesList.length === 0 && (
              <div className="py-8 text-center text-xs text-slate-400">暂无待挑首尾帧的剧集</div>
            )}
          </div>
        </section>

        {/* 2. 待审 360p 试卡 / 成片 */}
        <section className="studio-card rounded-2xl p-4 space-y-3 flex flex-col">
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
              <Film className="h-4 w-4 text-sky-600" /> 待审试卡与成片 ({videosList.length})
            </div>
            <span className="text-[11px] text-slate-400">流畅在线播</span>
          </div>
          <div className="space-y-2 flex-1">
            {videosList.map((ep) => {
              const epId = epIdOf(ep);
              return (
                <button
                  key={`vid-${epId}`}
                  type="button"
                  onClick={() => onJumpToEpisode(ep.project_id, epId, 'review_video')}
                  className="w-full text-left rounded-xl border border-sky-200/80 bg-sky-50/45 hover:bg-sky-50 p-3 transition cursor-pointer group"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-sky-800 truncate">{ep.project_name || '剧目'}</span>
                    <ArrowRight className="h-3.5 w-3.5 text-sky-600 shrink-0 group-hover:translate-x-0.5 transition" />
                  </div>
                  <div className="mt-1 text-xs font-semibold text-slate-900 truncate">
                    第 {ep.ep_number} 集 · {ep.title}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    {ep.web_preview_path && (
                      <span className="rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                        已备流畅预览版
                      </span>
                    )}
                    {(ep.video_ready_count ?? ep.ready_shot_count) !== undefined && (
                      <span>已就绪 {ep.video_ready_count ?? ep.ready_shot_count}/{ep.shot_count ?? 0} 镜</span>
                    )}
                    {!ep.my_vote && (
                      <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-800">待我审片</span>
                    )}
                  </div>
                </button>
              );
            })}
            {videosList.length === 0 && (
              <div className="py-8 text-center text-xs text-slate-400">暂无待审 360p 试卡或成片</div>
            )}
          </div>
        </section>

        {/* 3. 待投票封面 */}
        <section className="studio-card rounded-2xl p-4 space-y-3 flex flex-col">
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
              <Vote className="h-4 w-4 text-violet-600" /> 待投票双比例封面 ({coversList.length})
            </div>
            <span className="text-[11px] text-slate-400">9:16 & 3:4</span>
          </div>
          <div className="space-y-2 flex-1">
            {coversList.map((ep) => {
              const epId = epIdOf(ep);
              return (
                <button
                  key={`cover-${epId}`}
                  type="button"
                  onClick={() => onJumpToEpisode(ep.project_id, epId, 'review_covers')}
                  className="w-full text-left rounded-xl border border-violet-200/80 bg-violet-50/45 hover:bg-violet-50 p-3 transition cursor-pointer group"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-violet-800 truncate">{ep.project_name || '剧目'}</span>
                    <ArrowRight className="h-3.5 w-3.5 text-violet-600 shrink-0 group-hover:translate-x-0.5 transition" />
                  </div>
                  <div className="mt-1 text-xs font-semibold text-slate-900 truncate">
                    第 {ep.ep_number} 集 · {ep.title}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    {ep.cover_count !== undefined && <span>候选 {ep.cover_count} 张</span>}
                    {ep.selected_cover_path ? (
                      <span className="text-emerald-700 font-medium">已选定封面</span>
                    ) : (
                      <span className="text-violet-700 font-medium">待投票拍板</span>
                    )}
                  </div>
                </button>
              );
            })}
            {coversList.length === 0 && (
              <div className="py-8 text-center text-xs text-slate-400">暂无待投票封面</div>
            )}
          </div>
        </section>
      </div>

      {/* 下半区：最新改稿留痕时间线 + 规则建议 & 待回评论 */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* 最新修改留痕时间线 */}
        <section className="xl:col-span-7 studio-card rounded-2xl p-4 space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
              <History className="h-4 w-4 text-sky-600" /> 最新剧本与分镜修改留痕
            </div>
            <span className="text-[11px] text-slate-400">最近 {revisionsList.length} 条</span>
          </div>

          <div className="space-y-2.5 max-h-[420px] overflow-y-auto pr-1">
            {revisionsList.map((rev) => {
              const canJump = Boolean(rev.project_id && rev.episode_id);
              return (
                <div
                  key={rev.id}
                  onClick={() => {
                    if (rev.project_id && rev.episode_id) {
                      onJumpToEpisode(
                        rev.project_id,
                        rev.episode_id,
                        rev.entity_type === 'shot' ? 'shots' : 'script',
                        true
                      );
                    }
                  }}
                  className={`rounded-xl border border-slate-200 bg-slate-50/70 p-3 space-y-1.5 text-xs ${
                    canJump ? 'hover:border-sky-300 hover:bg-sky-50/40 cursor-pointer transition' : ''
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-1.5">
                    <div className="flex flex-wrap items-center gap-1.5 min-w-0">
                      <span className="font-semibold text-slate-900">{rev.actor_name || rev.actor_username}</span>
                      <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
                        {roleLabel(rev.actor_role)}
                      </span>
                      <span className="text-sky-700 font-medium">
                        修改了「{rev.field_label || formatFieldLabel(rev.field_name)}」
                      </span>
                    </div>
                    <span className="text-[10px] text-slate-400 font-mono">{rev.created_at}</span>
                  </div>
                  {(rev.project_name || rev.episode_title) && (
                    <div className="text-[11px] text-slate-500">
                      {rev.project_name ? `《${rev.project_name}》` : ''}
                      {rev.episode_title ? ` · ${rev.episode_title}` : ''}
                    </div>
                  )}
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 text-[11px]">
                    <div className="rounded-lg bg-rose-50/70 border border-rose-100 px-2 py-1 text-rose-800 line-clamp-2">
                      旧：{rev.old_value || '（空）'}
                    </div>
                    <div className="rounded-lg bg-emerald-50/70 border border-emerald-100 px-2 py-1 text-emerald-900 line-clamp-2">
                      新：{rev.new_value || '（空）'}
                    </div>
                  </div>
                </div>
              );
            })}
            {revisionsList.length === 0 && (
              <div className="py-10 text-center text-xs text-slate-400">暂无剧本或分镜修改记录</div>
            )}
          </div>
        </section>

        {/* 右侧：待处理规范建议 + 待回复粉丝评论 */}
        <div className="xl:col-span-5 space-y-4">
          <section className="studio-card rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
                <BookOpen className="h-4 w-4 text-emerald-600" /> 待处理创作规范建议 ({suggestionsList.length})
              </div>
              <button
                type="button"
                onClick={onJumpToRules}
                className="inline-flex items-center gap-0.5 text-xs font-medium text-sky-600 hover:underline cursor-pointer"
              >
                去处理 <ArrowRight className="h-3 w-3" />
              </button>
            </div>
            <div className="space-y-2">
              {suggestionsList.slice(0, 5).map((sg) => (
                <button
                  key={sg.id}
                  type="button"
                  onClick={onJumpToRules}
                  className="w-full text-left rounded-xl border border-emerald-200/80 bg-emerald-50/40 hover:bg-emerald-50 p-3 space-y-1 transition cursor-pointer"
                >
                  <div className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="font-semibold text-emerald-900 truncate">{sg.rule_title || sg.template_key}</span>
                    <span className="text-slate-400 shrink-0">{sg.actor_name || sg.actor_username}</span>
                  </div>
                  <div className="text-xs text-slate-700 line-clamp-2">{sg.suggestion_text}</div>
                </button>
              ))}
              {suggestionsList.length === 0 && (
                <div className="py-5 text-center text-xs text-slate-400">暂无待处理的规则建议</div>
              )}
            </div>
          </section>

          <section className="studio-card rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
                <MessageSquareText className="h-4 w-4 text-rose-500" /> 待回复粉丝评论 ({commentsList.length})
              </div>
              <button
                type="button"
                onClick={onJumpToComments}
                className="inline-flex items-center gap-0.5 text-xs font-medium text-sky-600 hover:underline cursor-pointer"
              >
                去拟稿回复 <ArrowRight className="h-3 w-3" />
              </button>
            </div>
            <div className="space-y-2">
              {commentsList.slice(0, 5).map((cm) => (
                <button
                  key={cm.id}
                  type="button"
                  onClick={onJumpToComments}
                  className="w-full text-left rounded-xl border border-rose-200/70 bg-rose-50/35 hover:bg-rose-50/70 p-3 space-y-1 transition cursor-pointer"
                >
                  <div className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-slate-700">
                      {cm.platform}
                    </span>
                    <span className="text-slate-400 font-mono text-[10px]">{cm.created_at}</span>
                  </div>
                  <div className="text-xs font-medium text-slate-800 line-clamp-2">「{cm.fan_comment}」</div>
                  {cm.topic_idea && (
                    <div className="inline-flex items-center gap-1 text-[11px] text-amber-800">
                      <Sparkles className="h-3 w-3 shrink-0" /> 潜在选题：{cm.topic_idea}
                    </div>
                  )}
                </button>
              ))}
              {commentsList.length === 0 && (
                <div className="py-5 text-center text-xs text-slate-400">暂无待回复的粉丝评论</div>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
};
