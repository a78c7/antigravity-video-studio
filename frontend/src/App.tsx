import React, { Suspense, useEffect, useState } from 'react';
import {
  BarChart3,
  BookOpen,
  Clapperboard,
  CloudDownload,
  Download,
  Flame,
  FolderKanban,
  Inbox,
  LogOut,
  Settings,
  User as UserIcon,
} from 'lucide-react';
import { api, roleLabel, UNAUTHORIZED_EVENT } from './api';
import type { CloudSection } from './components/CloudWorkspaceView';
import { EnvHealthBanner } from './components/EnvHealthBanner';
import { LoginPage } from './components/LoginPage';
import { AnalyticsPage } from './pages/AnalyticsPage';
import { CollabInboxPage, type JumpSection } from './pages/CollabInboxPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { RulesCollabPage } from './pages/RulesCollabPage';
import { TopicsPage } from './pages/TopicsPage';
import { WorkspacePage } from './pages/WorkspacePage';
import type {
  AppMode,
  AuthMeResponse,
  AuthUser,
  HealthStatus,
  InboxItem,
  InboxState,
  Project,
  PublicSettings,
} from './types';

// 仅本地出片模式按需懒加载，云端协作模式既不渲染也不下载对应 chunk
const InboxDrawer = React.lazy(() => import('./components/InboxDrawer').then((m) => ({ default: m.InboxDrawer })));
const QuotaWidget = React.lazy(() => import('./components/QuotaWidget').then((m) => ({ default: m.QuotaWidget })));
const SettingsPage = React.lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

type TopTab = 'collab_inbox' | 'projects' | 'topics' | 'analytics' | 'rules' | 'settings';

