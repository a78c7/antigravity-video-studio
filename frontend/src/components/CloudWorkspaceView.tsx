import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  FileCheck2,
  Film,
  History,
  Image as ImageIcon,
  Layers,
  MessageSquare,
  Pencil,
  RefreshCw,
  Save,
  Shield,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Vote,
} from 'lucide-react';
import { api, mediaUrl, roleLabel } from '../api';
import { ImageLightbox } from './ImageLightbox';
import { RevisionsDrawer } from './RevisionsDrawer';
import type {
  Asset,
  AuthUser,
  CoverCandidate,
  Episode,
  Project,
  RenderJob,
  ReviewVote,
  RevisionItem,
  Shot,
} from '../types';

export type CloudSection = 'review' | 'script' | 'shots' | 'assets';

interface Props {
  project: Project;
  initialEpisodeId?: number | null;
  initialSection?: CloudSection;
  initialOpenRevisions?: boolean;
  user: AuthUser | null;
  onBack: () => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const ASSET_TYPE_LABELS: Record<string, string> = {
  character: '角色',
  landmark: '场景/地标',
  prop: '道具',
  style: '画风',
};

const AUDIO_STRATEGY_CLOUD_LABELS: Record<string, string> = {
  keep_flow_sfx: '保留原生环境音效（无背景乐）',
  keep_flow_dialogue: '保留原生中文台词 + 环境音',
  mute_for_local_vo: '后期独立旁白配音（画面静音底）',
};

function parseCoverCandidates(ep: Episode | null): CoverCandidate[] {
  if (!ep) return [];
  if (Array.isArray(ep.cover_candidates) && ep.cover_candidates.length > 0) {
    return ep.cover_candidates;
  }
  if (ep.cover_candidates_json) {
    try {
      const parsed = JSON.parse(ep.cover_candidates_json);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      return [];
    }
  }
  return [];
}

/**
 * 远程协作专用剧目与审片工作台：
 * - 手机（390px）与桌面端自适应；
 * - 支持多方审片与封面投票墙、在线流畅看片、下载终版/AI 原创证明、剧本/分镜共创与修改留痕、资产圣经与防穿帮护盾展示。
 */
export const CloudWorkspaceView: React.FC<Props> = ({
  project,
  initialEpisodeId,
  initialSection = 'review',
  initialOpenRevisions = false,
  user,
  onBack,
  notify,
}) => {
  const [section, setSection] = useState<CloudSection>(initialSection);
  const [proj, setProj] = useState<Project>(project);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [activeEpId, setActiveEpId] = useState<number | null>(initialEpisodeId || null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [shots, setShots] = useState<Shot[]>([]);
  const [renderJobs, setRenderJobs] = useState<RenderJob[]>([]);
  const [votes, setVotes] = useState<ReviewVote[]>([]);
  const [revisions, setRevisions] = useState<RevisionItem[]>([]);

  const [refreshing, setRefreshing] = useState(false);
  const [revisionsOpen, setRevisionsOpen] = useState(initialOpenRevisions);
  const [lightbox, setLightbox] = useState<{ src: string; title: string; downloadHref?: string } | null>(null);

  // 剧本共创编辑
  const [epDraft, setEpDraft] = useState<Partial<Episode>>({});
  const [savingEp, setSavingEp] = useState(false);

  // 分镜内联共创编辑
  const [editingShotId, setEditingShotId] = useState<number | null>(null);
  const [shotDraft, setShotDraft] = useState<{
    shot_title: string;
    camera_type: string;
    duration_sec: number;
    audio_strategy: string;
    cn_action_desc: string;
    cn_dialogue: string;
    subtitle_text: string;
  } | null>(null);
  const [savingShot, setSavingShot] = useState(false);

  // 审片投票输入（首尾帧 / 360p 试卡与成片 / 双比例封面）
  const [voteNotes, setVoteNotes] = useState<Record<string, string>>({});
  const [votingKey, setVotingKey] = useState<string | null>(null);

  // 查看单镜视频展开状态
  const [showShotVideos, setShowShotVideos] = useState(false);

  const activeEp = episodes.find((e) => e.id === activeEpId) || null;
  const coverCandidates = useMemo(() => parseCoverCandidates(activeEp), [activeEp]);

  // 优先读取 episode 直接透出的总览图与 AI 原创证明路径，再兜底从 render_jobs 提取
  const latestAssetsFromJobs = useMemo(() => {
    let framesSheetPath = (activeEp?.frames_sheet_path || '').trim();
    let coversSheetPath = (activeEp?.covers_sheet_path || '').trim();
    let qcOverviewPath = (activeEp?.qc_overview_path || '').trim();
    let aiProofPath = (activeEp?.ai_proof_path || '').trim();
    for (const j of renderJobs) {
      const jt = (j.job_type || '').toLowerCase();
      const out = (j.output_path || '').trim();
      if (!out) continue;
      if (!framesSheetPath && (jt.includes('frame') || out.includes('frames_sheet'))) framesSheetPath = out;
      if (!coversSheetPath && (jt.includes('cover') || out.includes('covers_sheet'))) coversSheetPath = out;
      if (!qcOverviewPath && (jt.includes('qc') || out.includes('qc_overview'))) qcOverviewPath = out;
      if (!aiProofPath && (jt.includes('proof') || out.endsWith('.md'))) aiProofPath = out;
    }
    return { framesSheetPath, coversSheetPath, qcOverviewPath, aiProofPath };
  }, [activeEp, renderJobs]);

  const loadProjectCore = async () => {
    const [pRes, eRes, aRes] = await Promise.all([
      api.get<{ project: Project }>(`/api/projects/${project.id}`),
      api.get<{ episodes: Episode[] }>(`/api/projects/${project.id}/episodes`),
      api.get<{ assets: Asset[] }>(`/api/projects/${project.id}/assets`),
    ]);
    setProj(pRes.project);
    setEpisodes(eRes.episodes || []);
    setAssets(aRes.assets || []);

    let chosen = activeEpId;
    if (chosen && !eRes.episodes.some((e) => e.id === chosen)) chosen = null;
    if (!chosen && initialEpisodeId && eRes.episodes.some((e) => e.id === initialEpisodeId)) {
      chosen = initialEpisodeId;
    }
    if (!chosen && eRes.episodes.length > 0) {
      chosen = eRes.episodes[0].id;
    }
    setActiveEpId(chosen);
    return chosen;
  };

  const loadEpisodeAll = async (epId: number) => {
    const [sRes, vRes, rRes, tlRes] = await Promise.all([
      api.get<{ episode: Episode; shots: Shot[] }>(`/api/episodes/${epId}/shots`),
      api.get<{ votes: ReviewVote[] }>(`/api/episodes/${epId}/votes`).catch(() => ({ votes: [] })),
      api.get<{ revisions: RevisionItem[] }>(`/api/collab/revisions?episode_id=${epId}&limit=80`).catch(() => ({
        revisions: [],
      })),
      api.get<{ jobs: RenderJob[] }>(`/api/episodes/${epId}/timeline`).catch(() => ({ jobs: [] })),
    ]);
    setShots(sRes.shots || []);
    setEpDraft(sRes.episode);
    setEpisodes((prev) => prev.map((e) => (e.id === epId ? { ...e, ...sRes.episode } : e)));
    setVotes(vRes.votes || []);
    setRevisions(rRes.revisions || []);
    setRenderJobs(tlRes.jobs || []);
  };

  const refreshAll = async (showToast = false) => {
    setRefreshing(true);
    try {
      const epId = await loadProjectCore();
      if (epId) await loadEpisodeAll(epId);
      if (showToast) notify('已同步最新剧集与审片记录');
    } catch (err: any) {
      notify(err?.message || '加载剧目数据失败', 'err');
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    setSection(initialSection);
  }, [initialSection]);

  useEffect(() => {
    setRevisionsOpen(initialOpenRevisions);
  }, [initialOpenRevisions]);

  useEffect(() => {
    loadProjectCore().then((epId) => {
      if (epId) loadEpisodeAll(epId);
    });
  }, [project.id]);

  useEffect(() => {
    if (activeEpId) {
      loadEpisodeAll(activeEpId);
    }
  }, [activeEpId]);

  const handleSaveEpisode = async () => {
    if (!activeEpId) return;
    setSavingEp(true);
    try {
      const res = await api.patch<{ episode: Episode; revisions_logged?: number }>(`/api/episodes/${activeEpId}`, {
        title: epDraft.title,
        hook_0_3s: epDraft.hook_0_3s,
        script_text: epDraft.script_text,
        ending_cta: epDraft.ending_cta,
        next_ep_hook: epDraft.next_ep_hook,
        continuity_summary: epDraft.continuity_summary,
      });
      await loadEpisodeAll(activeEpId);
      const cnt = res.revisions_logged ?? 0;
      notify(cnt > 0 ? `剧本已保存，已自动记入 ${cnt} 条修改留痕并通知团队成员` : '剧本已保存（内容无实质变化）');
    } catch (err: any) {
      notify(err?.message || '保存剧本失败', 'err');
    } finally {
      setSavingEp(false);
    }
  };

  const openShotEditor = (s: Shot) => {
    if (editingShotId === s.id) {
      setEditingShotId(null);
      setShotDraft(null);
      return;
    }
    setEditingShotId(s.id);
    setShotDraft({
      shot_title: s.shot_title || '',
      camera_type: s.camera_type || '中景',
      duration_sec: s.duration_sec || 6,
      audio_strategy: s.audio_strategy || 'keep_flow_sfx',
      cn_action_desc: s.cn_action_desc || '',
      cn_dialogue: s.cn_dialogue || '',
      subtitle_text: s.subtitle_text || '',
    });
  };

  const saveShotEdit = async (s: Shot) => {
    if (!shotDraft || !activeEpId) return;
    setSavingShot(true);
    try {
      await api.patch(`/api/shots/${s.id}`, {
        shot_title: shotDraft.shot_title.trim() || `镜头 ${s.shot_no}`,
        camera_type: shotDraft.camera_type.trim() || '中景',
        duration_sec: Math.max(4, Math.min(15, Number(shotDraft.duration_sec) || 6)),
        audio_strategy: shotDraft.audio_strategy,
        cn_action_desc: shotDraft.cn_action_desc,
        cn_dialogue: shotDraft.cn_dialogue,
        subtitle_text: shotDraft.subtitle_text,
      });
      setEditingShotId(null);
      setShotDraft(null);
      await loadEpisodeAll(activeEpId);
      notify(`已保存 S${String(s.shot_no).padStart(2, '0')} 分镜修改并自动留痕`);
    } catch (err: any) {
      notify(err?.message || '保存分镜失败', 'err');
    } finally {
      setSavingShot(false);
    }
  };

  const submitVote = async (
    targetType: 'frame' | 'video_360p' | 'cover',
    targetKey: string,
    choice: string,
    verdict: 'approve' | 'change' | 'comment',
    noteBucketKey: string
  ) => {
    if (!activeEpId) return;
    setVotingKey(`${targetType}:${targetKey}:${verdict}`);
    try {
      const comment = (voteNotes[noteBucketKey] || '').trim();
      await api.post(`/api/episodes/${activeEpId}/votes`, {
        target_type: targetType,
        target_key: targetKey,
        choice,
        verdict,
        comment,
      });
      setVoteNotes((prev) => ({ ...prev, [noteBucketKey]: '' }));
      const vRes = await api.get<{ votes: ReviewVote[] }>(`/api/episodes/${activeEpId}/votes`);
      setVotes(vRes.votes || []);
      notify(verdict === 'approve' ? '已记录「赞同」投票并通知团队成员' : '已提交修改建议并通知团队成员');
    } catch (err: any) {
      notify(err?.message || '投票提交失败', 'err');
    } finally {
      setVotingKey(null);
    }
  };

  const renderVoteList = (targetType: string, filterTargetKey?: string) => {
    const matched = votes.filter(
      (v) => v.target_type === targetType && (!filterTargetKey || v.target_key === filterTargetKey)
    );
    if (matched.length === 0) {
      return <div className="text-[11px] text-slate-400">暂未收到投票或批注</div>;
    }
    return (
      <div className="space-y-1.5">
        {matched.map((v) => (
          <div
            key={v.id}
            className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs flex flex-wrap items-start justify-between gap-2"
          >
            <div className="space-y-0.5 min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-semibold text-slate-900">{v.actor_name || v.actor_username}</span>
                <span className="text-[10px] text-slate-500">({roleLabel(v.actor_role)})</span>
                {v.verdict === 'approve' ? (
                  <span className="inline-flex items-center gap-0.5 rounded-full bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
                    <ThumbsUp className="h-2.5 w-2.5" /> 赞同{v.choice ? ` · ${v.choice}` : ''}
                  </span>
                ) : v.verdict === 'change' ? (
                  <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                    <ThumbsDown className="h-2.5 w-2.5" /> 建议修改{v.choice ? ` · ${v.choice}` : ''}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-0.5 rounded-full bg-sky-50 border border-sky-200 px-2 py-0.5 text-[10px] font-medium text-sky-700">
                    <MessageSquare className="h-2.5 w-2.5" /> 批注{v.choice ? ` · ${v.choice}` : ''}
                  </span>
                )}
              </div>
              {v.comment && <div className="text-slate-700 leading-relaxed break-words">{v.comment}</div>}
            </div>
            <span className="text-[10px] text-slate-400 font-mono shrink-0">{v.created_at.slice(5, 16)}</span>
          </div>
        ))}
      </div>
    );
  };

  const playableVideoPath = activeEp?.web_preview_path || activeEp?.latest_output_path || '';
  const hasHighResMaster = Boolean(activeEp?.latest_output_path);

  return (
    <div className="space-y-4">
      {/* 顶部控制卡 */}
      <div className="studio-card rounded-2xl p-3.5 sm:p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2.5">
          <div className="flex items-center gap-2.5 min-w-0">
            <button
              type="button"
              onClick={onBack}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer shrink-0"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> 剧目列表
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="text-base sm:text-lg font-semibold text-slate-900 truncate">{proj.name}</h1>
                <span className="rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 shrink-0">
                  {proj.aspect_ratio}
                </span>
              </div>
              <div className="text-[11px] text-slate-500 truncate">{proj.visual_mode}</div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
            <select
              value={activeEpId || ''}
              onChange={(e) => setActiveEpId(Number(e.target.value))}
              className="studio-input flex-1 sm:flex-initial rounded-lg px-2.5 py-1.5 text-xs min-w-[160px]"
            >
              {episodes.length === 0 && <option value="">尚无分集</option>}
              {episodes.map((e) => (
                <option key={e.id} value={e.id}>
                  第 {e.ep_number} 集 · {e.title}
                </option>
              ))}
            </select>

            <button
              type="button"
              onClick={() => setRevisionsOpen(true)}
              className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-sky-50 px-2.5 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 cursor-pointer"
            >
              <History className="h-3.5 w-3.5" />
              留痕 ({revisions.length})
            </button>

            <button
              type="button"
              onClick={() => refreshAll(true)}
              disabled={refreshing}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-100 cursor-pointer"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin text-sky-600' : ''}`} /> 刷新
            </button>
          </div>
        </div>

        {/* 4 个清爽云端模块切换（移动端 2×2，桌面 4 列） */}
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
          {[
            { id: 'review' as const, label: '审片与投票墙', icon: Vote },
            { id: 'script' as const, label: '剧本共创', icon: Pencil },
            { id: 'shots' as const, label: `分镜共创 (${shots.length})`, icon: Film },
            { id: 'assets' as const, label: `资产与护盾 (${assets.length})`, icon: Layers },
          ].map((tabItem) => {
            const Icon = tabItem.icon;
            const active = section === tabItem.id;
            return (
              <button
                key={tabItem.id}
                type="button"
                onClick={() => setSection(tabItem.id)}
                className={`inline-flex items-center justify-center gap-1.5 rounded-xl px-3 py-2 text-xs font-medium transition cursor-pointer ${
                  active
                    ? 'bg-sky-600 text-white font-semibold shadow-xs'
                    : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border border-slate-200'
                }`}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{tabItem.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* ==================== 1. 审片与封面投票墙 ==================== */}
      {section === 'review' && (
        <div className="space-y-4">
          {!activeEp ? (
            <div className="studio-card rounded-2xl py-16 text-center text-xs text-slate-400">当前剧目还没有分集。</div>
          ) : (
            <>
              {/* A. 在线流畅看片 + 高清终版下载 + AI 原创证明 */}
              <div className="studio-card rounded-2xl p-4 sm:p-5 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-sm sm:text-base font-semibold text-slate-900 inline-flex items-center gap-2">
                      <Film className="h-4 w-4 text-sky-600" /> 成片在线审看与高清下载
                    </h2>
                    <p className="text-[11px] text-slate-500 mt-0.5">
                      {activeEp.web_preview_path
                        ? '已自动启用流畅网页预览版（低流量秒播），审定后可下载高清母带。'
                        : '直接播放当前最新合成视频。'}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {hasHighResMaster && (
                      <a
                        href={mediaUrl(activeEp.latest_output_path, true)}
                        download
                        className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700"
                      >
                        <Download className="h-3.5 w-3.5" /> 下载高清终版
                      </a>
                    )}
                    {latestAssetsFromJobs.aiProofPath && (
                      <a
                        href={mediaUrl(latestAssetsFromJobs.aiProofPath, true)}
                        download
                        className="inline-flex items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100"
                      >
                        <FileCheck2 className="h-3.5 w-3.5" /> AI 原创制作证明
                      </a>
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
                  <div className="lg:col-span-5 flex flex-col items-center">
                    {playableVideoPath ? (
                      <div className="w-full max-w-[320px] rounded-2xl overflow-hidden bg-slate-950 border border-slate-200 shadow-sm">
                        <video
                          key={playableVideoPath}
                          src={mediaUrl(playableVideoPath)}
                          controls
                          playsInline
                          preload="metadata"
                          className="w-full max-h-[520px] object-contain"
                        />
                      </div>
                    ) : (
                      <div className="w-full rounded-2xl border border-dashed border-slate-300 bg-slate-50 py-14 text-center text-xs text-slate-400">
                        本集尚未同步成片预览，可展开下方查看单镜视频
                      </div>
                    )}
                  </div>

                  {/* 视频/试卡投票面板 */}
                  <div className="lg:col-span-7 space-y-3 flex flex-col justify-between">
                    <div className="rounded-xl border border-sky-200/80 bg-sky-50/40 p-3.5 space-y-2.5">
                      <div className="text-xs font-semibold text-sky-900">成片 / 360p 试卡审片表态</div>
                      <textarea
                        value={voteNotes['video_360p'] || ''}
                        onChange={(e) => setVoteNotes((prev) => ({ ...prev, video_360p: e.target.value }))}
                        rows={2}
                        placeholder="写下观感批注（如：整体节奏很稳，S03 字幕稍早 0.5 秒就完美了）..."
                        className="studio-input w-full rounded-lg p-2.5 text-xs"
                      />
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={votingKey !== null}
                          onClick={() => submitVote('video_360p', 'episode', '成片/试卡通过', 'approve', 'video_360p')}
                          className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50 cursor-pointer"
                        >
                          <ThumbsUp className="h-3.5 w-3.5" /> 赞同通过
                        </button>
                        <button
                          type="button"
                          disabled={votingKey !== null}
                          onClick={() => submitVote('video_360p', 'episode', '成片/试卡需调整', 'change', 'video_360p')}
                          className="inline-flex items-center gap-1 rounded-lg bg-amber-500 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-50 cursor-pointer"
                        >
                          <ThumbsDown className="h-3.5 w-3.5" /> 建议修改
                        </button>
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <div className="text-xs font-semibold text-slate-700">团队试卡与成片投票记录</div>
                      {renderVoteList('video_360p')}
                    </div>

                    {latestAssetsFromJobs.qcOverviewPath && (
                      <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-2.5 flex items-center justify-between gap-2 text-xs">
                        <span className="text-slate-700 font-medium">全集音画体检总览图已就绪</span>
                        <button
                          type="button"
                          onClick={() =>
                            setLightbox({
                              src: mediaUrl(latestAssetsFromJobs.qcOverviewPath),
                              title: `第 ${activeEp.ep_number} 集 音画体检总览图`,
                              downloadHref: mediaUrl(latestAssetsFromJobs.qcOverviewPath, true),
                            })
                          }
                          className="rounded-lg border border-sky-200 bg-white px-2.5 py-1 text-xs font-medium text-sky-700 hover:bg-sky-50 cursor-pointer"
                        >
                          查看大图
                        </button>
                      </div>
                    )}
                  </div>
                </div>

                {/* 可折叠：逐镜视频抽查 */}
                <div className="pt-2 border-t border-slate-100">
                  <button
                    type="button"
                    onClick={() => setShowShotVideos((v) => !v)}
                    className="text-xs font-medium text-sky-700 hover:underline cursor-pointer"
                  >
                    {showShotVideos ? '收起逐镜定稿视频列表 ↑' : `展开逐镜视频审看（共 ${shots.length} 镜）↓`}
                  </button>
                  {showShotVideos && (
                    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                      {shots.map((s) => {
                        const finalTake = s.takes.find((t) => t.media_type === 'video' && t.is_final === 1) || s.takes[0];
                        return (
                          <div key={s.id} className="rounded-xl border border-slate-200 bg-slate-50/60 p-3 space-y-2">
                            <div className="flex items-center justify-between text-xs">
                              <span className="font-semibold text-slate-900 truncate">
                                S{String(s.shot_no).padStart(2, '0')} · {s.shot_title}
                              </span>
                              <span className="text-[10px] text-slate-500 shrink-0">{s.duration_sec}s</span>
                            </div>
                            {finalTake ? (
                              <video
                                src={mediaUrl(finalTake.file_path)}
                                controls
                                playsInline
                                preload="none"
                                poster={s.start_frame_path ? mediaUrl(s.start_frame_path) : undefined}
                                className="w-full max-h-52 rounded-lg bg-slate-900"
                              />
                            ) : (
                              <div className="h-32 rounded-lg border border-dashed border-slate-300 bg-white flex items-center justify-center text-[11px] text-slate-400">
                                暂无视频
                              </div>
                            )}
                            {s.subtitle_text && (
                              <div className="text-[11px] text-amber-900 bg-amber-50 border border-amber-200/70 rounded px-2 py-1">
                                字幕：{s.subtitle_text}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>

              {/* B. 双比例封面投票墙（9:16 & 3:4） */}
              <div className="studio-card rounded-2xl p-4 sm:p-5 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-sm sm:text-base font-semibold text-slate-900 inline-flex items-center gap-2">
                      <Vote className="h-4 w-4 text-violet-600" /> 双比例封面投票墙（9:16 竖屏 & 3:4 图文封面）
                    </h2>
                    <p className="text-[11px] text-slate-500 mt-0.5">
                      点击封面可放大看细节，直接在心仪方案下点「赞同」或写修改建议。
                    </p>
                  </div>
                  {latestAssetsFromJobs.coversSheetPath && (
                    <button
                      type="button"
                      onClick={() =>
                        setLightbox({
                          src: mediaUrl(latestAssetsFromJobs.coversSheetPath),
                          title: `第 ${activeEp.ep_number} 集 双比例封面总览图`,
                          downloadHref: mediaUrl(latestAssetsFromJobs.coversSheetPath, true),
                        })
                      }
                      className="inline-flex items-center gap-1 rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-xs font-medium text-violet-800 hover:bg-violet-100 cursor-pointer"
                    >
                      <ImageIcon className="h-3.5 w-3.5" /> 看封面编号总览图
                    </button>
                  )}
                </div>

                {coverCandidates.length > 0 ? (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    {coverCandidates.map((c) => {
                      const isSelected = Boolean(activeEp.selected_cover_path && activeEp.selected_cover_path === c.path);
                      const noteKey = `cover_${c.index}`;
                      const labelText = c.label || `#${c.index} ${c.platform || c.ratio}`;
                      return (
                        <div
                          key={`${c.index}-${c.path}`}
                          className={`rounded-2xl border p-3 space-y-2.5 bg-white ${
                            isSelected ? 'border-emerald-400 ring-2 ring-emerald-100' : 'border-slate-200'
                          }`}
                        >
                          <div className="flex items-center justify-between gap-1.5 text-xs">
                            <span className="font-semibold text-slate-900 truncate">{labelText}</span>
                            <span className="rounded bg-violet-50 border border-violet-200 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 shrink-0">
                              {c.ratio}
                            </span>
                          </div>

                          <div
                            onClick={() =>
                              setLightbox({
                                src: mediaUrl(c.path),
                                title: labelText,
                                downloadHref: mediaUrl(c.path, true),
                              })
                            }
                            className="relative h-56 rounded-xl overflow-hidden bg-slate-900 border border-slate-200 cursor-zoom-in"
                          >
                            <img src={mediaUrl(c.path)} alt={labelText} className="h-full w-full object-contain" />
                            {isSelected && (
                              <span className="absolute top-2 left-2 inline-flex items-center gap-1 rounded-full bg-emerald-600 px-2 py-0.5 text-[10px] font-semibold text-white shadow">
                                <CheckCircle2 className="h-3 w-3" /> 已选定
                              </span>
                            )}
                          </div>

                          <input
                            value={voteNotes[noteKey] || ''}
                            onChange={(e) => setVoteNotes((prev) => ({ ...prev, [noteKey]: e.target.value }))}
                            placeholder="可选：写一句封面意见"
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-[11px]"
                          />

                          <div className="grid grid-cols-2 gap-1.5">
                            <button
                              type="button"
                              disabled={votingKey !== null}
                              onClick={() => submitVote('cover', `cover_${c.index}`, labelText, 'approve', noteKey)}
                              className="inline-flex items-center justify-center gap-1 rounded-lg bg-emerald-600 py-1.5 text-[11px] font-semibold text-white hover:bg-emerald-700 cursor-pointer"
                            >
                              <ThumbsUp className="h-3 w-3" /> 赞同
                            </button>
                            <button
                              type="button"
                              disabled={votingKey !== null}
                              onClick={() => submitVote('cover', `cover_${c.index}`, labelText, 'change', noteKey)}
                              className="inline-flex items-center justify-center gap-1 rounded-lg border border-amber-300 bg-amber-50 py-1.5 text-[11px] font-semibold text-amber-800 hover:bg-amber-100 cursor-pointer"
                            >
                              <ThumbsDown className="h-3 w-3" /> 建议改
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 py-8 text-center text-xs text-slate-400">
                    本集暂未同步双比例候选封面
                  </div>
                )}

                <div className="space-y-1.5 pt-1">
                  <div className="text-xs font-semibold text-slate-700">团队封面投票记录</div>
                  {renderVoteList('cover')}
                </div>
              </div>

              {/* C. 首尾帧编号总览图 & 单镜帧审看 + 投票 */}
              <div className="studio-card rounded-2xl p-4 sm:p-5 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-sm sm:text-base font-semibold text-slate-900 inline-flex items-center gap-2">
                      <ImageIcon className="h-4 w-4 text-amber-600" /> 首尾帧审看与挑图投票
                    </h2>
                    <p className="text-[11px] text-slate-500 mt-0.5">
                      检查角色形象、场景还原度与画面衔接，点击任意单帧放大查看。
                    </p>
                  </div>
                  {latestAssetsFromJobs.framesSheetPath && (
                    <button
                      type="button"
                      onClick={() =>
                        setLightbox({
                          src: mediaUrl(latestAssetsFromJobs.framesSheetPath),
                          title: `第 ${activeEp.ep_number} 集 首尾帧编号总览图`,
                          downloadHref: mediaUrl(latestAssetsFromJobs.framesSheetPath, true),
                        })
                      }
                      className="inline-flex items-center gap-1 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                    >
                      <ImageIcon className="h-3.5 w-3.5" /> 查看首尾帧编号总览图
                    </button>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
                  {shots.map((s) => (
                    <div key={s.id} className="rounded-xl border border-slate-200 bg-slate-50/70 p-2 space-y-1.5">
                      <div className="flex items-center justify-between gap-1 text-[11px]">
                        <span className="font-mono font-bold text-sky-700">S{String(s.shot_no).padStart(2, '0')}</span>
                        <span className="truncate text-slate-700 font-medium">{s.shot_title}</span>
                      </div>
                      {s.start_frame_path ? (
                        <img
                          src={mediaUrl(s.start_frame_path)}
                          alt={s.shot_title}
                          onClick={() =>
                            setLightbox({
                              src: mediaUrl(s.start_frame_path),
                              title: `S${String(s.shot_no).padStart(2, '0')} 首帧 · ${s.shot_title}`,
                              downloadHref: mediaUrl(s.start_frame_path, true),
                            })
                          }
                          className="h-36 w-full rounded-lg object-cover bg-slate-900 cursor-zoom-in"
                        />
                      ) : (
                        <div className="h-36 rounded-lg border border-dashed border-slate-300 bg-white flex items-center justify-center text-[10px] text-slate-400">
                          待定首帧
                        </div>
                      )}
                      {s.end_frame_path && (
                        <button
                          type="button"
                          onClick={() =>
                            setLightbox({
                              src: mediaUrl(s.end_frame_path),
                              title: `S${String(s.shot_no).padStart(2, '0')} 尾帧 · ${s.shot_title}`,
                              downloadHref: mediaUrl(s.end_frame_path, true),
                            })
                          }
                          className="w-full rounded bg-violet-50 border border-violet-200 py-1 text-[10px] font-medium text-violet-700 hover:bg-violet-100 cursor-pointer"
                        >
                          查看本镜尾帧图
                        </button>
                      )}
                    </div>
                  ))}
                </div>

                {/* 首尾帧总评投票条 */}
                <div className="rounded-xl border border-amber-200/80 bg-amber-50/45 p-3.5 space-y-2.5">
                  <div className="text-xs font-semibold text-amber-900">首尾帧画风与角色一致性表态</div>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <input
                      value={voteNotes['frame'] || ''}
                      onChange={(e) => setVoteNotes((prev) => ({ ...prev, frame: e.target.value }))}
                      placeholder="写下首尾帧意见（如：S04 表情很到位，S07 建议光线再暖一点）"
                      className="studio-input flex-1 rounded-lg px-3 py-2 text-xs"
                    />
                    <div className="flex gap-2 shrink-0">
                      <button
                        type="button"
                        disabled={votingKey !== null}
                        onClick={() => submitVote('frame', 'episode', '首尾帧全部赞同', 'approve', 'frame')}
                        className="flex-1 sm:flex-initial inline-flex items-center justify-center gap-1 rounded-lg bg-emerald-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-emerald-700 cursor-pointer"
                      >
                        <ThumbsUp className="h-3.5 w-3.5" /> 赞同定稿
                      </button>
                      <button
                        type="button"
                        disabled={votingKey !== null}
                        onClick={() => submitVote('frame', 'episode', '首尾帧需局部修改', 'change', 'frame')}
                        className="flex-1 sm:flex-initial inline-flex items-center justify-center gap-1 rounded-lg bg-amber-500 px-3.5 py-2 text-xs font-semibold text-white hover:bg-amber-600 cursor-pointer"
                      >
                        <ThumbsDown className="h-3.5 w-3.5" /> 建议修改
                      </button>
                    </div>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <div className="text-xs font-semibold text-slate-700">团队首尾帧投票记录</div>
                  {renderVoteList('frame')}
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* ==================== 2. 剧本共创与修改留痕 ==================== */}
      {section === 'script' && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          <div className="xl:col-span-8 studio-card rounded-2xl p-4 sm:p-5 space-y-4">
            {!activeEp ? (
              <div className="py-12 text-center text-xs text-slate-400">暂无分集可编辑</div>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="text-sm sm:text-base font-semibold text-slate-900">
                      第 {activeEp.ep_number} 集剧本共创
                    </h2>
                    <p className="text-[11px] text-slate-500">
                      直接修改任意字段并保存，系统会自动记录修改人与改前/改后对比。
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setRevisionsOpen(true)}
                    className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 cursor-pointer"
                  >
                    <History className="h-3.5 w-3.5" /> 查看留痕 ({revisions.length})
                  </button>
                </div>

                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">本集标题</label>
                  <input
                    value={epDraft.title || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, title: e.target.value })}
                    className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                  />
                </div>

                <div>
                  <label className="mb-1 block text-xs font-medium text-amber-800">0–3 秒视觉奇观 / 开场钩子</label>
                  <input
                    value={epDraft.hook_0_3s || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, hook_0_3s: e.target.value })}
                    className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                  />
                </div>

                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-700">分段落拍摄剧本正文</label>
                  <textarea
                    value={epDraft.script_text || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, script_text: e.target.value })}
                    rows={12}
                    className="studio-input w-full rounded-xl p-3 text-xs leading-relaxed"
                  />
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">结尾评论区互动引导</label>
                    <input
                      value={epDraft.ending_cta || ''}
                      onChange={(e) => setEpDraft({ ...epDraft, ending_cta: e.target.value })}
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">下集悬念钩子</label>
                    <input
                      value={epDraft.next_ep_hook || ''}
                      onChange={(e) => setEpDraft({ ...epDraft, next_ep_hook: e.target.value })}
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                </div>

                <div className="pt-2 border-t border-slate-100 flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[11px] text-slate-500">
                    当前操作人：{user?.display_name || user?.username || '成员'} · 保存时自动对比旧文与新文留痕
                  </span>
                  <button
                    type="button"
                    disabled={savingEp}
                    onClick={handleSaveEpisode}
                    className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-sky-600 px-4 py-2 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-50 cursor-pointer shrink-0"
                  >
                    <Save className="h-3.5 w-3.5" />
                    {savingEp ? '保存中...' : '保存剧本并留痕'}
                  </button>
                </div>
              </>
            )}
          </div>

          {/* 右侧：跨集连续性台账与季大纲 */}
          <div className="xl:col-span-4 space-y-4">
            <div className="studio-card rounded-2xl p-4 space-y-2.5">
              <div className="text-sm font-semibold text-slate-900">跨集连续性台账</div>
              <div className="rounded-xl bg-slate-50 border border-slate-200 p-3 text-xs text-slate-700 whitespace-pre-wrap leading-relaxed">
                {proj.continuity_ledger?.notes || '暂无跨集台账备注'}
              </div>
            </div>

            <div className="studio-card rounded-2xl p-4 space-y-2.5">
              <div className="text-sm font-semibold text-slate-900">季大纲规划</div>
              {(proj.season_outline || []).map((o, idx) => (
                <div key={idx} className="rounded-xl bg-slate-50 border border-slate-200 p-3 text-xs space-y-1">
                  <div className="font-semibold text-sky-700">
                    第 {o.ep} 集：{o.title}
                  </div>
                  <div className="text-slate-600">钩子：{o.hook}</div>
                </div>
              ))}
              {(!proj.season_outline || proj.season_outline.length === 0) && (
                <div className="text-xs text-slate-400">暂无季大纲条目</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ==================== 3. 分镜共创与修改留痕 ==================== */}
      {section === 'shots' && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {shots.map((s) => {
              const isEditing = editingShotId === s.id && shotDraft !== null;
              return (
                <div
                  key={s.id}
                  className={`studio-card rounded-2xl p-4 space-y-3 ${
                    isEditing ? 'md:col-span-2 ring-2 ring-sky-200 border-sky-300' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="rounded bg-sky-50 border border-sky-200 px-2 py-0.5 text-xs font-bold text-sky-700 shrink-0">
                        S{String(s.shot_no).padStart(2, '0')}
                      </span>
                      <span className="text-sm font-semibold text-slate-900 truncate">{s.shot_title}</span>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <span className="text-[11px] text-slate-500">
                        {s.camera_type} · {s.duration_sec}s
                      </span>
                      <button
                        type="button"
                        onClick={() => openShotEditor(s)}
                        className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-medium cursor-pointer ${
                          isEditing
                            ? 'bg-sky-600 border-sky-600 text-white'
                            : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100'
                        }`}
                      >
                        <Pencil className="h-3 w-3" /> {isEditing ? '收起' : '共创修改'}
                      </button>
                    </div>
                  </div>

                  {s.start_frame_path && (
                    <div className={s.end_frame_path ? 'grid grid-cols-2 gap-2' : ''}>
                      <img
                        src={mediaUrl(s.start_frame_path)}
                        alt={s.shot_title}
                        onClick={() =>
                          setLightbox({
                            src: mediaUrl(s.start_frame_path),
                            title: `S${String(s.shot_no).padStart(2, '0')} 首帧`,
                            downloadHref: mediaUrl(s.start_frame_path, true),
                          })
                        }
                        className="h-36 w-full rounded-xl object-contain bg-slate-900 cursor-zoom-in"
                      />
                      {s.end_frame_path && (
                        <img
                          src={mediaUrl(s.end_frame_path)}
                          alt={`${s.shot_title} 尾帧`}
                          onClick={() =>
                            setLightbox({
                              src: mediaUrl(s.end_frame_path),
                              title: `S${String(s.shot_no).padStart(2, '0')} 尾帧`,
                              downloadHref: mediaUrl(s.end_frame_path, true),
                            })
                          }
                          className="h-36 w-full rounded-xl object-contain bg-slate-900 cursor-zoom-in"
                        />
                      )}
                    </div>
                  )}

                  {!isEditing ? (
                    <div className="space-y-2 text-xs">
                      <p className="text-slate-700 leading-relaxed">{s.cn_action_desc || '（暂无画面描述）'}</p>
                      {s.cn_dialogue && (
                        <div className="rounded-lg bg-sky-50 border border-sky-200/70 px-2.5 py-1.5 text-sky-900">
                          台词/旁白：{s.cn_dialogue}
                        </div>
                      )}
                      {s.subtitle_text && (
                        <div className="rounded-lg bg-amber-50 border border-amber-200/70 px-2.5 py-1.5 text-amber-900">
                          屏幕字幕：{s.subtitle_text}
                        </div>
                      )}
                      <div className="text-[11px] text-slate-500">
                        声音方案：{AUDIO_STRATEGY_CLOUD_LABELS[s.audio_strategy] || s.audio_strategy}
                      </div>
                    </div>
                  ) : (
                    <div className="rounded-xl border border-sky-200 bg-sky-50/40 p-3 space-y-2.5 text-xs">
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                        <div>
                          <label className="mb-1 block text-[11px] text-slate-600">镜头标题</label>
                          <input
                            value={shotDraft.shot_title}
                            onChange={(e) => setShotDraft({ ...shotDraft, shot_title: e.target.value })}
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11px] text-slate-600">景别 / 运镜</label>
                          <input
                            value={shotDraft.camera_type}
                            onChange={(e) => setShotDraft({ ...shotDraft, camera_type: e.target.value })}
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11px] text-slate-600">时长 (秒)</label>
                          <input
                            type="number"
                            min={4}
                            max={15}
                            value={shotDraft.duration_sec}
                            onChange={(e) => setShotDraft({ ...shotDraft, duration_sec: Number(e.target.value) })}
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                          />
                        </div>
                      </div>
                      <div>
                        <label className="mb-1 block text-[11px] text-slate-600">中文画面与动作描述</label>
                        <textarea
                          value={shotDraft.cn_action_desc}
                          onChange={(e) => setShotDraft({ ...shotDraft, cn_action_desc: e.target.value })}
                          rows={2}
                          className="studio-input w-full rounded-lg p-2 text-xs"
                        />
                      </div>
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <div>
                          <label className="mb-1 block text-[11px] text-slate-600">中文台词 / 旁白</label>
                          <input
                            value={shotDraft.cn_dialogue}
                            onChange={(e) => setShotDraft({ ...shotDraft, cn_dialogue: e.target.value })}
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11px] text-slate-600">屏幕字幕文案</label>
                          <input
                            value={shotDraft.subtitle_text}
                            onChange={(e) => setShotDraft({ ...shotDraft, subtitle_text: e.target.value })}
                            className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                          />
                        </div>
                      </div>
                      <div>
                        <label className="mb-1 block text-[11px] text-slate-600">声音方案</label>
                        <select
                          value={shotDraft.audio_strategy}
                          onChange={(e) => setShotDraft({ ...shotDraft, audio_strategy: e.target.value })}
                          className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                        >
                          {Object.entries(AUDIO_STRATEGY_CLOUD_LABELS).map(([k, lbl]) => (
                            <option key={k} value={k}>
                              {lbl}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="flex justify-end gap-2 pt-1">
                        <button
                          type="button"
                          onClick={() => {
                            setEditingShotId(null);
                            setShotDraft(null);
                          }}
                          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
                        >
                          取消
                        </button>
                        <button
                          type="button"
                          disabled={savingShot}
                          onClick={() => saveShotEdit(s)}
                          className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
                        >
                          <Save className="h-3.5 w-3.5" /> {savingShot ? '保存中...' : '保存并留痕'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {shots.length === 0 && (
              <div className="col-span-full studio-card rounded-2xl py-12 text-center text-xs text-slate-400">
                本集暂无分镜条目
              </div>
            )}
          </div>
        </div>
      )}

      {/* ==================== 4. 资产圣经与防穿帮护盾 ==================== */}
      {section === 'assets' && (
        <div className="space-y-4">
          <div className="studio-card rounded-2xl p-4 space-y-2">
            <div className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-900">
              <Sparkles className="h-4 w-4 text-amber-500" /> 跨集资产圣经、场景地标库与防穿帮护盾
            </div>
            <p className="text-xs text-slate-500">
              固定角色形象、实拍场景底图与永久「防穿帮护盾」（例如锁定角色关键外形细节与服饰轮廓），确保跨集、跨镜头视觉统一。
            </p>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {assets.map((a) => (
              <div key={a.id} className="studio-card rounded-2xl p-4 space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <span className="rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                      {ASSET_TYPE_LABELS[a.asset_type] || a.asset_type}
                    </span>
                    <h3 className="mt-1.5 text-sm font-semibold text-slate-900">{a.name}</h3>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2">
                  {[
                    { label: '参考底图', path: a.raw_image_path },
                    { label: '标准定妆图', path: a.clean_image_path },
                  ].map((box) => (
                    <div key={box.label} className="space-y-1">
                      <div className="text-[10px] text-slate-500">{box.label}</div>
                      {box.path ? (
                        <img
                          src={mediaUrl(box.path)}
                          alt={`${a.name}-${box.label}`}
                          onClick={() =>
                            setLightbox({
                              src: mediaUrl(box.path),
                              title: `${a.name} · ${box.label}`,
                              downloadHref: mediaUrl(box.path, true),
                            })
                          }
                          className="h-28 w-full rounded-xl object-cover bg-slate-900 cursor-zoom-in border border-slate-200"
                        />
                      ) : (
                        <div className="h-28 rounded-xl border border-dashed border-slate-300 bg-slate-50 flex items-center justify-center text-[11px] text-slate-400">
                          暂无图片
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                {a.negative_guard_en && (
                  <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-2.5 space-y-1">
                    <div className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-800">
                      <Shield className="h-3.5 w-3.5 text-emerald-600" /> 永久防穿帮护盾（自动生效）
                    </div>
                    <div className="text-[11px] font-mono text-emerald-950/90 leading-relaxed break-words">
                      {a.negative_guard_en}
                    </div>
                  </div>
                )}

                {a.notes && (
                  <div className="rounded-lg bg-slate-50 border border-slate-200 p-2.5 text-xs text-slate-700 leading-relaxed">
                    {a.notes}
                  </div>
                )}
              </div>
            ))}
            {assets.length === 0 && (
              <div className="col-span-full studio-card rounded-2xl py-12 text-center text-xs text-slate-400">
                暂无已同步资产
              </div>
            )}
          </div>
        </div>
      )}

      <RevisionsDrawer
        open={revisionsOpen}
        onClose={() => setRevisionsOpen(false)}
        title={activeEp ? `第 ${activeEp.ep_number} 集《${activeEp.title}》修改留痕` : '修改留痕'}
        revisions={revisions}
        onRefresh={() => activeEpId && loadEpisodeAll(activeEpId)}
      />

      <ImageLightbox
        src={lightbox?.src || null}
        title={lightbox?.title}
        downloadHref={lightbox?.downloadHref}
        onClose={() => setLightbox(null)}
      />
    </div>
  );
};
