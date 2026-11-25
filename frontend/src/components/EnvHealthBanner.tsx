import React, { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  HardDrive,
  PlayCircle,
  RefreshCw,
  Settings,
  Sparkles,
  Terminal,
  X,
  XCircle,
} from 'lucide-react';
import { copyToClipboard } from '../api';
import type { HealthStatus, Project, PublicSettings } from '../types';

interface Props {
  health: HealthStatus | null;
  settings: PublicSettings | null;
  projects: Project[];
  refreshingHealth?: boolean;
  onRefreshHealth: () => void;
  onOpenSettings: () => void;
  onOpenDemoProject: (proj: Project) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const ONBOARDING_DISMISS_KEY = 'agvs_v101_onboarding_dismissed';

export const EnvHealthBanner: React.FC<Props> = ({
  health,
  settings,
  projects,
  refreshingHealth = false,
  onRefreshHealth,
  onOpenSettings,
  onOpenDemoProject,
  notify,
}) => {
  const [onboardingDismissed, setOnboardingDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(ONBOARDING_DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  const ffmpegReady = health ? health.ffmpeg_ok !== false && health.ffprobe_ok !== false : true;
  const ffmpegMissing = Boolean(health && (health.ffmpeg_ok === false || health.ffprobe_ok === false));
  const diskLow = typeof health?.disk_free_gb === 'number' && health.disk_free_gb < 5;
  const showOnboarding = Boolean(settings && !settings.gemini_key_set && !onboardingDismissed);
  const demoProject =
    projects.find((p) => p.slug === 'demo-companion-drama') ||
    projects.find((p) => p.name.includes('示例') || p.name.includes('示范')) ||
    projects[0] ||
    null;

  const dismissOnboarding = () => {
    setOnboardingDismissed(true);
    try {
      localStorage.setItem(ONBOARDING_DISMISS_KEY, '1');
    } catch {
      // 忽略本地存储受限
    }
  };

  const handleCopyCmd = async (cmd: string, platformLabel: string) => {
    await copyToClipboard(cmd);
    notify(`已复制 ${platformLabel} FFmpeg 安装命令：${cmd}`);
  };

  if (!health && !showOnboarding) return null;

  return (
    <div className="space-y-3 mb-4">
      {/* 1. 环境体检红绿灯精简状态条 */}
      {health && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200/90 bg-white/90 px-3.5 py-2 text-[11px] shadow-2xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-slate-700">
              环境体检{health.version ? ` v${health.version}` : ''}
            </span>

            {/* FFmpeg / FFprobe */}
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${
                ffmpegReady
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                  : 'bg-rose-50 border-rose-200 text-rose-700'
              }`}
            >
              {ffmpegReady ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
              FFmpeg {ffmpegReady ? '就绪' : '未安装'}
            </span>

            {/* Google GenAI SDK */}
            {health.google_genai_sdk_ok !== undefined && (
              <span
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${
                  health.google_genai_sdk_ok
                    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : 'bg-amber-50 border-amber-200 text-amber-800'
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    health.google_genai_sdk_ok ? 'bg-emerald-500' : 'bg-amber-500'
                  }`}
                />
                GenAI SDK {health.google_genai_sdk_ok ? '可用' : '未装'}
              </span>
            )}

            {/* gemini-api CLI */}
            {health.gemini_cli_ok !== undefined && (
              <span
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${
                  health.gemini_cli_ok
                    ? 'bg-sky-50 border-sky-200 text-sky-700'
                    : 'bg-slate-50 border-slate-200 text-slate-500'
                }`}
              >
                <Terminal className="h-3 w-3" />
                gemini-api CLI {health.gemini_cli_ok ? '可用' : '可选未装'}
              </span>
            )}

            {/* Gemini Key 状态 */}
            {settings && (
              <span
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${
                  settings.gemini_key_set
                    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : 'bg-amber-50 border-amber-200 text-amber-800'
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    settings.gemini_key_set ? 'bg-emerald-500' : 'bg-amber-500'
                  }`}
                />
                API Key {settings.gemini_key_set ? '已配置' : '待配置'}
              </span>
            )}

            {/* 磁盘可用空间 */}
            {typeof health.disk_free_gb === 'number' && (
              <span
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${
                  diskLow
                    ? 'bg-amber-50 border-amber-300 text-amber-800'
                    : 'bg-slate-50 border-slate-200 text-slate-600'
                }`}
              >
                <HardDrive className="h-3 w-3" />
                磁盘剩余 {health.disk_free_gb.toFixed(1)} GB
              </span>
            )}
          </div>

          <button
            type="button"
            onClick={onRefreshHealth}
            disabled={refreshingHealth}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:opacity-50 cursor-pointer shrink-0"
          >
            <RefreshCw className={`h-3 w-3 ${refreshingHealth ? 'animate-spin' : ''}`} /> 重新体检
          </button>
        </div>
      )}

      {/* 2. 缺失 FFmpeg / FFprobe 醒目安装提示条 */}
      {ffmpegMissing && (
        <div className="rounded-xl border border-rose-300 bg-rose-50/90 px-4 py-3 text-xs text-rose-900 shadow-2xs space-y-2">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 text-rose-600 shrink-0 mt-0.5" />
              <div className="space-y-0.5">
                <div className="font-semibold">
                  未检测到本机 FFmpeg / FFprobe（影响本机 1080p 渲染、音画体检与三轨混音）
                </div>
                <div className="text-[11px] text-rose-800">
                  安装完成后重启终端或点击上方「重新体检」即可变绿灯，一键复制下方命令执行：
                </div>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 pl-6">
            <button
              type="button"
              onClick={() => handleCopyCmd('brew install ffmpeg', 'macOS')}
              className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-2.5 py-1 text-[11px] font-mono font-medium text-slate-800 hover:bg-rose-100 cursor-pointer"
            >
              <Copy className="h-3 w-3 text-rose-600" /> macOS: brew install ffmpeg
            </button>
            <button
              type="button"
              onClick={() => handleCopyCmd('winget install Gyan.FFmpeg', 'Windows')}
              className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-2.5 py-1 text-[11px] font-mono font-medium text-slate-800 hover:bg-rose-100 cursor-pointer"
            >
              <Copy className="h-3 w-3 text-rose-600" /> Windows: winget install Gyan.FFmpeg
            </button>
          </div>
        </div>
      )}

      {/* 3. 数据目录路径温和提醒（如含空格或非 ASCII 字符） */}
      {health?.path_warning && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/90 px-3.5 py-2 text-xs text-amber-900 flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5 text-amber-600 shrink-0" />
          <span>{health.path_warning}</span>
        </div>
      )}

      {/* 4. 未配置 GEMINI_API_KEY 时的可关闭 3 步开箱引导条 */}
      {showOnboarding && (
        <div className="rounded-2xl border border-sky-200 bg-gradient-to-r from-sky-50/95 via-white to-indigo-50/80 p-4 shadow-2xs space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-sky-600 text-white shrink-0">
                <Sparkles className="h-3.5 w-3.5" />
              </span>
              <div>
                <div className="text-sm font-semibold text-slate-900">
                  3 步完成开箱配置 · 立即体验全流程 AI 短剧制片台
                </div>
                <div className="text-[11px] text-slate-500">
                  检测到当前尚未填写 Gemini API Key，按下方 3 步操作即可开箱直出剧本、图片、视频、配乐与旁白。
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={dismissOnboarding}
              title="暂时关闭引导条"
              className="rounded-lg p-1 text-slate-400 hover:bg-slate-200/70 hover:text-slate-700 cursor-pointer shrink-0"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="grid grid-cols-1 gap-2.5 md:grid-cols-3">
            <div className="rounded-xl border border-sky-100 bg-white/90 p-3 space-y-1">
              <div className="text-xs font-semibold text-sky-900">① 选择 Google 订阅档位</div>
              <p className="text-[11px] text-slate-600 leading-relaxed">
                在系统设置点选 Free / AI Pro / AI Ultra / AI Ultra+赠金，自动校准三油箱额度红线。
              </p>
            </div>
            <div className="rounded-xl border border-sky-100 bg-white/90 p-3 space-y-1">
              <div className="text-xs font-semibold text-indigo-900">② 填入 API Key 与按需代理</div>
              <p className="text-[11px] text-slate-600 leading-relaxed">
                填入 Google AI Studio Key 即可通过内置官方 GenAI SDK 直连生图/生视频/配乐/TTS（代理留空直连）。
              </p>
            </div>
            <div className="rounded-xl border border-sky-100 bg-white/90 p-3 space-y-1">
              <div className="flex items-center justify-between gap-1 text-xs font-semibold text-emerald-900">
                <span>③ 核对 FFmpeg & 看示范样例</span>
                <span
                  className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                    ffmpegReady ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-700'
                  }`}
                >
                  FFmpeg {ffmpegReady ? '绿灯' : '红灯'}
                </span>
              </div>
              <p className="text-[11px] text-slate-600 leading-relaxed">
                已预置完整示范剧目（含 3 个防穿帮资产 + 6 镜完整脚本），可先浏览工作流再开拍。
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onOpenSettings}
              className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
            >
              <Settings className="h-3.5 w-3.5" /> 前往系统设置填写 Key
            </button>
            {demoProject && (
              <button
                type="button"
                onClick={() => onOpenDemoProject(demoProject)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-white px-3.5 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-50 cursor-pointer"
              >
                <PlayCircle className="h-3.5 w-3.5" /> 打开预置示范样例集（{demoProject.name}）
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