export function App() {
  const [authLoaded, setAuthLoaded] = useState(false);
  const [appMode, setAppMode] = useState<AppMode>('local_producer');
  const [brandName, setBrandName] = useState('反重力视频工坊');
  const [authenticated, setAuthenticated] = useState(false);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [publicConfig, setPublicConfig] = useState<Record<string, unknown> | null>(null);

  const [tab, setTab] = useState<TopTab>('projects');
  const [collabPendingCount, setCollabPendingCount] = useState(0);

  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [pendingEpId, setPendingEpId] = useState<number | null>(null);
  const [pendingCloudSection, setPendingCloudSection] = useState<CloudSection>('review');
  const [pendingOpenRevisions, setPendingOpenRevisions] = useState(false);

  // 仅 local_producer 模式使用的状态
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [refreshingHealth, setRefreshingHealth] = useState(false);
  const [inbox, setInbox] = useState<InboxState>({ status: 'stopped', watch_dir: '~/Downloads', error: '', items: [] });
  const [inboxOpen, setInboxOpen] = useState(false);
  const [keepChineseCopy, setKeepChineseCopy] = useState(true);
  const [claimSyncVersion, setClaimSyncVersion] = useState(0);
  const [pullingCloud, setPullingCloud] = useState(false);
  const [activeTarget, setActiveTarget] = useState<{ shotId: number | null; episodeId: number | null; label: string }>({
    shotId: null,
    episodeId: null,
    label: '',
  });
  const [toast, setToast] = useState<{ msg: string; type: 'ok' | 'err' } | null>(null);

  const isCloud = appMode === 'cloud_collab';
  const collabSyncEnabled = Boolean(
    !isCloud && settings?.enable_cloud_collab && (settings?.cloud_studio_url || '').trim()
  );
  const enableHotFeeds =
    settings?.enable_hot_feeds !== undefined
      ? settings.enable_hot_feeds
      : publicConfig?.enable_hot_feeds !== undefined
        ? Boolean(publicConfig.enable_hot_feeds)
        : true;

  const notify = (msg: string, type: 'ok' | 'err' = 'ok') => {
    setToast({ msg, type });
    setTimeout(() => setToast((cur) => (cur?.msg === msg ? null : cur)), 4600);
  };

  const loadProjects = async () => {
    const res = await api.get<{ projects: Project[] }>('/api/projects');
    setProjects(res.projects || []);
    setActiveProject((cur) => {
      if (!cur) return null;
      return (res.projects || []).find((p) => p.id === cur.id) || cur;
    });
  };

  const loadSettings = async () => {
    const res = await api.get<{ settings: PublicSettings }>('/api/settings');
    setSettings(res.settings);
    if (res.settings?.brand_name) {
      setBrandName(res.settings.brand_name);
    }
    if (typeof res.settings?.keep_chinese_copy_on_claim === 'boolean') {
      setKeepChineseCopy(res.settings.keep_chinese_copy_on_claim);
    }
  };

  const loadHealth = async (manual = false) => {
    if (manual) setRefreshingHealth(true);
    try {
      const res = await api.get<HealthStatus>('/api/health');
      setHealth(res);
      if (manual) {
        notify(
          res.ffmpeg_ok === false || res.ffprobe_ok === false
            ? '环境体检完成：尚未检测到 FFmpeg，请按上方指引安装'
            : '环境体检完成：核心运行环境状态已刷新'
        );
      }
    } catch {
      // 忽略偶发探测失败
    } finally {
      if (manual) setRefreshingHealth(false);
    }
  };

  const loadInbox = async () => {
    try {
      const res = await api.get<InboxState>('/api/inbox');
      setInbox(res);
    } catch {
      // 静默忽略轮询偶发失败
    }
  };

  const bootstrapAuth = async () => {
    try {
      const me = await api.get<AuthMeResponse>('/api/auth/me');
      const mode: AppMode = me.app_mode === 'cloud_collab' ? 'cloud_collab' : 'local_producer';
      const resolvedBrand = me.brand_name || '反重力视频工坊';
      setAppMode(mode);
      setBrandName(resolvedBrand);
      setAuthenticated(Boolean(me.authenticated));
      setUser(me.user || null);
      setPublicConfig(me.public_config || null);
      if (mode === 'cloud_collab') {
        setTab((prev) => (prev === 'settings' || prev === 'projects' ? 'collab_inbox' : prev));
        document.title = `${resolvedBrand} · AI 短剧多人共创与审片看板`;
      } else {
        document.title = `${resolvedBrand} · AI 短剧本地制片台`;
      }
      setAuthLoaded(true);
    } catch {
      // 回退默认本机单人模式
      setAppMode('local_producer');
      setBrandName('反重力视频工坊');
      setAuthenticated(true);
      setUser({ id: 0, username: 'local_director', display_name: '本地导演', role: 'admin' });
      setAuthLoaded(true);
    }
  };

  useEffect(() => {
    bootstrapAuth();
  }, []);

  // 遇 401 自动切回登录页（仅自托管协作模式生效）
  useEffect(() => {
    const onUnauth = () => {
      if (appMode === 'cloud_collab') {
        setAuthenticated(false);
        setUser(null);
        setActiveProject(null);
        notify('登录状态已失效，请重新登录', 'err');
      }
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauth);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauth);
  }, [appMode]);

  // 认证完成后按模式初始化数据，并支持推送深链 ?tab=... / ?ep=... 直达
  useEffect(() => {
    if (!authLoaded) return;
    if (isCloud && !authenticated) return;

    const initData = async () => {
      try {
        const res = await api.get<{ projects: Project[] }>('/api/projects');
        const list = res.projects || [];
        setProjects(list);

        const qs = new URLSearchParams(window.location.search);
        const qTab = qs.get('tab');
        const qEp = Number(qs.get('ep') || '0');
        if (qEp > 0) {
          for (const p of list) {
            try {
              const epRes = await api.get<{ episodes: Array<{ id: number }> }>(`/api/projects/${p.id}/episodes`);
              if ((epRes.episodes || []).some((e) => e.id === qEp)) {
                setActiveProject(p);
                setPendingEpId(qEp);
                setPendingCloudSection('review');
                setTab('projects');
                break;
              }
            } catch {
              // 忽略单剧目查询异常
            }
          }
        } else if (qTab === 'rules' && isCloud) {
          setTab('rules');
        } else if (qTab === 'analytics') {
          setTab('analytics');
        } else if (qTab === 'topics') {
          setTab('topics');
        } else if (qTab === 'projects') {
          setTab('projects');
        }
      } catch (err: any) {
        notify(err?.message || '加载剧目失败', 'err');
      }
    };

    initData();

    if (!isCloud) {
      loadSettings().catch(() => {});
      loadHealth(false).catch(() => {});
      loadInbox();
      const timer = setInterval(loadInbox, 5000);
      return () => clearInterval(timer);
    }
  }, [authLoaded, isCloud, authenticated]);

  const handleLogout = async () => {
    try {
      await api.post('/api/auth/logout');
    } catch {
      // 忽略网络错误，直接本地清态
    }
    setAuthenticated(false);
    setUser(null);
    setActiveProject(null);
    setProjects([]);
    notify('已安全退出登录');
  };

  const handleLocalPullCloud = async () => {
    setPullingCloud(true);
    try {
      const res = await api.post<{
        readable_summary?: string;
        total_changes?: number;
        summary?: string;
        message?: string;
      }>('/api/local-sync/pull', {});
      await loadProjects();
      setClaimSyncVersion((v) => v + 1);
      notify(res.readable_summary || res.summary || res.message || '已从协作端拉取最新团队成员修改、投票与建议！');
    } catch (err: any) {
      notify(err?.message || '拉取远程协作更新失败', 'err');
    } finally {
      setPullingCloud(false);
    }
  };

  const toggleKeepChineseCopy = async (val: boolean) => {
    setKeepChineseCopy(val);
    try {
      const res = await api.patch<{ settings: PublicSettings }>('/api/settings', {
        KEEP_CHINESE_COPY_ON_CLAIM: val,
      });
      setSettings(res.settings);
      notify(val ? '已开启：认领参考图后自动保留中文直拖副本到直拖目录' : '已关闭：认领后仅归档至媒体库，不留额外副本');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const renameInboxItem = async (id: string, newName: string) => {
    try {
      const res = await api.post<{ item: InboxItem; inbox: InboxState }>(`/api/inbox/${id}/rename`, {
        new_name: newName,
      });
      setInbox(res.inbox);
      notify(`已原地改名为：${res.item.filename}（可直接拖入 Flow）`);
    } catch (err: any) {
      notify(err.message, 'err');
      throw err;
    }
  };

  const trashInboxItem = async (id: string) => {
    try {
      const res = await api.post<{ trashed: string; inbox: InboxState }>(`/api/inbox/${id}/trash`);
      setInbox(res.inbox);
      notify(`已将不需要的废图移入系统回收站/废纸篓：${res.trashed}`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const trashAllInboxItems = async () => {
    if (
      !confirm(
        `确认将当前 Flow 收件箱里剩余的 ${inbox.items.length} 个未认领文件移入系统废纸篓/回收站？（不会动下载目录里的普通文件，随时可恢复）`
      )
    ) {
      return;
    }
    try {
      const res = await api.post<{ trashed_count: number; inbox: InboxState }>('/api/inbox/trash-all');
      setInbox(res.inbox);
      notify(`已将 ${res.trashed_count} 个未认领 Flow 废图安全移入回收站`);
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const scanInbox = async () => {
    const res = await api.post<InboxState>('/api/inbox/scan');
    setInbox(res);
  };

  const switchInboxMode = async (mode: 'flow_only' | 'all') => {
    const res = await api.post<InboxState>('/api/inbox/mode', { mode });
    setInbox(res);
    notify(mode === 'flow_only' ? '已切到「仅收 Flow」清爽模式（自动挡杂图）' : '已切到「全部下载」模式');
  };

  const clearInbox = async () => {
    const res = await api.post<InboxState>('/api/inbox/clear');
    setInbox(res);
    notify('已清空收件箱视图（磁盘原文件完好保留）');
  };

  const handlePromoteDone = async (projectId: number, episodeId: number) => {
    await loadProjects();
    const res = await api.get<{ projects: Project[] }>('/api/projects');
    const targetProj = (res.projects || []).find((p) => p.id === projectId) || null;
    if (targetProj) {
      setActiveProject(targetProj);
      setPendingEpId(episodeId);
      setPendingCloudSection('script');
      setPendingOpenRevisions(false);
      setTab('projects');
    }
  };

  const handleJumpToEpisode = async (
    projectId: number,
    episodeId: number,
    section: JumpSection,
    openRevisions = false
  ) => {
    let targetProj = projects.find((p) => p.id === projectId) || null;
    if (!targetProj) {
      const res = await api.get<{ projects: Project[] }>('/api/projects');
      setProjects(res.projects || []);
      targetProj = (res.projects || []).find((p) => p.id === projectId) || null;
    }
    if (!targetProj) {
      notify('未找到对应剧目，请刷新后重试', 'err');
      return;
    }
    const cloudSec: CloudSection =
      section === 'script' ? 'script' : section === 'shots' ? 'shots' : 'review';
    setActiveProject(targetProj);
    setPendingEpId(episodeId);
    setPendingCloudSection(cloudSec);
    setPendingOpenRevisions(openRevisions);
    setTab('projects');
  };

  const handleDrawerClaim = async (
    item: InboxItem,
    targetType: 'shot_video' | 'shot_start_frame' | 'shot_end_frame' | 'episode_bgm'
  ) => {
    const targetId = targetType === 'episode_bgm' ? activeTarget.episodeId : activeTarget.shotId;
    if (!targetId) {
      notify('请先进入剧目并选中对应分集/分镜槽位，再一键认领', 'err');
      return;
    }
    try {
      const res = await api.post<{ dock_copy_name?: string; trashed_siblings?: string[] }>('/api/inbox/claim', {
        inbox_item_id: item.id,
        target_type: targetType,
        target_id: targetId,
        keep_chinese_copy: keepChineseCopy,
      });
      await loadInbox();
      setClaimSyncVersion((v) => v + 1);
      const slotLabel =
        targetType === 'shot_end_frame'
          ? '尾帧图'
          : targetType === 'shot_start_frame'
            ? '首帧图'
            : targetType === 'episode_bgm'
              ? '本集配乐'
              : '视频 Take';
      const trashNote =
        res.trashed_siblings && res.trashed_siblings.length > 0
          ? `，同批 ${res.trashed_siblings.length} 张落选图已移入回收站`
          : '';
      notify(
        res.dock_copy_name
          ? `已认领到 ${activeTarget.label}（${slotLabel}），并同步到直拖素材目录：${res.dock_copy_name}${trashNote}`
          : `已认领 ${item.filename} 到 ${activeTarget.label}（${slotLabel}）${trashNote}`
      );
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  // 启动认证加载态
  if (!authLoaded) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f5f7fb] text-slate-500 text-sm">
        正在进入{brandName}...
      </div>
    );
  }

  // 自托管协作模式强制登录页（DOM 100% 干净零泄漏）
  if (isCloud && !authenticated) {
    return (
      <>
        <LoginPage
          brandName={brandName}
          onLoginSuccess={async () => {
            await bootstrapAuth();
            notify(`欢迎回来，已登录${brandName}`);
          }}
        />
        {toast && (
          <div
            className={`fixed bottom-5 right-5 z-50 max-w-md rounded-xl px-4 py-3 text-xs font-medium shadow-lg border ${
              toast.type === 'err'
                ? 'bg-rose-50 border-rose-300 text-rose-800'
                : 'bg-white border-sky-300 text-slate-800'
            }`}
          >
            {toast.msg}
          </div>
        )}
      </>
    );
  }

  const navItems: Array<{
    id: TopTab;
    label: string;
    mobileLabel: string;
    icon: React.ComponentType<{ className?: string }>;
    badge?: number;
  }> = isCloud
    ? [
        { id: 'collab_inbox', label: '待我协作', mobileLabel: '待协作', icon: Inbox, badge: collabPendingCount },
        { id: 'projects', label: '剧目与审片', mobileLabel: '剧目审片', icon: FolderKanban },
        { id: 'topics', label: '热点与选题雷达', mobileLabel: '热点雷达', icon: Flame },
        { id: 'analytics', label: '发布与评论留痕', mobileLabel: '发布评论', icon: BarChart3 },
        { id: 'rules', label: '创作规范与建议', mobileLabel: '创作规范', icon: BookOpen },
      ]
    : [
        { id: 'projects', label: '项目大厅', mobileLabel: '项目大厅', icon: FolderKanban },
        { id: 'topics', label: '热点与选题库', mobileLabel: '热点选题', icon: Flame },
        { id: 'analytics', label: '发布与数据复盘', mobileLabel: '发布复盘', icon: BarChart3 },
        { id: 'settings', label: '系统设置与规则', mobileLabel: '设置规则', icon: Settings },
      ];

  const subtitle = isCloud
    ? 'AI 短剧多人共创与审片看板'
    : collabSyncEnabled
      ? '本地制片台 · 已开启远程协作'
      : 'AI 短剧本地制片台';

  return (
    <div className="min-h-screen flex flex-col md:flex-row bg-[#f5f7fb] text-slate-900">
      {/* 移动端顶部栏（适配 390px 手机浏览器） */}
      <header className="md:hidden sticky top-0 z-30 flex items-center justify-between gap-2 border-b border-slate-200 bg-white/95 backdrop-blur px-3.5 py-2.5">
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-indigo-600 text-white shadow-xs">
            <Clapperboard className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-bold tracking-tight text-slate-900 truncate">{brandName}</div>
            <div className="text-[10px] text-slate-500 truncate">{subtitle}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {user && (
            <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 px-2 py-0.5 text-[10px] font-medium text-slate-700">
              <UserIcon className="h-2.5 w-2.5 text-sky-600" />
              {user.display_name || user.username}
            </span>
          )}
          {isCloud && (
            <button
              type="button"
              onClick={handleLogout}
              title="退出登录"
              className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </header>

      {/* 桌面端左侧主导航 */}
      <aside className="hidden md:flex w-56 shrink-0 border-r border-slate-200 bg-white flex-col justify-between p-3.5 sticky top-0 h-screen">
        <div className="space-y-6">
          <div className="px-2 pt-1">
            <div className="flex items-center gap-2 text-base font-bold tracking-tight text-slate-900">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-indigo-600 text-white shadow-xs">
                <Clapperboard className="h-4 w-4" />
              </span>
              {brandName}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">{subtitle}</div>
          </div>

          <nav className="space-y-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = tab === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => {
                    setTab(item.id);
                    if (item.id !== 'projects') setActiveProject(null);
                  }}
                  className={`w-full flex items-center justify-between gap-2 rounded-xl px-3 py-2.5 text-sm font-medium transition cursor-pointer ${
                    active
                      ? 'bg-sky-50 text-sky-700 border border-sky-200 font-semibold'
                      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900 border border-transparent'
                  }`}
                >
                  <span className="inline-flex items-center gap-2.5 truncate">
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="truncate">{item.label}</span>
                  </span>
                  {item.badge !== undefined && item.badge > 0 && (
                    <span className="rounded-full bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold text-white leading-none">
                      {item.badge}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        {/* 左下角常驻区：协作模式仅显示登录人+退出；本地单机清爽显示收件箱+额度监控（仅当显式开启远程协作且配置地址后才显示拉取按钮） */}
        <div className="space-y-2.5">
          {!isCloud && (
            <>
              {collabSyncEnabled && (
                <button
                  type="button"
                  onClick={handleLocalPullCloud}
                  disabled={pullingCloud}
                  title="从自托管协作服务端拉取团队成员的最新改稿、投票、规范建议与评论回复"
                  className="w-full flex items-center justify-center gap-1.5 rounded-xl border border-indigo-200 bg-indigo-50/70 px-3 py-2 text-xs font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-50 transition cursor-pointer"
                >
                  <CloudDownload className={`h-3.5 w-3.5 text-indigo-600 ${pullingCloud ? 'animate-bounce' : ''}`} />
                  {pullingCloud ? '拉取中...' : '拉取远程协作更新'}
                </button>
              )}

              <button
                onClick={() => setInboxOpen(true)}
                className="w-full flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-medium text-slate-700 hover:border-sky-300 hover:bg-sky-50/60 transition cursor-pointer"
              >
                <span className="inline-flex items-center gap-2">
                  <Download className="h-3.5 w-3.5 text-sky-600" /> Flow 收件箱
                </span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${
                    inbox.items.length > 0 ? 'bg-amber-500 text-white' : 'bg-slate-200 text-slate-600'
                  }`}
                >
                  {inbox.items.length}
                </span>
              </button>

              <Suspense fallback={null}>
                <QuotaWidget notify={notify} />
              </Suspense>
            </>
          )}

          {user && (
            <div className="rounded-xl border border-slate-200 bg-slate-50/90 px-3 py-2.5 flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-xs font-semibold text-slate-900 truncate">
                  {user.display_name || user.username}
                </div>
                <div className="text-[10px] text-slate-500 truncate">{roleLabel(user.role)}</div>
              </div>
              {isCloud && (
                <button
                  type="button"
                  onClick={handleLogout}
                  title="退出登录"
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-rose-50 hover:border-rose-200 hover:text-rose-700 cursor-pointer shrink-0"
                >
                  <LogOut className="h-3 w-3" /> 退出
                </button>
              )}
            </div>
          )}
        </div>
      </aside>

      {/* 主内容区（移动端留出底部导航高度防遮挡） */}
      <main className="flex-1 min-w-0 p-3.5 sm:p-5 md:p-6 pb-24 md:pb-6 overflow-x-hidden">
        {!isCloud && (
          <EnvHealthBanner
            health={health}
            settings={settings}
            projects={projects}
            refreshingHealth={refreshingHealth}
            onRefreshHealth={() => loadHealth(true)}
            onOpenSettings={() => {
              setActiveProject(null);
              setTab('settings');
            }}
            onOpenDemoProject={(demoProj) => {
              setPendingEpId(null);
              setPendingCloudSection('review');
              setPendingOpenRevisions(false);
              setActiveProject(demoProj);
              setTab('projects');
            }}
            notify={notify}
          />
        )}

        {isCloud && tab === 'collab_inbox' && (
          <CollabInboxPage
            user={user}
            onJumpToEpisode={handleJumpToEpisode}
            onJumpToRules={() => {
              setActiveProject(null);
              setTab('rules');
            }}
            onJumpToComments={() => {
              setActiveProject(null);
              setTab('analytics');
            }}
            onInboxLoaded={setCollabPendingCount}
            notify={notify}
          />
        )}

        {tab === 'projects' && !activeProject && (
          <ProjectsPage
            projects={projects}
            appMode={appMode}
            user={user}
            onRefresh={loadProjects}
            onOpenProject={(p) => {
              setPendingEpId(null);
              setPendingCloudSection('review');
              setPendingOpenRevisions(false);
              setActiveProject(p);
            }}
            notify={notify}
          />
        )}

        {tab === 'projects' && activeProject && (
          <WorkspacePage
            project={activeProject}
            appMode={appMode}
            user={user}
            settings={settings}
            initialEpisodeId={pendingEpId}
            initialCloudSection={pendingCloudSection}
            initialOpenRevisions={pendingOpenRevisions}
            inbox={inbox}
            keepChineseCopy={keepChineseCopy}
            onToggleKeepChineseCopy={toggleKeepChineseCopy}
            onRenameInboxItem={renameInboxItem}
            onTrashInboxItem={trashInboxItem}
            claimSyncVersion={claimSyncVersion}
            onRefreshInbox={scanInbox}
            onPullCloud={collabSyncEnabled ? handleLocalPullCloud : undefined}
            onBack={() => {
              setActiveProject(null);
              loadProjects();
            }}
            onProjectUpdated={loadProjects}
            onActiveTargetChange={setActiveTarget}
            notify={notify}
          />
        )}

        {tab === 'topics' && (
          <TopicsPage
            projects={projects}
            appMode={appMode}
            enableHotFeeds={enableHotFeeds}
            onOpenSettings={!isCloud ? () => setTab('settings') : undefined}
            onPromoteDone={handlePromoteDone}
            notify={notify}
          />
        )}

        {tab === 'analytics' && <AnalyticsPage projects={projects} appMode={appMode} user={user} notify={notify} />}

        {isCloud && tab === 'rules' && <RulesCollabPage user={user} notify={notify} />}

        {!isCloud && tab === 'settings' && (
          <Suspense fallback={<div className="py-12 text-center text-xs text-slate-400">正在加载本机设置...</div>}>
            <SettingsPage
              settings={settings}
              onSettingsUpdated={async () => {
                await loadSettings();
                await loadHealth(false);
              }}
              notify={notify}
            />
          </Suspense>
        )}
      </main>

      {/* 移动端底部导航栏（适配单手手机浏览器） */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-30 border-t border-slate-200 bg-white/95 backdrop-blur px-1.5 py-1.5 pb-[max(0.375rem,env(safe-area-inset-bottom))]">
        <div
          className="grid gap-1"
          style={{ gridTemplateColumns: `repeat(${navItems.length}, minmax(0, 1fr))` }}
        >
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setTab(item.id);
                  if (item.id !== 'projects') setActiveProject(null);
                }}
                className={`relative flex flex-col items-center justify-center gap-0.5 rounded-xl py-1.5 text-[10px] font-medium transition cursor-pointer ${
                  active ? 'bg-sky-50 text-sky-700 font-semibold' : 'text-slate-500'
                }`}
              >
                <Icon className="h-4 w-4" />
                <span className="truncate max-w-full px-0.5">{item.mobileLabel}</span>
                {item.badge !== undefined && item.badge > 0 && (
                  <span className="absolute top-1 right-2 min-w-[16px] h-4 px-1 rounded-full bg-amber-500 text-[9px] font-bold text-white flex items-center justify-center">
                    {item.badge > 99 ? '99+' : item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </nav>

      {/* 仅本地出片模式挂载 Downloads 收件箱抽屉（云端协作模式绝不渲染） */}
      {!isCloud && (
        <Suspense fallback={null}>
          <InboxDrawer
            open={inboxOpen}
            onClose={() => setInboxOpen(false)}
            inbox={inbox}
            keepChineseCopy={keepChineseCopy}
            onToggleKeepChineseCopy={toggleKeepChineseCopy}
            onScan={scanInbox}
            onSwitchMode={switchInboxMode}
            onClearAll={clearInbox}
            onDismiss={async (id) => {
              const res = await api.del<InboxState>(`/api/inbox/${id}`);
              setInbox(res);
            }}
            onTrashItem={trashInboxItem}
            onTrashAll={trashAllInboxItems}
            onRenameItem={renameInboxItem}
            activeShotLabel={activeTarget.label}
            onClaimToShotVideo={activeTarget.shotId ? (it) => handleDrawerClaim(it, 'shot_video') : undefined}
            onClaimToStartFrame={activeTarget.shotId ? (it) => handleDrawerClaim(it, 'shot_start_frame') : undefined}
            onClaimToEndFrame={activeTarget.shotId ? (it) => handleDrawerClaim(it, 'shot_end_frame') : undefined}
            onClaimToBgm={activeTarget.episodeId ? (it) => handleDrawerClaim(it, 'episode_bgm') : undefined}
          />
        </Suspense>
      )}

      {/* 全局轻提示 Toast */}
      {toast && (
        <div
          className={`fixed bottom-20 md:bottom-5 right-3.5 left-3.5 md:left-auto md:right-5 z-50 md:max-w-md rounded-xl px-4 py-3 text-xs font-medium shadow-lg border ${
            toast.type === 'err'
              ? 'bg-rose-50 border-rose-300 text-rose-800'
              : 'bg-white border-sky-300 text-slate-800'
          }`}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}

export default App;
