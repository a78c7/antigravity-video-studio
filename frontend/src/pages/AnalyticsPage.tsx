import React, { useEffect, useState } from 'react';
import {
  BarChart3,
  CheckCircle2,
  Copy,
  Lightbulb,
  MessageSquareText,
  Plus,
  Share2,
  ShieldCheck,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { api, copyToClipboard } from '../api';
import type { AppMode, AuthUser, CommentDraft, MultiPlatformCopyPack, Project, PublishLog } from '../types';

interface EpisodeBrief {
  id: number;
  project_id: number;
  ep_number: number;
  title: string;
  hook_0_3s: string;
  ending_cta: string;
  script_text: string;
  project_name: string;
}

interface Props {
  projects: Project[];
  appMode: AppMode;
  user?: AuthUser | null;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

export const AnalyticsPage: React.FC<Props> = ({ projects, appMode, notify }) => {
  const isCloud = appMode === 'cloud_collab';
  const aiLabel = isCloud ? '云端 AI' : 'AI';

  const [logs, setLogs] = useState<PublishLog[]>([]);
  const [comments, setComments] = useState<CommentDraft[]>([]);
  const [commentFilter, setCommentFilter] = useState<'all' | 'draft' | 'replied' | 'promoted'>('all');
  const [reviewText, setReviewText] = useState('');
  const [reviewing, setReviewing] = useState(false);

  // 新增发布数据表单
  const [platform, setPlatform] = useState('微信视频号');
  const [postTitle, setPostTitle] = useState('');
  const [projectId, setProjectId] = useState<number>(projects[0]?.id || 0);
  const [views, setViews] = useState(0);
  const [likes, setLikes] = useState(0);
  const [shares, setShares] = useState(0);
  const [commentsCnt, setCommentsCnt] = useState(0);
  const [favorites, setFavorites] = useState(0);
  const [completionRate, setCompletionRate] = useState(0);
  const [momentsFeedback, setMomentsFeedback] = useState('');

  // 粉丝评论互动留痕状态（0 自动发评）
  const [fanComment, setFanComment] = useState('');
  const [commentPlatform, setCommentPlatform] = useState('抖音');
  const [commentTone, setCommentTone] = useState('自然亲切 + 有梗互动');
  const [commentContext, setCommentContext] = useState('');
  const [drafting, setDrafting] = useState(false);
  const [editableDrafts, setEditableDrafts] = useState<Record<string, string>>({});
  const [customReplyText, setCustomReplyText] = useState<Record<number, string>>({});
  const [markingId, setMarkingId] = useState<number | null>(null);

  // 四平台发布文案一键适配
  const [episodesBrief, setEpisodesBrief] = useState<EpisodeBrief[]>([]);
  const [copyEpisodeId, setCopyEpisodeId] = useState<number>(0);
  const [copyCustomTitle, setCopyCustomTitle] = useState('');
  const [copyExtraNotes, setCopyExtraNotes] = useState('');
  const [copyBusy, setCopyBusy] = useState(false);
  const [copyPack, setCopyPack] = useState<MultiPlatformCopyPack | null>(null);
  const [copyEdits, setCopyEdits] = useState<Record<string, string>>({});

  const loadAll = async () => {
    try {
      const [lRes, cRes, eRes] = await Promise.all([
        api.get<{ logs: PublishLog[] }>('/api/publish-logs'),
        api.get<{ items: CommentDraft[] }>('/api/comments'),
        api.get<{ episodes: EpisodeBrief[] }>('/api/publish-copy/episodes'),
      ]);
      setLogs(lRes.logs || []);
      setComments(cRes.items || []);
      setEpisodesBrief(eRes.episodes || []);
      if (!copyEpisodeId && eRes.episodes?.length) {
        setCopyEpisodeId(eRes.episodes[0].id);
      }
      if (lRes.logs[0]?.ai_review) setReviewText(lRes.logs[0].ai_review);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  const handleAddLog = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!postTitle.trim()) return;
    try {
      await api.post('/api/publish-logs', {
        project_id: projectId || null,
        platform,
        post_title: postTitle.trim(),
        views: Number(views) || 0,
        likes: Number(likes) || 0,
        shares: Number(shares) || 0,
        comments: Number(commentsCnt) || 0,
        favorites: Number(favorites) || 0,
        completion_rate: Number(completionRate) || 0,
        moments_feedback: momentsFeedback.trim(),
      });
      setPostTitle('');
      setViews(0);
      setLikes(0);
      setShares(0);
      setCommentsCnt(0);
      setFavorites(0);
      setCompletionRate(0);
      setMomentsFeedback('');
      notify('发布数据已记录');
      await loadAll();
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleAiReview = async () => {
    setReviewing(true);
    try {
      const res = await api.post<{ review: string; used_model: string }>('/api/publish-logs/ai-review', {});
      setReviewText(res.review);
      notify(`已完成复盘诊断${isCloud ? '' : ` (${res.used_model})`}`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setReviewing(false);
    }
  };

  const handleGenerateCommentDraft = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!fanComment.trim()) return;
    setDrafting(true);
    try {
      const res = await api.post<{ item: CommentDraft; used_model: string }>('/api/comments/draft', {
        fan_comment: fanComment.trim(),
        platform: commentPlatform,
        tone: commentTone,
        episode_context: commentContext.trim(),
      });
      setComments((prev) => [res.item, ...prev]);
      setFanComment('');
      notify('已生成 3 版安全回复建议，复制发出后记得点「标记已回复」留痕！');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setDrafting(false);
    }
  };

  const handleCopyDraft = async (key: string, defaultText: string) => {
    const finalTxt = editableDrafts[key] ?? defaultText;
    await copyToClipboard(finalTxt);
    notify('已复制审核后的回复文案，请手动粘贴到平台发布');
  };

  const handleMarkReplied = async (draftId: number, replyText: string) => {
    const cleanText = replyText.trim();
    if (!cleanText) {
      notify('请填写或选择实际回复给粉丝的内容', 'err');
      return;
    }
    setMarkingId(draftId);
    try {
      const res = await api.post<{ item?: CommentDraft; comment?: CommentDraft }>(
        `/api/comments/${draftId}/mark-replied`,
        { replied_text: cleanText }
      );
      const updated = res.item || res.comment;
      if (updated) {
        setComments((prev) => prev.map((c) => (c.id === draftId ? { ...c, ...updated } : c)));
      } else {
        await loadAll();
      }
      notify('已标记「已回复」并记入协作留痕');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setMarkingId(null);
    }
  };

  const handleConvertToTopic = async (draftId: number) => {
    try {
      const res = await api.post<{ topic: { id: number; title: string } }>(`/api/comments/${draftId}/to-topic`);
      setComments((prev) =>
        prev.map((c) => (c.id === draftId ? { ...c, converted_topic_id: res.topic.id, status: 'promoted' } : c))
      );
      notify(`已转入选题雷达：《${res.topic.title}》`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleGenerateMultiPlatformCopy = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!copyEpisodeId && !copyCustomTitle.trim()) {
      notify('请先选择一个分集或输入作品标题', 'err');
      return;
    }
    setCopyBusy(true);
    try {
      const res = await api.post<{ used_model: string; pack: MultiPlatformCopyPack; linked_campaigns?: number }>(
        '/api/publish-copy/generate',
        {
          episode_id: copyEpisodeId || null,
          episode_title: copyCustomTitle.trim(),
          extra_notes: copyExtraNotes.trim(),
        }
      );
      setCopyPack(res.pack);
      setCopyEdits({});
      const campHint = res.linked_campaigns ? `（已自动联动 ${res.linked_campaigns} 个进行中官方扶持活动标签）` : '';
      notify(`已生成多平台专属文案${campHint}，审核后一键复制粘贴即可`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCopyBusy(false);
    }
  };

  const totalViews = logs.reduce((acc, x) => acc + (x.views || 0), 0);
  const totalShares = logs.reduce((acc, x) => acc + (x.shares || 0), 0);
  const totalLikes = logs.reduce((acc, x) => acc + (x.likes || 0), 0);

  const commentStatusOf = (c: CommentDraft): 'draft' | 'replied' | 'promoted' => {
    if (c.status === 'replied' || c.replied_at) return 'replied';
    if (c.status === 'promoted' || c.converted_topic_id) return 'promoted';
    return 'draft';
  };

  const filteredComments = comments.filter((c) => {
    if (commentFilter === 'all') return true;
    return commentStatusOf(c) === commentFilter;
  });

  const pendingCommentCount = comments.filter((c) => commentStatusOf(c) === 'draft').length;
  const repliedCommentCount = comments.filter((c) => commentStatusOf(c) === 'replied').length;

  return (
    <div className="space-y-5 sm:space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl sm:text-2xl font-semibold text-slate-900">
            {isCloud ? '发布与评论留痕' : '发布与数据复盘'}
          </h1>
          <p className="mt-1 text-xs sm:text-sm text-slate-500">
            多平台文案一键适配、真实发布数据复盘诊断，以及粉丝评论 AI 拟稿与「已回复留痕」看板。
          </p>
        </div>
        <button
          onClick={handleAiReview}
          disabled={reviewing || logs.length === 0}
          className="inline-flex items-center gap-1.5 rounded-xl bg-sky-600 px-4 py-2 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-40 cursor-pointer"
        >
          <Sparkles className="h-3.5 w-3.5" />
          {reviewing ? `${aiLabel} 复盘分析中...` : '生成 AI 复盘与下集建议'}
        </button>
      </div>

      {/* KPI 概览 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { label: '累计发片条数', val: logs.length, unit: '条' },
          { label: '累计总播放', val: totalViews.toLocaleString(), unit: '' },
          { label: '累计转发（视频号核心）', val: totalShares.toLocaleString(), unit: '' },
          { label: '累计点赞', val: totalLikes.toLocaleString(), unit: '' },
        ].map((k) => (
          <div key={k.label} className="studio-card rounded-xl p-3.5 sm:p-4">
            <div className="text-[11px] sm:text-xs text-slate-500">{k.label}</div>
            <div className="mt-1 text-xl sm:text-2xl font-bold text-slate-900">
              {k.val}
              <span className="ml-1 text-xs font-normal text-slate-400">{k.unit}</span>
            </div>
          </div>
        ))}
      </div>

      {/* 四平台发布文案一键适配 */}
      <div className="studio-card rounded-2xl p-4 sm:p-5 border-emerald-200 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-emerald-800">
              <Share2 className="h-4 w-4 text-emerald-600" /> 四平台发布文案一键适配（视频号 / 抖音 / 小红书 / B站）
            </div>
            <p className="mt-1 text-xs text-slate-500">
              选一集剧本 → {aiLabel} 一次写出四套贴合各平台算法的标题、正文、话题标签与置顶首评 → 审核后手动复制发布。
            </p>
          </div>
          <span className="shrink-0 inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 text-[11px] font-medium text-emerald-700 border border-emerald-200">
            <ShieldCheck className="h-3.5 w-3.5" /> 纯人工复制 · 0 封号风险
          </span>
        </div>

        <form onSubmit={handleGenerateMultiPlatformCopy} className="grid grid-cols-1 gap-2.5 md:grid-cols-12">
          <select
            value={copyEpisodeId}
            onChange={(e) => setCopyEpisodeId(Number(e.target.value))}
            className="studio-input md:col-span-4 rounded-lg px-2.5 py-2 text-xs"
          >
            <option value={0}>手动输入标题（不绑定已有分集）</option>
            {episodesBrief.map((ep) => (
              <option key={ep.id} value={ep.id}>
                《{ep.project_name}》第{ep.ep_number}集 · {ep.title}
              </option>
            ))}
          </select>
          <input
            value={copyCustomTitle}
            onChange={(e) => setCopyCustomTitle(e.target.value)}
            placeholder={copyEpisodeId ? '可选：覆盖标题' : '输入本集标题（如：雨夜咖啡馆的秘密）*'}
            className="studio-input md:col-span-3 rounded-lg px-3 py-2 text-xs"
          />
          <input
            value={copyExtraNotes}
            onChange={(e) => setCopyExtraNotes(e.target.value)}
            placeholder="补充亮点/核心关键词（可选）"
            className="studio-input md:col-span-3 rounded-lg px-3 py-2 text-xs"
          />
          <button
            type="submit"
            disabled={copyBusy}
            className="md:col-span-2 inline-flex items-center justify-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50 cursor-pointer"
          >
            <Sparkles className="h-3.5 w-3.5" />
            {copyBusy ? '生成中...' : '一键出文案'}
          </button>
        </form>

        {copyPack && (
          <div className="space-y-3 pt-1">
            {copyPack.cover_headline && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-amber-50 border border-amber-200 px-3.5 py-2 text-xs">
                <span className="text-amber-900">
                  通用封面吸睛大字建议：<strong className="text-slate-900 ml-1">{copyPack.cover_headline}</strong>
                </span>
                <button
                  type="button"
                  onClick={async () => {
                    await copyToClipboard(copyPack.cover_headline);
                    notify('已复制封面花字建议');
                  }}
                  className="inline-flex items-center gap-1 rounded bg-white border border-amber-300 px-2 py-0.5 text-[11px] font-medium text-amber-800 hover:bg-amber-500 hover:text-white cursor-pointer"
                >
                  <Copy className="h-3 w-3" /> 复制封面字
                </button>
              </div>
            )}

            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {(copyPack.platforms || []).map((p, idx) => {
                const tagsStr = (p.hashtags || []).join(' ');
                const defaultFullBody = `${p.body}\n\n${tagsStr}`.trim();
                const editKey = `${idx}-${p.platform}`;
                const curBody = copyEdits[editKey] ?? defaultFullBody;
                return (
                  <div key={editKey} className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 space-y-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <span className="rounded bg-sky-50 border border-sky-200 px-2 py-0.5 text-xs font-semibold text-sky-700">
                          {p.platform}
                        </span>
                        <span className="ml-2 text-[11px] text-slate-500">{p.angle}</span>
                      </div>
                      <button
                        type="button"
                        onClick={async () => {
                          await copyToClipboard(`${p.title}\n\n${curBody}`);
                          notify(`已复制「${p.platform}」完整标题 + 正文 + 标签`);
                        }}
                        className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700 cursor-pointer shrink-0"
                      >
                        <Copy className="h-3 w-3" /> 复制整套
                      </button>
                    </div>

                    <div className="flex items-center justify-between gap-2 rounded-lg bg-white border border-slate-200 px-2.5 py-1.5 text-xs">
                      <span className="text-slate-900 font-medium truncate" title={p.title}>
                        {p.title}
                      </span>
                      <button
                        type="button"
                        onClick={async () => {
                          await copyToClipboard(p.title);
                          notify(`已复制「${p.platform}」标题`);
                        }}
                        className="shrink-0 text-[10px] font-medium text-sky-600 hover:underline cursor-pointer"
                      >
                        复制标题
                      </button>
                    </div>

                    <textarea
                      value={curBody}
                      onChange={(e) => setCopyEdits((prev) => ({ ...prev, [editKey]: e.target.value }))}
                      rows={4}
                      className="studio-input w-full rounded-lg p-2.5 text-xs leading-relaxed"
                    />

                    {p.pinned_comment && (
                      <div className="flex items-start justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[11px]">
                        <span className="text-slate-700">
                          <strong className="text-amber-800">置顶首评：</strong>
                          {p.pinned_comment}
                        </span>
                        <button
                          type="button"
                          onClick={async () => {
                            await copyToClipboard(p.pinned_comment);
                            notify(`已复制「${p.platform}」置顶首评`);
                          }}
                          className="shrink-0 font-medium text-amber-700 hover:underline cursor-pointer"
                        >
                          复制
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
        {/* 左栏：发布数据录入 + 复盘表 */}
        <div className="xl:col-span-6 space-y-5">
          <form onSubmit={handleAddLog} className="studio-card rounded-2xl p-4 space-y-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <BarChart3 className="h-4 w-4 text-sky-600" /> 录入每集发布数据
            </div>
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
              <select
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs"
              >
                <option value="微信视频号">微信视频号（社交推荐核心）</option>
                <option value="抖音">抖音</option>
                <option value="小红书">小红书</option>
                <option value="B站">B站</option>
              </select>
              <select
                value={projectId}
                onChange={(e) => setProjectId(Number(e.target.value))}
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs"
              >
                <option value={0}>不绑定剧目</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <input
                value={postTitle}
                onChange={(e) => setPostTitle(e.target.value)}
                placeholder="作品标题 *"
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs"
                required
              />
            </div>

            <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
              {[
                { label: '播放', val: views, set: setViews },
                { label: '转发', val: shares, set: setShares },
                { label: '点赞', val: likes, set: setLikes },
                { label: '评论', val: commentsCnt, set: setCommentsCnt },
                { label: '收藏', val: favorites, set: setFavorites },
                { label: '完播%', val: completionRate, set: setCompletionRate },
              ].map((f) => (
                <div key={f.label}>
                  <label className="block text-[10px] text-slate-500 mb-0.5">{f.label}</label>
                  <input
                    type="number"
                    step="any"
                    value={f.val}
                    onChange={(e) => f.set(Number(e.target.value))}
                    className="studio-input w-full rounded px-2 py-1 text-xs"
                  />
                </div>
              ))}
            </div>

            <div className="flex flex-col sm:flex-row gap-2">
              <input
                value={momentsFeedback}
                onChange={(e) => setMomentsFeedback(e.target.value)}
                placeholder="观众/社群真实反馈备注（可选）"
                className="studio-input flex-1 rounded-lg px-3 py-1.5 text-xs"
              />
              <button
                type="submit"
                className="inline-flex items-center justify-center gap-1 rounded-lg bg-slate-800 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-slate-700 cursor-pointer shrink-0"
              >
                <Plus className="h-3.5 w-3.5" /> 记一笔
              </button>
            </div>
          </form>

          {reviewText && (
            <div className="studio-card rounded-2xl p-4 border-sky-200 bg-sky-50/40 space-y-2">
              <div className="text-xs font-semibold text-sky-800">AI 最新复盘诊断与下集调优建议</div>
              <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-slate-700">{reviewText}</pre>
            </div>
          )}

          <div className="studio-card rounded-2xl overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 bg-slate-50/60">
              历史发布台账
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-3 py-2">平台 / 标题</th>
                    <th className="px-2 py-2 text-right">播放</th>
                    <th className="px-2 py-2 text-right">转发</th>
                    <th className="px-2 py-2 text-right">点赞</th>
                    <th className="px-2 py-2 text-right">完播</th>
                    <th className="px-2 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {logs.map((l) => (
                    <tr key={l.id} className="hover:bg-slate-50">
                      <td className="px-3 py-2.5">
                        <span className="mr-1.5 rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
                          {l.platform}
                        </span>
                        <span className="text-slate-900 font-medium">{l.post_title}</span>
                      </td>
                      <td className="px-2 py-2.5 text-right font-mono text-slate-700">{l.views}</td>
                      <td className="px-2 py-2.5 text-right font-mono font-semibold text-emerald-700">{l.shares}</td>
                      <td className="px-2 py-2.5 text-right font-mono text-slate-700">{l.likes}</td>
                      <td className="px-2 py-2.5 text-right font-mono text-slate-700">{l.completion_rate}%</td>
                      <td className="px-2 py-2.5">
                        <button
                          onClick={async () => {
                            await api.del(`/api/publish-logs/${l.id}`);
                            setLogs((p) => p.filter((x) => x.id !== l.id));
                          }}
                          className="text-slate-400 hover:text-rose-600 cursor-pointer"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </td>
                    </tr>
                  ))}
                  {logs.length === 0 && (
                    <tr>
                      <td colSpan={6} className="py-10 text-center text-slate-400">
                        发完第一集后把数据记在这里，即可生成 AI 复盘诊断。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* 右栏：粉丝评论互动留痕板块 */}
        <div className="xl:col-span-6 studio-card rounded-2xl p-4 border-amber-200 flex flex-col gap-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-amber-800">
                <MessageSquareText className="h-4 w-4 text-amber-600" /> 粉丝评论互动留痕板块
              </div>
              <p className="mt-1 text-[11px] text-slate-500">
                粘贴粉丝评论 → {aiLabel} 秒出 3 版回复建议 → 发完点「标记已回复」，方便后续追踪与团队协作查看。
              </p>
            </div>
            <span className="shrink-0 inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700 border border-emerald-200">
              <ShieldCheck className="h-3 w-3" /> 人工审核 · 绝不自动发评
            </span>
          </div>

          <form onSubmit={handleGenerateCommentDraft} className="space-y-2.5">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <select
                value={commentPlatform}
                onChange={(e) => setCommentPlatform(e.target.value)}
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs"
              >
                <option value="抖音">抖音评论</option>
                <option value="微信视频号">微信视频号评论</option>
                <option value="小红书">小红书评论</option>
                <option value="B站">B站评论</option>
              </select>
              <input
                value={commentTone}
                onChange={(e) => setCommentTone(e.target.value)}
                placeholder="回复语气侧重"
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs"
              />
            </div>
            <input
              value={commentContext}
              onChange={(e) => setCommentContext(e.target.value)}
              placeholder="来自哪集视频（可选，如：第1集 主角初登场）"
              className="studio-input w-full rounded-lg px-3 py-1.5 text-xs"
            />
            <textarea
              value={fanComment}
              onChange={(e) => setFanComment(e.target.value)}
              rows={3}
              placeholder="直接粘贴粉丝评论原文（例：这个转场太丝滑了！下一集想看主角去星空夜市！）"
              className="studio-input w-full rounded-lg px-3 py-2 text-xs"
              required
            />
            <button
              type="submit"
              disabled={drafting}
              className="w-full inline-flex items-center justify-center gap-1.5 rounded-lg bg-amber-500 py-2 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-50 cursor-pointer"
            >
              <Sparkles className="h-3.5 w-3.5" />
              {drafting ? `${aiLabel} 正在拟定 3 版回复...` : '生成 3 版安全回复建议'}
            </button>
          </form>

          {/* 评论状态筛选条 */}
          <div className="flex flex-wrap items-center gap-1.5 text-[11px] pt-1 border-t border-slate-100">
            {[
              { id: 'all' as const, label: `全部 (${comments.length})` },
              { id: 'draft' as const, label: `待回复 (${pendingCommentCount})` },
              { id: 'replied' as const, label: `已回复 (${repliedCommentCount})` },
              { id: 'promoted' as const, label: '已转选题' },
            ].map((f) => (
              <button
                key={f.id}
                type="button"
                onClick={() => setCommentFilter(f.id)}
                className={`rounded-full px-2.5 py-1 border transition cursor-pointer ${
                  commentFilter === f.id
                    ? 'bg-slate-800 border-slate-800 text-white font-medium'
                    : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>

          <div className="space-y-3 max-h-[60vh] overflow-y-auto pr-0.5">
            {filteredComments.map((c) => {
              const st = commentStatusOf(c);
              return (
                <div
                  key={c.id}
                  className={`rounded-xl border p-3 space-y-2.5 ${
                    st === 'replied'
                      ? 'border-emerald-200 bg-emerald-50/30'
                      : 'border-slate-200 bg-slate-50/80'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="text-xs space-y-1 min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
                          {c.platform}
                        </span>
                        {st === 'replied' ? (
                          <span className="inline-flex items-center gap-0.5 rounded-full bg-emerald-100 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-800">
                            <CheckCircle2 className="h-3 w-3" /> 已回复留痕
                          </span>
                        ) : st === 'promoted' ? (
                          <span className="rounded-full bg-sky-50 border border-sky-200 px-2 py-0.5 text-[10px] font-semibold text-sky-700">
                            已转选题
                          </span>
                        ) : (
                          <span className="rounded-full bg-amber-100 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                            待回复
                          </span>
                        )}
                      </div>
                      <div className="text-slate-900 font-medium break-words">「{c.fan_comment}」</div>
                    </div>
                    <button
                      onClick={async () => {
                        await api.del(`/api/comments/${c.id}`);
                        setComments((prev) => prev.filter((x) => x.id !== c.id));
                      }}
                      className="text-slate-400 hover:text-rose-600 cursor-pointer shrink-0"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>

                  {/* 已回复留痕高亮条 */}
                  {(c.replied_at || c.replied_text) && (
                    <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs space-y-1">
                      <div className="flex flex-wrap items-center justify-between gap-1 text-[11px] text-emerald-800 font-semibold">
                        <span className="inline-flex items-center gap-1">
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          已由 {c.replied_by || '团队成员'} 标记已回复
                        </span>
                        <span className="font-mono font-normal text-emerald-700">{c.replied_at}</span>
                      </div>
                      {c.replied_text && (
                        <div className="text-emerald-950 leading-relaxed break-words">实发回复：{c.replied_text}</div>
                      )}
                    </div>
                  )}

                  {/* 3 版 AI 回复草稿 */}
                  <div className="space-y-2">
                    {(c.drafts || []).map((d, idx) => {
                      const key = `${c.id}-${idx}`;
                      const curVal = editableDrafts[key] ?? d.text;
                      return (
                        <div key={key} className="rounded-lg bg-white p-2.5 space-y-1.5 border border-slate-200">
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <span className="text-[10px] font-semibold text-sky-700">{d.style}</span>
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => handleCopyDraft(key, d.text)}
                                className="inline-flex items-center gap-1 rounded bg-slate-100 border border-slate-200 px-2 py-0.5 text-[10px] font-medium text-slate-700 hover:bg-sky-600 hover:border-sky-600 hover:text-white transition cursor-pointer"
                              >
                                <Copy className="h-2.5 w-2.5" /> 复制
                              </button>
                              <button
                                type="button"
                                disabled={markingId === c.id}
                                onClick={() => handleMarkReplied(c.id, curVal)}
                                className="inline-flex items-center gap-1 rounded bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 hover:bg-emerald-600 hover:text-white transition cursor-pointer"
                              >
                                <CheckCircle2 className="h-2.5 w-2.5" /> 标记已回复
                              </button>
                            </div>
                          </div>
                          <textarea
                            value={curVal}
                            onChange={(e) => setEditableDrafts((prev) => ({ ...prev, [key]: e.target.value }))}
                            rows={2}
                            className="w-full bg-transparent text-xs text-slate-800 focus:outline-none resize-none"
                          />
                        </div>
                      );
                    })}
                  </div>

                  {/* 自定义实际回复留痕输入条 */}
                  <div className="flex gap-1.5 pt-1">
                    <input
                      value={customReplyText[c.id] || ''}
                      onChange={(e) => setCustomReplyText((prev) => ({ ...prev, [c.id]: e.target.value }))}
                      placeholder="或粘贴你实际发出的回复内容，一键留痕..."
                      className="studio-input flex-1 min-w-0 rounded-lg px-2.5 py-1 text-[11px]"
                    />
                    <button
                      type="button"
                      disabled={markingId === c.id || !(customReplyText[c.id] || '').trim()}
                      onClick={async () => {
                        await handleMarkReplied(c.id, customReplyText[c.id] || '');
                        setCustomReplyText((prev) => ({ ...prev, [c.id]: '' }));
                      }}
                      className="rounded-lg bg-emerald-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700 disabled:opacity-40 cursor-pointer shrink-0"
                    >
                      记为已回
                    </button>
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-2 pt-1.5 border-t border-slate-200">
                    <div className="text-[11px] text-amber-800 truncate flex-1 min-w-0">
                      {c.topic_idea ? `💡 潜藏选题：${c.topic_idea}` : '可直接把这条粉丝留言转为下集灵感'}
                    </div>
                    {c.converted_topic_id ? (
                      <span className="shrink-0 rounded bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                        已转入选题库
                      </span>
                    ) : (
                      <button
                        onClick={() => handleConvertToTopic(c.id)}
                        className="shrink-0 inline-flex items-center gap-1 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                      >
                        <Lightbulb className="h-3 w-3" /> 一键转选题
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {filteredComments.length === 0 && (
              <div className="py-8 text-center text-xs text-slate-400">
                当前筛选下暂无评论记录。粘贴任意粉丝评论，立刻生成 3 版安全回复建议！
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
