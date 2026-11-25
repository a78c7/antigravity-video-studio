import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clapperboard,
  Cloud,
  CloudDownload,
  CloudUpload,
  Copy,
  Download,
  ExternalLink,
  FileCheck2,
  Film,
  FolderOpen,
  Image as ImageIcon,
  LayoutGrid,
  List,
  Loader2,
  Mic,
  Music,
  Package,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Save,
  Scissors,
  Shield,
  Sparkles,
  Star,
  Terminal,
  Trash2,
  Upload,
  Wand2,
} from 'lucide-react';
import { api, copyToClipboard, mediaUrl } from '../api';
import type { CloudSection } from '../components/CloudWorkspaceView';
import type {
  AppMode,
  Asset,
  AuthUser,
  BudgetSummary,
  CliImageCandidate,
  CoverCandidate,
  EditorialReport,
  Episode,
  InboxItem,
  InboxState,
  ProbeColab,
  Project,
  PublicSettings,
  RenderJob,
  Shot,
  Take,
  TimelineItem,
} from '../types';

interface Props {
  project: Project;
  appMode?: AppMode;
  user?: AuthUser | null;
  settings?: PublicSettings | null;
  initialEpisodeId?: number | null;
  initialCloudSection?: CloudSection;
  initialOpenRevisions?: boolean;
  inbox: InboxState;
  keepChineseCopy: boolean;
  onToggleKeepChineseCopy: (val: boolean) => Promise<void>;
  onRenameInboxItem: (id: string, newName: string) => Promise<void>;
  onTrashInboxItem: (id: string) => Promise<void>;
  claimSyncVersion?: number;
  onRefreshInbox: () => Promise<void>;
  onPullCloud?: () => Promise<void>;
  onBack: () => void;
  onProjectUpdated: () => Promise<void>;
  onActiveTargetChange: (info: { shotId: number | null; episodeId: number | null; label: string }) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const STEPS = [
  { id: 1, title: '① 季大纲与剧本' },
  { id: 2, title: '② 跨集资产圣经' },
  { id: 3, title: '③ 拆分镜 & 静帧预演' },
  { id: 4, title: '④ Flow 快传抽卡' },
  { id: 5, title: '⑤ 连播时间线预演' },
  { id: 6, title: '⑥ 合成与导出' },
];

const FLOW_MODE_OPTIONS = [
  { id: 'omni12_i2v', label: 'Omni Flash 1.2 (主力/≤10图)' },
  { id: 'veo31_lite', label: 'Veo 3.1 Lite (5分或0分/≤3图)' },
  { id: 'frames_to_video', label: '首尾双帧插值 (≤2图)' },
  { id: 'omni_extend', label: 'Omni Extend 续写 (+10s)' },
  { id: 'nb2_frame', label: '仅 Nano Banana 静帧' },
];

const AUDIO_STRATEGY_OPTIONS = [
  { id: 'keep_flow_sfx', label: '保留 Flow 原生环境音效（禁 BGM）' },
  { id: 'keep_flow_dialogue', label: '保留 Flow 原生中文台词 + 环境音' },
  { id: 'mute_for_local_vo', label: '仅取画面（后期剪映 / 克隆旁白配音）' },
];

const ASSET_TYPE_LABELS: Record<string, string> = {
  character: '角色',
  landmark: '场景/地标',
  prop: '道具',
  style: '画风',
};

interface ShotEditDraft {
  shot_title: string;
  camera_type: string;
  duration_sec: number;
  flow_mode: string;
  audio_strategy: string;
  ref_asset_ids: number[];
  cn_action_desc: string;
  cn_dialogue: string;
  subtitle_text: string;
  frame_prompt_en: string;
  end_frame_prompt_en: string;
  video_prompt_en: string;
}

const FLOW_IMAGE_MODELS = ['Nano Banana 2.1 Pro', 'Nano Banana 2.1'];

const formatAspectPhrase = (aspect: string): string => {
  const a = (aspect || '9:16').trim();
  if (['9:16', '3:4', '2:3', '4:5'].includes(a)) return `vertical ${a}`;
  if (['16:9', '4:3', '3:2', '21:9'].includes(a)) return `horizontal ${a}`;
  if (a === '1:1') return 'square 1:1';
  return a;
};

const hasEndFrameFeature = (s: Shot): boolean =>
  s.flow_mode === 'frames_to_video' ||
  Boolean((s.end_frame_path || '').trim()) ||
  Boolean((s.end_frame_prompt_en || '').trim());

export const LocalWorkspacePage: React.FC<Props> = ({
  project,
  settings,
  initialEpisodeId,
  inbox,
  keepChineseCopy,
  onToggleKeepChineseCopy,
  onRenameInboxItem,
  onTrashInboxItem,
  claimSyncVersion,
  onRefreshInbox,
  onPullCloud,
  onBack,
  onProjectUpdated,
  onActiveTargetChange,
  notify,
}) => {
  const collabSyncEnabled = Boolean(settings?.enable_cloud_collab && (settings?.cloud_studio_url || '').trim());
  const cliImageModel = settings?.cli_image_model || 'gemini-nano-banana-2.1';
  const cliVideoModel = settings?.cli_video_model || 'gemini-omni-1.2-flash';
  const cliMusicModel = settings?.cli_music_model || 'lyria-3.5';
  const priceImg1K = settings?.cli_pricing?.image?.['1K'] ?? 0.0336;
  const priceImg2K = settings?.cli_pricing?.image?.['2K'] ?? 0.0504;
  const priceVid360 = settings?.cli_pricing?.video_per_sec?.['360p'] ?? 0.023;
  const priceVid720 = settings?.cli_pricing?.video_per_sec?.['720p'] ?? 0.07;
  const priceVid1080 = settings?.cli_pricing?.video_per_sec?.['1080p'] ?? 0.104;
  const priceMusic = settings?.cli_pricing?.music ?? 0.08;

  const [step, setStep] = useState<number>(1);
  const [proj, setProj] = useState<Project>(project);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [activeEpId, setActiveEpId] = useState<number | null>(initialEpisodeId || null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [shots, setShots] = useState<Shot[]>([]);
  const [budget, setBudget] = useState<BudgetSummary | null>(null);
  const [activeShotId, setActiveShotId] = useState<number | null>(null);
  const [shotView, setShotView] = useState<'table' | 'board'>('board');

  // Step 1 剧本与大纲编辑状态
  const [epDraft, setEpDraft] = useState<Partial<Episode>>({});
  const [scriptNotes, setScriptNotes] = useState('');
  const [editorialMode, setEditorialMode] = useState(true);
  const [editorialReport, setEditorialReport] = useState<EditorialReport | null>(null);
  const [globalStyleDraft, setGlobalStyleDraft] = useState(project.global_style_prompt || '');
  const [ledgerNotesDraft, setLedgerNotesDraft] = useState(project.continuity_ledger?.notes || '');
  const [busyAI, setBusyAI] = useState(false);

  // Step 2 资产新增与收件箱快捷选择
  const [newAssetType, setNewAssetType] = useState<'character' | 'landmark' | 'prop' | 'style'>('character');
  const [newAssetName, setNewAssetName] = useState('');
  const [newAssetTag, setNewAssetTag] = useState('');
  const [newAssetEn, setNewAssetEn] = useState('');
  const [assetInboxPick, setAssetInboxPick] = useState<Record<number, string>>({});

  // Step 3 & Step 4 单镜内联折叠编辑状态
  const [editingShotId, setEditingShotId] = useState<number | null>(null);
  const [shotDraft, setShotDraft] = useState<ShotEditDraft | null>(null);
  const [savingShotEdit, setSavingShotEdit] = useState(false);

  // Google Flow 智能体显式模型与抽卡张数/条数偏好
  const [flowImageModel, setFlowImageModel] = useState<string>(
    () => localStorage.getItem('agvs_flow_img_model') || 'Nano Banana 2.1 Pro'
  );
  const [flowImageCount, setFlowImageCount] = useState<number>(
    () => Number(localStorage.getItem('agvs_flow_img_count')) || 4
  );
  const [flowVideoCount, setFlowVideoCount] = useState<number>(
    () => Number(localStorage.getItem('agvs_flow_vid_count')) || 2
  );
  const flowParamsRef = useRef({ imageModel: flowImageModel, imageCount: flowImageCount, videoCount: flowVideoCount });

  // 官方 gemini-api CLI 一键直出参数（走 API 月度预算，不消耗 Flow 积分）
  const [cliBusyKey, setCliBusyKey] = useState<string | null>(null);
  const [cliImageSize, setCliImageSize] = useState<'1K' | '2K'>(
    () => (localStorage.getItem('agvs_cli_img_size') === '1K' ? '1K' : '2K')
  );
  const [cliImageCount, setCliImageCount] = useState<1 | 2 | 4>(() => {
    const v = Number(localStorage.getItem('agvs_cli_img_count'));
    return v === 1 || v === 4 ? v : 2;
  });
  const [cliVideoRes, setCliVideoRes] = useState<'360p' | '720p' | '1080p'>(() => {
    const v = localStorage.getItem('agvs_cli_vid_res');
    return v === '720p' || v === '1080p' ? v : '360p';
  });
  const [cliVideoCount, setCliVideoCount] = useState<1 | 2>(
    () => (Number(localStorage.getItem('agvs_cli_vid_count')) === 2 ? 2 : 1)
  );
  const [assetCandidates, setAssetCandidates] = useState<Record<number, CliImageCandidate[]>>({});
  const [shotFrameCandidates, setShotFrameCandidates] = useState<Record<string, CliImageCandidate[]>>({});

  // Step 4 内嵌收件箱原地改中文名状态
  const [renamingInboxId, setRenamingInboxId] = useState<string | null>(null);
  const [renamingInboxVal, setRenamingInboxVal] = useState('');

  // 外部写库同步刷新状态
  const [refreshing, setRefreshing] = useState(false);
  const activeEpIdRef = useRef<number | null>(activeEpId);
  const stepRef = useRef<number>(step);
  const lastSilentSyncRef = useRef<number>(0);

  // Step 5 & 6 时间线与导出
  const [timelineItems, setTimelineItems] = useState<TimelineItem[]>([]);
  const [missingShots, setMissingShots] = useState<number[]>([]);
  const [unupscaledShots, setUnupscaledShots] = useState<number[]>([]);
  const [renderJobs, setRenderJobs] = useState<RenderJob[]>([]);
  const [playingIdx, setPlayingIdx] = useState<number>(0);
  const [isPlayingAll, setIsPlayingAll] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const bgmRef = useRef<HTMLAudioElement | null>(null);
  const [probeColab, setProbeColab] = useState<ProbeColab | null>(null);
  const [colabGpuTier, setColabGpuTier] = useState<string>(settings?.colab_default_gpu || 'L4');
  const [colabTaskMode, setColabTaskMode] = useState<'upscale_4k_30fps' | 'upscale_4k_60fps' | 'ai_60fps_1080p'>('upscale_4k_30fps');
  const [colabHdrHlg, setColabHdrHlg] = useState(true);
  const [colabBurnSubtitles, setColabBurnSubtitles] = useState(true);
  const [localHdrHlg, setLocalHdrHlg] = useState(true);
  const [localBurnSubtitles, setLocalBurnSubtitles] = useState(true);
  const [rendering, setRendering] = useState(false);

  // 可选远程协作同步 + 双比例封面 + 手动 Flow 发车包状态
  const [syncBusy, setSyncBusy] = useState<'push' | 'pull' | null>(null);
  const [pushStage, setPushStage] = useState<'master_ready' | 'frames_ready' | 'video_360p_ready' | 'covers_ready' | 'update'>('master_ready');
  const [coverCandidates, setCoverCandidates] = useState<CoverCandidate[]>([]);
  const [coverSheetPath, setCoverSheetPath] = useState('');
  const [coverBusy, setCoverBusy] = useState(false);
  const [flowPackBusy, setFlowPackBusy] = useState(false);

  const activeEp = episodes.find((e) => e.id === activeEpId) || null;
  const activeShot = shots.find((s) => s.id === activeShotId) || shots[0] || null;
  const inboxImages = inbox.items.filter((it) => it.media_type === 'image');
  const timelineTotalSec = timelineItems.reduce((acc, it) => acc + it.effective_duration, 0);

  useEffect(() => {
    if (settings?.colab_default_gpu) {
      setColabGpuTier(settings.colab_default_gpu);
    }
  }, [settings?.colab_default_gpu]);

  useEffect(() => {
    activeEpIdRef.current = activeEpId;
  }, [activeEpId]);

  useEffect(() => {
    stepRef.current = step;
  }, [step]);

  const flowCompileQuery = () => {
    const p = flowParamsRef.current;
    return `?image_count=${p.imageCount}&image_model=${encodeURIComponent(p.imageModel)}&video_count=${p.videoCount}`;
  };

  useEffect(() => {
    flowParamsRef.current = { imageModel: flowImageModel, imageCount: flowImageCount, videoCount: flowVideoCount };
    localStorage.setItem('agvs_flow_img_model', flowImageModel);
    localStorage.setItem('agvs_flow_img_count', String(flowImageCount));
    localStorage.setItem('agvs_flow_vid_count', String(flowVideoCount));
    if (activeEpIdRef.current) {
      loadEpisodeShots(activeEpIdRef.current, { silent: true }).catch(() => {});
    }
  }, [flowImageModel, flowImageCount, flowVideoCount]);

  useEffect(() => {
    localStorage.setItem('agvs_cli_img_size', cliImageSize);
    localStorage.setItem('agvs_cli_img_count', String(cliImageCount));
    localStorage.setItem('agvs_cli_vid_res', cliVideoRes);
    localStorage.setItem('agvs_cli_vid_count', String(cliVideoCount));
  }, [cliImageSize, cliImageCount, cliVideoRes, cliVideoCount]);

  const triggerQuotaRefresh = () => {
    window.dispatchEvent(new Event('agvs-quota-refresh'));
  };

  const estimateCliVideoCost = (durationSec: number) => {
    const rate = cliVideoRes === '1080p' ? priceVid1080 : cliVideoRes === '720p' ? priceVid720 : priceVid360;
    return (Math.max(3, durationSec || 6) * rate * cliVideoCount).toFixed(2);
  };

  const handleCliGenerateBgm = async () => {
    if (!activeEpId) return;
    const promptText = (epDraft.bgm_prompt || '').trim();
    if (!promptText) {
      notify('请先填写或 AI 生成本集的英文配乐提示词', 'err');
      return;
    }
    setCliBusyKey('bgm');
    try {
      const res = await api.post<{ episode: Episode; est_cost_usd: number; filename: string }>(
        `/api/episodes/${activeEpId}/cli-generate-bgm`,
        { prompt: promptText }
      );
      setEpDraft(res.episode);
      await loadEpisodeShots(activeEpId);
      triggerQuotaRefresh();
      notify(`CLI 已生成原创配乐 ${res.filename}（${cliMusicModel} · 约 $${res.est_cost_usd.toFixed(2)}）`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const handleCliGenerateAssetClean = async (asset: Asset, mode: 'portrait' | 'full' = 'portrait') => {
    setCliBusyKey(`asset-${asset.id}`);
    try {
      const res = await api.post<{
        asset: Asset;
        auto_bound: boolean;
        dock_copy_name?: string;
        candidates: CliImageCandidate[];
        est_cost_usd: number;
      }>(`/api/assets/${asset.id}/cli-generate-clean`, {
        image_size: cliImageSize,
        count: cliImageCount,
        mode,
      });
      triggerQuotaRefresh();
      if (res.auto_bound) {
        setAssetCandidates((prev) => {
          const next = { ...prev };
          delete next[asset.id];
          return next;
        });
        await loadProjectCore({ silent: true });
        if (activeEpId) await loadEpisodeShots(activeEpId, { silent: true });
        notify(
          `CLI 棚拍定妆图已直接绑定「${asset.name}」（约 $${res.est_cost_usd.toFixed(3)}）${
            res.dock_copy_name ? `，并同步到直拖目录：${res.dock_copy_name}` : ''
          }`
        );
      } else {
        setAssetCandidates((prev) => ({ ...prev, [asset.id]: res.candidates }));
        notify(`CLI 已生成 ${res.candidates.length} 张「${asset.name}」棚拍候选图（约 $${res.est_cost_usd.toFixed(3)}），请在卡片内点选定妆`);
      }
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const handleSelectAssetCandidate = async (asset: Asset, cand: CliImageCandidate) => {
    setCliBusyKey(`asset-pick-${asset.id}`);
    try {
      const res = await api.post<{ asset: Asset; dock_copy_name?: string }>(
        `/api/assets/${asset.id}/select-clean-candidate`,
        { candidate_path: cand.path }
      );
      setAssetCandidates((prev) => {
        const next = { ...prev };
        delete next[asset.id];
        return next;
      });
      await loadProjectCore({ silent: true });
      if (activeEpId) await loadEpisodeShots(activeEpId, { silent: true });
      notify(
        `已选定「${asset.name}」定妆图${res.dock_copy_name ? `，并同步 6 字短名到直拖目录：${res.dock_copy_name}` : ''}`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const handleCliGenerateShotFrame = async (shot: Shot, slot: 'start_frame' | 'end_frame') => {
    const key = `frame-${shot.id}-${slot}`;
    setCliBusyKey(key);
    try {
      const res = await api.post<{
        slot: string;
        auto_bound: boolean;
        dock_copy_name?: string;
        candidates: CliImageCandidate[];
        est_cost_usd: number;
        episode_data?: { shots: Shot[]; budget: BudgetSummary };
      }>(`/api/shots/${shot.id}/cli-generate-frame${flowCompileQuery()}`, {
        slot,
        image_size: cliImageSize,
        count: cliImageCount,
      });
      triggerQuotaRefresh();
      const candKey = `${shot.id}:${slot}`;
      const slotCn = slot === 'end_frame' ? '尾帧' : '首帧';
      if (res.auto_bound && res.episode_data) {
        setShots(res.episode_data.shots);
        setBudget(res.episode_data.budget);
        setShotFrameCandidates((prev) => {
          const next = { ...prev };
          delete next[candKey];
          return next;
        });
        notify(
          `CLI 已生成并绑定 S${String(shot.shot_no).padStart(2, '0')} ${slotCn}（约 $${res.est_cost_usd.toFixed(3)}）${
            res.dock_copy_name ? `，已同步直拖目录：${res.dock_copy_name}` : ''
          }`
        );
      } else {
        setShotFrameCandidates((prev) => ({ ...prev, [candKey]: res.candidates }));
        notify(
          `CLI 已出 ${res.candidates.length} 张 S${String(shot.shot_no).padStart(2, '0')} ${slotCn}候选（约 $${res.est_cost_usd.toFixed(3)}），请点击下方缩略图选定`
        );
      }
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const handleSelectShotFrameCandidate = async (shot: Shot, slot: 'start_frame' | 'end_frame', cand: CliImageCandidate) => {
    setCliBusyKey(`pick-${shot.id}-${slot}`);
    try {
      const res = await api.post<{
        dock_copy_name?: string;
        episode_data: { shots: Shot[]; budget: BudgetSummary };
      }>(`/api/shots/${shot.id}/select-frame-candidate${flowCompileQuery()}`, {
        slot,
        candidate_path: cand.path,
      });
      setShots(res.episode_data.shots);
      setBudget(res.episode_data.budget);
      setShotFrameCandidates((prev) => {
        const next = { ...prev };
        delete next[`${shot.id}:${slot}`];
        return next;
      });
      const slotCn = slot === 'end_frame' ? '尾帧' : '首帧';
      notify(
        `已绑定 S${String(shot.shot_no).padStart(2, '0')} ${slotCn}${res.dock_copy_name ? `，已同步直拖目录：${res.dock_copy_name}` : ''}`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const handleCliGenerateShotVideo = async (shot: Shot) => {
    setCliBusyKey(`video-${shot.id}`);
    try {
      const res = await api.post<{
        created_count: number;
        est_cost_usd: number;
        resolution: string;
        episode_data: { shots: Shot[]; budget: BudgetSummary };
      }>(`/api/shots/${shot.id}/cli-generate-video${flowCompileQuery()}`, {
        resolution: cliVideoRes,
        count: cliVideoCount,
        duration_sec: shot.duration_sec,
      });
      setShots(res.episode_data.shots);
      setBudget(res.episode_data.budget);
      triggerQuotaRefresh();
      notify(
        `CLI 已直出 ${res.created_count} 条 S${String(shot.shot_no).padStart(2, '0')} 视频（${cliVideoModel} · ${res.resolution} · 约 $${res.est_cost_usd.toFixed(2)}）并自动入库！`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setCliBusyKey(null);
    }
  };

  const renderCliMediaControlBar = (compact = false) => (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-indigo-200 bg-indigo-50/55 px-3 py-2 text-[11px] text-slate-700">
      <span className="inline-flex items-center gap-1 font-semibold text-indigo-900">
        <Terminal className="h-3.5 w-3.5 text-indigo-600" />
        官方 CLI 直出（走 API 月度预算 · 0 Flow 积分）：
      </span>
      <div className="inline-flex items-center gap-1">
        <span className="text-slate-500">生图画质</span>
        {(['1K', '2K'] as const).map((sz) => (
          <button
            key={sz}
            type="button"
            onClick={() => setCliImageSize(sz)}
            className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
              cliImageSize === sz
                ? 'bg-indigo-600 border-indigo-600 text-white'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
            }`}
          >
            {sz} (${(sz === '1K' ? priceImg1K : priceImg2K).toFixed(3)})
          </button>
        ))}
      </div>
      <div className="inline-flex items-center gap-1">
        <span className="text-slate-500">生图张数</span>
        {([1, 2, 4] as const).map((cnt) => (
          <button
            key={cnt}
            type="button"
            onClick={() => setCliImageCount(cnt)}
            className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
              cliImageCount === cnt
                ? 'bg-indigo-600 border-indigo-600 text-white'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
            }`}
          >
            {cnt}张
          </button>
        ))}
      </div>
      {!compact && (
        <>
          <div className="inline-flex items-center gap-1">
            <span className="text-slate-500">CLI 视频</span>
            {([
              { id: '360p', label: `360p试卡($${priceVid360}/s)` },
              { id: '720p', label: `720p($${priceVid720}/s)` },
              { id: '1080p', label: `1080p($${priceVid1080}/s)` },
            ] as const).map((r) => (
              <button
                key={r.id}
                type="button"
                onClick={() => setCliVideoRes(r.id)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
                  cliVideoRes === r.id
                    ? 'bg-emerald-600 border-emerald-600 text-white'
                    : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <div className="inline-flex items-center gap-1">
            <span className="text-slate-500">条数</span>
            {([1, 2] as const).map((cnt) => (
              <button
                key={cnt}
                type="button"
                onClick={() => setCliVideoCount(cnt)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
                  cliVideoCount === cnt
                    ? 'bg-emerald-600 border-emerald-600 text-white'
                    : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
                }`}
              >
                {cnt}条
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );

  const renderShotFrameCandidatePicker = (shot: Shot, slot: 'start_frame' | 'end_frame') => {
    const cands = shotFrameCandidates[`${shot.id}:${slot}`];
    if (!cands || cands.length === 0) return null;
    const slotCn = slot === 'end_frame' ? '尾帧' : '首帧';
    return (
      <div className="rounded-xl border border-indigo-200 bg-indigo-50/45 p-2.5 space-y-2">
        <div className="flex items-center justify-between text-[11px]">
          <span className="font-semibold text-indigo-900">
            CLI 候选{slotCn}（{cands.length} 张 · 点选定稿并自动同步直拖目录）
          </span>
          <button
            type="button"
            onClick={() =>
              setShotFrameCandidates((prev) => {
                const next = { ...prev };
                delete next[`${shot.id}:${slot}`];
                return next;
              })
            }
            className="text-slate-500 hover:text-slate-800 cursor-pointer"
          >
            收起
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {cands.map((c) => (
            <div key={c.path} className="rounded-lg border border-indigo-200/80 bg-white p-1.5 space-y-1.5">
              <img src={mediaUrl(c.path)} alt={c.filename} className="h-32 w-full rounded object-contain bg-slate-900" />
              <button
                type="button"
                disabled={cliBusyKey !== null}
                onClick={() => handleSelectShotFrameCandidate(shot, slot, c)}
                className="w-full rounded bg-indigo-600 py-1 text-[10px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-45 cursor-pointer"
              >
                选为{slotCn} #{c.index}
              </button>
            </div>
          ))}
        </div>
      </div>
    );
  };

  const isUserTypingInField = () => {
    const tag = document.activeElement?.tagName || '';
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  };

  const loadProjectCore = async (opts?: { silent?: boolean }) => {
    const [pRes, eRes, aRes] = await Promise.all([
      api.get<{ project: Project }>(`/api/projects/${project.id}`),
      api.get<{ episodes: Episode[] }>(`/api/projects/${project.id}/episodes`),
      api.get<{ assets: Asset[] }>(`/api/projects/${project.id}/assets`),
    ]);
    setProj(pRes.project);
    if (!opts?.silent || !isUserTypingInField()) {
      setGlobalStyleDraft(pRes.project.global_style_prompt || '');
      setLedgerNotesDraft(pRes.project.continuity_ledger?.notes || '');
    }
    setEpisodes(eRes.episodes);
    setAssets(aRes.assets);

    let chosenEpId = activeEpIdRef.current;
    if (chosenEpId && !eRes.episodes.some((x) => x.id === chosenEpId)) {
      chosenEpId = null;
    }
    if (initialEpisodeId && eRes.episodes.some((x) => x.id === initialEpisodeId) && !chosenEpId) {
      chosenEpId = initialEpisodeId;
    }
    if (!chosenEpId && eRes.episodes.length > 0) {
      chosenEpId = eRes.episodes[0].id;
    }
    if (chosenEpId !== activeEpIdRef.current) {
      activeEpIdRef.current = chosenEpId;
      setActiveEpId(chosenEpId);
    }
    return chosenEpId;
  };

  const loadEpisodeShots = async (epId: number, opts?: { silent?: boolean }) => {
    const res = await api.get<{ episode: Episode; shots: Shot[]; budget: BudgetSummary }>(
      `/api/episodes/${epId}/shots${flowCompileQuery()}`
    );
    setShots(res.shots);
    setBudget(res.budget);
    if (!opts?.silent || !isUserTypingInField()) {
      setEpDraft(res.episode);
    }
    try {
      const parsedCovers = res.episode.cover_candidates_json ? JSON.parse(res.episode.cover_candidates_json) : [];
      setCoverCandidates(Array.isArray(parsedCovers) ? parsedCovers : []);
    } catch {
      setCoverCandidates([]);
    }
    setEpisodes((prev) => prev.map((e) => (e.id === epId ? { ...e, ...res.episode } : e)));
    setActiveShotId((cur) => {
      if (res.shots.length === 0) return null;
      if (cur && res.shots.some((s) => s.id === cur)) return cur;
      return res.shots[0].id;
    });
  };

  const loadTimeline = async (epId: number) => {
    const res = await api.get<{
      items: TimelineItem[];
      missing_shots: number[];
      unupscaled_shots: number[];
      jobs: RenderJob[];
    }>(`/api/episodes/${epId}/timeline`);
    setTimelineItems(res.items);
    setMissingShots(res.missing_shots);
    setUnupscaledShots(res.unupscaled_shots);
    setRenderJobs(res.jobs);
    setPlayingIdx((cur) => (cur < res.items.length ? cur : 0));
  };

  const refreshAllData = async (silent = false) => {
    const now = Date.now();
    if (silent && now - lastSilentSyncRef.current < 1500) return;
    lastSilentSyncRef.current = now;
    if (!silent) setRefreshing(true);
    try {
      const epId = await loadProjectCore({ silent });
      const targetEpId = epId ?? activeEpIdRef.current;
      if (targetEpId) {
        await loadEpisodeShots(targetEpId, { silent });
        if (stepRef.current === 5 || stepRef.current === 6) {
          await loadTimeline(targetEpId);
        }
      }
      if (!silent) notify('已同步最新剧目、资产与分镜数据');
    } catch (err: any) {
      if (!silent) notify(err.message, 'err');
    } finally {
      if (!silent) setRefreshing(false);
    }
  };

  useEffect(() => {
    loadProjectCore();
  }, [project.id]);

  // 窗口切回前台 / 重新聚焦时静默同步外部写入，保持当前步骤与选中镜头不变
  useEffect(() => {
    const handleFocus = () => {
      refreshAllData(true);
    };
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') {
        refreshAllData(true);
      }
    };
    window.addEventListener('focus', handleFocus);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      window.removeEventListener('focus', handleFocus);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [project.id]);

  // 全局右侧 Flow 收件箱抽屉认领成功后立即静默刷新当前集分镜
  useEffect(() => {
    if (claimSyncVersion && claimSyncVersion > 0) {
      refreshAllData(true);
    }
  }, [claimSyncVersion]);

  useEffect(() => {
    if (activeEpId) {
      loadEpisodeShots(activeEpId);
      if (step === 5 || step === 6) loadTimeline(activeEpId);
    } else {
      setShots([]);
      setBudget(null);
      setEpDraft({});
    }
  }, [activeEpId]);

  useEffect(() => {
    if (activeEpId && (step === 5 || step === 6)) {
      loadTimeline(activeEpId);
    }
  }, [step]);

  useEffect(() => {
    const label = activeEp
      ? activeShot
        ? `E${String(activeEp.ep_number).padStart(2, '0')} · S${String(activeShot.shot_no).padStart(2, '0')} ${activeShot.shot_title}`
        : `E${String(activeEp.ep_number).padStart(2, '0')} 《${activeEp.title}》`
      : proj.name;
    onActiveTargetChange({ shotId: activeShot?.id || null, episodeId: activeEp?.id || null, label });
  }, [activeEp?.id, activeShot?.id, activeShot?.shot_title]);

  const reportInboxContext = (label: string) => {
    if (!label.trim()) return;
    api.post('/api/inbox/context', { label: label.trim() }).catch(() => {});
  };

  useEffect(() => {
    if (activeEp && activeShot) {
      const slotCn = activeShot.start_frame_path ? '视频' : '首帧';
      const shortTitle = (activeShot.shot_title || '').replace(/[，。！？、；：“”‘’（）()【】\[\]\s]+/g, '').slice(0, 12);
      const epNo = String(activeEp.ep_number).padStart(2, '0');
      const shotNo = String(activeShot.shot_no).padStart(2, '0');
      reportInboxContext(shortTitle ? `E${epNo}_S${shotNo}_${slotCn}_${shortTitle}` : `E${epNo}_S${shotNo}_${slotCn}`);
    }
  }, [activeEp?.id, activeShot?.id, activeShot?.start_frame_path]);

  const handleSyncDockTray = async () => {
    if (!activeEpId) return;
    try {
      const res = await api.post<{ tray_dir: string; synced_count: number; files: string[] }>(
        `/api/episodes/${activeEpId}/sync-dock-tray`,
        { clear_existing: true }
      );
      notify(`已将本集 ${res.synced_count} 张中文短名图同步到直拖素材目录！`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handlePushEpisodeToCloud = async (stageOverride?: typeof pushStage) => {
    if (!activeEpId) return;
    const stage = stageOverride || pushStage;
    setSyncBusy('push');
    try {
      const res = await api.post<{
        files_uploaded?: number;
        files_skipped?: number;
        uploaded_mb?: number;
        elapsed_sec?: number;
        summary?: string;
        message?: string;
      }>(`/api/local-sync/push-episode/${activeEpId}`, { notify_stage: stage, include_final_master: true });
      const detail =
        res.summary ||
        res.message ||
        `已同步推送至远程协作端（新传 ${res.files_uploaded ?? 0} 个 / 跳过 ${res.files_skipped ?? 0} 个 · ${res.uploaded_mb ?? 0} MB · ${res.elapsed_sec ?? 0}s）`;
      notify(detail);
    } catch (err: any) {
      notify(err?.message || '推送至协作端失败', 'err');
    } finally {
      setSyncBusy(null);
    }
  };

  const handlePullCloudChanges = async () => {
    setSyncBusy('pull');
    try {
      if (onPullCloud) {
        await onPullCloud();
      } else {
        const res = await api.post<{ readable_summary?: string; summary?: string; message?: string }>(
          '/api/local-sync/pull',
          {}
        );
        notify(res.readable_summary || res.summary || res.message || '已拉取协作端最新团队成员修改与投票记录');
      }
      await refreshAllData(true);
    } catch (err: any) {
      notify(err?.message || '拉取远程协作更新失败', 'err');
    } finally {
      setSyncBusy(null);
    }
  };

  const handleGenerateCovers = async () => {
    if (!activeEpId) return;
    setCoverBusy(true);
    try {
      const res = await api.post<{
        sheet_path?: string;
        candidates: CoverCandidate[];
        total_cost_usd?: number;
      }>(`/api/episodes/${activeEpId}/cli-generate-covers`, {
        count_per_ratio: 2,
        image_size: '2K',
      });
      setCoverCandidates(res.candidates || []);
      if (res.sheet_path) setCoverSheetPath(res.sheet_path);
      triggerQuotaRefresh();
      await loadEpisodeShots(activeEpId, { silent: true });
      notify(
        `已生成 ${res.candidates?.length || 4} 张双比例封面（9:16 + 3:4 · 约 $${(res.total_cost_usd || 0).toFixed(2)}），可点选锁定${
          collabSyncEnabled ? '或推送至协作端供团队成员投票' : ''
        }`
      );
    } catch (err: any) {
      notify(err?.message || '生成双比例封面失败', 'err');
    } finally {
      setCoverBusy(false);
    }
  };

  const handleSelectCover = async (candidateIndex: number) => {
    if (!activeEpId) return;
    setCoverBusy(true);
    try {
      await api.post(`/api/episodes/${activeEpId}/select-cover`, { candidate_index: candidateIndex });
      await loadEpisodeShots(activeEpId, { silent: true });
      notify(`已锁定 #${candidateIndex} 为本集定稿封面`);
    } catch (err: any) {
      notify(err?.message || '锁定封面失败', 'err');
    } finally {
      setCoverBusy(false);
    }
  };

  const handlePrepareManualFlowPack = async () => {
    if (!activeEpId) return;
    setFlowPackBusy(true);
    try {
      const res = await api.post<{ pack_dir: string; checklist_path: string; copied_count?: number }>(
        `/api/episodes/${activeEpId}/prepare-manual-flow-pack`,
        {
          clear_existing: true,
          include_assets: true,
          image_count: flowImageCount,
          image_model: flowImageModel,
          video_count: flowVideoCount,
        }
      );
      notify('已生成手动 Flow 发车包（编号首尾帧 + 逐镜Flow提示词清单.md），正在打开文件夹！');
      if (res.checklist_path) await revealFileLocation(res.checklist_path);
    } catch (err: any) {
      notify(err?.message || '生成手动 Flow 发车包失败', 'err');
    } finally {
      setFlowPackBusy(false);
    }
  };

  const handleCreateEpisode = async () => {
    const title = prompt('输入新的一集标题（可直接回车默认）', `第 ${episodes.length + 1} 集`);
    if (title === null) return;
    try {
      const res = await api.post<{ episode: Episode }>(`/api/projects/${proj.id}/episodes`, {
        title: title.trim() || `第 ${episodes.length + 1} 集`,
      });
      await loadProjectCore();
      setActiveEpId(res.episode.id);
      notify(`已新建第 ${res.episode.ep_number} 集`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleSaveProjectMeta = async () => {
    try {
      await api.patch(`/api/projects/${proj.id}`, {
        global_style_prompt: globalStyleDraft,
        continuity_ledger: { ...(proj.continuity_ledger || {}), notes: ledgerNotesDraft },
      });
      await loadProjectCore();
      await onProjectUpdated();
      notify('全局影调锁词与跨集台账已保存');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleSaveEpisode = async () => {
    if (!activeEpId) return;
    try {
      await api.patch(`/api/episodes/${activeEpId}`, {
        title: epDraft.title,
        hook_0_3s: epDraft.hook_0_3s,
        script_text: epDraft.script_text,
        ending_cta: epDraft.ending_cta,
        next_ep_hook: epDraft.next_ep_hook,
        bgm_prompt: epDraft.bgm_prompt,
        continuity_summary: epDraft.continuity_summary,
      });
      await loadEpisodeShots(activeEpId);
      notify('本集剧本已保存');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleAiGenerateScript = async () => {
    if (!activeEpId) return;
    setBusyAI(true);
    try {
      const res = await api.post<{
        used_model: string;
        editorial_mode?: boolean;
        editorial_report?: EditorialReport | null;
        episode: Episode;
      }>(`/api/episodes/${activeEpId}/generate-script`, {
        topic_title: epDraft.title || '',
        topic_pitch: epDraft.hook_0_3s || '',
        extra_notes: scriptNotes,
        editorial_mode: editorialMode,
      });
      setEpDraft(res.episode);
      setEditorialReport(res.editorial_report || null);
      await loadEpisodeShots(activeEpId);
      notify(
        editorialMode
          ? `已用 ${res.used_model} 完成「编剧初稿 → 毒舌主编挑刺 → 返工终稿」双轮打磨！`
          : `已用 ${res.used_model} 快速生成 45-85 秒剧本`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setBusyAI(false);
    }
  };

  const handleAiCritiqueScript = async () => {
    if (!activeEpId) return;
    setBusyAI(true);
    try {
      await api.patch(`/api/episodes/${activeEpId}`, {
        title: epDraft.title,
        hook_0_3s: epDraft.hook_0_3s,
        script_text: epDraft.script_text,
        ending_cta: epDraft.ending_cta,
        next_ep_hook: epDraft.next_ep_hook,
      });
      const res = await api.post<{
        used_model: string;
        editorial_report: EditorialReport;
        episode: Episode;
      }>(`/api/episodes/${activeEpId}/critique-script`, {
        extra_notes: scriptNotes,
      });
      setEpDraft(res.episode);
      setEditorialReport(res.editorial_report);
      await loadEpisodeShots(activeEpId);
      notify(`毒舌主编已按 ${res.used_model} 完成挑刺与返工升级！`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setBusyAI(false);
    }
  };

  const handleAiBreakShots = async () => {
    if (!activeEpId) return;
    if (shots.length > 0 && !confirm('重新拆分镜会替换当前未锁定的旧分镜列表，确认继续？')) return;
    setBusyAI(true);
    try {
      const res = await api.post<{ used_model: string; shots: Shot[]; budget: BudgetSummary; added_assets: Asset[] }>(
        `/api/episodes/${activeEpId}/break-shots`,
        { replace_existing: true }
      );
      setShots(res.shots);
      setBudget(res.budget);
      if (res.shots[0]) setActiveShotId(res.shots[0].id);
      await loadProjectCore();
      notify(`拆分完成 (${res.used_model})：共 ${res.shots.length} 镜，自动补齐 ${res.added_assets.length} 个新资产`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setBusyAI(false);
    }
  };

  const handleCreateAsset = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAssetName.trim()) return;
    try {
      await api.post(`/api/projects/${proj.id}/assets`, {
        asset_type: newAssetType,
        name: newAssetName.trim(),
        flow_tag: newAssetTag.trim() || `@${newAssetName.trim()}`,
        en_anchor_prompt: newAssetEn.trim(),
      });
      setNewAssetName('');
      setNewAssetTag('');
      setNewAssetEn('');
      await loadProjectCore();
      notify('资产已加入跨集资产库');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleUploadAssetFile = async (assetId: number, slot: 'raw' | 'clean' | 'voice_sample', file: File) => {
    const fd = new FormData();
    fd.append('slot', slot);
    fd.append('file', file);
    try {
      await api.upload(`/api/assets/${assetId}/upload`, fd);
      await loadProjectCore();
      if (activeEpId) await loadEpisodeShots(activeEpId);
      notify(slot === 'voice_sample' ? '声音克隆干声样本已登记' : '参考图已上传并绑定');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleUploadBgm = async (file: File) => {
    if (!activeEpId) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      await api.upload(`/api/episodes/${activeEpId}/upload-bgm`, fd);
      await loadEpisodeShots(activeEpId);
      notify('本集原创配乐已绑定');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handlePatchAsset = async (assetId: number, patch: Record<string, unknown>, okMsg?: string) => {
    try {
      await api.patch(`/api/assets/${assetId}`, patch);
      await loadProjectCore({ silent: true });
      if (activeEpId) await loadEpisodeShots(activeEpId, { silent: true });
      if (okMsg) notify(okMsg);
    } catch (err: any) {
      notify(err.message, 'err');
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
      flow_mode: s.flow_mode || 'omni12_i2v',
      audio_strategy: s.audio_strategy || 'keep_flow_sfx',
      ref_asset_ids: [...(s.ref_asset_ids || [])],
      cn_action_desc: s.cn_action_desc || '',
      cn_dialogue: s.cn_dialogue || '',
      subtitle_text: s.subtitle_text || '',
      frame_prompt_en: s.frame_prompt_en || '',
      end_frame_prompt_en: s.end_frame_prompt_en || '',
      video_prompt_en: s.video_prompt_en || '',
    });
  };

  const toggleDraftRefAsset = (assetId: number) => {
    setShotDraft((prev) => {
      if (!prev) return prev;
      const exists = prev.ref_asset_ids.includes(assetId);
      return {
        ...prev,
        ref_asset_ids: exists ? prev.ref_asset_ids.filter((id) => id !== assetId) : [...prev.ref_asset_ids, assetId],
      };
    });
  };

  const saveShotEditor = async (shot: Shot) => {
    if (!shotDraft) return;
    setSavingShotEdit(true);
    try {
      const clampedDuration = Math.max(4, Math.min(15, Number(shotDraft.duration_sec) || 6));
      const res = await api.patch<{ shots: Shot[]; budget: BudgetSummary }>(`/api/shots/${shot.id}${flowCompileQuery()}`, {
        shot_title: shotDraft.shot_title.trim() || `镜头 ${shot.shot_no}`,
        camera_type: shotDraft.camera_type.trim() || '中景',
        duration_sec: clampedDuration,
        flow_mode: shotDraft.flow_mode,
        audio_strategy: shotDraft.audio_strategy,
        ref_asset_ids: shotDraft.ref_asset_ids,
        cn_action_desc: shotDraft.cn_action_desc,
        cn_dialogue: shotDraft.cn_dialogue,
        subtitle_text: shotDraft.subtitle_text,
        frame_prompt_en: shotDraft.frame_prompt_en,
        end_frame_prompt_en: shotDraft.end_frame_prompt_en,
        video_prompt_en: shotDraft.video_prompt_en,
      });
      setShots(res.shots);
      setBudget(res.budget);
      setEditingShotId(null);
      setShotDraft(null);
      notify(`已保存 S${String(shot.shot_no).padStart(2, '0')} 修改，Flow Prompt 与积分预算已实时刷新`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setSavingShotEdit(false);
    }
  };

  const handlePatchShot = async (shotId: number, patch: Record<string, unknown>) => {
    try {
      const res = await api.patch<{ shots: Shot[]; budget: BudgetSummary }>(`/api/shots/${shotId}${flowCompileQuery()}`, patch);
      setShots(res.shots);
      setBudget(res.budget);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleUploadShotMedia = async (shotId: number, slot: 'video' | 'start_frame' | 'end_frame', file: File) => {
    const fd = new FormData();
    fd.append('slot', slot);
    fd.append('file', file);
    try {
      const res = await api.upload<{ shots: Shot[]; budget: BudgetSummary }>(`/api/shots/${shotId}/upload-media`, fd);
      setShots(res.shots);
      setBudget(res.budget);
      notify(slot === 'video' ? '视频 Take 已导入' : slot === 'end_frame' ? '尾帧参考图已导入' : '首帧参考图已导入');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleClaimInbox = async (
    item: InboxItem,
    targetType: 'shot_video' | 'shot_start_frame' | 'shot_end_frame' | 'episode_bgm' | 'asset_clean',
    targetId: number
  ) => {
    try {
      const res = await api.post<{
        dock_copy_name?: string;
        trashed_siblings?: string[];
        episode_data?: { shots: Shot[]; budget: BudgetSummary };
      }>(`/api/inbox/claim${flowCompileQuery()}`, {
        inbox_item_id: item.id,
        target_type: targetType,
        target_id: targetId,
        keep_chinese_copy: keepChineseCopy,
      });
      if (res.episode_data) {
        setShots(res.episode_data.shots);
        setBudget(res.episode_data.budget);
      }
      if (targetType === 'asset_clean') await loadProjectCore();
      if (targetType === 'episode_bgm' && activeEpId) await loadEpisodeShots(activeEpId);
      await onRefreshInbox();
      const trashNote =
        res.trashed_siblings && res.trashed_siblings.length > 0
          ? `，同批 ${res.trashed_siblings.length} 张落选废图已自动移入回收站`
          : '';
      notify(
        res.dock_copy_name
          ? `已归档并同步到直拖素材目录：${res.dock_copy_name}${trashNote}`
          : `已认领并规范重命名：${item.filename}${trashNote}`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const buildAssetChineseStem = (
    a: { id?: number; asset_type?: string; name: string },
    slot: 'raw' | 'clean' = 'clean'
  ) => {
    const prefixMap: Record<string, string> = { character: '角色', landmark: '场景', prop: '道具', style: '画风' };
    const prefix = prefixMap[a.asset_type || ''] || '资产';
    const raw = (a.name || '').replace(/[\\/:*?"<>|，。！？、；：“”‘’（）()【】\[\]\s_]+/g, '').trim();

    const numMatch = raw.match(/(\d+)$/);
    let numStr = '';
    let core = raw;
    if (numMatch) {
      numStr = String(Number(numMatch[1])).padStart(2, '0');
      core = raw.slice(0, raw.length - numMatch[1].length);
    } else if (a.id) {
      const sameType = assets.filter((x) => x.asset_type === a.asset_type);
      const idx = sameType.findIndex((x) => x.id === a.id);
      if (idx >= 0) numStr = String(idx + 1).padStart(2, '0');
    }

    core = core.split(/[与和及的·]/)[0] || core;
    if (core.length > 3) {
      core = a.asset_type === 'character' ? core.slice(-3) : core.slice(0, 3);
    }
    const base = `${prefix}_${core || '素材'}${numStr}`;
    return slot === 'raw' ? `${base}原` : base;
  };

  const buildShotFrameChineseStem = (s: Shot, slot: 'start' | 'end') => {
    const shotNo = String(s.shot_no).padStart(2, '0');
    const slotCn = slot === 'start' ? '首帧' : '尾帧';
    return `S${shotNo}${slotCn}`;
  };

  const handleCopyDirectorKickoffPrompt = async () => {
    const nextEpNo = episodes.length > 0 ? Math.max(...episodes.map((e) => e.ep_number)) + 1 : 1;
    const aspectPhrase = formatAspectPhrase(proj.aspect_ratio);
    const imgNoun = flowImageCount === 1 ? 'image' : 'images';
    const vidNoun = flowVideoCount === 1 ? 'video' : 'videos';

    const assetLines =
      assets.length > 0
        ? assets
            .map((a) => {
              const shortName = buildAssetChineseStem(a, 'clean');
              const typeCn = ASSET_TYPE_LABELS[a.asset_type] || a.asset_type;
              const status = a.clean_image_path ? '已定妆' : a.raw_image_path ? '仅有原图' : '待传图';
              return `- [${typeCn}] **${a.name}**｜Flow标签 \`${a.flow_tag}\`｜直拖短名 \`${shortName}.jpg\`（${status}）\n  英文锁词：\`${(a.en_anchor_prompt || '待补').trim()}\``;
            })
            .join('\n')
        : '- （当前暂无已登记资产，开拍前请在制片台 Step 2 补齐）';

    const epLines =
      episodes.length > 0
        ? episodes.map((e) => `- 第 ${String(e.ep_number).padStart(2, '0')} 集《${e.title}》（${e.shot_count ?? 0} 镜 · 状态：${e.status}）`).join('\n')
        : '- （尚无历史分集）';

    const kickoff = [
      `# 导演开工规范｜${proj.name} · 筹备第 ${String(nextEpNo).padStart(2, '0')} 集`,
      ``,
      `请作为本剧目的 AI 短剧总编剧 + Google Flow 实战分镜导演，直接按以下标准推进第 ${nextEpNo} 集：`,
      ``,
      `## 一、剧目档案与写库红线`,
      `- **剧目名称**：${proj.name}（\`project_id=${proj.id}\` · \`slug=${proj.slug}\`）`,
      `- **赛道与画幅**：${proj.track} · ${proj.visual_mode} · \`${proj.aspect_ratio}\`（目标时长 ≥60s）`,
      `- **本地制片台 API / 数据库**：通过本地制片台 API（默认 \`http://127.0.0.1:8765\`，数据位于项目 \`data/studio.db\`）写入新集 \`episode\` 与 \`shots\`，**严禁清空或覆盖已有项目、资产及前序分集**`,
      `- **全局视觉锁词**：\`${(proj.global_style_prompt || '未填写').trim()}\``,
      `- **跨集防吃书台账**：${(proj.continuity_ledger?.notes || '无').trim()}`,
      ``,
      `### 已有分集（防撞题/接伏笔）`,
      epLines,
      ``,
      `## 二、跨集固定资产圣经（直拖素材目录 6 字短名清单）`,
      assetLines,
      ``,
      `## 三、Google Flow 实战铁律（全剧集统一执行）`,
      `1. **极简 6 字短名规范（防 Flow 卡片截断）**：`,
      `   - Flow 卡片底部仅显示约 6–7 个汉字，直拖素材目录严格控长：资产统一「类别_简名NN」（如 \`角色_主角01.jpg\`、\`道具_核心物01.jpg\`、\`场景_主场景01.jpg\`）；分镜首尾帧统一 \`S01首帧.jpg\` / \`S08首帧.jpg\` / \`S08尾帧.jpg\`。`,
      `2. **「固定卡放库 + 一镜一清」单镜闭环（严禁攒满废图）**：`,
      `   - 仅 Flow 画布里没有的外部图（场景实拍、初始棚拍角色卡）从直拖素材目录上传一次；`,
      `   - 逐镜推进：本镜抽 ${flowImageCount} 张首帧 → 挑 1 张冠军后**立刻在 Flow 删掉其余废图**（冠军图点下载 1 次给制片台归档留底）；`,
      `   - **趁热原地转视频**：画布最前只剩这唯一 1 张冠军图，直接拖进输入框（或点卡片顶部 ↪️）+ 粘视频 Prompt 出 ${flowVideoCount} 条视频；`,
      `   - **首尾双帧镜（\`frames_to_video\`）闭环**：抽首帧删废图 → 直接把该首帧拖进框改出尾帧删废图 → 画布最前即一对首尾帧，原地转视频；`,
      `   - 挑 1 条最好视频点 **免费 1080p** 下载（删另 1 条废视频），收件箱一键认领归位后再进下一镜。`,
      `3. **Flow 智能体显式指令头（防忽略齿轮只出 1 张）**：`,
      `   - 首/尾帧英文 Prompt 开头强制带：\`Generate ${flowImageCount} ${aspectPhrase} ${imgNoun} using ${flowImageModel}.\``,
      `   - 视频英文 Prompt 开头按模式强制带：\`Generate ${flowVideoCount} ${aspectPhrase} {duration}s ${vidNoun} using Veo 3.1 Lite.\` / \`Omni Flash 1.2.\` / \`Frames to Video (first and last frame interpolation).\` / \`Extend this ${aspectPhrase} video by 10s using Omni Extend.\``,
      `   - 无对白镜头末尾必保留防幻觉音频护盾：\`Audio: Ambient sound only, crisp realistic environmental SFX, strictly no spoken dialogue and no background music.\``,
      `4. **三油箱分层省分闭环**：`,
      `   - **官方 CLI 媒体直出（走 API 月度预算）**：棚拍洗图（\`${cliImageModel}\`）、原创配乐（\`${cliMusicModel}\`）、双比例封面与核心静帧可直接调用 CLI 直出，不占 Flow 积分；`,
      `   - **Google Flow 网页端**：负责 \`Veo 3.1 Lite\` 与 \`Omni Flash 1.2\` 视频生成，一律优先下载 **免费 1080p**（\`1080p_free\`），不把高额积分浪费在平台内单镜 4K；`,
      `   - **后制封装**：全片三轨混音与 4K/30fps HLG HDR 统一交由 Step 6 的 **Google Colab GPU 通道** 或 **本机 1080p 渲染**。`,
      ``,
      `现在请先和我确认第 ${nextEpNo} 集的选题钩子，再按以上规范出完整剧本并拆解 ≥60s 分镜写入制片台。`,
    ].join('\n');

    await copyToClipboard(kickoff);
    notify(`已复制《${proj.name}》第 ${nextEpNo} 集导演开工规范，新开会话直接粘贴即用！`);
  };

  const renderOneShotWorkflowBanner = () => (
    <div className="rounded-xl border border-emerald-200 bg-emerald-50/55 px-3.5 py-2.5 text-[11px] text-slate-700 space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-emerald-900">
          ⚡ 实战标准闭环：固定卡放库 + 一镜一清（不攒废图 · 全程优先下免费 1080p）
        </span>
        <span className="text-[10px] text-emerald-700 font-medium">
          全片 4K HDR 超分留给 Step 6 Google Colab GPU 统一出母带
        </span>
      </div>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 xl:grid-cols-4 text-[11px] leading-snug">
        <div className="rounded-lg bg-white/85 border border-emerald-200/80 px-2.5 py-1.5">
          <strong className="text-emerald-800">① 固定卡入库</strong>：仅外部图（场景实拍/角色卡）从直拖素材目录以 6 字短名上传一次。
        </div>
        <div className="rounded-lg bg-white/85 border border-emerald-200/80 px-2.5 py-1.5">
          <strong className="text-emerald-800">② 抽 4 定 1 即清</strong>：本镜抽 4 张首帧 → 定 1 张冠军后<strong className="text-slate-900">立刻在 Flow 删另 3 张废图</strong>（冠军图下 1 次留底）。
        </div>
        <div className="rounded-lg bg-white/85 border border-emerald-200/80 px-2.5 py-1.5">
          <strong className="text-emerald-800">③ 趁热原地转视频</strong>：直接拖画布最前唯一冠军图进框（或点 ↪️）粘视频词出 2 条（双帧镜先原地出尾帧删废图）。
        </div>
        <div className="rounded-lg bg-white/85 border border-emerald-200/80 px-2.5 py-1.5">
          <strong className="text-emerald-800">④ 只下免费 1080p</strong>：挑 1 条最好视频下<strong className="text-slate-900">免费 1080p</strong>（删另 1 条），收件箱一键认领后再进下一镜。
        </div>
      </div>
    </div>
  );

  const copyToDockDownloads = async (absPath: string, chineseStem: string) => {
    if (!absPath) return;
    try {
      const res = await api.post<{ filename: string; copy_path: string }>('/api/system/copy-to-downloads', {
        path: absPath,
        chinese_stem: chineseStem,
      });
      notify(`已同步中文名图到直拖素材目录：${res.filename}`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const renderFlowBatchControls = () => (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-sky-200 bg-sky-50/60 px-3 py-2 text-[11px] text-slate-700">
      <span className="font-semibold text-sky-900">Flow 智能体显式指令头：</span>
      <div className="inline-flex items-center gap-1">
        <span className="text-slate-500">生图模型</span>
        <select
          value={flowImageModel}
          onChange={(e) => setFlowImageModel(e.target.value)}
          className="studio-input rounded px-1.5 py-0.5 text-[11px] font-medium text-slate-800"
        >
          {FLOW_IMAGE_MODELS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <div className="inline-flex items-center gap-1">
        <span className="text-slate-500">生图张数</span>
        {[1, 2, 4].map((cnt) => (
          <button
            key={cnt}
            type="button"
            onClick={() => setFlowImageCount(cnt)}
            className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
              flowImageCount === cnt
                ? 'bg-amber-500 border-amber-500 text-white'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
            }`}
          >
            {cnt}张
          </button>
        ))}
      </div>
      <div className="inline-flex items-center gap-1">
        <span className="text-slate-500">视频条数</span>
        {[1, 2, 4].map((cnt) => (
          <button
            key={cnt}
            type="button"
            onClick={() => setFlowVideoCount(cnt)}
            className={`rounded px-2 py-0.5 text-[11px] font-medium border transition cursor-pointer ${
              flowVideoCount === cnt
                ? 'bg-sky-600 border-sky-600 text-white'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
            }`}
          >
            {cnt}条
          </button>
        ))}
      </div>
      <span className="text-[10px] text-slate-500 ml-auto">
        自动写进 Prompt 开头，防止 Flow 智能体忽略界面设置只出 1 张
      </span>
    </div>
  );

  const handleExtractLastFrame = async (shotId: number) => {
    try {
      const res = await api.post<{ shots: Shot[]; budget: BudgetSummary; applied_next_shot_id?: number }>(
        `/api/shots/${shotId}/extract-last-frame?apply_to_next=true`
      );
      setShots(res.shots);
      setBudget(res.budget);
      notify(res.applied_next_shot_id ? '已抽取末帧 PNG 并自动填入下一镜首帧参考图！' : '已抽取末帧 PNG 存为本镜尾帧');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleMoveShot = async (index: number, delta: -1 | 1) => {
    if (!activeEpId) return;
    const nextIdx = index + delta;
    if (nextIdx < 0 || nextIdx >= shots.length) return;
    const ids = shots.map((s) => s.id);
    [ids[index], ids[nextIdx]] = [ids[nextIdx], ids[index]];
    const res = await api.post<{ shots: Shot[]; budget: BudgetSummary }>(`/api/episodes/${activeEpId}/shots/reorder`, {
      shot_ids: ids,
    });
    setShots(res.shots);
    setBudget(res.budget);
    await loadTimeline(activeEpId);
  };

  const revealFileLocation = async (absPath: string) => {
    if (!absPath) return;
    try {
      await api.post('/api/system/reveal-finder', { path: absPath });
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  // 连播控制
  const startSequentialPlay = () => {
    if (timelineItems.length === 0) return;
    setPlayingIdx(0);
    setIsPlayingAll(true);
    setTimeout(() => {
      videoRef.current?.play().catch(() => {});
      if (bgmRef.current) {
        bgmRef.current.currentTime = 0;
        bgmRef.current.volume = activeEp?.bgm_volume ?? 0.25;
        bgmRef.current.play().catch(() => {});
      }
    }, 80);
  };

  const handleVideoEnded = () => {
    if (!isPlayingAll) return;
    if (playingIdx + 1 < timelineItems.length) {
      setPlayingIdx(playingIdx + 1);
      setTimeout(() => videoRef.current?.play().catch(() => {}), 60);
    } else {
      setIsPlayingAll(false);
      bgmRef.current?.pause();
    }
  };

  const runDeliveryAction = async (
    kind: 'local' | 'local_hdr' | 'fcpxml' | 'ai_proof' | 'colab_probe' | 'colab_4k60' | 'colab_pack' | 'cleanup'
  ) => {
    if (!activeEpId) return;
    setRendering(true);
    try {
      if (kind === 'local' || kind === 'local_hdr') {
        const useHdr = kind === 'local_hdr' ? true : localHdrHlg;
        const res = await api.post<{ output_path: string; log: string }>(`/api/episodes/${activeEpId}/render-local`, {
          hdr_hlg: useHdr,
          burn_subtitles: localBurnSubtitles,
        });
        notify(res.log || (useHdr ? '本机 1080p 10-bit HLG HDR 三轨母带已完成！' : '本机 1080p 三轨初审片已完成！'));
        await loadTimeline(activeEpId);
        await loadEpisodeShots(activeEpId);
      } else if (kind === 'fcpxml') {
        const res = await api.post<{ fcpxml_path: string; bundle_dir: string }>(`/api/episodes/${activeEpId}/export-fcpxml`);
        notify('已导出 .fcpxml + .srt，正在打开导出目录（剪映点“导入工程”直读）');
        await revealFileLocation(res.fcpxml_path);
        await loadTimeline(activeEpId);
      } else if (kind === 'ai_proof') {
        const res = await api.post<{ proof_path: string; shot_count: number; asset_count: number }>(
          `/api/episodes/${activeEpId}/export-ai-proof`
        );
        notify(`已导出 AI 原创制作证明（含 ${res.asset_count} 项资产、${res.shot_count} 镜 Prompt），正在打开目录`);
        await revealFileLocation(res.proof_path);
        await loadTimeline(activeEpId);
      } else if (kind === 'colab_probe') {
        const res = await api.post<{ gpu_tier: string; log: string }>(
          `/api/episodes/${activeEpId}/dispatch-colab`,
          { gpu_tier: colabGpuTier, task_mode: 'probe_gpu' }
        );
        notify(`Colab (${res.gpu_tier}) 云端 GPU 冒烟测通并已自动释放 VM！`);
        await loadTimeline(activeEpId);
      } else if (kind === 'colab_4k60') {
        const res = await api.post<{ gpu_tier: string; mode: string; output_path: string; log: string }>(
          `/api/episodes/${activeEpId}/dispatch-colab`,
          {
            gpu_tier: colabGpuTier,
            task_mode: colabTaskMode,
            hdr_hlg: colabHdrHlg,
            burn_subtitles: colabBurnSubtitles,
          }
        );
        const modeCn =
          res.mode === 'upscale_4k_30fps'
            ? `真 AI 4K/30fps ${colabHdrHlg ? '10-bit HLG HDR ' : ''}电影感母带`
            : res.mode === 'ai_60fps_1080p'
              ? '真 AI 1080p/60fps 补帧'
              : `真 AI 4K/60fps ${colabHdrHlg ? 'HLG HDR ' : ''}母带`;
        notify(`Colab (${res.gpu_tier}) ${modeCn}已完成并拉回本机，云端 GPU 已自动释放！`);
        await loadTimeline(activeEpId);
        await loadEpisodeShots(activeEpId);
      } else if (kind === 'colab_pack') {
        const res = await api.post<{ manifest_path: string; rclone_cmd: string }>(
          `/api/episodes/${activeEpId}/export-colab-pack`
        );
        await copyToClipboard(res.rclone_cmd);
        notify('已导出 Colab 轻量任务清单包，并将上传命令复制到剪贴板');
        await revealFileLocation(res.manifest_path);
        await loadTimeline(activeEpId);
      } else if (kind === 'cleanup') {
        const res = await api.post<{ removed_files: number; freed_mb: number }>(`/api/episodes/${activeEpId}/cleanup-drafts`);
        notify(`已清理 ${res.removed_files} 个未定稿临时废片，释放 ${res.freed_mb} MB 本地磁盘空间`);
        await loadEpisodeShots(activeEpId);
      }
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setRendering(false);
    }
  };

  const renderSixtySecBadge = (totalSec: number) => {
    const rounded = Math.round(totalSec * 10) / 10;
    if (rounded >= 60) {
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 border border-emerald-200 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-700">
          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
          已达 ≥60s 完整短剧时长线
        </span>
      );
    }
    const diff = Math.max(0.1, Math.round((60 - rounded) * 10) / 10);
    const diffText = Number.isInteger(diff) ? String(diff) : diff.toFixed(1);
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 border border-amber-200 px-2.5 py-0.5 text-[11px] font-semibold text-amber-800">
        <AlertTriangle className="h-3.5 w-3.5 text-amber-600" />
        距 60s 时长线还差 {diffText} 秒
      </span>
    );
  };

  const renderShotEditor = (s: Shot) => {
    if (editingShotId !== s.id || !shotDraft) return null;
    return (
      <div className="rounded-xl border border-sky-200 bg-sky-50/40 p-3.5 space-y-3 text-xs">
        <div className="flex items-center justify-between">
          <span className="font-semibold text-sky-900">
            编辑 S{String(s.shot_no).padStart(2, '0')} 参数、引用资产与中英文 Prompt
          </span>
          <button
            type="button"
            onClick={() => {
              setEditingShotId(null);
              setShotDraft(null);
            }}
            className="text-[11px] text-slate-500 hover:text-slate-900 cursor-pointer"
          >
            收起
          </button>
        </div>

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
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
              placeholder="如：特写慢推 / 航拍跟镜"
              className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">时长 (秒)</label>
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={4}
                max={15}
                value={shotDraft.duration_sec}
                onChange={(e) => setShotDraft({ ...shotDraft, duration_sec: Number(e.target.value) })}
                className="studio-input w-16 rounded-lg px-2 py-1.5 text-xs"
              />
              {shotDraft.flow_mode === 'veo31_lite' && (
                <div className="flex items-center gap-1">
                  {[4, 6, 8].map((sec) => (
                    <button
                      key={sec}
                      type="button"
                      onClick={() => setShotDraft({ ...shotDraft, duration_sec: sec })}
                      className={`rounded-md px-2 py-1 text-[11px] font-medium border transition cursor-pointer ${
                        shotDraft.duration_sec === sec
                          ? 'bg-sky-600 border-sky-600 text-white'
                          : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100'
                      }`}
                    >
                      {sec}s
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">Flow 生成模式</label>
            <select
              value={shotDraft.flow_mode}
              onChange={(e) => setShotDraft({ ...shotDraft, flow_mode: e.target.value })}
              className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
            >
              {FLOW_MODE_OPTIONS.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">音频策略</label>
            <select
              value={shotDraft.audio_strategy}
              onChange={(e) => setShotDraft({ ...shotDraft, audio_strategy: e.target.value })}
              className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
            >
              {AUDIO_STRATEGY_OPTIONS.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-[11px] font-medium text-slate-700">
            本镜引用的跨集资产（勾选后自动拼入 Flow 英文锁词并列出参考图）
          </label>
          {assets.length === 0 ? (
            <div className="text-[11px] text-slate-400">当前项目暂无资产，可先在「② 跨集资产圣经」添加角色或场景参考。</div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {assets.map((a) => {
                const checked = shotDraft.ref_asset_ids.includes(a.id);
                return (
                  <label
                    key={a.id}
                    className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] transition cursor-pointer ${
                      checked
                        ? 'bg-sky-600 border-sky-600 text-white font-medium'
                        : 'bg-white border-slate-200 text-slate-700 hover:border-sky-300'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleDraftRefAsset(a.id)}
                      className="sr-only"
                    />
                    <span>[{ASSET_TYPE_LABELS[a.asset_type] || a.asset_type}]</span>
                    <span>{a.name}</span>
                    <span className={checked ? 'text-sky-100 font-mono text-[10px]' : 'text-slate-400 font-mono text-[10px]'}>
                      {a.flow_tag}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>

        <div>
          <label className="mb-1 block text-[11px] text-slate-600">中文画面与动作描述</label>
          <textarea
            value={shotDraft.cn_action_desc}
            onChange={(e) => setShotDraft({ ...shotDraft, cn_action_desc: e.target.value })}
            rows={2}
            className="studio-input w-full rounded-lg p-2 text-xs leading-relaxed"
          />
        </div>

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">中文台词 / 旁白</label>
            <input
              value={shotDraft.cn_dialogue}
              onChange={(e) => setShotDraft({ ...shotDraft, cn_dialogue: e.target.value })}
              placeholder="无台词留空"
              className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">屏幕字幕文案</label>
            <input
              value={shotDraft.subtitle_text}
              onChange={(e) => setShotDraft({ ...shotDraft, subtitle_text: e.target.value })}
              placeholder="导出 .srt / 硬压字幕使用"
              className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
            />
          </div>
        </div>

        <div className="space-y-2">
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">首帧英文 Prompt（frame_prompt_en）</label>
            <textarea
              value={shotDraft.frame_prompt_en}
              onChange={(e) => setShotDraft({ ...shotDraft, frame_prompt_en: e.target.value })}
              rows={2}
              className="studio-input w-full rounded-lg p-2 text-[11px] font-mono leading-relaxed"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-violet-800 font-medium">
              尾帧英文 Prompt（end_frame_prompt_en，仅首尾双帧插值需要时填写）
            </label>
            <textarea
              value={shotDraft.end_frame_prompt_en}
              onChange={(e) => setShotDraft({ ...shotDraft, end_frame_prompt_en: e.target.value })}
              rows={2}
              placeholder="留空则不编译尾帧；填写后自动拼接画幅、资产锁词与全局影调"
              className="studio-input w-full rounded-lg p-2 text-[11px] font-mono leading-relaxed"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] text-slate-600">动态视频英文 Prompt（video_prompt_en）</label>
            <textarea
              value={shotDraft.video_prompt_en}
              onChange={(e) => setShotDraft({ ...shotDraft, video_prompt_en: e.target.value })}
              rows={3}
              className="studio-input w-full rounded-lg p-2 text-[11px] font-mono leading-relaxed"
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 pt-1">
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
            disabled={savingShotEdit}
            onClick={() => saveShotEditor(s)}
            className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-50 cursor-pointer"
          >
            <Save className="h-3.5 w-3.5" />
            {savingShotEdit ? '保存中...' : '保存并刷新 Flow Prompt'}
          </button>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* 顶部剧集控制栏 + 6 步线性进度条 */}
      <div className="studio-card rounded-2xl p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              onClick={onBack}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> 大厅
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-semibold text-slate-900">{proj.name}</h1>
                <span className="rounded bg-sky-50 border border-sky-200 px-2 py-0.5 text-[11px] font-medium text-sky-700">{proj.aspect_ratio}</span>
              </div>
              <div className="text-xs text-slate-500">{proj.visual_mode}</div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <select
              value={activeEpId || ''}
              onChange={(e) => setActiveEpId(Number(e.target.value))}
              className="studio-input rounded-lg px-3 py-1.5 text-xs min-w-[200px]"
            >
              {episodes.length === 0 && <option value="">尚无分集（点右侧新建）</option>}
              {episodes.map((e) => (
                <option key={e.id} value={e.id}>
                  第 {e.ep_number} 集 · {e.title} ({e.ready_shot_count ?? 0}/{e.shot_count ?? 0}镜)
                </option>
              ))}
            </select>
            <button
              onClick={handleCopyDirectorKickoffPrompt}
              title="一键复制当前剧目的角色卡短名清单 + 一镜一清流程 + 显式模型张数标准开工规范，新开会话直接粘贴即用"
              className="inline-flex items-center gap-1 rounded-lg border border-violet-200 bg-violet-50 px-2.5 py-1.5 text-xs font-medium text-violet-800 hover:bg-violet-100 cursor-pointer"
            >
              <Copy className="h-3.5 w-3.5 text-violet-600" /> 复制新集开工规范
            </button>
            <button
              onClick={handleSyncDockTray}
              disabled={!activeEpId}
              title="将本集全部角色/场景定妆图与首尾帧，按 ≤6 字极简短名一键同步到直拖素材目录（防 Flow 截断）"
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-40 cursor-pointer"
            >
              <FolderOpen className="h-3.5 w-3.5 text-emerald-600" /> 同步直拖素材目录
            </button>
            {collabSyncEnabled && (
              <>
                <button
                  type="button"
                  onClick={() => handlePushEpisodeToCloud()}
                  disabled={!activeEpId || syncBusy !== null}
                  title="将本集脱敏同步推送至自托管远程协作端（自动压制流畅预览版并通知团队成员审片）"
                  className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-sky-50 px-2.5 py-1.5 text-xs font-semibold text-sky-800 hover:bg-sky-100 disabled:opacity-40 cursor-pointer"
                >
                  <CloudUpload className="h-3.5 w-3.5 text-sky-600" />
                  {syncBusy === 'push' ? '推送中...' : '推送至协作端'}
                </button>
                <button
                  type="button"
                  onClick={handlePullCloudChanges}
                  disabled={syncBusy !== null}
                  title="从自托管远程协作端拉取团队成员的最新改稿、投票、规范建议与评论回复"
                  className="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 text-xs font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-40 cursor-pointer"
                >
                  <CloudDownload className="h-3.5 w-3.5 text-indigo-600" />
                  {syncBusy === 'pull' ? '拉取中...' : '拉取协作更新'}
                </button>
              </>
            )}
            <button
              onClick={() => refreshAllData(false)}
              disabled={refreshing}
              title="刷新当前项目资产、剧本与分镜（保持当前步骤和镜头不变，切回窗口也会自动同步）"
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50 cursor-pointer"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin text-sky-600' : ''}`} /> 刷新本集
            </button>
            <button
              onClick={handleCreateEpisode}
              className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
            >
              <Plus className="h-3.5 w-3.5" /> 新建分集
            </button>
          </div>
        </div>

        {/* 6 步线性进度条 */}
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6">
          {STEPS.map((st) => (
            <button
              key={st.id}
              onClick={() => setStep(st.id)}
              className={`rounded-lg px-2.5 py-2 text-xs font-medium transition text-left truncate cursor-pointer ${
                step === st.id
                  ? 'bg-sky-600 text-white font-semibold shadow-xs'
                  : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border border-slate-200'
              }`}
            >
              {st.title}
            </button>
          ))}
        </div>
      </div>

      {/* ==================== Step 1: 季大纲与剧本 ==================== */}
      {step === 1 && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
          <div className="xl:col-span-8 studio-card rounded-2xl p-5 space-y-4">
            {!activeEp ? (
              <div className="py-16 text-center space-y-3">
                <div className="text-sm text-slate-600">当前剧目还是空骨架</div>
                <button
                  onClick={handleCreateEpisode}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-4 py-2 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
                >
                  <Plus className="h-4 w-4" /> 创建第 1 集（或从左侧「热点与选题库」一键立项）
                </button>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-semibold text-slate-900">第 {activeEp.ep_number} 集剧本（45–85秒黄金节奏）</div>
                  <div className="flex flex-wrap items-center gap-2">
                    <label
                      className="inline-flex items-center gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] font-medium text-amber-800 cursor-pointer"
                      title="开启后将自动运行：编剧写初稿 → 毒舌主编挑刺 → 编剧定向返工定稿"
                    >
                      <input
                        type="checkbox"
                        checked={editorialMode}
                        onChange={(e) => setEditorialMode(e.target.checked)}
                        className="accent-amber-600"
                      />
                      双轮编辑部精修
                    </label>
                    <button
                      onClick={handleAiGenerateScript}
                      disabled={busyAI}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-amber-500 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-50 cursor-pointer"
                    >
                      <Sparkles className="h-3.5 w-3.5" />
                      {busyAI ? (editorialMode ? '编剧写稿 + 主编审改中...' : 'AI 写剧本中...') : 'AI 一键写本集剧本'}
                    </button>
                    <button
                      onClick={handleAiCritiqueScript}
                      disabled={busyAI || !(epDraft.script_text || '').trim()}
                      className="inline-flex items-center gap-1 rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-xs font-medium text-violet-700 hover:bg-violet-100 disabled:opacity-40 cursor-pointer"
                    >
                      毒舌主编审改当前稿
                    </button>
                    <button
                      onClick={handleSaveEpisode}
                      className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
                    >
                      <Save className="h-3.5 w-3.5" /> 保存剧本
                    </button>
                  </div>
                </div>

                {editorialReport && (
                  <div className="rounded-xl border border-violet-200 bg-violet-50/60 p-3.5 space-y-2 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-semibold text-violet-800">
                        多角色编辑部审稿与改稿记录（初稿初评 {editorialReport.score_before || '—'} 分 → 已定向返工）
                      </span>
                      <button
                        onClick={() => setEditorialReport(null)}
                        className="text-[11px] text-slate-500 hover:text-slate-900 cursor-pointer"
                      >
                        收起
                      </button>
                    </div>
                    {editorialReport.verdict && (
                      <div className="text-amber-800 font-medium">主编毒舌总评：{editorialReport.verdict}</div>
                    )}
                    {editorialReport.weaknesses.length > 0 && (
                      <div className="space-y-0.5 text-[11px] text-slate-700">
                        <div className="text-slate-500">挑出的初稿硬伤：</div>
                        {editorialReport.weaknesses.map((w, idx) => (
                          <div key={idx}>• {w}</div>
                        ))}
                      </div>
                    )}
                    {editorialReport.revision_summary && (
                      <div className="rounded-lg bg-emerald-50 border border-emerald-200 px-2.5 py-1.5 text-[11px] text-emerald-800">
                        本轮已落实升级：{editorialReport.revision_summary}
                      </div>
                    )}
                  </div>
                )}

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">本集标题</label>
                    <input
                      value={epDraft.title || ''}
                      onChange={(e) => setEpDraft({ ...epDraft, title: e.target.value })}
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">给 AI 编剧的额外叮嘱（可选）</label>
                    <input
                      value={scriptNotes}
                      onChange={(e) => setScriptNotes(e.target.value)}
                      placeholder="例：强化中段反转节奏、结尾加一句互动彩蛋"
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                </div>

                <div>
                  <label className="mb-1 block text-xs font-medium text-amber-800">0–3 秒视觉奇观 / 反常识钩子（生死线）</label>
                  <input
                    value={epDraft.hook_0_3s || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, hook_0_3s: e.target.value })}
                    className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                  />
                </div>

                <div>
                  <label className="mb-1 block text-xs text-slate-600">分段落完整拍摄剧本（规避 &gt;5 秒真人正脸长对白）</label>
                  <textarea
                    value={epDraft.script_text || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, script_text: e.target.value })}
                    rows={12}
                    placeholder="点击右上角「AI 一键写本集剧本」，或直接粘贴你的剧本正文..."
                    className="studio-input w-full rounded-xl p-3 text-xs leading-relaxed"
                  />
                </div>

                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">结尾评论区互动诱饵</label>
                    <input
                      value={epDraft.ending_cta || ''}
                      onChange={(e) => setEpDraft({ ...epDraft, ending_cta: e.target.value })}
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-slate-600">最后 3 秒下集悬念钩子</label>
                    <input
                      value={epDraft.next_ep_hook || ''}
                      onChange={(e) => setEpDraft({ ...epDraft, next_ep_hook: e.target.value })}
                      className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                    />
                  </div>
                </div>

                {/* Flow Music / Lyria 专属配乐区 */}
                <div className="rounded-xl border border-emerald-200 bg-emerald-50/50 p-3.5 space-y-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-emerald-800">
                      <Music className="h-3.5 w-3.5 text-emerald-600" /> 本集原创配乐（Google Flow Music / {cliMusicModel}）
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={cliBusyKey !== null || !(epDraft.bgm_prompt || '').trim()}
                        onClick={handleCliGenerateBgm}
                        title={`调用官方 gemini-api music (${cliMusicModel})，约 $${priceMusic.toFixed(2)}/首，生成后自动绑定到本集`}
                        className="inline-flex items-center gap-1 rounded bg-indigo-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-45 cursor-pointer"
                      >
                        {cliBusyKey === 'bgm' ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Wand2 className="h-3 w-3" />
                        )}
                        {cliBusyKey === 'bgm' ? 'CLI 生成配乐中...' : `CLI 一键生成配乐 (${cliMusicModel} · $${priceMusic.toFixed(2)})`}
                      </button>
                      {epDraft.bgm_prompt && (
                        <button
                          onClick={async () => {
                            await copyToClipboard(epDraft.bgm_prompt || '');
                            notify('已复制配乐英文提示词');
                          }}
                          className="inline-flex items-center gap-1 rounded bg-white border border-slate-200 px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-emerald-600 hover:border-emerald-600 hover:text-white cursor-pointer"
                        >
                          <Copy className="h-3 w-3" /> 复制配乐 Prompt
                        </button>
                      )}
                      <label className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-emerald-700 cursor-pointer">
                        <Upload className="h-3 w-3" /> 上传 MP3/WAV
                        <input
                          type="file"
                          accept="audio/*"
                          className="hidden"
                          onChange={(e) => e.target.files?.[0] && handleUploadBgm(e.target.files[0])}
                        />
                      </label>
                    </div>
                  </div>
                  <input
                    value={epDraft.bgm_prompt || ''}
                    onChange={(e) => setEpDraft({ ...epDraft, bgm_prompt: e.target.value })}
                    placeholder="英文原创配乐提示词（AI 写剧本时自动生成）"
                    className="studio-input w-full rounded-lg px-3 py-1.5 text-xs font-mono"
                  />
                  {activeEp.bgm_file_path && (
                    <audio src={mediaUrl(activeEp.bgm_file_path)} controls className="w-full h-8" />
                  )}
                </div>
              </>
            )}
          </div>

          {/* 右侧：全局影调锁词 + 季大纲与跨集台账 */}
          <div className="xl:col-span-4 space-y-4">
            <div className="studio-card rounded-2xl p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="text-sm font-semibold text-slate-900">全局视觉锁词 & 跨集防吃书台账</div>
                <button
                  onClick={handleSaveProjectMeta}
                  className="inline-flex items-center gap-1 rounded bg-slate-800 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700 cursor-pointer"
                >
                  <Save className="h-3 w-3" /> 保存
                </button>
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-600">全局画风英文锁词（自动拼接进每镜 Flow Prompt）</label>
                <textarea
                  value={globalStyleDraft}
                  onChange={(e) => setGlobalStyleDraft(e.target.value)}
                  rows={4}
                  className="studio-input w-full rounded-lg p-2.5 text-xs font-mono"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-600">跨集连续性台账（角色状态/常驻场景/伏笔）</label>
                <textarea
                  value={ledgerNotesDraft}
                  onChange={(e) => setLedgerNotesDraft(e.target.value)}
                  rows={4}
                  className="studio-input w-full rounded-lg p-2.5 text-xs"
                />
              </div>
            </div>

            <div className="studio-card rounded-2xl p-4 space-y-2.5">
              <div className="text-sm font-semibold text-slate-900">季大纲参考（1–3 集空位规划）</div>
              {(proj.season_outline || []).map((o, idx) => (
                <div key={idx} className="rounded-lg bg-slate-50 border border-slate-200 p-2.5 text-xs space-y-1">
                  <div className="font-semibold text-sky-700">
                    第 {o.ep} 集：{o.title}
                  </div>
                  <div className="text-slate-600">钩子：{o.hook}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ==================== Step 2: 跨集资产圣经 ==================== */}
      {step === 2 && (
        <div className="space-y-4">
          <form onSubmit={handleCreateAsset} className="studio-card rounded-2xl p-4 grid grid-cols-1 gap-3 md:grid-cols-12 items-end">
            <div className="md:col-span-2">
              <label className="mb-1 block text-xs text-slate-600">资产分类</label>
              <select
                value={newAssetType}
                onChange={(e) => setNewAssetType(e.target.value as any)}
                className="studio-input w-full rounded-lg px-2.5 py-2 text-xs"
              >
                <option value="character">主角/角色</option>
                <option value="landmark">场景/地标</option>
                <option value="prop">关键道具</option>
                <option value="style">画风参考</option>
              </select>
            </div>
            <div className="md:col-span-2">
              <label className="mb-1 block text-xs text-slate-600">中文名 *</label>
              <input
                value={newAssetName}
                onChange={(e) => setNewAssetName(e.target.value)}
                placeholder="如：复古咖啡馆窗景"
                className="studio-input w-full rounded-lg px-2.5 py-2 text-xs"
                required
              />
            </div>
            <div className="md:col-span-2">
              <label className="mb-1 block text-xs text-slate-600">Flow 标签 (@引用)</label>
              <input
                value={newAssetTag}
                onChange={(e) => setNewAssetTag(e.target.value)}
                placeholder="@CafeWindow"
                className="studio-input w-full rounded-lg px-2.5 py-2 text-xs"
              />
            </div>
            <div className="md:col-span-4">
              <label className="mb-1 block text-xs text-slate-600">英文外貌/场景不变锁词</label>
              <input
                value={newAssetEn}
                onChange={(e) => setNewAssetEn(e.target.value)}
                placeholder="英文固定特征，避免跨镜变脸或场景走样"
                className="studio-input w-full rounded-lg px-2.5 py-2 text-xs font-mono"
              />
            </div>
            <div className="md:col-span-2">
              <button
                type="submit"
                className="w-full inline-flex items-center justify-center gap-1 rounded-lg bg-sky-600 py-2 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
              >
                <Plus className="h-3.5 w-3.5" /> 添加资产
              </button>
            </div>
          </form>

          {renderCliMediaControlBar(true)}

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {assets.map((a) => {
              const selectedInboxId = assetInboxPick[a.id] || inboxImages[0]?.id || '';
              const selectedInboxItem = inboxImages.find((it) => it.id === selectedInboxId) || inboxImages[0] || null;
              return (
                <div key={a.id} className="studio-card rounded-2xl p-4 space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <select
                          value={a.asset_type}
                          onChange={(e) => handlePatchAsset(a.id, { asset_type: e.target.value }, '已更新资产分类')}
                          className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 cursor-pointer"
                        >
                          <option value="character">主角/角色</option>
                          <option value="landmark">场景/地标</option>
                          <option value="prop">关键道具</option>
                          <option value="style">画风参考</option>
                        </select>
                        <input
                          key={`tag-${a.id}-${a.flow_tag}`}
                          defaultValue={a.flow_tag}
                          onBlur={(e) => {
                            const val = e.target.value.trim();
                            if (val && val !== a.flow_tag) {
                              handlePatchAsset(a.id, { flow_tag: val }, '已更新 @flow_tag');
                            }
                          }}
                          placeholder="@FlowTag"
                          className="studio-input flex-1 min-w-[90px] rounded px-2 py-0.5 font-mono text-[11px] font-medium text-sky-700"
                        />
                      </div>
                      <input
                        key={`name-${a.id}-${a.name}`}
                        defaultValue={a.name}
                        onBlur={(e) => {
                          const val = e.target.value.trim();
                          if (val && val !== a.name) {
                            handlePatchAsset(a.id, { name: val }, `已改名为「${val}」`);
                          }
                        }}
                        placeholder="资产中文名（失焦自动保存）"
                        className="studio-input w-full rounded-lg px-2 py-1 text-sm font-semibold text-slate-900"
                      />
                    </div>
                    <button
                      onClick={async () => {
                        await api.del(`/api/assets/${a.id}`);
                        await loadProjectCore();
                      }}
                      title="删除该资产"
                      className="text-slate-400 hover:text-rose-600 cursor-pointer pt-1"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    {[
                      { slot: 'raw' as const, label: '公开原图/草图', path: a.raw_image_path },
                      { slot: 'clean' as const, label: '标准定妆图', path: a.clean_image_path },
                    ].map((box) => (
                      <div key={box.slot} className="rounded-xl border border-slate-200 bg-slate-50/70 p-2 space-y-1.5">
                        <div className="flex items-center justify-between gap-1 text-[10px] text-slate-500">
                          <span className="truncate">{box.label}</span>
                          {box.path && (
                            <div className="flex items-center gap-1.5 shrink-0">
                              <button
                                type="button"
                                onClick={() => copyToDockDownloads(box.path, buildAssetChineseStem(a, box.slot))}
                                title="复制中文短名副本到直拖素材目录（方便拖入 Flow）"
                                className="inline-flex items-center gap-0.5 text-emerald-700 font-medium hover:underline cursor-pointer"
                              >
                                <Download className="h-2.5 w-2.5" /> 拷到直拖
                              </button>
                              <button
                                type="button"
                                onClick={() => revealFileLocation(box.path)}
                                className="text-sky-700 font-medium hover:underline cursor-pointer"
                              >
                                目录
                              </button>
                            </div>
                          )}
                        </div>
                        {box.path ? (
                          <img src={mediaUrl(box.path)} alt={a.name} className="h-28 w-full rounded-lg object-cover bg-slate-900" />
                        ) : (
                          <div className="h-28 flex items-center justify-center rounded-lg border border-dashed border-slate-300 bg-white text-[11px] text-slate-400">
                            未上传
                          </div>
                        )}
                        <label className="block text-center rounded bg-white border border-slate-200 py-1 text-[10px] font-medium text-slate-700 hover:bg-slate-100 cursor-pointer">
                          上传图片
                          <input
                            type="file"
                            accept="image/*"
                            className="hidden"
                            onChange={(e) => e.target.files?.[0] && handleUploadAssetFile(a.id, box.slot, e.target.files[0])}
                          />
                        </label>
                        {box.slot === 'clean' && (
                          <button
                            type="button"
                            disabled={cliBusyKey !== null}
                            onClick={() => handleCliGenerateAssetClean(a, 'portrait')}
                            title={`调用官方 gemini-api image (${cliImageModel} · ${cliImageSize} · ${cliImageCount}张)，自动带入原图做棚拍洗图`}
                            className="w-full inline-flex items-center justify-center gap-1 rounded bg-indigo-600 py-1 text-[10px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-45 cursor-pointer"
                          >
                            {cliBusyKey === `asset-${a.id}` ? (
                              <Loader2 className="h-2.5 w-2.5 animate-spin" />
                            ) : (
                              <Wand2 className="h-2.5 w-2.5" />
                            )}
                            {cliBusyKey === `asset-${a.id}` ? 'CLI 洗图中...' : `CLI 棚拍洗图 (${cliImageSize}×${cliImageCount})`}
                          </button>
                        )}
                        {box.slot === 'clean' && inboxImages.length > 0 && (
                          <div className="space-y-1 pt-1 border-t border-slate-200/80">
                            <select
                              value={selectedInboxId}
                              onChange={(e) => setAssetInboxPick((prev) => ({ ...prev, [a.id]: e.target.value }))}
                              className="studio-input w-full rounded px-1.5 py-0.5 text-[10px]"
                              title="选择 Flow 收件箱里刚洗好的纯净角色卡"
                            >
                              {inboxImages.map((it) => (
                                <option key={it.id} value={it.id}>
                                  {it.filename}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              onClick={() => selectedInboxItem && handleClaimInbox(selectedInboxItem, 'asset_clean', a.id)}
                              className="w-full rounded bg-amber-500 py-1 text-[10px] font-semibold text-white hover:bg-amber-600 cursor-pointer"
                            >
                              一键认领收件箱定妆图
                            </button>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  {assetCandidates[a.id] && assetCandidates[a.id].length > 0 && (
                    <div className="rounded-xl border border-indigo-200 bg-indigo-50/45 p-2.5 space-y-2">
                      <div className="flex items-center justify-between text-[11px]">
                        <span className="font-semibold text-indigo-900">
                          CLI 棚拍候选图（点选 1 张设为定妆图）
                        </span>
                        <button
                          type="button"
                          onClick={() =>
                            setAssetCandidates((prev) => {
                              const next = { ...prev };
                              delete next[a.id];
                              return next;
                            })
                          }
                          className="text-slate-500 hover:text-slate-800 cursor-pointer"
                        >
                          收起
                        </button>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        {assetCandidates[a.id].map((c) => (
                          <div key={c.path} className="rounded-lg border border-indigo-200/80 bg-white p-1.5 space-y-1">
                            <img src={mediaUrl(c.path)} alt={c.filename} className="h-28 w-full rounded object-contain bg-slate-900" />
                            <button
                              type="button"
                              disabled={cliBusyKey !== null}
                              onClick={() => handleSelectAssetCandidate(a, c)}
                              className="w-full rounded bg-indigo-600 py-1 text-[10px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-45 cursor-pointer"
                            >
                              设为定妆图 #{c.index}
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div>
                    <label className="mb-1 block text-[10px] font-medium text-slate-500">英文不变锁词（离开输入框自动保存）</label>
                    <textarea
                      key={`en-${a.id}-${a.en_anchor_prompt}`}
                      defaultValue={a.en_anchor_prompt}
                      onBlur={(e) => {
                        if (e.target.value !== a.en_anchor_prompt) {
                          handlePatchAsset(a.id, { en_anchor_prompt: e.target.value }, '已保存英文锁词');
                        }
                      }}
                      rows={2}
                      placeholder="英文固定特征，避免跨镜变脸或场景走样"
                      className="studio-input w-full rounded-lg p-2 text-[11px] font-mono"
                    />
                  </div>

                  <div>
                    <label className="mb-1 flex items-center gap-1 text-[10px] font-semibold text-emerald-700">
                      <Shield className="h-3 w-3" /> 永久防穿帮护盾（negative_guard_en，编译自动注入）
                    </label>
                    <textarea
                      key={`guard-${a.id}-${a.negative_guard_en || ''}`}
                      defaultValue={a.negative_guard_en || ''}
                      onBlur={(e) => {
                        if (e.target.value !== (a.negative_guard_en || '')) {
                          handlePatchAsset(a.id, { negative_guard_en: e.target.value }, '已保存永久防穿帮护盾');
                        }
                      }}
                      rows={2}
                      placeholder="例：strictly preserve character face, silhouette and signature accessories, no extra limbs or unintended costume changes"
                      className="studio-input w-full rounded-lg p-2 text-[11px] font-mono border-emerald-200 bg-emerald-50/30"
                    />
                  </div>

                  <div>
                    <label className="mb-1 block text-[10px] font-medium text-slate-500">棚拍与角色制作备注（离开输入框自动保存）</label>
                    <textarea
                      key={`notes-${a.id}-${a.notes || ''}`}
                      defaultValue={a.notes || ''}
                      onBlur={(e) => {
                        if (e.target.value !== (a.notes || '')) {
                          handlePatchAsset(a.id, { notes: e.target.value }, '已保存资产备注');
                        }
                      }}
                      rows={2}
                      placeholder="记录棚拍洗图要求、角色核心特征或实拍场景来源..."
                      className="studio-input w-full rounded-lg p-2 text-[11px] leading-relaxed"
                    />
                  </div>

                  {/* Flow 通用单主体棚拍定妆提示词 + 声音克隆样音位 */}
                  {a.asset_type === 'character' && (
                    <div className="space-y-2">
                      <div className="rounded-lg bg-violet-50/70 border border-violet-200 p-2 space-y-1.5">
                        <div className="text-[10px] font-semibold text-violet-800">
                          Google Flow 角色卡四步法（Nano Banana · 通用中性棚拍，适用人/萌宠/拟物主角）
                        </div>
                        <div className="grid grid-cols-2 gap-1.5">
                          <button
                            type="button"
                            onClick={async () => {
                              const desc = (a.en_anchor_prompt || a.name).trim();
                              const imgNoun = flowImageCount === 1 ? 'image' : 'images';
                              const header = `Generate ${flowImageCount} ${formatAspectPhrase(proj.aspect_ratio)} ${imgNoun} using ${flowImageModel}.`;
                              const portraitPrompt = `${header} ${desc}. Clean dark neutral matte studio background, remove outdoor clutter and busy reflections, soft even cinematic studio key light, standard front three-quarter close-up reference sheet locking exact surface texture, materials, silhouette and signature accessories, photorealistic 35mm film quality.`;
                              await copyToClipboard(portraitPrompt);
                              reportInboxContext(buildAssetChineseStem(a, 'clean'));
                              notify(`已复制①正面棚拍定妆词（已显式指定 ${flowImageModel} · ${flowImageCount} 张，新下载图自动按「${a.name}」命名）`);
                            }}
                            className="rounded bg-white border border-violet-200 px-2 py-1 text-[10px] font-medium text-violet-700 hover:bg-violet-600 hover:text-white cursor-pointer"
                          >
                            复制①正面棚拍定妆词
                          </button>
                          <button
                            type="button"
                            onClick={async () => {
                              const desc = (a.en_anchor_prompt || a.name).trim();
                              const imgNoun = flowImageCount === 1 ? 'image' : 'images';
                              const header = `Generate ${flowImageCount} ${formatAspectPhrase(proj.aspect_ratio)} ${imgNoun} using ${flowImageModel}.`;
                              const fullBodyPrompt = `${header} Full-view complete silhouette reference of the exact same subject (${desc}), showing full head-to-tail and top-to-bottom proportions, structure, surface materials and signature details clearly, centered in a clean dark neutral matte studio background, soft even cinematic studio lighting, photorealistic 35mm film quality, no extra unrequested clothing or props.`;
                              await copyToClipboard(fullBodyPrompt);
                              reportInboxContext(buildAssetChineseStem(a, 'clean'));
                              notify(`已复制②全貌比例棚拍词（已显式指定 ${flowImageModel} · ${flowImageCount} 张，新下载图自动按「${a.name}」命名）`);
                            }}
                            className="rounded bg-white border border-violet-200 px-2 py-1 text-[10px] font-medium text-violet-700 hover:bg-violet-600 hover:text-white cursor-pointer"
                          >
                            复制②全貌比例棚拍词
                          </button>
                        </div>
                        <div className="text-[10px] text-slate-600 leading-snug">
                          不向非人/拟物角色强塞不合理的人类服饰词，确保造型稳定；建卡后生片时点弹窗选中 {a.flow_tag}。
                        </div>
                      </div>

                      <div className="flex items-center justify-between rounded-lg bg-slate-50 px-2.5 py-1.5 text-[11px] border border-slate-200">
                        <span className="inline-flex items-center gap-1 text-slate-600">
                          <Mic className="h-3 w-3 text-amber-600" />
                          {a.voice_clone_sample_path ? '已备声音克隆干声样本' : '专属配音克隆样音位'}
                        </span>
                        <label className="text-sky-700 font-medium hover:underline cursor-pointer">
                          上传样音
                          <input
                            type="file"
                            accept="audio/*"
                            className="hidden"
                            onChange={(e) => e.target.files?.[0] && handleUploadAssetFile(a.id, 'voice_sample', e.target.files[0])}
                          />
                        </label>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {assets.length === 0 && (
              <div className="col-span-full studio-card rounded-2xl py-12 text-center text-xs text-slate-400">
                可手动添加角色/场景地标/道具，或在下一步点「AI 自动提取资产并拆分镜」自动建档。
              </div>
            )}
          </div>
        </div>
      )}

      {/* ==================== Step 3: 拆分镜 & 静帧预演 ==================== */}
      {step === 3 && (
        <div className="space-y-4">
          <div className="studio-card rounded-2xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={handleAiBreakShots}
                  disabled={busyAI || !activeEpId}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-amber-500 px-3.5 py-2 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-50 cursor-pointer"
                >
                  <Sparkles className="h-3.5 w-3.5" />
                  {busyAI ? 'AI 拆镜并提取资产中...' : `AI 自动提取资产并拆 ${proj.aspect_ratio} 分镜`}
                </button>
                <button
                  onClick={async () => {
                    if (!activeEpId) return;
                    const res = await api.post<{ shots: Shot[]; budget: BudgetSummary }>(`/api/episodes/${activeEpId}/shots`, {
                      shot_title: `镜头 ${shots.length + 1}`,
                    });
                    setShots(res.shots);
                    setBudget(res.budget);
                  }}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 hover:bg-slate-100 cursor-pointer"
                >
                  <Plus className="h-3.5 w-3.5" /> 手动加一镜
                </button>
              </div>

              <div className="flex items-center gap-1.5 rounded-lg bg-slate-100 p-1 border border-slate-200">
                <button
                  onClick={() => setShotView('board')}
                  className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs cursor-pointer ${
                    shotView === 'board' ? 'bg-white text-slate-900 font-semibold shadow-2xs' : 'text-slate-600'
                  }`}
                >
                  <LayoutGrid className="h-3.5 w-3.5" /> 故事板预演
                </button>
                <button
                  onClick={() => setShotView('table')}
                  className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs cursor-pointer ${
                    shotView === 'table' ? 'bg-white text-slate-900 font-semibold shadow-2xs' : 'text-slate-600'
                  }`}
                >
                  <List className="h-3.5 w-3.5" /> 分镜表格
                </button>
              </div>
            </div>

            {budget && (
              <div className="flex flex-wrap items-center gap-3 rounded-xl bg-slate-50 px-3.5 py-2.5 text-xs border border-slate-200 text-slate-700">
                <span>共 <strong className="text-slate-900">{budget.shot_count}</strong> 镜 · 预计 <strong className="text-slate-900">{budget.total_duration_sec}s</strong></span>
                {renderSixtySecBadge(budget.total_duration_sec)}
                <span className="text-emerald-700">Nano Banana 首帧：约 {budget.nb_daily_images} 张（<strong>0 积分</strong>/走日配额）</span>
                <span className="text-sky-700">视频抽卡+定稿预估：<strong>{budget.total_estimated_credits}</strong> Flow 积分</span>
                <span className="text-slate-500">1080p 超分：0 积分免费 · 4K 英雄镜：{budget.four_k_count} 镜</span>
              </div>
            )}

            {renderFlowBatchControls()}

            {renderCliMediaControlBar(true)}

            {renderOneShotWorkflowBanner()}

            {budget?.alerts?.map((al, i) => (
              <div key={i} className="flex items-center gap-2 rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-900">
                <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" /> {al}
              </div>
            ))}
          </div>

          {shotView === 'board' ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {shots.map((s) => {
                const showEnd = hasEndFrameFeature(s);
                const isEditing = editingShotId === s.id;
                const endPromptToCopy = s.compiled.compiled_end_frame_prompt || s.end_frame_prompt_en || '';
                return (
                  <div
                    key={s.id}
                    className={`studio-card rounded-2xl p-4 space-y-3 flex flex-col justify-between ${
                      isEditing ? 'md:col-span-2 ring-2 ring-sky-200 border-sky-300' : ''
                    }`}
                  >
                    <div className="space-y-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="rounded bg-sky-50 border border-sky-200 px-2 py-0.5 text-xs font-bold text-sky-700 shrink-0">
                            S{String(s.shot_no).padStart(2, '0')}
                          </span>
                          <span className="text-sm font-semibold text-slate-900 truncate">{s.shot_title}</span>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <span className="text-xs text-slate-500">{s.camera_type} · {s.duration_sec}s</span>
                          <button
                            type="button"
                            onClick={() => openShotEditor(s)}
                            className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium transition cursor-pointer ${
                              isEditing
                                ? 'bg-sky-600 border-sky-600 text-white'
                                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-100'
                            }`}
                          >
                            <Pencil className="h-3 w-3" />
                            {isEditing ? '收起' : '编辑本镜'}
                            {isEditing ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                          </button>
                        </div>
                      </div>

                      {isEditing && renderShotEditor(s)}

                      {showEnd ? (
                        <div className="grid grid-cols-2 gap-2">
                          <div className="space-y-1">
                            <div className="flex items-center justify-between text-[10px] text-slate-500">
                              <span className="font-medium text-amber-800">首帧 Start</span>
                              {s.start_frame_path && (
                                <div className="flex items-center gap-1.5">
                                  <button
                                    type="button"
                                    onClick={() => copyToDockDownloads(s.start_frame_path, buildShotFrameChineseStem(s, 'start'))}
                                    title="复制中文短名副本到直拖素材目录"
                                    className="text-emerald-700 font-medium hover:underline cursor-pointer"
                                  >
                                    拷到直拖
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => revealFileLocation(s.start_frame_path)}
                                    className="text-sky-700 font-medium hover:underline cursor-pointer"
                                  >
                                    目录
                                  </button>
                                </div>
                              )}
                            </div>
                            <div className={`relative rounded-xl overflow-hidden border h-40 flex items-center justify-center ${s.start_frame_path ? 'bg-slate-900 border-slate-200' : 'bg-slate-50 border-dashed border-slate-300'}`}>
                              {s.start_frame_path ? (
                                <img src={mediaUrl(s.start_frame_path)} alt={`${s.shot_title}-首帧`} className="h-full w-full object-contain" />
                              ) : (
                                <div className="text-center text-[11px] text-slate-400 px-2">待定首帧图</div>
                              )}
                            </div>
                          </div>
                          <div className="space-y-1">
                            <div className="flex items-center justify-between text-[10px] text-slate-500">
                              <span className="font-medium text-violet-800">尾帧 End</span>
                              {s.end_frame_path && (
                                <div className="flex items-center gap-1.5">
                                  <button
                                    type="button"
                                    onClick={() => copyToDockDownloads(s.end_frame_path, buildShotFrameChineseStem(s, 'end'))}
                                    title="复制中文短名副本到直拖素材目录"
                                    className="text-emerald-700 font-medium hover:underline cursor-pointer"
                                  >
                                    拷到直拖
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => revealFileLocation(s.end_frame_path)}
                                    className="text-sky-700 font-medium hover:underline cursor-pointer"
                                  >
                                    目录
                                  </button>
                                </div>
                              )}
                            </div>
                            <div className={`relative rounded-xl overflow-hidden border h-40 flex items-center justify-center ${s.end_frame_path ? 'bg-slate-900 border-violet-200' : 'bg-violet-50/40 border-dashed border-violet-300'}`}>
                              {s.end_frame_path ? (
                                <img src={mediaUrl(s.end_frame_path)} alt={`${s.shot_title}-尾帧`} className="h-full w-full object-contain" />
                              ) : (
                                <div className="text-center text-[11px] text-violet-600/80 px-2">待定尾帧图（Frames to Video）</div>
                              )}
                            </div>
                          </div>
                        </div>
                      ) : (
                        <div className={`relative rounded-xl overflow-hidden border h-44 flex items-center justify-center ${s.start_frame_path ? 'bg-slate-900 border-slate-200' : 'bg-slate-50 border-dashed border-slate-300'}`}>
                          {s.start_frame_path ? (
                            <>
                              <img src={mediaUrl(s.start_frame_path)} alt={s.shot_title} className="h-full w-full object-contain" />
                              <div className="absolute top-2 right-2 flex items-center gap-1">
                                <button
                                  type="button"
                                  onClick={() => copyToDockDownloads(s.start_frame_path, buildShotFrameChineseStem(s, 'start'))}
                                  title="复制中文短名副本到直拖素材目录"
                                  className="inline-flex items-center gap-0.5 rounded-md bg-emerald-600/95 px-2 py-0.5 text-[10px] font-medium text-white shadow-xs hover:bg-emerald-700 cursor-pointer"
                                >
                                  <Download className="h-2.5 w-2.5" /> 拷到直拖
                                </button>
                                <button
                                  type="button"
                                  onClick={() => revealFileLocation(s.start_frame_path)}
                                  className="rounded-md bg-white/90 px-2 py-0.5 text-[10px] font-medium text-slate-700 shadow-xs hover:bg-white cursor-pointer"
                                >
                                  目录
                                </button>
                              </div>
                            </>
                          ) : (
                            <div className="text-center text-xs text-slate-500 px-4">
                              一镜一清：复制首帧词去 Flow 抽 {flowImageCount} 张 → 定 1 冠军删废图（下 1 次留底）→ 原地转视频
                            </div>
                          )}
                        </div>
                      )}

                      <p className="text-xs text-slate-700 leading-relaxed">{s.cn_action_desc}</p>
                      {s.compiled.ref_images.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {s.compiled.ref_images.map((r) => (
                            <span
                              key={r.asset_id}
                              className="rounded bg-slate-100 border border-slate-200 px-1.5 py-0.5 text-[10px] font-mono text-slate-600"
                            >
                              {r.flow_tag}
                            </span>
                          ))}
                        </div>
                      )}
                      {s.subtitle_text && (
                        <div className="rounded bg-amber-50 border border-amber-200/70 px-2.5 py-1 text-xs text-amber-900">字幕：{s.subtitle_text}</div>
                      )}

                      {s.compiled.warnings.map((w, i) => (
                        <div key={i} className="text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1">{w}</div>
                      ))}
                    </div>

                    <div className="space-y-2 pt-2 border-t border-slate-100">
                      <div className="grid grid-cols-2 gap-1.5">
                        <select
                          value={s.flow_mode}
                          onChange={(e) => handlePatchShot(s.id, { flow_mode: e.target.value })}
                          className="studio-input rounded px-2 py-1 text-[11px]"
                        >
                          {FLOW_MODE_OPTIONS.map((o) => (
                            <option key={o.id} value={o.id}>{o.label}</option>
                          ))}
                        </select>
                        <select
                          value={s.upscale_target}
                          onChange={(e) => handlePatchShot(s.id, { upscale_target: e.target.value })}
                          className="studio-input rounded px-2 py-1 text-[11px]"
                        >
                          <option value="1080p_free">1080p 免费超分</option>
                          <option value="4k_hero">4K 英雄镜 (+50分)</option>
                        </select>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        <button
                          type="button"
                          disabled={cliBusyKey !== null}
                          onClick={() => handleCliGenerateShotFrame(s, 'start_frame')}
                          title={`调用官方 gemini-api image (${cliImageModel} · ${cliImageSize} · ${cliImageCount}张)，自动挂载本镜绑定资产参考图`}
                          className="inline-flex items-center justify-center gap-1 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-indigo-700 disabled:opacity-45 cursor-pointer"
                        >
                          {cliBusyKey === `frame-${s.id}-start_frame` ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <Wand2 className="h-3 w-3" />
                          )}
                          {cliBusyKey === `frame-${s.id}-start_frame` ? 'CLI 出图中...' : 'CLI 生首帧'}
                        </button>
                        <button
                          onClick={async () => {
                            await copyToClipboard(s.compiled.compiled_frame_prompt);
                            reportInboxContext(buildShotFrameChineseStem(s, 'start'));
                            notify(`已复制 S${String(s.shot_no).padStart(2, '0')} 首帧英文 Prompt（新下载图自动按本镜中文名命名）`);
                          }}
                          className="flex-1 inline-flex items-center justify-center gap-1 rounded-lg bg-amber-50 border border-amber-200 py-1.5 text-[11px] font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                        >
                          <Copy className="h-3 w-3" /> 复制首帧 Prompt
                        </button>
                        <label className="inline-flex items-center gap-1 rounded-lg bg-white border border-slate-200 px-2.5 py-1.5 text-[11px] text-slate-700 hover:bg-slate-100 cursor-pointer">
                          <Upload className="h-3 w-3" /> 传首帧
                          <input
                            type="file"
                            accept="image/*"
                            className="hidden"
                            onChange={(e) => e.target.files?.[0] && handleUploadShotMedia(s.id, 'start_frame', e.target.files[0])}
                          />
                        </label>
                        <button
                          onClick={() => {
                            setActiveShotId(s.id);
                            setStep(4);
                          }}
                          className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                        >
                          去出视频 →
                        </button>
                      </div>

                      {renderShotFrameCandidatePicker(s, 'start_frame')}

                      {showEnd && (
                        <>
                          <div className="flex flex-wrap gap-1.5">
                            <button
                              type="button"
                              disabled={cliBusyKey !== null || !endPromptToCopy}
                              onClick={() => handleCliGenerateShotFrame(s, 'end_frame')}
                              title={endPromptToCopy ? `调用官方 CLI 自动挂载首帧+资产生成尾帧 (${cliImageSize}×${cliImageCount})` : '请先点「编辑本镜」填写尾帧英文 Prompt'}
                              className="inline-flex items-center justify-center gap-1 rounded-lg bg-violet-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-violet-700 disabled:opacity-45 cursor-pointer"
                            >
                              {cliBusyKey === `frame-${s.id}-end_frame` ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : (
                                <Wand2 className="h-3 w-3" />
                              )}
                              {cliBusyKey === `frame-${s.id}-end_frame` ? 'CLI 出尾帧...' : 'CLI 生尾帧'}
                            </button>
                            <button
                              type="button"
                              disabled={!endPromptToCopy}
                              onClick={async () => {
                                if (!endPromptToCopy) return;
                                await copyToClipboard(endPromptToCopy);
                                reportInboxContext(buildShotFrameChineseStem(s, 'end'));
                                notify(`已复制 S${String(s.shot_no).padStart(2, '0')} 尾帧英文 Prompt（新下载图自动按本镜尾帧命名）`);
                              }}
                              title={endPromptToCopy ? '复制已编译的尾帧英文 Prompt' : '请先点「编辑本镜」填写尾帧英文 Prompt'}
                              className="flex-1 inline-flex items-center justify-center gap-1 rounded-lg bg-violet-50 border border-violet-200 py-1.5 text-[11px] font-medium text-violet-800 hover:bg-violet-100 disabled:opacity-45 cursor-pointer"
                            >
                              <Copy className="h-3 w-3" /> 复制尾帧 Prompt
                            </button>
                            <label className="inline-flex items-center gap-1 rounded-lg bg-white border border-violet-200 px-2.5 py-1.5 text-[11px] text-violet-700 hover:bg-violet-50 cursor-pointer">
                              <Upload className="h-3 w-3" /> 上传尾帧
                              <input
                                type="file"
                                accept="image/*"
                                className="hidden"
                                onChange={(e) => e.target.files?.[0] && handleUploadShotMedia(s.id, 'end_frame', e.target.files[0])}
                              />
                            </label>
                            {s.end_frame_path && (
                              <button
                                type="button"
                                onClick={() => revealFileLocation(s.end_frame_path)}
                                className="inline-flex items-center gap-1 rounded-lg bg-white border border-slate-200 px-2 py-1.5 text-[11px] text-slate-700 hover:bg-slate-100 cursor-pointer"
                              >
                                <FolderOpen className="h-3 w-3" /> 目录
                              </button>
                            )}
                          </div>
                          {renderShotFrameCandidatePicker(s, 'end_frame')}
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="studio-card rounded-2xl overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-3 py-2.5">镜号</th>
                    <th className="px-3 py-2.5">画面与运镜</th>
                    <th className="px-3 py-2.5">台词 / 字幕</th>
                    <th className="px-3 py-2.5">Flow 模式</th>
                    <th className="px-3 py-2.5">首/尾帧</th>
                    <th className="px-3 py-2.5">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {shots.map((s) => {
                    const showEnd = hasEndFrameFeature(s);
                    const isEditing = editingShotId === s.id;
                    const endPromptToCopy = s.compiled.compiled_end_frame_prompt || s.end_frame_prompt_en || '';
                    return (
                      <React.Fragment key={s.id}>
                        <tr className="hover:bg-slate-50">
                          <td className="px-3 py-2.5 font-mono font-semibold text-sky-700">S{String(s.shot_no).padStart(2, '0')} ({s.duration_sec}s)</td>
                          <td className="px-3 py-2.5 max-w-[280px]">
                            <div className="font-medium text-slate-900">{s.shot_title} · {s.camera_type}</div>
                            <div className="text-slate-600 mt-0.5">{s.cn_action_desc}</div>
                          </td>
                          <td className="px-3 py-2.5 max-w-[220px] text-amber-800">{s.subtitle_text || s.cn_dialogue || '—'}</td>
                          <td className="px-3 py-2.5 text-slate-600">{s.flow_mode}</td>
                          <td className="px-3 py-2.5 space-y-0.5">
                            <div>
                              {s.start_frame_path ? <span className="text-emerald-700 font-medium">首帧已定</span> : <span className="text-slate-400">首帧待定</span>}
                            </div>
                            {showEnd && (
                              <div>
                                {s.end_frame_path ? <span className="text-violet-700 font-medium">尾帧已定</span> : <span className="text-violet-400">尾帧待定</span>}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2.5 space-x-1.5 whitespace-nowrap">
                            <button
                              type="button"
                              onClick={() => openShotEditor(s)}
                              className={`rounded border px-2 py-1 text-[11px] cursor-pointer ${
                                isEditing
                                  ? 'bg-sky-600 border-sky-600 text-white font-medium'
                                  : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100'
                              }`}
                            >
                              {isEditing ? '收起' : '编辑本镜'}
                            </button>
                            <button
                              onClick={async () => {
                                await copyToClipboard(s.compiled.compiled_frame_prompt);
                                notify('已复制首帧 Prompt');
                              }}
                              className="rounded bg-white border border-slate-200 px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 cursor-pointer"
                            >
                              首帧词
                            </button>
                            {showEnd && endPromptToCopy && (
                              <button
                                onClick={async () => {
                                  await copyToClipboard(endPromptToCopy);
                                  notify('已复制尾帧 Prompt');
                                }}
                                className="rounded bg-violet-50 border border-violet-200 px-2 py-1 text-[11px] text-violet-800 hover:bg-violet-100 cursor-pointer"
                              >
                                尾帧词
                              </button>
                            )}
                            <button
                              onClick={() => {
                                setActiveShotId(s.id);
                                setStep(4);
                              }}
                              className="rounded bg-sky-600 px-2 py-1 text-[11px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                            >
                              抽视频
                            </button>
                          </td>
                        </tr>
                        {isEditing && (
                          <tr className="bg-sky-50/30">
                            <td colSpan={6} className="p-3">
                              {renderShotEditor(s)}
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ==================== Step 4: 视频生成（Flow 快传抽卡） ==================== */}
      {step === 4 && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
          {/* 左列：分镜导航列表 */}
          <div className="xl:col-span-3 studio-card rounded-2xl overflow-hidden max-h-[75vh] flex flex-col">
            <div className="border-b border-slate-200 px-3.5 py-3 flex items-center justify-between bg-slate-50/60">
              <span className="text-xs font-semibold text-slate-900">分镜槽位 ({shots.length})</span>
              <a
                href="https://labs.google/fx/tools/flow"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[11px] font-medium text-sky-600 hover:underline"
              >
                打开 Flow <ExternalLink className="h-3 w-3" />
              </a>
            </div>
            <div className="flex-1 overflow-y-auto divide-y divide-slate-100">
              {shots.map((s) => {
                const hasFinal = s.takes.some((t) => t.media_type === 'video' && t.is_final === 1);
                const selected = activeShot?.id === s.id;
                return (
                  <button
                    key={s.id}
                    onClick={() => setActiveShotId(s.id)}
                    className={`w-full px-3.5 py-2.5 text-left transition flex items-center justify-between gap-2 cursor-pointer ${
                      selected ? 'bg-sky-50 border-l-2 border-sky-600' : 'hover:bg-slate-50'
                    }`}
                  >
                    <div className="min-w-0">
                      <div className={`text-xs font-semibold truncate ${selected ? 'text-sky-900' : 'text-slate-800'}`}>
                        S{String(s.shot_no).padStart(2, '0')} · {s.shot_title}
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5">
                        {s.duration_sec}s · {s.takes.length} 版抽卡
                      </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      {s.upscale_done === 1 && (
                        <span className="rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[9px] font-medium text-sky-700">1080p</span>
                      )}
                      <span className={`h-2 w-2 rounded-full ${hasFinal ? 'bg-emerald-500' : 'bg-slate-300'}`} />
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* 右列：当前镜头 Flow 提示词编译 + Downloads 一键认领 + 多版抽卡对比打星 */}
          <div className="xl:col-span-9 space-y-4">
            {renderOneShotWorkflowBanner()}
            {!activeShot ? (
              <div className="studio-card rounded-2xl py-16 text-center text-xs text-slate-400">请先在第 ③ 步拆分镜。</div>
            ) : (
              <>
                <div className="studio-card rounded-2xl p-4 space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <div className="text-sm font-semibold text-slate-900">
                        当前激活：S{String(activeShot.shot_no).padStart(2, '0')} · {activeShot.shot_title}（{activeShot.camera_type} · {activeShot.duration_sec}s）
                      </div>
                      <div className="text-xs text-slate-500 mt-0.5">{activeShot.cn_action_desc}</div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => openShotEditor(activeShot)}
                        className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition cursor-pointer ${
                          editingShotId === activeShot.id
                            ? 'bg-sky-600 border-sky-600 text-white'
                            : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-100'
                        }`}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        {editingShotId === activeShot.id ? '收起编辑' : '编辑本镜'}
                      </button>
                      <label className="inline-flex items-center gap-1.5 rounded-lg bg-slate-50 border border-slate-200 px-2.5 py-1.5 text-xs text-slate-700 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={activeShot.upscale_done === 1}
                          onChange={(e) => handlePatchShot(activeShot.id, { upscale_done: e.target.checked ? 1 : 0 })}
                          className="accent-emerald-600"
                        />
                        已下 Flow 免费 1080p（0分 · 4K走Colab）
                      </label>
                      <button
                        onClick={() => handleExtractLastFrame(activeShot.id)}
                        className="inline-flex items-center gap-1 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                      >
                        <Scissors className="h-3.5 w-3.5" /> 抽末帧 → 填下镜首帧
                      </button>
                    </div>
                  </div>

                  {editingShotId === activeShot.id && renderShotEditor(activeShot)}

                  {hasEndFrameFeature(activeShot) && (
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 rounded-xl border border-violet-200 bg-violet-50/35 p-3">
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between text-[11px]">
                          <span className="font-semibold text-amber-800">首帧 Start Frame</span>
                          <div className="flex items-center gap-2">
                            <label className="text-sky-700 font-medium hover:underline cursor-pointer">
                              上传首帧
                              <input
                                type="file"
                                accept="image/*"
                                className="hidden"
                                onChange={(e) => e.target.files?.[0] && handleUploadShotMedia(activeShot.id, 'start_frame', e.target.files[0])}
                              />
                            </label>
                            {activeShot.start_frame_path && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => copyToDockDownloads(activeShot.start_frame_path, buildShotFrameChineseStem(activeShot, 'start'))}
                                  title="复制中文短名副本到直拖素材目录"
                                  className="text-emerald-700 font-medium hover:underline cursor-pointer"
                                >
                                  拷到直拖
                                </button>
                                <button
                                  type="button"
                                  onClick={() => revealFileLocation(activeShot.start_frame_path)}
                                  className="text-slate-600 hover:text-slate-900 hover:underline cursor-pointer"
                                >
                                  目录
                                </button>
                              </>
                            )}
                          </div>
                        </div>
                        <div className={`h-36 rounded-lg overflow-hidden border flex items-center justify-center ${activeShot.start_frame_path ? 'bg-slate-900 border-slate-200' : 'bg-white border-dashed border-slate-300'}`}>
                          {activeShot.start_frame_path ? (
                            <img src={mediaUrl(activeShot.start_frame_path)} alt="首帧" className="h-full w-full object-contain" />
                          ) : (
                            <span className="text-[11px] text-slate-400">未上传首帧（可下方收件箱一键认领）</span>
                          )}
                        </div>
                      </div>

                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between text-[11px]">
                          <span className="font-semibold text-violet-800">尾帧 End Frame（Frames to Video）</span>
                          <div className="flex items-center gap-2">
                            <label className="text-violet-700 font-medium hover:underline cursor-pointer">
                              上传尾帧
                              <input
                                type="file"
                                accept="image/*"
                                className="hidden"
                                onChange={(e) => e.target.files?.[0] && handleUploadShotMedia(activeShot.id, 'end_frame', e.target.files[0])}
                              />
                            </label>
                            {activeShot.end_frame_path && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => copyToDockDownloads(activeShot.end_frame_path, buildShotFrameChineseStem(activeShot, 'end'))}
                                  title="复制中文短名副本到直拖素材目录"
                                  className="text-emerald-700 font-medium hover:underline cursor-pointer"
                                >
                                  拷到直拖
                                </button>
                                <button
                                  type="button"
                                  onClick={() => revealFileLocation(activeShot.end_frame_path)}
                                  className="text-slate-600 hover:text-slate-900 hover:underline cursor-pointer"
                                >
                                  目录
                                </button>
                              </>
                            )}
                          </div>
                        </div>
                        <div className={`h-36 rounded-lg overflow-hidden border flex items-center justify-center ${activeShot.end_frame_path ? 'bg-slate-900 border-violet-200' : 'bg-white border-dashed border-violet-300'}`}>
                          {activeShot.end_frame_path ? (
                            <img src={mediaUrl(activeShot.end_frame_path)} alt="尾帧" className="h-full w-full object-contain" />
                          ) : (
                            <span className="text-[11px] text-violet-500">未上传尾帧（可下方收件箱认领为尾帧图）</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {/* 编译后的英文 Flow 视频提示词 + 官方 CLI 直出区 */}
                  <div className="rounded-xl bg-slate-50 border border-slate-200 p-3 space-y-2.5">
                    {renderFlowBatchControls()}
                    {renderCliMediaControlBar(false)}
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2">
                      <div className="text-[11px] text-emerald-900">
                        <span className="font-semibold">一键 CLI 自动直出</span>：自动带入本镜
                        {activeShot.start_frame_path && activeShot.end_frame_path
                          ? '首尾双帧'
                          : activeShot.start_frame_path
                            ? '首帧图'
                            : '绑定资产图'}
                        ，跑完自动落库 Take 墙（不占 Flow 积分 · 约 <strong>${estimateCliVideoCost(activeShot.duration_sec)}</strong>）
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <button
                          type="button"
                          disabled={cliBusyKey !== null}
                          onClick={() => handleCliGenerateShotFrame(activeShot, 'start_frame')}
                          className="inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-white px-2.5 py-1 text-[11px] font-medium text-indigo-700 hover:bg-indigo-600 hover:text-white disabled:opacity-45 cursor-pointer"
                        >
                          {cliBusyKey === `frame-${activeShot.id}-start_frame` ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <Wand2 className="h-3 w-3" />
                          )}
                          CLI 生首帧
                        </button>
                        {hasEndFrameFeature(activeShot) && (
                          <button
                            type="button"
                            disabled={
                              cliBusyKey !== null ||
                              !(activeShot.compiled.compiled_end_frame_prompt || activeShot.end_frame_prompt_en)
                            }
                            onClick={() => handleCliGenerateShotFrame(activeShot, 'end_frame')}
                            className="inline-flex items-center gap-1 rounded-md border border-violet-200 bg-white px-2.5 py-1 text-[11px] font-medium text-violet-700 hover:bg-violet-600 hover:text-white disabled:opacity-45 cursor-pointer"
                          >
                            {cliBusyKey === `frame-${activeShot.id}-end_frame` ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <Wand2 className="h-3 w-3" />
                            )}
                            CLI 生尾帧
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={cliBusyKey !== null}
                          onClick={() => handleCliGenerateShotVideo(activeShot)}
                          className="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700 disabled:opacity-45 cursor-pointer shadow-2xs"
                        >
                          {cliBusyKey === `video-${activeShot.id}` ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <Film className="h-3.5 w-3.5" />
                          )}
                          {cliBusyKey === `video-${activeShot.id}`
                            ? `${cliVideoModel} (${cliVideoRes}) 生成轮询中...`
                            : `CLI 自动生视频 (${cliVideoRes}×${cliVideoCount} · $${estimateCliVideoCost(activeShot.duration_sec)})`}
                        </button>
                      </div>
                    </div>
                    {renderShotFrameCandidatePicker(activeShot, 'start_frame')}
                    {renderShotFrameCandidatePicker(activeShot, 'end_frame')}
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] font-semibold text-sky-800">
                        已编译 Flow 英文视频 Prompt（自动带全局影调 + 资产锁词 + 普通话锁 + 禁乱塞 BGM）
                      </span>
                      <div className="flex flex-wrap gap-1.5">
                        <button
                          onClick={async () => {
                            await copyToClipboard(activeShot.compiled.compiled_frame_prompt);
                            reportInboxContext(buildShotFrameChineseStem(activeShot, 'start'));
                            notify('已复制首帧英文 Prompt（新下载图将自动按本镜中文名命名）');
                          }}
                          className="inline-flex items-center gap-1 rounded bg-white border border-slate-200 px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100 cursor-pointer"
                        >
                          <Copy className="h-3 w-3" /> 复制首帧词
                        </button>
                        {hasEndFrameFeature(activeShot) && (
                          <button
                            type="button"
                            disabled={!(activeShot.compiled.compiled_end_frame_prompt || activeShot.end_frame_prompt_en)}
                            onClick={async () => {
                              const ep = activeShot.compiled.compiled_end_frame_prompt || activeShot.end_frame_prompt_en || '';
                              if (!ep) return;
                              await copyToClipboard(ep);
                              reportInboxContext(buildShotFrameChineseStem(activeShot, 'end'));
                              notify('已复制尾帧英文 Prompt（新下载图将自动按本镜尾帧中文名命名）');
                            }}
                            title={
                              activeShot.compiled.compiled_end_frame_prompt || activeShot.end_frame_prompt_en
                                ? '复制已编译的尾帧英文 Prompt'
                                : '请先点「编辑本镜」填写尾帧英文 Prompt'
                            }
                            className="inline-flex items-center gap-1 rounded bg-violet-50 border border-violet-200 px-2.5 py-1 text-[11px] font-medium text-violet-800 hover:bg-violet-100 disabled:opacity-45 cursor-pointer"
                          >
                            <Copy className="h-3 w-3" /> 复制尾帧 Prompt
                          </button>
                        )}
                        <button
                          onClick={async () => {
                            await copyToClipboard(activeShot.compiled.compiled_video_prompt);
                            const shotNo = String(activeShot.shot_no).padStart(2, '0');
                            reportInboxContext(`S${shotNo}视频`);
                            notify(`已复制 S${shotNo} 视频英文 Prompt（新下载视频自动命名为 待选_S${shotNo}视频，请只下免费 1080p）`);
                          }}
                          className="inline-flex items-center gap-1 rounded bg-sky-600 px-3 py-1 text-[11px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                        >
                          <Copy className="h-3 w-3" /> 复制视频 Prompt
                        </button>
                      </div>
                    </div>
                    {activeShot.compiled.flow_entry_guide && (
                      <div className="rounded-lg border border-sky-200 bg-sky-50 px-2.5 py-1.5 text-[11px] text-sky-900">
                        🎯 官方 Flow 操作指引：{activeShot.compiled.flow_entry_guide}
                      </div>
                    )}
                    <p className="font-mono text-xs leading-relaxed text-slate-800 bg-white border border-slate-200 rounded-lg p-2.5 select-all">
                      {activeShot.compiled.compiled_video_prompt}
                    </p>
                    {activeShot.compiled.compiled_end_frame_prompt && (
                      <div className="rounded-lg border border-violet-200 bg-violet-50/50 p-2 space-y-1">
                        <div className="text-[10px] font-semibold text-violet-800">已编译尾帧英文 Prompt（Nano Banana 尾帧定妆用）</div>
                        <p className="font-mono text-[11px] leading-relaxed text-slate-700 select-all">
                          {activeShot.compiled.compiled_end_frame_prompt}
                        </p>
                      </div>
                    )}
                    {activeShot.compiled.warnings.map((w, i) => (
                      <div key={i} className="text-xs text-amber-800">⚠️ {w}</div>
                    ))}
                    {activeShot.compiled.quick_edit_prompts && activeShot.compiled.quick_edit_prompts.length > 0 && (
                      <div className="pt-2 border-t border-slate-200 space-y-1.5">
                        <div className="text-[10px] text-slate-500">
                          Omni 1.2 Flash 时间线单镜微调急救词（1次只改1个目标，比整镜重抽省分）：
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {activeShot.compiled.quick_edit_prompts.map((qe, idx) => (
                            <button
                              key={idx}
                              type="button"
                              onClick={async () => {
                                await copyToClipboard(qe.prompt);
                                notify(`已复制微调词「${qe.label}」，点 Flow 场景里该镜头底部输入框粘贴即可`);
                              }}
                              className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[10px] text-slate-700 hover:border-violet-300 hover:bg-violet-50 hover:text-violet-700 cursor-pointer"
                            >
                              <Copy className="h-2.5 w-2.5" /> {qe.label}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* 本镜需要拖进官方 Flow 的参考图快捷条 */}
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="text-slate-500">本镜参考图：</span>
                    {activeShot.start_frame_path && (
                      <div className="inline-flex items-center rounded-md border border-amber-200 bg-amber-50 overflow-hidden">
                        <button
                          onClick={() => revealFileLocation(activeShot.start_frame_path)}
                          className="inline-flex items-center gap-1 px-2 py-1 font-medium text-amber-800 hover:bg-amber-100 cursor-pointer"
                        >
                          <FolderOpen className="h-3 w-3" /> 首帧图
                        </button>
                        <button
                          type="button"
                          onClick={() => copyToDockDownloads(activeShot.start_frame_path, buildShotFrameChineseStem(activeShot, 'start'))}
                          title="复制中文短名副本到直拖素材目录"
                          className="inline-flex items-center gap-0.5 border-l border-amber-200 px-1.5 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-50 cursor-pointer"
                        >
                          <Download className="h-3 w-3" /> 拷到直拖
                        </button>
                      </div>
                    )}
                    {activeShot.end_frame_path && (
                      <div className="inline-flex items-center rounded-md border border-violet-200 bg-violet-50 overflow-hidden">
                        <button
                          onClick={() => revealFileLocation(activeShot.end_frame_path)}
                          className="inline-flex items-center gap-1 px-2 py-1 font-medium text-violet-800 hover:bg-violet-100 cursor-pointer"
                        >
                          <FolderOpen className="h-3 w-3" /> 尾帧图
                        </button>
                        <button
                          type="button"
                          onClick={() => copyToDockDownloads(activeShot.end_frame_path, buildShotFrameChineseStem(activeShot, 'end'))}
                          title="复制中文短名副本到直拖素材目录"
                          className="inline-flex items-center gap-0.5 border-l border-violet-200 px-1.5 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-50 cursor-pointer"
                        >
                          <Download className="h-3 w-3" /> 拷到直拖
                        </button>
                      </div>
                    )}
                    {activeShot.compiled.ref_images.map((r) => (
                      <div
                        key={r.asset_id}
                        className={`inline-flex items-center rounded-md border border-slate-200 bg-slate-100 overflow-hidden ${!r.image_path ? 'opacity-45' : ''}`}
                      >
                        <button
                          disabled={!r.image_path}
                          onClick={() => revealFileLocation(r.image_path)}
                          className="inline-flex items-center gap-1 px-2 py-1 text-slate-700 hover:bg-slate-200/70 cursor-pointer"
                        >
                          <FolderOpen className="h-3 w-3" /> {r.flow_tag} {r.image_path ? '' : '(未传图)'}
                        </button>
                        {r.image_path && (
                          <button
                            type="button"
                            onClick={() => copyToDockDownloads(r.image_path, buildAssetChineseStem({ id: r.asset_id, asset_type: r.asset_type, name: r.name }, 'clean'))}
                            title={`复制「${buildAssetChineseStem({ id: r.asset_id, asset_type: r.asset_type, name: r.name }, 'clean')}」到直拖素材目录（≤6字短名不截断）`}
                            className="inline-flex items-center gap-0.5 border-l border-slate-200 bg-white px-1.5 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-50 cursor-pointer"
                          >
                            <Download className="h-3 w-3" />
                          </button>
                        )}
                      </div>
                    ))}
                    <label className="ml-auto inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-slate-700 hover:bg-slate-100 cursor-pointer">
                      <Upload className="h-3 w-3" /> 手动上传视频 Take
                      <input
                        type="file"
                        accept="video/*"
                        className="hidden"
                        onChange={(e) => e.target.files?.[0] && handleUploadShotMedia(activeShot.id, 'video', e.target.files[0])}
                      />
                    </label>
                  </div>
                </div>

                {/* 内嵌 Downloads 快捷认领条 */}
                <div className="studio-card rounded-2xl p-3.5 border-sky-200 space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-sky-800 inline-flex items-center gap-1.5">
                      <Download className="h-3.5 w-3.5 text-sky-600" /> 下载目录最新落盘一键认领（Flow 下载 UUID 文件秒归位）
                    </span>
                    <div className="flex flex-wrap items-center gap-3">
                      <label
                        className="inline-flex items-center gap-1.5 text-[11px] text-slate-700 cursor-pointer"
                        title="认领首尾帧/资产图归档后，自动以中文短名同步进直拖素材目录，同批落选废图自动进回收站"
                      >
                        <input
                          type="checkbox"
                          checked={keepChineseCopy}
                          onChange={(e) => onToggleKeepChineseCopy(e.target.checked)}
                          className="accent-sky-600"
                        />
                        认领自动同步直拖素材目录（中文短名+清废图）
                      </label>
                      <button
                        onClick={onRefreshInbox}
                        className="inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-900 cursor-pointer"
                      >
                        <RefreshCw className="h-3 w-3" /> 扫描近 30 分钟
                      </button>
                    </div>
                  </div>
                  {inbox.items.length === 0 ? (
                    <div className="text-xs text-slate-400">在官方 Flow 网页点下载，新文件会自动出现在这里让你一键认领进 S{String(activeShot.shot_no).padStart(2, '0')}。</div>
                  ) : (
                    <div className="flex gap-2.5 overflow-x-auto pb-1">
                      {inbox.items.slice(0, 6).map((item) => (
                        <div key={item.id} className="shrink-0 w-60 rounded-xl bg-slate-50 border border-slate-200 p-2.5 space-y-1.5">
                          <div className="flex items-center justify-between gap-1">
                            <div className="truncate text-[11px] font-medium text-slate-900" title={item.filename}>
                              {item.filename}
                            </div>
                            <div className="flex items-center gap-1 shrink-0">
                              <button
                                type="button"
                                onClick={() => {
                                  if (renamingInboxId === item.id) {
                                    setRenamingInboxId(null);
                                  } else {
                                    const dot = item.filename.lastIndexOf('.');
                                    setRenamingInboxId(item.id);
                                    setRenamingInboxVal(dot > 0 ? item.filename.slice(0, dot) : item.filename);
                                  }
                                }}
                                title="原地改为中文名（直接重命名下载监听目录中的文件）"
                                className="inline-flex items-center gap-0.5 rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-600 hover:border-sky-300 hover:text-sky-700 cursor-pointer"
                              >
                                <Pencil className="h-2.5 w-2.5" /> 改名
                              </button>
                              <button
                                type="button"
                                onClick={() => onTrashInboxItem(item.id)}
                                title="不要这张：移入系统废纸篓/回收站（随时可恢复）"
                                className="rounded p-0.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
                              >
                                <Trash2 className="h-3 w-3" />
                              </button>
                            </div>
                          </div>
                          {renamingInboxId === item.id && (
                            <div className="flex items-center gap-1">
                              <input
                                autoFocus
                                value={renamingInboxVal}
                                onFocus={(e) => e.target.select()}
                                onChange={(e) => setRenamingInboxVal(e.target.value)}
                                onKeyDown={async (e) => {
                                  if (e.key === 'Enter' && renamingInboxVal.trim()) {
                                    e.preventDefault();
                                    await onRenameInboxItem(item.id, renamingInboxVal.trim());
                                    setRenamingInboxId(null);
                                  } else if (e.key === 'Escape') {
                                    setRenamingInboxId(null);
                                  }
                                }}
                                placeholder="输入中文名"
                                className="studio-input flex-1 min-w-0 rounded px-1.5 py-0.5 text-[10px]"
                              />
                              <button
                                type="button"
                                onClick={async () => {
                                  if (!renamingInboxVal.trim()) return;
                                  await onRenameInboxItem(item.id, renamingInboxVal.trim());
                                  setRenamingInboxId(null);
                                }}
                                className="rounded bg-sky-600 px-1.5 py-0.5 text-[10px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                              >
                                确定
                              </button>
                            </div>
                          )}
                          <div className="text-[10px] text-slate-500">
                            {item.media_type} · {item.resolution_label || '未知'} {item.duration_sec ? `· ${item.duration_sec}s` : ''}
                          </div>
                          <div className="flex gap-1">
                            {item.media_type === 'video' && (
                              <button
                                onClick={() => handleClaimInbox(item, 'shot_video', activeShot.id)}
                                className="flex-1 rounded bg-sky-600 py-1 text-[10px] font-semibold text-white hover:bg-sky-700 cursor-pointer"
                              >
                                认领为本镜 Take
                              </button>
                            )}
                            {item.media_type === 'image' && (
                              <>
                                <button
                                  onClick={() => handleClaimInbox(item, 'shot_start_frame', activeShot.id)}
                                  className="flex-1 rounded bg-amber-500 py-1 text-[10px] font-semibold text-white hover:bg-amber-600 cursor-pointer"
                                >
                                  认领为首帧图
                                </button>
                                <button
                                  onClick={() => handleClaimInbox(item, 'shot_end_frame', activeShot.id)}
                                  className="flex-1 rounded bg-violet-600 py-1 text-[10px] font-semibold text-white hover:bg-violet-700 cursor-pointer"
                                >
                                  认领为尾帧图
                                </button>
                              </>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 多版抽卡并排对比打星 */}
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {activeShot.takes.map((t: Take, idx) => (
                    <div
                      key={t.id}
                      className={`studio-card rounded-2xl p-3 space-y-2.5 ${
                        t.is_final ? 'border-emerald-400 ring-2 ring-emerald-100' : ''
                      }`}
                    >
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-semibold text-slate-900">
                          Take #{activeShot.takes.length - idx}
                          <span className="ml-1.5 rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
                            {t.resolution_label || 'video'} · {t.duration_sec}s
                          </span>
                        </span>
                        {t.is_final === 1 && (
                          <span className="inline-flex items-center gap-0.5 rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
                            <Check className="h-3 w-3" /> 定稿
                          </span>
                        )}
                      </div>
                      <video src={mediaUrl(t.file_path)} controls className="w-full max-h-60 rounded-xl bg-slate-900" />
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-0.5">
                          {[1, 2, 3, 4, 5].map((st) => (
                            <button
                              key={st}
                              onClick={async () => {
                                const res = await api.patch<{ shots: Shot[]; budget: BudgetSummary }>(`/api/takes/${t.id}`, {
                                  rating: st,
                                });
                                setShots(res.shots);
                                setBudget(res.budget);
                              }}
                              className="cursor-pointer"
                            >
                              <Star
                                className={`h-3.5 w-3.5 ${st <= t.rating ? 'fill-amber-400 text-amber-500' : 'text-slate-300'}`}
                              />
                            </button>
                          ))}
                        </div>
                        <div className="flex items-center gap-1.5">
                          {t.is_final !== 1 && (
                            <button
                              onClick={async () => {
                                const res = await api.patch<{ shots: Shot[]; budget: BudgetSummary }>(`/api/takes/${t.id}`, {
                                  is_final: 1,
                                });
                                setShots(res.shots);
                                setBudget(res.budget);
                                notify('已设为本镜定稿');
                              }}
                              className="rounded bg-emerald-600 px-2 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700 cursor-pointer"
                            >
                              设为定稿
                            </button>
                          )}
                          <button
                            onClick={async () => {
                              const res = await api.del<{ shots: Shot[]; budget: BudgetSummary }>(`/api/takes/${t.id}`);
                              setShots(res.shots);
                              setBudget(res.budget);
                            }}
                            className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                  {activeShot.takes.length === 0 && (
                    <div className="col-span-full studio-card rounded-2xl py-12 text-center text-xs text-slate-400">
                      这一镜还没有认领抽卡视频：复制上方英文 Prompt 去官方 Flow 生成，下载后在上方一键认领即可并排打星对比。
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ==================== Step 5: 连播时间线预演 ==================== */}
      {step === 5 && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
          <div className="xl:col-span-5 studio-card rounded-2xl p-4 flex flex-col items-center space-y-3">
            <div className="w-full flex items-center justify-between text-xs">
              <span className="font-semibold text-slate-900">整集连播预演器</span>
              <button
                onClick={startSequentialPlay}
                disabled={timelineItems.length === 0}
                className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-40 cursor-pointer"
              >
                <Play className="h-3.5 w-3.5" /> 从第 1 镜连播预演
              </button>
            </div>

            <div className="relative w-full max-w-[300px] aspect-[9/16] rounded-2xl overflow-hidden bg-slate-900 border border-slate-200 flex items-center justify-center shadow-sm">
              {timelineItems[playingIdx] ? (
                <>
                  <video
                    ref={videoRef}
                    key={timelineItems[playingIdx].file_path}
                    src={mediaUrl(timelineItems[playingIdx].file_path)}
                    controls
                    onEnded={handleVideoEnded}
                    className="h-full w-full object-contain"
                  />
                  {(timelineItems[playingIdx].subtitle_text || timelineItems[playingIdx].cn_dialogue) && (
                    <div className="pointer-events-none absolute bottom-12 inset-x-3 text-center">
                      <span className="inline-block rounded-lg bg-black/75 px-3 py-1 text-xs font-medium text-white shadow">
                        {timelineItems[playingIdx].subtitle_text || timelineItems[playingIdx].cn_dialogue}
                      </span>
                    </div>
                  )}
                </>
              ) : (
                <div className="text-xs text-slate-400 px-4 text-center">请先在第 ④ 步认领并定稿视频镜头</div>
              )}
            </div>

            {activeEp?.bgm_file_path && (
              <audio ref={bgmRef} src={mediaUrl(activeEp.bgm_file_path)} controls className="w-full h-8" />
            )}
          </div>

          <div className="xl:col-span-7 studio-card rounded-2xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-sm font-semibold text-slate-900">
                  有序定稿时间线（已就绪 {timelineItems.length} 镜 · 总长 {timelineTotalSec.toFixed(1)}s）
                </div>
                {renderSixtySecBadge(timelineItems.length > 0 ? timelineTotalSec : (budget?.total_duration_sec || 0))}
              </div>
              {missingShots.length > 0 && (
                <span className="text-xs font-medium text-amber-700">缺定稿镜号：{missingShots.map((n) => `S${n}`).join(', ')}</span>
              )}
            </div>

            <div className="space-y-2 max-h-[62vh] overflow-y-auto pr-1">
              {shots.map((s, idx) => {
                const tl = timelineItems.find((x) => x.shot_id === s.id);
                return (
                  <div
                    key={s.id}
                    className={`rounded-xl border p-3 flex flex-wrap items-center gap-3 ${
                      tl && timelineItems[playingIdx]?.shot_id === s.id
                        ? 'border-sky-400 bg-sky-50/70'
                        : 'border-slate-200 bg-slate-50/60'
                    }`}
                  >
                    <div className="flex flex-col gap-0.5">
                      <button
                        onClick={() => handleMoveShot(idx, -1)}
                        disabled={idx === 0}
                        className="rounded p-0.5 text-slate-500 hover:bg-slate-200 disabled:opacity-25 cursor-pointer"
                      >
                        <ArrowUp className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => handleMoveShot(idx, 1)}
                        disabled={idx === shots.length - 1}
                        className="rounded p-0.5 text-slate-500 hover:bg-slate-200 disabled:opacity-25 cursor-pointer"
                      >
                        <ArrowDown className="h-3.5 w-3.5" />
                      </button>
                    </div>

                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex items-center gap-2 text-xs">
                        <span className="font-mono font-bold text-sky-700">S{String(s.shot_no).padStart(2, '0')}</span>
                        <span className="font-medium text-slate-900">{s.shot_title}</span>
                        {tl ? (
                          <span className="rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                            {tl.resolution_label} · {tl.effective_duration}s
                          </span>
                        ) : (
                          <span className="rounded bg-slate-200/80 px-1.5 py-0.5 text-[10px] text-slate-500">未定稿</span>
                        )}
                      </div>
                      <input
                        defaultValue={s.subtitle_text}
                        onBlur={(e) => handlePatchShot(s.id, { subtitle_text: e.target.value })}
                        placeholder="本镜字幕文案（失焦自动保存）"
                        className="studio-input w-full rounded px-2.5 py-1 text-xs"
                      />
                    </div>

                    <div className="flex items-center gap-1.5 text-[11px]">
                      <div>
                        <span className="text-slate-500 block">起(s)</span>
                        <input
                          type="number"
                          step="0.5"
                          defaultValue={s.trim_start}
                          onBlur={(e) => handlePatchShot(s.id, { trim_start: Number(e.target.value) })}
                          className="studio-input w-14 rounded px-1.5 py-1 text-xs"
                        />
                      </div>
                      <div>
                        <span className="text-slate-500 block">止(s)</span>
                        <input
                          type="number"
                          step="0.5"
                          defaultValue={s.trim_end}
                          onBlur={(e) => handlePatchShot(s.id, { trim_end: Number(e.target.value) })}
                          className="studio-input w-14 rounded px-1.5 py-1 text-xs"
                        />
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* ==================== Step 6: 合成与导出（三大主力出口 + 封面 + 可选协作推送） ==================== */}
      {step === 6 && (
        <div className="space-y-5">
          {/* 顶部 60s 短剧时长检查 + 一键导出 AI 原创制作证明 */}
          <div className="studio-card rounded-2xl px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2.5 text-xs text-slate-700">
              <span className="font-semibold text-slate-900">
                {activeEp ? `第 ${activeEp.ep_number} 集《${activeEp.title}》` : '本集交付自检'}
              </span>
              <span>
                已定稿 <strong className="text-slate-900">{timelineItems.length}/{shots.length}</strong> 镜 ·
                {timelineItems.length > 0
                  ? ` 定稿总长 ${timelineTotalSec.toFixed(1)}s`
                  : ` 分镜规划 ${budget?.total_duration_sec ?? 0}s`}
              </span>
              {renderSixtySecBadge(timelineItems.length > 0 ? timelineTotalSec : (budget?.total_duration_sec || 0))}
            </div>
            <button
              type="button"
              onClick={() => runDeliveryAction('ai_proof')}
              disabled={rendering || !activeEpId}
              title="归档本集剧本、资产定妆图、逐镜中英文 Prompt 与模型记录，作为 AI 原创合规核验材料"
              className="inline-flex items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs font-semibold text-sky-800 hover:bg-sky-100 disabled:opacity-40 cursor-pointer"
            >
              <FileCheck2 className="h-3.5 w-3.5 text-sky-600" />
              导出 AI 原创制作证明 (.md)
            </button>
          </div>

          {unupscaledShots.length > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900 flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
              提醒：镜号 {unupscaledShots.map((n) => `S${n}`).join(', ')} 当前定稿分辨率还是 360p/720p，建议先去官方 Flow 免费 1080p 超分后再出终版！
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* 出口 A：Google Colab 云端 GPU（4K HDR 超分 + 补帧） */}
            <div className="studio-card rounded-2xl p-5 border-violet-200 flex flex-col justify-between space-y-4">
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-violet-800 inline-flex items-center gap-1.5">
                    <Cloud className="h-4 w-4 text-violet-600" /> 出口 A：Colab 云端 GPU（4K HDR）
                  </span>
                  <button
                    onClick={async () => {
                      try {
                        setProbeColab(await api.get<ProbeColab>('/api/settings/probe-colab'));
                      } catch (err: any) {
                        notify(err.message, 'err');
                      }
                    }}
                    className="text-[11px] font-medium text-violet-700 hover:underline cursor-pointer shrink-0"
                  >
                    检测 CU 额度
                  </button>
                </div>
                <p className="text-xs text-slate-600 leading-relaxed">
                  直连 Google Colab 官方 GPU，跑<strong className="text-slate-900">真 AI 4K 超分（Real-ESRGAN v3）+ 补帧（RIFE v4.26）</strong>，替代 Flow 50分/镜 4K 烧分。跑完<strong className="text-slate-900">自动销毁释放 VM</strong>。
                </p>
                {probeColab && (
                  <div className={`text-[11px] rounded-lg border p-2 ${probeColab.authorized ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                    {probeColab.message}
                  </div>
                )}
                <div className="space-y-2 pt-1 text-xs">
                  <div className="flex items-center gap-1.5">
                    <span className="text-slate-600 shrink-0">卡型：</span>
                    <select
                      value={colabGpuTier}
                      onChange={(e) => setColabGpuTier(e.target.value)}
                      className="studio-input flex-1 rounded px-2 py-1 text-xs"
                    >
                      <option value="L4">L4（默认 · 秒开省 CU）</option>
                      <option value="A100">A100（忙时自动降 L4）</option>
                      <option value="H100">H100（忙时自动降 A100/L4）</option>
                      <option value="T4">T4（最省 CU）</option>
                      <option value="CPU">CPU（仅自检）</option>
                    </select>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-slate-600 shrink-0">模式：</span>
                    <select
                      value={colabTaskMode}
                      onChange={(e) =>
                        setColabTaskMode(e.target.value as 'upscale_4k_30fps' | 'upscale_4k_60fps' | 'ai_60fps_1080p')
                      }
                      className="studio-input flex-1 rounded px-2 py-1 text-xs"
                    >
                      <option value="upscale_4k_30fps">真 AI 4K/30fps HLG HDR（推荐电影感）</option>
                      <option value="upscale_4k_60fps">真 AI 4K + 60fps（精修补帧母带）</option>
                      <option value="ai_60fps_1080p">真 AI 1080p 60fps（极速丝滑）</option>
                    </select>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  <label className="inline-flex items-center gap-1.5 text-xs text-slate-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={colabHdrHlg}
                      onChange={(e) => setColabHdrHlg(e.target.checked)}
                      className="accent-violet-600"
                    />
                    10-bit HLG HDR
                  </label>
                  <label className="inline-flex items-center gap-1.5 text-xs text-slate-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={colabBurnSubtitles}
                      onChange={(e) => setColabBurnSubtitles(e.target.checked)}
                      className="accent-violet-600"
                    />
                    烧录黄金安全区字幕
                  </label>
                </div>
              </div>
              <div className="space-y-2">
                <button
                  onClick={() => runDeliveryAction('colab_4k60')}
                  disabled={rendering || timelineItems.length === 0}
                  className="w-full rounded-lg bg-violet-600 py-2.5 text-xs font-semibold text-white hover:bg-violet-700 disabled:opacity-40 cursor-pointer"
                >
                  {rendering
                    ? 'Colab 云端 AI 处理中...'
                    : colabTaskMode === 'upscale_4k_30fps'
                      ? `派发 Colab (${colabGpuTier}) 4K/30fps ${colabHdrHlg ? 'HLG HDR ' : ''}母带`
                      : colabTaskMode === 'ai_60fps_1080p'
                        ? `派发 Colab (${colabGpuTier}) 1080p/60fps 补帧`
                        : `派发 Colab (${colabGpuTier}) 4K/60fps ${colabHdrHlg ? 'HLG HDR ' : ''}母带`}
                </button>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    onClick={() => runDeliveryAction('colab_probe')}
                    disabled={rendering}
                    className="rounded-lg border border-violet-200 bg-violet-50 py-1.5 text-xs font-medium text-violet-700 hover:bg-violet-100 disabled:opacity-40 cursor-pointer"
                  >
                    GPU 冒烟测通
                  </button>
                  <button
                    onClick={() => runDeliveryAction('colab_pack')}
                    disabled={rendering || timelineItems.length === 0}
                    className="rounded-lg border border-slate-200 bg-white py-1.5 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-40 cursor-pointer"
                  >
                    导出轻量清单包
                  </button>
                </div>
              </div>
            </div>

            {/* 出口 B：本机 1080p 三轨 / 10-bit HLG HDR 母带 */}
            <div className="studio-card rounded-2xl p-5 border-sky-200 flex flex-col justify-between space-y-4">
              <div className="space-y-2">
                <div className="text-sm font-semibold text-sky-800 inline-flex items-center gap-1.5">
                  <Film className="h-4 w-4 text-sky-600" /> 出口 B：本机 1080p 三轨 / HLG HDR
                </div>
                <p className="text-xs text-slate-600 leading-relaxed">
                  自动探测本机硬件编码器（Apple VideoToolbox / NVIDIA NVENC / 通用软件回退），本地一键合成真 1080p 三轨混音（原生音 + VO 旁白 + BGM 侧链闪避），支持直出 10-bit HLG HDR 与底部 25% 黄金安全区中文字幕。
                </p>
                <div className="flex flex-wrap items-center gap-3 pt-1">
                  <label className="inline-flex items-center gap-1.5 text-xs text-slate-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={localHdrHlg}
                      onChange={(e) => setLocalHdrHlg(e.target.checked)}
                      className="accent-sky-600"
                    />
                    启用 10-bit HLG HDR
                  </label>
                  <label className="inline-flex items-center gap-1.5 text-xs text-slate-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={localBurnSubtitles}
                      onChange={(e) => setLocalBurnSubtitles(e.target.checked)}
                      className="accent-sky-600"
                    />
                    烧录黄金安全区字幕
                  </label>
                </div>
              </div>
              <div className="space-y-2">
                <button
                  onClick={() => runDeliveryAction('local_hdr')}
                  disabled={rendering || timelineItems.length === 0}
                  className="w-full rounded-lg bg-sky-600 py-2.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-40 cursor-pointer"
                >
                  {rendering ? '合成中...' : '本机直出 1080p HLG HDR 母带'}
                </button>
                <button
                  onClick={() => runDeliveryAction('local')}
                  disabled={rendering || timelineItems.length === 0}
                  className="w-full rounded-lg border border-sky-200 bg-sky-50 py-1.5 text-xs font-semibold text-sky-800 hover:bg-sky-100 disabled:opacity-40 cursor-pointer"
                >
                  {rendering ? '拼接中...' : localHdrHlg ? '按勾选参数渲染' : '本机 1080p SDR 初审'}
                </button>
              </div>
            </div>

            {/* 出口 C：.fcpxml + .srt 进剪映精修 + 手动 Flow 发车包 + 清理废片 */}
            <div className="studio-card rounded-2xl p-5 flex flex-col justify-between space-y-4">
              <div className="space-y-2">
                <div className="text-sm font-semibold text-amber-800 inline-flex items-center gap-1.5">
                  <Clapperboard className="h-4 w-4 text-amber-600" /> 出口 C：导出 .fcpxml 进剪映 & 发车包
                </div>
                <p className="text-xs text-slate-600 leading-relaxed">
                  支持一键导出三轨 <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-800">.fcpxml</code> + <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-800">.srt</code> 进剪映/Final Cut 精修，或生成编号首尾帧 + <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-800">逐镜Flow提示词清单.md</code> 手动 Flow 发车包。
                </p>
              </div>
              <div className="space-y-2">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  <button
                    onClick={() => runDeliveryAction('fcpxml')}
                    disabled={rendering || timelineItems.length === 0}
                    className="rounded-lg bg-amber-500 py-2 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-40 cursor-pointer"
                  >
                    导出 .fcpxml + .srt
                  </button>
                  <button
                    type="button"
                    onClick={handlePrepareManualFlowPack}
                    disabled={flowPackBusy || !activeEpId}
                    className="inline-flex items-center justify-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 py-2 text-xs font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-40 cursor-pointer"
                  >
                    <Package className="h-3.5 w-3.5" />
                    {flowPackBusy ? '打包中...' : '手动 Flow 发车包'}
                  </button>
                </div>
                <button
                  onClick={() => runDeliveryAction('cleanup')}
                  disabled={rendering}
                  className="w-full rounded-lg border border-rose-200 bg-rose-50 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-100 cursor-pointer"
                >
                  清理本集未定稿本地临时废片（保定稿与成片）
                </button>
              </div>
            </div>
          </div>

          {/* 双比例自动封面（9:16 + 3:4） & 仅开启远程协作时展示推送卡 */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
            <div className={`${collabSyncEnabled ? 'lg:col-span-7' : 'lg:col-span-12'} studio-card rounded-2xl p-5 border-violet-200 space-y-3`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold text-violet-900 inline-flex items-center gap-1.5">
                  <ImageIcon className="h-4 w-4 text-violet-600" /> 双比例自动封面生成（抖音/视频号 9:16 + 小红书 3:4）
                </div>
                <div className="flex items-center gap-2">
                  {coverSheetPath && (
                    <button
                      type="button"
                      onClick={() => revealFileLocation(coverSheetPath)}
                      className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100 cursor-pointer"
                    >
                      <FolderOpen className="h-3 w-3" /> 打开封面总览图
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={handleGenerateCovers}
                    disabled={coverBusy || !activeEpId}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-violet-700 disabled:opacity-45 cursor-pointer"
                  >
                    {coverBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                    {coverBusy ? 'CLI 生封面中...' : `一键生成双比例封面 (~$${(priceImg2K * 4).toFixed(2)})`}
                  </button>
                </div>
              </div>

              {coverCandidates.length > 0 ? (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
                  {coverCandidates.map((c) => {
                    const locked = Boolean(activeEp?.selected_cover_path && activeEp.selected_cover_path === c.path);
                    return (
                      <div
                        key={`${c.index}-${c.path}`}
                        className={`rounded-xl border p-2 space-y-1.5 bg-slate-50/70 ${
                          locked ? 'border-emerald-400 ring-2 ring-emerald-100' : 'border-slate-200'
                        }`}
                      >
                        <div className="flex items-center justify-between text-[10px]">
                          <span className="font-semibold text-slate-800 truncate">{c.label || `#${c.index}`}</span>
                          <span className="text-violet-700 font-medium">{c.ratio}</span>
                        </div>
                        <img src={mediaUrl(c.path)} alt={c.label || ''} className="h-32 w-full rounded-lg object-contain bg-slate-900" />
                        <button
                          type="button"
                          disabled={coverBusy}
                          onClick={() => handleSelectCover(c.index)}
                          className={`w-full rounded py-1 text-[10px] font-semibold cursor-pointer ${
                            locked
                              ? 'bg-emerald-100 text-emerald-800'
                              : 'bg-violet-600 text-white hover:bg-violet-700'
                          }`}
                        >
                          {locked ? '✓ 已选定封面' : `锁定 #${c.index}`}
                        </button>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="text-xs text-slate-500">
                  点右上角按钮自动基于本集高光首帧与防穿帮护盾生成 9:16 × 2 与 3:4 × 2 共 4 张无字封面，可直接锁定
                  {collabSyncEnabled ? '或推送至协作端供团队成员投票' : '作为本集定稿封面'}。
                </div>
              )}
            </div>

            {collabSyncEnabled && (
              <div className="lg:col-span-5 studio-card rounded-2xl p-5 border-sky-200 flex flex-col justify-between space-y-3">
                <div className="space-y-2">
                  <div className="text-sm font-semibold text-sky-900 inline-flex items-center gap-1.5">
                    <CloudUpload className="h-4 w-4 text-sky-600" /> 远程协作：推送至自托管协作端
                  </div>
                  <p className="text-xs text-slate-600 leading-relaxed">
                    自动压制轻量网页流畅预览版（<code className="rounded bg-slate-100 px-1 py-0.5 text-slate-800">web_preview.mp4</code>），把首尾帧、封面、终版与 AI 原创证明同步推送至你的自托管协作服务端，支持配合开源 <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-800">ntfy</code> 通知团队成员审片。
                  </p>
                  <div className="flex items-center gap-2 text-xs pt-1">
                    <span className="text-slate-600 shrink-0">通知节点：</span>
                    <select
                      value={pushStage}
                      onChange={(e) => setPushStage(e.target.value as typeof pushStage)}
                      className="studio-input flex-1 rounded-lg px-2.5 py-1.5 text-xs"
                    >
                      <option value="master_ready">成片终版已就绪（推流畅预览+高清终版）</option>
                      <option value="covers_ready">双比例封面已就绪（请团队成员投票）</option>
                      <option value="video_360p_ready">360p 试卡已就绪（请团队成员审动态）</option>
                      <option value="frames_ready">首尾帧已就绪（请团队成员挑图）</option>
                      <option value="update">静默增量同步更新</option>
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => handlePushEpisodeToCloud()}
                    disabled={syncBusy !== null || !activeEpId}
                    className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-sky-600 py-2.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-45 cursor-pointer"
                  >
                    <CloudUpload className="h-3.5 w-3.5" />
                    {syncBusy === 'push' ? '正在推送至协作端...' : '推送至协作端'}
                  </button>
                  <button
                    type="button"
                    onClick={handlePullCloudChanges}
                    disabled={syncBusy !== null}
                    className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 py-2.5 text-xs font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-45 cursor-pointer"
                  >
                    <CloudDownload className="h-3.5 w-3.5" />
                    {syncBusy === 'pull' ? '拉取中...' : '拉取远程协作更新'}
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* 成片预览与历史交付 */}
          <div className="studio-card rounded-2xl p-5 space-y-3">
            <div className="text-sm font-semibold text-slate-900">最近合成与导出记录</div>
            <div className="space-y-2">
              {renderJobs.map((j) => (
                <div key={j.id} className="rounded-xl border border-slate-200 bg-slate-50/70 p-3 flex flex-wrap items-center justify-between gap-3 text-xs">
                  <div className="space-y-0.5 min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="rounded bg-sky-50 border border-sky-200 px-2 py-0.5 text-[10px] font-medium text-sky-700">{j.job_type}</span>
                      <span className={j.status === 'done' ? 'text-emerald-700 font-medium' : j.status === 'failed' ? 'text-rose-600 font-medium' : 'text-amber-700 font-medium'}>
                        {j.status}
                      </span>
                      <span className="text-slate-400">{j.created_at}</span>
                    </div>
                    <div className="text-slate-700 whitespace-pre-wrap">{j.log_text}</div>
                  </div>
                  {j.output_path && (
                    <button
                      onClick={() => revealFileLocation(j.output_path)}
                      className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-100 cursor-pointer"
                    >
                      <FolderOpen className="h-3.5 w-3.5" /> 打开文件位置
                    </button>
                  )}
                </div>
              ))}
              {renderJobs.length === 0 && <div className="text-xs text-slate-400">还没有导出记录，选好定稿镜头后点上方任一出口立刻成片。</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
