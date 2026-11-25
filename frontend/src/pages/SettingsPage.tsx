import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Cloud,
  ExternalLink,
  Flame,
  FolderOpen,
  Gauge,
  KeyRound,
  LogOut,
  PowerOff,
  RefreshCw,
  RotateCcw,
  Save,
  Sliders,
  Sparkles,
  Users,
} from 'lucide-react';
import { api } from '../api';
import type { ProbeColab, PromptTemplate, PublicSettings, SubscriptionPreset } from '../types';

interface Props {
  settings: PublicSettings | null;
  onSettingsUpdated: () => Promise<void>;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const FALLBACK_PRESETS: SubscriptionPreset[] = [
  {
    id: 'free',
    name: '免费层（AI Studio 免费 API）',
    description: '零预算起步：免费文本配额完成选题、编剧与分镜编译，本机 1080p 封装母带。',
    gemini_daily_limit: 100,
    gemini_monthly_credit_usd: 0,
    flow_monthly_total: 0,
    default_mode: 'local_1080p',
    media_strategy: 'manual_flow',
    recommended_gpu: 'T4',
  },
  {
    id: 'ai_pro',
    name: 'Google AI Pro',
    description: '含基础月度 Flow 积分与 Colab 云端 GPU 能力；手工 Flow 抽卡 + 免费 1080p + Colab 4K。',
    gemini_daily_limit: 250,
    gemini_monthly_credit_usd: 0,
    flow_monthly_total: 1000,
    default_mode: 'colab_4k_30fps',
    media_strategy: 'manual_flow',
    recommended_gpu: 'L4',
  },
  {
    id: 'ai_ultra',
    name: 'Google AI Ultra',
    description: '含高额月度 Flow 积分与 Colab Pro+ 高级 GPU；批量出片 + 4K/30fps HLG HDR 母带。',
    gemini_daily_limit: 500,
    gemini_monthly_credit_usd: 0,
    flow_monthly_total: 25000,
    default_mode: 'colab_4k_30fps',
    media_strategy: 'manual_flow',
    recommended_gpu: 'L4',
  },
  {
    id: 'ai_ultra_dev',
    name: 'AI Ultra + API 月赠金',
    description: '三油箱全开：月度 API 赠金负责 CLI 直出图/乐/封面，Flow 专注视频，Colab 跑 4K HDR。',
    gemini_daily_limit: 500,
    gemini_monthly_credit_usd: 100,
    flow_monthly_total: 25000,
    default_mode: 'colab_4k_30fps',
    media_strategy: 'cli_hybrid',
    recommended_gpu: 'L4',
  },
  {
    id: 'custom',
    name: '自定义配置',
    description: '手动设定每日调用上限、月度 API 预算与 Flow 积分池，按实际账户灵活校准。',
    gemini_daily_limit: 250,
    gemini_monthly_credit_usd: 0,
    flow_monthly_total: 0,
    default_mode: 'local_1080p',
    media_strategy: 'manual_flow',
    recommended_gpu: 'L4',
  },
];

export const SettingsPage: React.FC<Props> = ({ settings, onSettingsUpdated, notify }) => {
  // 主力与备用文本模型
  const [geminiKey, setGeminiKey] = useState('');
  const [geminiModel, setGeminiModel] = useState('gemini-2.5-flash');
  const [proxyUrl, setProxyUrl] = useState('');
  const [deepseekEnabled, setDeepseekEnabled] = useState(false);
  const [deepseekKey, setDeepseekKey] = useState('');
  const [deepseekModel, setDeepseekModel] = useState('deepseek-chat');
  const [defaultProvider, setDefaultProvider] = useState<'gemini' | 'deepseek'>('gemini');

  // 下载监听与直拖目录
  const [watchDir, setWatchDir] = useState('~/Downloads');
  const [dockDragDir, setDockDragDir] = useState('');

  // Colab 云端 GPU
  const [colabApiBase, setColabApiBase] = useState('https://colaboratory.googleapis.com/v1beta');
  const [colabMcpEndpoint, setColabMcpEndpoint] = useState('https://colaboratory.googleapis.com/mcp');
  const [colabDefaultGpu, setColabDefaultGpu] = useState('L4');
  const [colabGdriveRoot, setColabGdriveRoot] = useState('AntigravityVideoStudio');

  // Google 订阅档位预设
  const [presets, setPresets] = useState<SubscriptionPreset[]>(FALLBACK_PRESETS);
  const [currentTier, setCurrentTier] = useState('custom');
  const [applyingTier, setApplyingTier] = useState<string | null>(null);

  // 媒体生成引擎模式与模型单价（可折叠）
  const [mediaEngineMode, setMediaEngineMode] = useState<'auto' | 'sdk' | 'cli'>('auto');
  const [cliSectionOpen, setCliSectionOpen] = useState(false);
  const [cliImageModel, setCliImageModel] = useState('gemini-nano-banana-2.1');
  const [cliVideoModel, setCliVideoModel] = useState('gemini-omni-1.2-flash');
  const [cliMusicModel, setCliMusicModel] = useState('lyria-3.5');
  const [cliTtsModel, setCliTtsModel] = useState('gemini-3.8-flash-tts');
  const [cliPricingJson, setCliPricingJson] = useState('');

  // 国内热榜开关 & 可选自托管多人协作开关（可折叠）
  const [enableHotFeeds, setEnableHotFeeds] = useState(true);
  const [collabSectionOpen, setCollabSectionOpen] = useState(false);
  const [enableCloudCollab, setEnableCloudCollab] = useState(false);
  const [cloudStudioUrl, setCloudStudioUrl] = useState('');
  const [syncApiToken, setSyncApiToken] = useState('');

  const [detectedModels, setDetectedModels] = useState<Array<{ id: string; display_name: string }>>([]);
  const [detecting, setDetecting] = useState(false);
  const [saving, setSaving] = useState(false);

  const [probeColab, setProbeColab] = useState<ProbeColab | null>(null);
  const [probingColab, setProbingColab] = useState(false);
  const [oauthUrl, setOauthUrl] = useState('');
  const [oauthState, setOauthState] = useState('');
  const [oauthCode, setOauthCode] = useState('');
  const [oauthBusy, setOauthBusy] = useState(false);

  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [activeTplKey, setActiveTplKey] = useState('topic_scorer');
  const [tplDraft, setTplDraft] = useState('');

  useEffect(() => {
    if (settings) {
      setGeminiModel(settings.gemini_model || 'gemini-2.5-flash');
      setProxyUrl(settings.google_proxy_url || '');
      setDeepseekEnabled(Boolean(settings.deepseek_enabled));
      setDeepseekModel(settings.deepseek_model || 'deepseek-chat');
      setDefaultProvider(settings.default_provider || 'gemini');
      setWatchDir(settings.watch_downloads_dir || '~/Downloads');
      setDockDragDir(settings.dock_drag_dir || '');
      if (settings.colab_api_base) setColabApiBase(settings.colab_api_base);
      if (settings.colab_mcp_endpoint) setColabMcpEndpoint(settings.colab_mcp_endpoint);
      if (settings.colab_default_gpu) setColabDefaultGpu(settings.colab_default_gpu);
      if (settings.colab_gdrive_root) setColabGdriveRoot(settings.colab_gdrive_root);
      if (settings.subscription_tier) setCurrentTier(settings.subscription_tier);
      if (typeof settings.enable_hot_feeds === 'boolean') setEnableHotFeeds(settings.enable_hot_feeds);
      if (typeof settings.enable_cloud_collab === 'boolean') {
        setEnableCloudCollab(settings.enable_cloud_collab);
        if (settings.enable_cloud_collab) setCollabSectionOpen(true);
      }
      setCloudStudioUrl(settings.cloud_studio_url || '');
      if (settings.media_engine_mode === 'sdk' || settings.media_engine_mode === 'cli' || settings.media_engine_mode === 'auto') {
        setMediaEngineMode(settings.media_engine_mode);
      } else {
        setMediaEngineMode('auto');
      }
      if (settings.cli_image_model) setCliImageModel(settings.cli_image_model);
      if (settings.cli_video_model) setCliVideoModel(settings.cli_video_model);
      if (settings.cli_music_model) setCliMusicModel(settings.cli_music_model);
      if (settings.cli_tts_model) setCliTtsModel(settings.cli_tts_model);
      setCliPricingJson(settings.cli_pricing_json || '');
    }
  }, [settings]);

  const loadSubscriptionPresets = async () => {
    try {
      const res = await api.get<{
        presets?: SubscriptionPreset[] | Record<string, SubscriptionPreset>;
        current_tier?: string;
      }>('/api/subscription-presets');
      if (res.presets) {
        const list = Array.isArray(res.presets) ? res.presets : Object.values(res.presets);
        if (list.length > 0) setPresets(list);
      }
      if (res.current_tier) setCurrentTier(res.current_tier);
    } catch {
      // 兜底使用内置档位列表
    }
  };

  const loadTemplates = async () => {
    try {
      const res = await api.get<{ templates: PromptTemplate[] }>('/api/prompt-templates');
      setTemplates(res.templates);
      const cur = res.templates.find((t) => t.key === activeTplKey) || res.templates[0];
      if (cur) {
        setActiveTplKey(cur.key);
        setTplDraft(cur.content);
      }
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  useEffect(() => {
    loadSubscriptionPresets();
    loadTemplates();
  }, []);

  const handleApplyPreset = async (preset: SubscriptionPreset) => {
    setApplyingTier(preset.id);
    try {
      await api.post('/api/quota/apply-preset', { tier: preset.id });
      setCurrentTier(preset.id);
      if (preset.recommended_gpu) {
        setColabDefaultGpu(preset.recommended_gpu);
      }
      await onSettingsUpdated();
      window.dispatchEvent(new Event('agvs-quota-refresh'));
      notify(`已应用「${preset.name}」并同步校准三油箱额度红线！`);
    } catch (err: any) {
      notify(err?.message || '应用订阅档位失败', 'err');
    } finally {
      setApplyingTier(null);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    const pricingTrimmed = cliPricingJson.trim();
    if (pricingTrimmed) {
      try {
        const parsed = JSON.parse(pricingTrimmed);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          notify('CLI_PRICING_JSON 必须是合法的 JSON 对象（或留空使用默认单价）', 'err');
          return;
        }
      } catch {
        notify('CLI_PRICING_JSON 格式有误，请检查 JSON 语法或留空恢复默认', 'err');
        return;
      }
    }

    setSaving(true);
    try {
      await api.patch('/api/settings', {
        GEMINI_API_KEY: geminiKey.trim() || undefined,
        GEMINI_MODEL: geminiModel.trim(),
        GOOGLE_PROXY_URL: proxyUrl.trim(),
        DEEPSEEK_ENABLED: deepseekEnabled,
        DEEPSEEK_API_KEY: deepseekKey.trim() || undefined,
        DEEPSEEK_MODEL: deepseekModel.trim(),
        DEFAULT_PROVIDER: deepseekEnabled ? defaultProvider : 'gemini',
        WATCH_DOWNLOADS_DIR: watchDir.trim(),
        DOCK_DRAG_DIR: dockDragDir.trim(),
        COLAB_API_BASE: colabApiBase.trim(),
        COLAB_MCP_ENDPOINT: colabMcpEndpoint.trim(),
        COLAB_DEFAULT_GPU: colabDefaultGpu.trim(),
        COLAB_GDRIVE_ROOT: colabGdriveRoot.trim(),
        ENABLE_HOT_FEEDS: enableHotFeeds,
        ENABLE_CLOUD_COLLAB: enableCloudCollab,
        CLOUD_STUDIO_URL: cloudStudioUrl.trim(),
        SYNC_API_TOKEN: syncApiToken.trim() || undefined,
        MEDIA_ENGINE_MODE: mediaEngineMode,
        CLI_IMAGE_MODEL: cliImageModel.trim(),
        CLI_VIDEO_MODEL: cliVideoModel.trim(),
        CLI_MUSIC_MODEL: cliMusicModel.trim(),
        CLI_TTS_MODEL: cliTtsModel.trim(),
        CLI_PRICING_JSON: pricingTrimmed,
      });
      setGeminiKey('');
      setDeepseekKey('');
      setSyncApiToken('');
      await onSettingsUpdated();
      window.dispatchEvent(new Event('agvs-quota-refresh'));
      notify('本机设置已安全保存（非敏感项写入 .env，敏感密钥隔离存入系统安全存储）');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setSaving(false);
    }
  };

  const handleDetectModels = async () => {
    setDetecting(true);
    try {
      const res = await api.post<{ models: Array<{ id: string; display_name: string }> }>('/api/settings/gemini-models');
      setDetectedModels(res.models);
      notify(`探测成功：你的 Gemini Key 当前可用 ${res.models.length} 个模型`);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setDetecting(false);
    }
  };

  const handleProbeColab = async () => {
    setProbingColab(true);
    try {
      const res = await api.get<ProbeColab>('/api/settings/probe-colab');
      setProbeColab(res);
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setProbingColab(false);
    }
  };

  const handleStartColabOAuth = async () => {
    setOauthBusy(true);
    try {
      const res = await api.post<{ auth_url: string; state: string; instructions: string }>('/api/settings/colab/oauth-start');
      setOauthUrl(res.auth_url);
      setOauthState(res.state);
      window.open(res.auth_url, '_blank', 'noopener,noreferrer');
      notify('已打开 Google 官方授权页，请允许后复制授权码粘贴到下方输入框');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setOauthBusy(false);
    }
  };

  const handleFinishColabOAuth = async () => {
    if (!oauthCode.trim()) {
      notify('请先粘贴 Google 官方页面返回的授权码', 'err');
      return;
    }
    setOauthBusy(true);
    try {
      const res = await api.post<ProbeColab>('/api/settings/colab/oauth-finish', {
        auth_code: oauthCode.trim(),
        state: oauthState,
      });
      setProbeColab(res);
      setOauthCode('');
      setOauthUrl('');
      setOauthState('');
      notify('Google Colab 官方授权成功！令牌已安全保存（制片台与终端 colab 命令共用）');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setOauthBusy(false);
    }
  };

  const handleLogoutColab = async () => {
    setOauthBusy(true);
    try {
      const res = await api.post<ProbeColab>('/api/settings/colab/oauth-logout');
      setProbeColab(res);
      notify('已清除本地保存的 Colab 官方授权令牌');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setOauthBusy(false);
    }
  };

  const handleStopColabSession = async (endpoint: string) => {
    setOauthBusy(true);
    try {
      const res = await api.post<ProbeColab>('/api/settings/colab/stop-session', { endpoint });
      setProbeColab(res);
      notify('已停止并释放该 Colab 云端运行时');
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setOauthBusy(false);
    }
  };

  const handleSelectTpl = (t: PromptTemplate) => {
    setActiveTplKey(t.key);
    setTplDraft(t.content);
  };

  const handleSaveTpl = async () => {
    try {
      await api.patch(`/api/prompt-templates/${activeTplKey}`, { content: tplDraft });
      await loadTemplates();
      notify('AI 规则模板已生效，下次生成立即按新规则执行');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const handleResetTpl = async () => {
    try {
      const res = await api.post<{ template: PromptTemplate }>(`/api/prompt-templates/${activeTplKey}/reset`);
      setTplDraft(res.template.content);
      await loadTemplates();
      notify('已恢复内置默认规则');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const currentTpl = templates.find((t) => t.key === activeTplKey);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">系统设置 & AI 导演规则库</h1>
        <p className="mt-1 text-sm text-slate-500">
          全站默认单机免登录运行，优先使用 Gemini API（代理按需填写，留空直连）；所有敏感密钥均隔离加密存储，不入代码仓库。
        </p>
      </div>

      {/* 1. Google 订阅档位一键预设区 */}
      <div className="studio-card rounded-2xl p-5 space-y-4 border-emerald-200">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-emerald-900">
              <Gauge className="h-4 w-4 text-emerald-600" /> Google 订阅档位一键预设（自动校准三油箱红线）
            </div>
            <p className="mt-0.5 text-xs text-slate-500">
              按你当前的 Google 账号订阅情况点选对应档位，一键同步校准 Gemini 日调用上限、月度 API 赠金预算与 Flow 月度积分池。
            </p>
          </div>
          <span className="rounded-full bg-emerald-50 border border-emerald-200 px-2.5 py-0.5 text-[11px] font-medium text-emerald-700">
            当前档位：{presets.find((p) => p.id === currentTier)?.name || currentTier}
          </span>
        </div>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
          {presets.map((p) => {
            const active = currentTier === p.id;
            const busy = applyingTier === p.id;
            return (
              <div
                key={p.id}
                className={`rounded-xl border p-3.5 flex flex-col justify-between gap-3 transition ${
                  active
                    ? 'border-emerald-400 bg-emerald-50/60 ring-2 ring-emerald-100'
                    : 'border-slate-200 bg-slate-50/60 hover:border-sky-300 hover:bg-white'
                }`}
              >
                <div className="space-y-1.5">
                  <div className="flex items-start justify-between gap-1.5">
                    <span className="text-xs font-semibold text-slate-900 leading-snug">{p.name}</span>
                    {active && (
                      <span className="shrink-0 rounded-full bg-emerald-600 px-1.5 py-0.5 text-[9px] font-semibold text-white">
                        已启用
                      </span>
                    )}
                  </div>
                  <p className="text-[11px] text-slate-600 leading-relaxed">{p.description}</p>
                  <div className="flex flex-wrap gap-1 pt-1 text-[10px] font-mono">
                    <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-slate-700">
                      API ${p.gemini_monthly_credit_usd}/月
                    </span>
                    <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-amber-800">
                      Flow {p.flow_monthly_total.toLocaleString()}
                    </span>
                    <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-violet-700">
                      GPU {p.recommended_gpu}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={applyingTier !== null}
                  onClick={() => handleApplyPreset(p)}
                  className={`w-full rounded-lg py-1.5 text-[11px] font-semibold transition cursor-pointer disabled:opacity-50 ${
                    active
                      ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                      : 'bg-white border border-slate-300 text-slate-700 hover:bg-sky-600 hover:border-sky-600 hover:text-white'
                  }`}
                >
                  {busy ? '校准中...' : active ? '重新校准红线' : '一键套用此档位'}
                </button>
              </div>
            );
          })}
        </div>
      </div>

      <form onSubmit={handleSaveSettings} className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {/* 2. Gemini 默认主力 */}
        <div className="studio-card rounded-2xl p-5 space-y-4 border-sky-200">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-semibold text-sky-800">
              <Sparkles className="h-4 w-4 text-sky-600" /> 主力引擎：Google Gemini API
            </div>
            {settings?.gemini_key_set && (
              <span className="inline-flex items-center gap-1 rounded bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                <CheckCircle2 className="h-3 w-3" /> 已配 ({settings.gemini_key_masked})
              </span>
            )}
          </div>

          <div>
            <label className="mb-1 block text-xs text-slate-600">Gemini API Key（留空保持已保存密钥不变）</label>
            <input
              type="password"
              value={geminiKey}
              onChange={(e) => setGeminiKey(e.target.value)}
              placeholder={settings?.gemini_key_set ? '已保存（输入新 Key 可覆盖）' : '粘贴你的 Google AI Studio / Gemini API Key'}
              className="studio-input w-full rounded-lg px-3 py-2 text-xs"
            />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-slate-600">Gemini 文本模型 ID</label>
              <input
                value={geminiModel}
                onChange={(e) => setGeminiModel(e.target.value)}
                list="gemini-model-list"
                className="studio-input w-full rounded-lg px-3 py-2 text-xs"
              />
              <datalist id="gemini-model-list">
                <option value="gemini-2.5-flash" />
                <option value="gemini-3-flash-preview" />
                <option value="gemini-3.8-flash" />
                <option value="gemini-2.5-pro" />
                {detectedModels.map((m) => (
                  <option key={m.id} value={m.id} />
                ))}
              </datalist>
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-600">Google 服务代理（仅 Google 请求使用）</label>
              <input
                value={proxyUrl}
                onChange={(e) => setProxyUrl(e.target.value)}
                placeholder="http://127.0.0.1:PORT（按需填写，留空直连）"
                className="studio-input w-full rounded-lg px-3 py-2 text-xs"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleDetectModels}
              disabled={detecting}
              className="inline-flex items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs font-medium text-sky-700 hover:bg-sky-100 cursor-pointer"
            >
              <RefreshCw className={`h-3 w-3 ${detecting ? 'animate-spin' : ''}`} />
              {detecting ? '探测中...' : '一键探测该 Key 当前可用模型'}
            </button>
            {detectedModels.length > 0 && (
              <select
                value={geminiModel}
                onChange={(e) => setGeminiModel(e.target.value)}
                className="studio-input rounded-lg px-2.5 py-1.5 text-xs max-w-[240px]"
              >
                {detectedModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* 媒体生成引擎模式选择（v1.0.1 SDK 直连主通道 + CLI 兼容） */}
          <div className="pt-3 border-t border-sky-100 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-1.5">
              <label className="text-xs font-semibold text-slate-800">
                媒体生成引擎模式（MEDIA_ENGINE_MODE）
              </label>
              <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                <span
                  className={`rounded-full border px-2 py-0.5 font-medium ${
                    settings?.google_genai_sdk_ok !== false
                      ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                      : 'bg-amber-50 border-amber-200 text-amber-800'
                  }`}
                >
                  GenAI SDK {settings?.google_genai_sdk_ok !== false ? '已就绪' : '未安装'}
                </span>
                <span
                  className={`rounded-full border px-2 py-0.5 font-medium ${
                    settings?.gemini_cli_ok
                      ? 'bg-sky-50 border-sky-200 text-sky-700'
                      : 'bg-slate-100 border-slate-200 text-slate-500'
                  }`}
                >
                  gemini-api CLI {settings?.gemini_cli_ok ? '已安装' : '可选'}
                </span>
              </div>
            </div>
            <select
              value={mediaEngineMode}
              onChange={(e) => setMediaEngineMode(e.target.value as 'auto' | 'sdk' | 'cli')}
              className="studio-input w-full rounded-lg px-3 py-2 text-xs"
            >
              <option value="auto">auto · 自动优先官方 google-genai SDK 直连（按需回退 CLI · 推荐）</option>
              <option value="sdk">sdk · 强制官方 google-genai SDK 直连（仅需 GEMINI_API_KEY）</option>
              <option value="cli">cli · 强制调用本机 gemini-api CLI 通道</option>
            </select>
            <div className="rounded-xl border border-sky-200/80 bg-sky-50/70 px-3 py-2 text-[11px] text-slate-700 leading-relaxed space-y-1">
              <div>
                <strong className="text-sky-900">开箱即用提示：</strong>
                填好上方 <code className="font-mono text-sky-800">GEMINI_API_KEY</code> 即可通过内置官方{' '}
                <code className="font-mono text-sky-800">google-genai</code> SDK 直接生成图片、视频、原创配乐与中文 TTS 旁白，无需额外安装 CLI。
              </div>
              <div className="text-slate-600">
                注：Google AI Pro / Ultra 开发者需先在 Google AI Studio 绑定结算账号（Billing），方可激活每月开发者 API 赠金。
              </div>
            </div>
          </div>
        </div>

        {/* 3. DeepSeek 备用引擎 + 本地下载监听目录 */}
        <div className="studio-card rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
              <KeyRound className="h-4 w-4 text-slate-500" /> 备用引擎：DeepSeek API（默认关闭省钱）
            </div>
            <label className="inline-flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={deepseekEnabled}
                onChange={(e) => setDeepseekEnabled(e.target.checked)}
                className="accent-sky-600 h-4 w-4"
              />
              <span className={deepseekEnabled ? 'text-amber-700 font-semibold' : 'text-slate-500'}>
                {deepseekEnabled ? '已启用备用开关' : '关闭（平时 0 花费）'}
              </span>
            </label>
          </div>

          {deepseekEnabled && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 rounded-xl bg-slate-50 p-3 border border-slate-200">
              <div>
                <label className="mb-1 block text-xs text-slate-600">
                  DeepSeek API Key {settings?.deepseek_key_set ? `(${settings.deepseek_key_masked})` : ''}
                </label>
                <input
                  type="password"
                  value={deepseekKey}
                  onChange={(e) => setDeepseekKey(e.target.value)}
                  placeholder="国内直连，不经代理"
                  className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-600">默认优先使用</label>
                <select
                  value={defaultProvider}
                  onChange={(e) => setDefaultProvider(e.target.value as 'gemini' | 'deepseek')}
                  className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                >
                  <option value="gemini">Gemini 优先（推荐）</option>
                  <option value="deepseek">切换为 DeepSeek 优先</option>
                </select>
              </div>
            </div>
          )}

          <div className="pt-2 border-t border-slate-200 space-y-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
              <FolderOpen className="h-4 w-4 text-emerald-600" /> 本机下载目录监听与素材直拖托盘
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-[11px] text-slate-600">本机下载监听目录</label>
                <input
                  value={watchDir}
                  onChange={(e) => setWatchDir(e.target.value)}
                  placeholder="~/Downloads"
                  className="studio-input w-full rounded px-2.5 py-1.5 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-[11px] text-slate-600">直拖素材副本目录（留空=下载目录）</label>
                <input
                  value={dockDragDir}
                  onChange={(e) => setDockDragDir(e.target.value)}
                  placeholder="留空默认跟随监听下载目录"
                  className="studio-input w-full rounded px-2.5 py-1.5 text-xs"
                />
              </div>
            </div>
          </div>

          <div className="flex justify-end pt-1">
            <button
              type="submit"
              disabled={saving}
              className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-4 py-2 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-50 cursor-pointer"
            >
              <Save className="h-3.5 w-3.5" /> {saving ? '保存中...' : '保存全部本机设置'}
            </button>
          </div>
        </div>

        {/* 4. 媒体生成模型与估算单价（可配置折叠区） */}
        <div className="studio-card rounded-2xl overflow-hidden border-indigo-200 lg:col-span-2">
          <button
            type="button"
            onClick={() => setCliSectionOpen((v) => !v)}
            className="w-full px-5 py-4 flex flex-wrap items-center justify-between gap-2 bg-indigo-50/45 hover:bg-indigo-50/70 transition cursor-pointer text-left"
          >
            <div className="space-y-0.5">
              <div className="flex items-center gap-2 text-sm font-semibold text-indigo-900">
                <Sliders className="h-4 w-4 text-indigo-600" /> 媒体生成模型与估算单价（SDK 直连 / CLI 通用配置）
              </div>
              <div className="text-[11px] text-slate-500 font-mono">
                图：{cliImageModel} · 视频：{cliVideoModel} · 配乐：{cliMusicModel} · 配音：{cliTtsModel}
              </div>
            </div>
            <span className="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-white px-2.5 py-1 text-xs font-medium text-indigo-700">
              {cliSectionOpen ? (
                <>
                  收起配置 <ChevronUp className="h-3.5 w-3.5" />
                </>
              ) : (
                <>
                  展开编辑模型与单价 <ChevronDown className="h-3.5 w-3.5" />
                </>
              )}
            </span>
          </button>

          {cliSectionOpen && (
            <div className="p-5 space-y-4 border-t border-indigo-100">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <label className="mb-1 block text-[11px] text-slate-600">生图模型（CLI_IMAGE_MODEL）</label>
                  <input
                    value={cliImageModel}
                    onChange={(e) => setCliImageModel(e.target.value)}
                    placeholder="gemini-nano-banana-2.1"
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-slate-600">生视频模型（CLI_VIDEO_MODEL）</label>
                  <input
                    value={cliVideoModel}
                    onChange={(e) => setCliVideoModel(e.target.value)}
                    placeholder="gemini-omni-1.2-flash"
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-slate-600">原创配乐模型（CLI_MUSIC_MODEL）</label>
                  <input
                    value={cliMusicModel}
                    onChange={(e) => setCliMusicModel(e.target.value)}
                    placeholder="lyria-3.5"
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-slate-600">旁白配音模型（CLI_TTS_MODEL）</label>
                  <input
                    value={cliTtsModel}
                    onChange={(e) => setCliTtsModel(e.target.value)}
                    placeholder="gemini-3.8-flash-tts"
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <label className="text-[11px] text-slate-600">
                    自定义估算单价 JSON（<code className="font-mono">CLI_PRICING_JSON</code>，留空使用内置默认单价）
                  </label>
                  {settings?.cli_pricing && (
                    <button
                      type="button"
                      onClick={() => setCliPricingJson(JSON.stringify(settings.cli_pricing, null, 2))}
                      className="text-[11px] font-medium text-indigo-700 hover:underline cursor-pointer"
                    >
                      填入当前生效单价模板
                    </button>
                  )}
                </div>
                <textarea
                  value={cliPricingJson}
                  onChange={(e) => setCliPricingJson(e.target.value)}
                  rows={4}
                  placeholder='{"image":{"1K":0.0336,"2K":0.0504},"video_per_sec":{"360p":0.023,"720p":0.07,"1080p":0.104},"music":0.08,"tts":0.01}'
                  className="studio-input w-full rounded-xl p-2.5 font-mono text-xs leading-relaxed"
                />
              </div>

              <div className="flex justify-end">
                <button
                  type="submit"
                  disabled={saving}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50 cursor-pointer"
                >
                  <Save className="h-3.5 w-3.5" /> {saving ? '保存中...' : '保存模型与单价'}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* 5. 国内热榜雷达开关 & 可选自托管多人协作开关 */}
        <div className="studio-card rounded-2xl p-5 space-y-4 lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-3 pb-3.5 border-b border-slate-100">
            <div className="space-y-0.5">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                <Flame className="h-4 w-4 text-orange-500" /> 国内五大实时热榜雷达（ENABLE_HOT_FEEDS）
              </div>
              <p className="text-xs text-slate-500">
                开启后在「热点与选题库」中直连抓取抖音 / 今日头条 / 百度 / B站 / 微博热搜；如面向海外或不需要热榜可关闭以保持界面清爽。
              </p>
            </div>
            <label className="inline-flex items-center gap-2 text-xs font-medium cursor-pointer shrink-0">
              <input
                type="checkbox"
                checked={enableHotFeeds}
                onChange={(e) => setEnableHotFeeds(e.target.checked)}
                className="accent-sky-600 h-4 w-4"
              />
              <span className={enableHotFeeds ? 'text-emerald-700 font-semibold' : 'text-slate-500'}>
                {enableHotFeeds ? '已开启国内热榜' : '已关闭（仅保留 AI 搜题与手动录入）'}
              </span>
            </label>
          </div>

          {/* 可选自托管多人协作折叠区 */}
          <div className="rounded-xl border border-slate-200 overflow-hidden">
            <div className="px-4 py-3 bg-slate-50/70 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Users className="h-4 w-4 text-sky-600" />
                <span className="text-sm font-semibold text-slate-900">可选自托管多人协作（ENABLE_CLOUD_COLLAB）</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium border ${
                    enableCloudCollab
                      ? 'bg-sky-50 border-sky-200 text-sky-700'
                      : 'bg-slate-100 border-slate-200 text-slate-500'
                  }`}
                >
                  {enableCloudCollab ? '已启用协作同步' : '默认关闭（纯本地单机模式）'}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <label className="inline-flex items-center gap-1.5 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={enableCloudCollab}
                    onChange={(e) => {
                      const next = e.target.checked;
                      setEnableCloudCollab(next);
                      if (next) setCollabSectionOpen(true);
                    }}
                    className="accent-sky-600 h-4 w-4"
                  />
                  <span className="text-slate-700">启用远程协作同步</span>
                </label>
                <button
                  type="button"
                  onClick={() => setCollabSectionOpen((v) => !v)}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100 cursor-pointer"
                >
                  {collabSectionOpen ? (
                    <>
                      收起 <ChevronUp className="h-3 w-3" />
                    </>
                  ) : (
                    <>
                      配置地址 <ChevronDown className="h-3 w-3" />
                    </>
                  )}
                </button>
              </div>
            </div>

            {collabSectionOpen && (
              <div className="p-4 space-y-3 bg-white border-t border-slate-200">
                <p className="text-xs text-slate-500 leading-relaxed">
                  开启并填写你自托管的远程协作服务地址与同步 Token 后，侧边栏与剧目工作台才会显示「拉取远程协作更新 / 推送至协作端」按钮；
                  服务端部署模板见仓库 <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-700">deploy/self-hosted/</code>，亦可选配合 <code className="rounded bg-slate-100 px-1 py-0.5 text-slate-700">ntfy</code> 等开源推送服务接收审片提醒。
                </p>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-600">自托管协作服务端地址（CLOUD_STUDIO_URL）</label>
                    <input
                      value={cloudStudioUrl}
                      onChange={(e) => setCloudStudioUrl(e.target.value)}
                      placeholder="https://your-domain.example/studio（按需填写）"
                      className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-600">
                      同步密钥 Token（SYNC_API_TOKEN）{settings?.sync_token_set ? ' · 已配置' : ''}
                    </label>
                    <input
                      type="password"
                      value={syncApiToken}
                      onChange={(e) => setSyncApiToken(e.target.value)}
                      placeholder={settings?.sync_token_set ? '已保存（留空保持不变，输入新值可覆盖）' : '填写与协作端一致的同步 Token'}
                      className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                    />
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="flex justify-end">
            <button
              type="submit"
              disabled={saving}
              className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-50 cursor-pointer"
            >
              <Save className="h-3.5 w-3.5" /> {saving ? '保存中...' : '保存开关与协作配置'}
            </button>
          </div>
        </div>

        {/* 6. Google Colab 官方 API & MCP 算力中心 */}
        <div className="studio-card rounded-2xl p-5 space-y-4 border-violet-200 lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-semibold text-violet-800">
              <Cloud className="h-4 w-4 text-violet-600" /> Google Colab 官方 API & 托管 MCP 算力中心（100% 官方通道 · 0 封号风险）
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium ${
                  settings?.colab_mcp_configured
                    ? 'bg-emerald-50 border border-emerald-200 text-emerald-700'
                    : 'bg-slate-100 border border-slate-200 text-slate-600'
                }`}
              >
                <CheckCircle2 className="h-3 w-3" />
                {settings?.colab_mcp_configured ? 'Antigravity 官方 MCP 已注册' : 'MCP 未注册'}
              </span>
              <button
                type="button"
                onClick={handleProbeColab}
                disabled={probingColab}
                className="inline-flex items-center gap-1 rounded-md border border-violet-200 bg-violet-50 px-2.5 py-1 text-xs font-medium text-violet-700 hover:bg-violet-100 cursor-pointer"
              >
                <RefreshCw className={`h-3 w-3 ${probingColab ? 'animate-spin' : ''}`} />
                {probingColab ? '探测中...' : '按需探测 Colab 状态与 CU 余额'}
              </button>
              {probeColab?.authorized && (
                <button
                  type="button"
                  onClick={handleLogoutColab}
                  disabled={oauthBusy}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
                >
                  <LogOut className="h-3 w-3" /> 退出授权
                </button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <div className="md:col-span-2">
              <label className="mb-1 block text-[11px] text-slate-600">官方 Colab REST API 地址</label>
              <input
                value={colabApiBase}
                onChange={(e) => setColabApiBase(e.target.value)}
                className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
              />
            </div>
            <div>
              <label className="mb-1 block text-[11px] text-slate-600">官方托管 MCP 端点</label>
              <input
                value={colabMcpEndpoint}
                onChange={(e) => setColabMcpEndpoint(e.target.value)}
                className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs font-mono"
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="mb-1 block text-[11px] text-slate-600">默认 GPU 档位</label>
                <select
                  value={colabDefaultGpu}
                  onChange={(e) => setColabDefaultGpu(e.target.value)}
                  className="studio-input w-full rounded-lg px-2 py-1.5 text-xs"
                >
                  <option value="L4">L4（默认 · 秒开抗排队省 CU）</option>
                  <option value="A100">A100（忙时自动降 L4）</option>
                  <option value="H100">H100（顶配 · 忙时自动降级）</option>
                  <option value="T4">T4（最省 CU）</option>
                  <option value="CPU">CPU（拉模型免费）</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block text-[11px] text-slate-600">云盘根目录</label>
                <input
                  value={colabGdriveRoot}
                  onChange={(e) => setColabGdriveRoot(e.target.value)}
                  className="studio-input w-full rounded-lg px-2 py-1.5 text-xs"
                />
              </div>
            </div>
          </div>

          {probeColab && (
            <div
              className={`rounded-xl px-3.5 py-3 text-xs space-y-2 border ${
                probeColab.authorized
                  ? 'bg-emerald-50 text-emerald-900 border-emerald-200'
                  : 'bg-amber-50 text-amber-900 border-amber-200'
              }`}
            >
              <div className="font-medium">{probeColab.message}</div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-600">
                <span>官方 CLI：{probeColab.cli_installed ? `v${probeColab.cli_version || 'ok'}` : '未安装'}</span>
                {probeColab.public_api_note && <span>REST API：{probeColab.public_api_note}</span>}
                {probeColab.available_specs.length > 0 && (
                  <span>可用规格：{probeColab.available_specs.join(' / ')}</span>
                )}
              </div>
              {probeColab.active_sessions.length > 0 && (
                <div className="pt-2 border-t border-slate-200 space-y-1.5">
                  <div className="text-[11px] text-amber-800 font-medium">
                    当前活跃云端运行时（用完请及时释放，避免持续消耗 CU）：
                  </div>
                  {probeColab.active_sessions.map((s) => (
                    <div
                      key={s.endpoint}
                      className="flex items-center justify-between rounded-lg bg-white border border-slate-200 px-2.5 py-1.5 text-[11px]"
                    >
                      <span className="font-mono text-slate-800">
                        {s.accelerator} · {s.endpoint} {s.machine_shape ? `(${s.machine_shape})` : ''}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleStopColabSession(s.endpoint)}
                        disabled={oauthBusy}
                        className="inline-flex items-center gap-1 rounded bg-rose-50 border border-rose-200 px-2 py-0.5 font-medium text-rose-700 hover:bg-rose-100 cursor-pointer"
                      >
                        <PowerOff className="h-3 w-3" /> 停止释放
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 两步官方 OAuth2 加密授权区 */}
          {!probeColab?.authorized && (
            <div className="rounded-xl border border-slate-200 bg-slate-50/80 p-3.5 space-y-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-xs text-slate-600">
                  <strong className="text-slate-900">两步完成官方 OAuth2 登录</strong>（令牌安全保存在本机凭据存储中，制片台与终端{' '}
                  <code className="rounded bg-slate-200/70 px-1 py-0.5 text-slate-800">colab</code> 命令共用）：
                </div>
                <button
                  type="button"
                  onClick={handleStartColabOAuth}
                  disabled={oauthBusy}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-violet-700 disabled:opacity-50 cursor-pointer"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {oauthBusy ? '生成中...' : '① 生成并打开 Google 官方授权页'}
                </button>
              </div>
              {oauthUrl && (
                <div className="text-[11px] text-slate-500 break-all">
                  若浏览器未自动弹出，可手动点此链接：
                  <a href={oauthUrl} target="_blank" rel="noreferrer" className="text-violet-700 underline ml-1 font-medium">
                    打开官方授权页面
                  </a>
                </div>
              )}
              <div className="flex flex-wrap gap-2">
                <input
                  value={oauthCode}
                  onChange={(e) => setOauthCode(e.target.value)}
                  placeholder="② 在 Google 页面点“允许”后，把页面显示的授权码粘贴到这里"
                  className="studio-input flex-1 min-w-[260px] rounded-lg px-3 py-1.5 text-xs font-mono"
                />
                <button
                  type="button"
                  onClick={handleFinishColabOAuth}
                  disabled={oauthBusy || !oauthCode.trim()}
                  className="rounded-lg bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-40 cursor-pointer"
                >
                  完成加密授权
                </button>
              </div>
            </div>
          )}
        </div>
      </form>

      {/* 7. 可编辑 AI 导演/编剧规则模板 */}
      <div className="studio-card rounded-2xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-900">网页可编辑 AI 导演/审核规则模板</h2>
            <p className="text-xs text-slate-500">修改后立即对全站 AI 编剧、5 维选题打分、Flow 提示词编译和评论拟稿生效，无需改代码。</p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleResetTpl}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
            >
              <RotateCcw className="h-3.5 w-3.5" /> 恢复默认
            </button>
            <button
              onClick={handleSaveTpl}
              className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 cursor-pointer"
            >
              <Save className="h-3.5 w-3.5" /> 保存规则
            </button>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          {templates.map((t) => (
            <button
              key={t.key}
              onClick={() => handleSelectTpl(t)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition cursor-pointer ${
                activeTplKey === t.key
                  ? 'bg-sky-600 text-white shadow-2xs'
                  : 'bg-slate-100 border border-slate-200 text-slate-700 hover:bg-slate-200/70'
              }`}
            >
              {t.title}
            </button>
          ))}
        </div>

        {currentTpl && (
          <div className="space-y-2">
            <div className="text-xs text-slate-500">{currentTpl.description}</div>
            <textarea
              value={tplDraft}
              onChange={(e) => setTplDraft(e.target.value)}
              rows={11}
              className="studio-input w-full rounded-xl p-3.5 font-mono text-xs leading-relaxed"
            />
          </div>
        )}
      </div>
    </div>
  );
};
