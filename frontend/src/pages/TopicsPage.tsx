import React, { useEffect, useState } from 'react';
import {
  Flame,
  RefreshCw,
  Search,
  Sparkles,
  CheckSquare,
  Square,
  ShieldAlert,
  ArrowUpRight,
  Trash2,
  Plus,
  Wifi,
  Megaphone,
  Copy,
  Lightbulb,
  ChevronDown,
  ChevronUp,
  Compass,
  Settings,
} from 'lucide-react';
import { api, copyToClipboard, fmtHot } from '../api';
import type { AppMode, HotItem, PlatformCampaign, Project, Topic } from '../types';

interface Props {
  projects: Project[];
  appMode: AppMode;
  enableHotFeeds?: boolean;
  onOpenSettings?: () => void;
  onPromoteDone: (projectId: number, episodeId: number) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const SOURCES = [
  { id: 'douyin', label: '抖音热搜' },
  { id: 'toutiao', label: '今日头条' },
  { id: 'baidu', label: '百度实时' },
  { id: 'bilibili', label: 'B站热搜' },
  { id: 'weibo', label: '微博热搜' },
];

const TIER_QUALITY = '【精品口碑级】';
const TIER_VIRAL = '【破圈玩梗级】';

function resolveDisplayTier(t: Topic): string {
  if (t.content_tier === TIER_QUALITY || t.content_tier === TIER_VIRAL) return t.content_tier;
  const hay = `${t.summary || ''} ${t.ai_pitch || ''} ${t.title || ''}`;
  if (hay.includes(TIER_QUALITY) || hay.includes('精品口碑级')) return TIER_QUALITY;
  if (hay.includes(TIER_VIRAL) || hay.includes('破圈玩梗级')) return TIER_VIRAL;
  return '';
}

export const TopicsPage: React.FC<Props> = ({
  projects,
  appMode,
  enableHotFeeds = true,
  onOpenSettings,
  onPromoteDone,
  notify,
}) => {
  const isCloud = appMode === 'cloud_collab';

  const [source, setSource] = useState('douyin');
  const [hotItems, setHotItems] = useState<HotItem[]>([]);
  const [hotMeta, setHotMeta] = useState<{
    fetched_at: string;
    cached: boolean;
    latency_ms: number;
    degraded?: boolean;
    warning?: string;
  } | null>(null);
  const [hotDisabledByServer, setHotDisabledByServer] = useState(false);
  const [loadingHot, setLoadingHot] = useState(false);
  const [selectedHot, setSelectedHot] = useState<Set<number>>(new Set());

  const [topics, setTopics] = useState<Topic[]>([]);
  const [gradeFilter, setGradeFilter] = useState<string>('ALL');
  const [tierFilter, setTierFilter] = useState<string>('ALL');
  const [scoring, setScoring] = useState(false);
  const [searchingTopic, setSearchingTopic] = useState(false);
  const [searchingBenchmarks, setSearchingBenchmarks] = useState(false);
  const [topicKeyword, setTopicKeyword] = useState('');
  const [benchmarkFocus, setBenchmarkFocus] = useState('');

  const [manualTitle, setManualTitle] = useState('');
  const [manualNote, setManualNote] = useState('');
  const [promoteTargets, setPromoteTargets] = useState<Record<number, number>>({});

  // 官方流量扶持活动雷达
  const [campaigns, setCampaigns] = useState<PlatformCampaign[]>([]);
  const [campaignsOpen, setCampaignsOpen] = useState(true);
  const [loadingCampaigns, setLoadingCampaigns] = useState(false);

  const showHotFeeds = enableHotFeeds !== false && !hotDisabledByServer;
  const defaultTheme = projects[0]?.track || projects[0]?.name || '';

  const loadCampaigns = async (showToast = false) => {
    setLoadingCampaigns(true);
    try {
      const res = await api.get<{ campaigns: PlatformCampaign[]; absorbed_offline?: number }>('/api/campaigns');
      setCampaigns(res.campaigns || []);
      if (showToast) {
        notify(
          res.absorbed_offline && res.absorbed_offline > 0
            ? `已自动吸收 ${res.absorbed_offline} 条官方扶持活动`
            : `已刷新官方扶持活动雷达（当前 ${(res.campaigns || []).length} 条）`
        );
      }
    } catch (err: any) {
      if (showToast) notify(err.message, 'err');
    } finally {
      setLoadingCampaigns(false);
    }
  };

  const handleCampaignToTopic = async (c: PlatformCampaign) => {
    try {
      const res = await api.post<{ topic: { id: number; title: string } }>(`/api/campaigns/${c.id}/to-topic`);
      setCampaigns((prev) => prev.map((x) => (x.id === c.id ? { ...x, converted_topic_id: res.topic.id } : x)));
      await loadTopics(gradeFilter, tierFilter);
      notify(`已转入选题池：《${res.topic.title}》`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleDeleteCampaign = async (id: number) => {
    await api.del(`/api/campaigns/${id}`);
    setCampaigns((prev) => prev.filter((x) => x.id !== id));
  };

  const loadHot = async (src: string, force = false) => {
    if (enableHotFeeds === false) return;
    setLoadingHot(true);
    setSelectedHot(new Set());
    try {
      const res = await api.get<{
        enabled?: boolean;
        items?: HotItem[];
        fetched_at?: string;
        cached?: boolean;
        latency_ms?: number;
        degraded?: boolean;
        warning?: string;
      }>(`/api/hot/${src}?force=${force ? 'true' : 'false'}`);
      if (res.enabled === false) {
        setHotDisabledByServer(true);
        setHotItems([]);
        setHotMeta(null);
      } else {
        setHotDisabledByServer(false);
        setHotItems(res.items || []);
        setHotMeta({
          fetched_at: res.fetched_at || '',
          cached: Boolean(res.cached),
          latency_ms: res.latency_ms || 0,
          degraded: Boolean(res.degraded),
          warning: res.warning || '',
        });
      }
    } catch (err: any) {
      setHotMeta({
        fetched_at: '',
        cached: false,
        latency_ms: 0,
        degraded: true,
        warning: err?.message || '当前第三方热榜源暂不可达，请稍后重试或改用上方 AI 联网搜选题。',
      });
    } finally {
      setLoadingHot(false);
    }
  };

  const loadTopics = async (grade = gradeFilter, tier = tierFilter) => {
    try {
      const params = new URLSearchParams();
      params.set('grade', grade);
      if (tier && tier !== 'ALL') params.set('content_tier', tier);
      const res = await api.get<{ topics: Topic[] }>(`/api/topics?${params.toString()}`);
      setTopics(res.topics || []);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  useEffect(() => {
    if (enableHotFeeds !== false) {
      setHotDisabledByServer(false);
      loadHot(source, false);
    }
  }, [source, enableHotFeeds]);

  useEffect(() => {
    loadTopics(gradeFilter, tierFilter);
  }, [gradeFilter, tierFilter]);

  useEffect(() => {
    loadCampaigns(false);
  }, []);

  const toggleHot = (rank: number) => {
    const next = new Set(selectedHot);
    if (next.has(rank)) next.delete(rank);
    else next.add(rank);
    setSelectedHot(next);
  };

  const handleScoreSelected = async () => {
    const chosen = hotItems.filter((x) => selectedHot.has(x.rank));
    if (chosen.length === 0) {
      notify('请先在热榜勾选 1-8 条热点', 'err');
      return;
    }
    setScoring(true);
    try {
      const res = await api.post<{ used_model: string; saved_count: number }>('/api/topics/score-hot', {
        items: chosen.slice(0, 10),
        theme: defaultTheme,
      });
      notify(`已完成 ${res.saved_count} 条热点主题转译与双轨 5 维打分${isCloud ? '' : ` (${res.used_model})`}`);
      setSelectedHot(new Set());
      await loadTopics(gradeFilter, tierFilter);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setScoring(false);
    }
  };

  const handleSearchTopic = async (e: React.FormEvent) => {
    e.preventDefault();
    setSearchingTopic(true);
    try {
      const res = await api.post<{ used_model: string; saved_count: number }>('/api/topics/search-topic', {
        keyword: topicKeyword.trim(),
        theme: defaultTheme,
      });
      notify(`已挖回 ${res.saved_count} 个高势能短剧选题${isCloud ? '' : ` (${res.used_model})`}`);
      await loadTopics(gradeFilter, tierFilter);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setSearchingTopic(false);
    }
  };

  const handleSearchBenchmarks = async () => {
    setSearchingBenchmarks(true);
    try {
      const res = await api.post<{ used_model?: string; saved_count: number }>('/api/topics/search-benchmarks', {
        focus: benchmarkFocus.trim(),
        theme: defaultTheme,
        keyword: topicKeyword.trim(),
        count: 6,
      });
      notify(`全网爆款结构对标完成：已转译入库 ${res.saved_count} 个原创升级选题！`);
      await loadTopics(gradeFilter, tierFilter);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setSearchingBenchmarks(false);
    }
  };

  const handleAddManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!manualTitle.trim()) return;
    try {
      await api.post('/api/topics', { title: manualTitle.trim(), summary: manualNote.trim(), source: 'manual' });
      setManualTitle('');
      setManualNote('');
      notify('已录入选题池');
      await loadTopics(gradeFilter, tierFilter);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleScoreSingleManual = async (t: Topic) => {
    setScoring(true);
    try {
      const res = await api.post<{ used_model: string }>('/api/topics/score-hot', {
        items: [{ title: t.title, summary: t.summary, source: t.source }],
        theme: defaultTheme,
      });
      await api.del(`/api/topics/${t.id}`);
      notify(`已完成双轨 5 维打分${isCloud ? '' : ` (${res.used_model})`}`);
      await loadTopics(gradeFilter, tierFilter);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setScoring(false);
    }
  };

  const handlePromote = async (t: Topic) => {
    const pid = promoteTargets[t.id] || t.recommended_project_id || projects[0]?.id;
    if (!pid) {
      notify('请先创建一个剧目', 'err');
      return;
    }
    try {
      const res = await api.post<{ episode: { id: number; project_id: number; ep_number: number } }>(
        `/api/topics/${t.id}/promote`,
        { project_id: pid }
      );
      notify(`已立项为第 ${res.episode.ep_number} 集，正在进入剧目工作台...`);
      onPromoteDone(res.episode.project_id, res.episode.id);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleDeleteTopic = async (id: number) => {
    await api.del(`/api/topics/${id}`);
    setTopics((prev) => prev.filter((x) => x.id !== id));
  };

  const gradeBadge = (t: Topic) => {
    if (t.veto) {
      return (
        <span className="inline-flex items-center gap-1 rounded bg-rose-50 px-2 py-0.5 text-xs font-semibold text-rose-700 border border-rose-200">
          <ShieldAlert className="h-3 w-3" /> 红线淘汰
        </span>
      );
    }
    if (t.grade === 'S') {
      return (
        <span className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-700 border border-emerald-200">
          S级 · {t.total_score}分
        </span>
      );
    }
    if (t.grade === 'A') {
      return (
        <span className="rounded bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-800 border border-amber-200">
          A级 · {t.total_score}分
        </span>
      );
    }
    if (t.grade === 'DROP') {
      return (
        <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-500 border border-slate-200">
          低分折叠 · {t.total_score}分
        </span>
      );
    }
    return <span className="rounded bg-sky-50 px-2 py-0.5 text-xs text-sky-700 border border-sky-200">未打分</span>;
  };

  const tierBadge = (t: Topic) => {
    const tier = resolveDisplayTier(t);
    if (tier === TIER_QUALITY) {
      return (
        <span className="rounded-full bg-indigo-50 border border-indigo-200 px-2 py-0.5 text-[11px] font-semibold text-indigo-700">
          🎬 {TIER_QUALITY}
        </span>
      );
    }
    if (tier === TIER_VIRAL) {
      return (
        <span className="rounded-full bg-fuchsia-50 border border-fuchsia-200 px-2 py-0.5 text-[11px] font-semibold text-fuchsia-700">
          🔥 {TIER_VIRAL}
        </span>
      );
    }
    return null;
  };

  return (
    <div className="space-y-5">
      {/* 头部标题 + 通用主题选题 & 全网爆款结构对标工具栏 */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl sm:text-2xl font-semibold text-slate-900">
              {isCloud ? '热点与选题雷达' : '热点与选题库'}
            </h1>
            <p className="mt-1 text-xs sm:text-sm text-slate-500">
              AI 双轨 5 维打分（自动区分{' '}
              <span className="font-semibold text-indigo-700">{TIER_QUALITY}</span> 与{' '}
              <span className="font-semibold text-fuchsia-700">{TIER_VIRAL}</span>）+ 全网爆款结构拆解转译
              {showHotFeeds ? ' + 国内五大公开热榜直连抓取。' : '。'}
            </p>
          </div>
        </div>

        {/* 双搜索工具条（适配 390px 手机不溢出） */}
        <div className="studio-card rounded-2xl p-3.5 grid grid-cols-1 gap-2.5 lg:grid-cols-12">
          <form onSubmit={handleSearchTopic} className="lg:col-span-6 flex flex-col sm:flex-row gap-2">
            <input
              value={topicKeyword}
              onChange={(e) => setTopicKeyword(e.target.value)}
              placeholder="主题关键词（如：科幻奇观、萌宠拟人、美食反转、职场微剧...）"
              className="studio-input flex-1 min-w-0 rounded-lg px-3 py-2 text-xs"
            />
            <button
              type="submit"
              disabled={searchingTopic}
              className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-amber-500 px-3.5 py-2 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-50 cursor-pointer shrink-0"
            >
              <Search className="h-3.5 w-3.5" />
              {searchingTopic ? 'AI 挖题中...' : isCloud ? 'AI 搜主题选题' : 'AI 联网搜选题'}
            </button>
          </form>

          <div className="lg:col-span-6 flex flex-col sm:flex-row gap-2">
            <input
              value={benchmarkFocus}
              onChange={(e) => setBenchmarkFocus(e.target.value)}
              placeholder="全网爆款对标方向（可选，如：高反转剧情/微距奇观/器物觉醒）"
              className="studio-input flex-1 min-w-0 rounded-lg px-3 py-2 text-xs"
            />
            <button
              type="button"
              onClick={handleSearchBenchmarks}
              disabled={searchingBenchmarks}
              className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50 cursor-pointer shrink-0 shadow-2xs"
            >
              <Compass className={`h-3.5 w-3.5 ${searchingBenchmarks ? 'animate-spin' : ''}`} />
              {searchingBenchmarks ? '正在拆解对标爆款...' : '全网爆款结构对标'}
            </button>
          </div>
        </div>
      </div>

      {/* 官方流量扶持活动雷达 */}
      <div className="studio-card rounded-2xl overflow-hidden border-violet-200">
        <div className="px-4 py-3 bg-violet-50/60 border-b border-violet-100 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Megaphone className="h-4 w-4 text-violet-600" />
            <span className="text-sm font-semibold text-violet-950">官方流量扶持活动雷达</span>
            <span className="rounded-full bg-violet-100 border border-violet-200 px-2 py-0.5 text-[10px] font-semibold text-violet-800">
              进行中 {campaigns.filter((c) => c.status === 'active').length} 项
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => loadCampaigns(true)}
              disabled={loadingCampaigns}
              className="inline-flex items-center gap-1 rounded-md border border-violet-200 bg-white px-2.5 py-1 text-xs text-violet-700 hover:bg-violet-50 cursor-pointer"
            >
              <RefreshCw className={`h-3 w-3 ${loadingCampaigns ? 'animate-spin' : ''}`} /> 同步最新
            </button>
            <button
              onClick={() => setCampaignsOpen((v) => !v)}
              className="rounded-md border border-slate-200 bg-white p-1 text-slate-500 hover:bg-slate-100 cursor-pointer"
            >
              {campaignsOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>

        {campaignsOpen && (
          <div className="p-3.5 bg-white">
            {campaigns.length === 0 ? (
              <div className="py-5 text-center text-xs text-slate-500">
                暂无已同步的官方流量扶持活动，有新大赛或官方话题时会自动出现在这里。
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                {campaigns.map((c) => {
                  const expired = c.status === 'expired';
                  return (
                    <div
                      key={c.id}
                      className={`rounded-xl border p-3.5 space-y-2 flex flex-col justify-between ${
                        expired ? 'border-slate-200 bg-slate-50/70 opacity-65' : 'border-slate-200 bg-[#fbfcfe]'
                      }`}
                    >
                      <div className="space-y-1.5">
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="rounded bg-violet-50 border border-violet-200 px-1.5 py-0.5 text-[10px] font-semibold text-violet-700">
                              {c.platform}
                            </span>
                            {expired ? (
                              <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">已截止</span>
                            ) : (
                              c.deadline && (
                                <span className="rounded bg-amber-50 border border-amber-200 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
                                  截止：{c.deadline}
                                </span>
                              )
                            )}
                          </div>
                          <button
                            onClick={() => handleDeleteCampaign(c.id)}
                            className="text-slate-400 hover:text-rose-600 cursor-pointer"
                            title="移除此活动"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>

                        <div className="text-xs font-semibold text-slate-900 leading-snug">{c.title}</div>

                        {c.hashtags && c.hashtags.length > 0 && (
                          <div className="flex flex-wrap gap-1 pt-0.5">
                            {c.hashtags.map((tag) => (
                              <span
                                key={tag}
                                className="rounded-md bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
                              >
                                {tag}
                              </span>
                            ))}
                          </div>
                        )}

                        {c.incentive && (
                          <div className="text-[11px] text-emerald-800 bg-emerald-50/70 border border-emerald-200/70 rounded-md px-2 py-1">
                            激励门槛：{c.incentive}
                          </div>
                        )}
                        {c.angle_pitch && (
                          <div className="text-[11px] text-slate-600 leading-relaxed">切入思路：{c.angle_pitch}</div>
                        )}
                      </div>

                      <div className="flex items-center justify-between gap-2 pt-2 border-t border-slate-100">
                        <button
                          onClick={async () => {
                            const txt = (c.hashtags || []).join(' ');
                            if (!txt) {
                              notify('此活动暂无必带话题标签', 'err');
                              return;
                            }
                            await copyToClipboard(txt);
                            notify(`已复制必带标签：${txt}`);
                          }}
                          className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-sky-50 hover:border-sky-200 hover:text-sky-700 cursor-pointer"
                        >
                          <Copy className="h-3 w-3" /> 复制标签
                        </button>
                        {c.converted_topic_id ? (
                          <span className="rounded bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                            已入选题池
                          </span>
                        ) : (
                          <button
                            onClick={() => handleCampaignToTopic(c)}
                            className="inline-flex items-center gap-1 rounded-md bg-violet-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-violet-700 cursor-pointer"
                          >
                            <Lightbulb className="h-3 w-3" /> 转入选题池
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 国内热榜关闭时的友好提示条 */}
      {!showHotFeeds && (
        <div className="studio-card rounded-xl px-4 py-3 flex flex-wrap items-center justify-between gap-2 border-slate-200 bg-slate-50/80 text-xs text-slate-600">
          <span>
            国内五大实时热榜雷达当前已关闭（按需隐藏）。你仍可使用上方 AI 联网搜选题、全网爆款结构对标与下方手动记灵感。
          </span>
          {onOpenSettings && (
            <button
              type="button"
              onClick={onOpenSettings}
              className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-white px-2.5 py-1 text-xs font-medium text-sky-700 hover:bg-sky-50 cursor-pointer shrink-0"
            >
              <Settings className="h-3.5 w-3.5" /> 前往系统设置重新开启
            </button>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
        {/* 左列：实时国内直连热榜（当 enable_hot_feeds 为 true 时显示） */}
        {showHotFeeds && (
          <div className="xl:col-span-5 studio-card rounded-2xl flex flex-col overflow-hidden max-h-[72vh]">
            <div className="border-b border-slate-200 p-3.5 sm:p-4 space-y-3 bg-slate-50/50">
              <div className="flex items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-1.5 text-sm font-semibold text-slate-900">
                  <Flame className="h-4 w-4 text-orange-500" />
                  实时公开热榜
                  <span className="inline-flex items-center gap-1 rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                    <Wifi className="h-2.5 w-2.5" /> 国内直连
                  </span>
                </div>
                <button
                  onClick={() => loadHot(source, true)}
                  disabled={loadingHot}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
                >
                  <RefreshCw className={`h-3 w-3 ${loadingHot ? 'animate-spin' : ''}`} /> 强刷
                </button>
              </div>

              <div className="flex flex-wrap gap-1.5">
                {SOURCES.map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setSource(s.id)}
                    className={`rounded-md px-2.5 py-1 text-xs font-medium transition cursor-pointer ${
                      source === s.id
                        ? 'bg-sky-600 text-white shadow-2xs'
                        : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-100'
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
                <span>
                  {hotMeta
                    ? `${
                        hotMeta.degraded
                          ? '降级缓存'
                          : hotMeta.cached
                            ? '缓存命中'
                            : `直连 ${hotMeta.latency_ms}ms`
                      }${hotMeta.fetched_at ? ` · ${hotMeta.fetched_at.slice(11)}` : ''}`
                    : '加载中...'}
                </span>
                <button
                  onClick={handleScoreSelected}
                  disabled={scoring || selectedHot.size === 0}
                  className="inline-flex items-center gap-1 rounded-md bg-sky-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-40 cursor-pointer"
                >
                  <Sparkles className="h-3 w-3" />
                  {scoring ? 'AI 评估中...' : `送 AI 双轨打分 (${selectedHot.size})`}
                </button>
              </div>

              {hotMeta?.degraded && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900 leading-relaxed">
                  ⚠️ {hotMeta.warning || '当前热榜源访问波动，已自动降级展示最近一次历史缓存（不影响 AI 搜题与手动记灵感）。'}
                </div>
              )}
            </div>

            <div className="flex-1 overflow-y-auto divide-y divide-slate-100">
              {hotItems.map((item) => {
                const checked = selectedHot.has(item.rank);
                return (
                  <div
                    key={`${item.source}-${item.rank}`}
                    onClick={() => toggleHot(item.rank)}
                    className={`flex items-start gap-2.5 px-3.5 py-2.5 text-sm transition cursor-pointer ${
                      checked ? 'bg-sky-50' : 'hover:bg-slate-50'
                    }`}
                  >
                    <div className="mt-0.5 text-sky-600">
                      {checked ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4 text-slate-300" />}
                    </div>
                    <span
                      className={`w-5 shrink-0 text-center text-xs font-bold mt-0.5 ${
                        item.rank <= 3 ? 'text-amber-600' : 'text-slate-400'
                      }`}
                    >
                      {item.rank}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-slate-800 leading-snug text-xs sm:text-sm break-words">{item.title}</div>
                      {(item.summary || item.hot_value > 0) && (
                        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                          {item.hot_value > 0 && (
                            <span className="text-orange-600 font-medium">热度 {fmtHot(item.hot_value)}</span>
                          )}
                          {item.video_count > 0 && <span className="text-sky-700">关联视频 {item.video_count} 条</span>}
                          {item.summary && !item.summary.startsWith('关联视频') && (
                            <span className="truncate max-w-[220px]">{item.summary}</span>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
              {!loadingHot && hotItems.length === 0 && (
                <div className="p-8 text-center text-xs text-slate-400">暂无数据，点击右上角强刷重试</div>
              )}
            </div>
          </div>
        )}

        {/* 右列（热榜关闭时自动占满 12 列）：5 维双轨打分选题池 + 手动记灵感 */}
        <div className={`${showHotFeeds ? 'xl:col-span-7' : 'xl:col-span-12'} flex flex-col gap-4 max-h-[78vh]`}>
          <form onSubmit={handleAddManual} className="studio-card rounded-xl p-3.5 flex flex-col sm:flex-row gap-2">
            <input
              value={manualTitle}
              onChange={(e) => setManualTitle(e.target.value)}
              placeholder="随手记一条短剧灵感 / 热点标题..."
              className="studio-input flex-1 min-w-0 rounded-lg px-3 py-1.5 text-xs"
            />
            <input
              value={manualNote}
              onChange={(e) => setManualNote(e.target.value)}
              placeholder="补充卖点或链接（可选）"
              className="studio-input sm:w-48 rounded-lg px-3 py-1.5 text-xs"
            />
            <button
              type="submit"
              className="inline-flex items-center justify-center gap-1 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 cursor-pointer shrink-0"
            >
              <Plus className="h-3.5 w-3.5" /> 记入选题池
            </button>
          </form>

          <div className="studio-card rounded-2xl flex-1 flex flex-col overflow-hidden">
            <div className="border-b border-slate-200 px-3.5 py-3 space-y-2 bg-slate-50/50">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold text-slate-900">双轨 5 维打分选题池（≥85 S级立项）</div>
                <div className="flex flex-wrap gap-1 text-xs">
                  {[
                    { id: 'ALL', label: '全部' },
                    { id: 'S', label: 'S 级' },
                    { id: 'A', label: 'A 级' },
                    { id: 'UNSCORED', label: '未打分' },
                    { id: 'DROP', label: '已淘汰' },
                  ].map((tab) => (
                    <button
                      key={tab.id}
                      onClick={() => setGradeFilter(tab.id)}
                      className={`rounded-md px-2 py-1 transition cursor-pointer ${
                        gradeFilter === tab.id
                          ? 'bg-slate-800 text-white font-medium'
                          : 'text-slate-600 hover:bg-slate-200/70'
                      }`}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* 双轨筛选条 */}
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="text-slate-500">双轨筛选：</span>
                {[
                  { id: 'ALL', label: '全部轨道' },
                  { id: TIER_QUALITY, label: `🎬 ${TIER_QUALITY}` },
                  { id: TIER_VIRAL, label: `🔥 ${TIER_VIRAL}` },
                ].map((tr) => (
                  <button
                    key={tr.id}
                    type="button"
                    onClick={() => setTierFilter(tr.id)}
                    className={`rounded-full px-2.5 py-0.5 border transition cursor-pointer ${
                      tierFilter === tr.id
                        ? 'bg-indigo-600 border-indigo-600 text-white font-medium'
                        : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
                    }`}
                  >
                    {tr.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-3.5 sm:p-4 space-y-3 bg-[#f8fafc]">
              {topics.map((t) => {
                const targetProjId = promoteTargets[t.id] || t.recommended_project_id || projects[0]?.id || 0;
                const s = t.scores || {};
                const themeVal = s.theme ?? t.score_theme ?? 0;
                return (
                  <div
                    key={t.id}
                    className={`rounded-xl border p-3.5 sm:p-4 space-y-2.5 shadow-2xs ${
                      t.veto
                        ? 'border-rose-200 bg-rose-50/50'
                        : t.grade === 'S'
                          ? 'border-emerald-200 bg-emerald-50/40'
                          : 'border-slate-200 bg-white'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="space-y-1.5 min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          {gradeBadge(t)}
                          {tierBadge(t)}
                          <span className="text-[11px] text-slate-500">来源：{t.source}</span>
                          {t.status === 'promoted' && (
                            <span className="rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
                              已立项
                            </span>
                          )}
                        </div>
                        <h3 className="text-sm font-semibold text-slate-900 break-words">{t.title}</h3>
                      </div>
                      <button
                        onClick={() => handleDeleteTopic(t.id)}
                        className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600 cursor-pointer shrink-0"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>

                    {t.veto_reason && (
                      <div className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-1.5">
                        一票否决原因：{t.veto_reason}
                      </div>
                    )}

                    {t.ai_pitch && (
                      <p className="text-xs leading-relaxed text-amber-900 bg-amber-50/80 border border-amber-200/70 rounded-lg px-3 py-2 break-words">
                        卖点与钩子：{t.ai_pitch}
                      </p>
                    )}
                    {t.summary && <p className="text-xs text-slate-600 leading-relaxed break-words">{t.summary}</p>}

                    {(Object.keys(s).length > 0 || (t.score_theme ?? 0) > 0) && (
                      <div className="grid grid-cols-2 sm:grid-cols-5 gap-1.5 pt-1 text-[11px]">
                        {[
                          { k: 'theme', val: themeVal, label: '主题契合', max: 25 },
                          { k: 'hook', val: s.hook ?? 0, label: '前3秒奇观', max: 25 },
                          { k: 'safe', val: s.safe ?? 0, label: '合规体面', max: 20 },
                          { k: 'viral', val: s.viral ?? 0, label: '互动二创', max: 15 },
                          { k: 'serial', val: s.serial ?? 0, label: 'AI可拍度', max: 15 },
                        ].map((dim) => (
                          <div key={dim.k} className="rounded-lg bg-white border border-slate-200 px-2 py-1.5">
                            <div className="text-slate-500 text-[10px]">{dim.label}</div>
                            <div className="font-semibold text-slate-900">
                              {dim.val}
                              <span className="text-slate-400 font-normal">/{dim.max}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="flex flex-wrap items-center justify-end gap-2 pt-2 border-t border-slate-200/70">
                      {t.grade === 'UNSCORED' && (
                        <button
                          onClick={() => handleScoreSingleManual(t)}
                          disabled={scoring}
                          className="inline-flex items-center gap-1 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                        >
                          <Sparkles className="h-3 w-3" /> AI 双轨打分
                        </button>
                      )}
                      {!t.veto && projects.length > 0 && (
                        <>
                          <select
                            value={targetProjId}
                            onChange={(e) =>
                              setPromoteTargets((prev) => ({ ...prev, [t.id]: Number(e.target.value) }))
                            }
                            className="studio-input rounded-md px-2 py-1 text-xs max-w-[180px]"
                          >
                            {projects.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.name}
                              </option>
                            ))}
                          </select>
                          <button
                            onClick={() => handlePromote(t)}
                            className="inline-flex items-center gap-1 rounded-md bg-sky-600 px-3 py-1 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
                          >
                            一键立项进剧集 <ArrowUpRight className="h-3.5 w-3.5" />
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
              {topics.length === 0 && (
                <div className="py-14 text-center text-xs text-slate-400">
                  选题池还是空的：{showHotFeeds ? '勾选左侧热榜送 AI 双轨打分，或' : ''}点上方「AI 联网搜选题」「全网爆款结构对标」立刻开挖！
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
