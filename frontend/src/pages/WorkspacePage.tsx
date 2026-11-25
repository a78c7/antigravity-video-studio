import React, { Suspense } from 'react';
import { CloudWorkspaceView, type CloudSection } from '../components/CloudWorkspaceView';
import type { AppMode, AuthUser, InboxState, Project, PublicSettings } from '../types';

// 仅本地出片模式按需懒加载，云端协作模式既不渲染也不下载本机重型工作台 chunk
const LocalWorkspacePage = React.lazy(() =>
  import('./LocalWorkspacePage').then((m) => ({ default: m.LocalWorkspacePage }))
);

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

export const WorkspacePage: React.FC<Props> = (props) => {
  if (props.appMode === 'cloud_collab') {
    return (
      <CloudWorkspaceView
        project={props.project}
        initialEpisodeId={props.initialEpisodeId}
        initialSection={props.initialCloudSection}
        initialOpenRevisions={props.initialOpenRevisions}
        user={props.user || null}
        onBack={props.onBack}
        notify={props.notify}
      />
    );
  }

  return (
    <Suspense fallback={<div className="py-16 text-center text-xs text-slate-400">正在加载本地出片工作台...</div>}>
      <LocalWorkspacePage {...props} />
    </Suspense>
  );
};
