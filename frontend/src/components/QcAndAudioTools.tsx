import React, { useMemo, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Eye,
  FolderOpen,
  Grid,
  Loader2,
  Mic,
  Sparkles,
  Trash2,
  Upload,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react';
import { api, mediaUrl } from '../api';
import { ImageLightbox } from './ImageLightbox';
import type {
  AsyncJob,
  Episode,
  QcFramesSheetResponse,
  QcInspectVideoItem,
  QcInspectVideosResponse,
  QcShotReport,
  Shot,
} from '../types';

/* ============================================================================
 * 1. 定图区：全集首尾帧编号总览拼图 + 灯箱放大（POST /api/episodes/{id}/qc-frames-sheet）
 * ============================================================================ */

interface FramesSheetToolProps {
  episodeId: number | null;
  episode: Episode | null;
  shots: Shot[];
  onUpdated: () => Promise<void>;
  onRevealPath?: (path: string) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

export const FramesSheetTool: React.FC<FramesSheetToolProps> = ({
  episodeId,
  episode,
  shots,
  onUpdated,
  onRevealPath,
  notify,
}) => {
  const [busy, setBusy] = useState(false);
  const [sheetPath, setSheetPath] = useState<string>('');
  const [lightboxOpen, setLightboxOpen] = useState(false);

  const effectiveSheet = sheetPath || episode?.frames_sheet_path || '';
  const readyFrameCount = shots.filter((s) => Boolean((s.start_frame_path || '').trim())).length;

  const handleBuildSheet = async () => {
    if (!episodeId) return;
    setBusy(true);
    try {
      const res = await api.post<QcFramesSheetResponse>(
        `/api/episodes/${episodeId}/qc-frames-sheet`,
        { cols: 2 }
      );
      const outPath = res.sheet_path || res.output_path || '';
      setSheetPath(outPath);
      await onUpdated();
      notify(`已生成全集首尾帧编号总览拼图（共 ${res.image_count} 张静帧）`);
      if (outPath) setLightboxOpen(true);
    } catch (err: any) {
      notify(err?.message || '生成首尾帧编号总览拼图失败', 'err');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="inline-flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={handleBuildSheet}
          disabled={busy || !episodeId || readyFrameCount === 0}
          title={
            readyFrameCount === 0
              ? '请先至少生成或上传 1 张首帧图'
              : '将本集全部已定稿首尾帧拼成高对比度大号编号总览对比图'
          }
          className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-45 cursor-pointer"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Grid className="h-3.5 w-3.5" />}
          {busy ? '拼图生成中...' : `生成全集首尾帧编号总览拼图 (${readyFrameCount}/${shots.length})`}
        </button>

        {effectiveSheet && (
          <>
            <button
              type="button"
              onClick={() => setLightboxOpen(true)}
              className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-white px-2.5 py-2 text-xs font-medium text-sky-700 hover:bg-sky-50 cursor-pointer"
            >
              <Eye className="h-3.5 w-3.5" /> 放大查看总览图
            </button>
            {onRevealPath && (
              <button
                type="button"
                onClick={() => onRevealPath(effectiveSheet)}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
              >
                <FolderOpen className="h-3.5 w-3.5" /> 目录
              </button>
            )}
          </>
        )}
      </div>

      <ImageLightbox
        src={lightboxOpen && effectiveSheet ? `${mediaUrl(effectiveSheet)}&t=${Date.now()}` : null}
        title={episode ? `第 ${episode.ep_number} 集 · 全集首尾帧编号总览拼图` : '全集首尾帧编号总览拼图'}
        downloadHref={effectiveSheet ? mediaUrl(effectiveSheet, true) : undefined}
        onClose={() => setLightboxOpen(false)}
      />
    </>
  );
};

/* ============================================================================
 * 2. 视频体检区：3 帧 + 音轨幻觉体检（POST /api/episodes/{id}/qc-inspect-videos）
 * ============================================================================ */

interface VideoQcInspectToolProps {
  episodeId: number | null;
  episode: Episode | null;
  shots: Shot[];
  onUpdated: () => Promise<void>;
  onRevealPath?: (path: string) => void;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

function parseShotQcReport(raw?: string): QcShotReport | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? (parsed as QcShotReport) : null;
  } catch {
    return null;
  }
}

function isMidbandHallucinationWarning(w: string): boolean {
  return w.includes('中频') || w.includes('幻觉') || w.includes('300-3400Hz');
}

export const VideoQcInspectTool: React.FC<VideoQcInspectToolProps> = ({
  episodeId,
  episode,
  shots,
  onUpdated,
  onRevealPath,
  notify,
}) => {
  const [busy, setBusy] = useState(false);
  const [latestResult, setLatestResult] = useState<QcInspectVideosResponse | null>(null);
  const [lightboxSrc, setLightboxSrc] = useState<{ url: string; title: string; downloadHref?: string } | null>(null);

  // 自动从当前 shots 与 episode 恢复已落盘的体检结果，刷新页面不丢失
  const displayItems: QcInspectVideoItem[] = useMemo(() => {
    if (latestResult && latestResult.episode_id === episodeId) {
      return latestResult.shots;
    }
    const items: QcInspectVideoItem[] = [];
    for (const s of shots) {
      const report = parseShotQcReport(s.qc_report_json);
      if (!report && !s.qc_strip_path) continue;
      const warnings = Array.isArray(report?.warnings) ? report!.warnings : [];
      const hasWarn = warnings.length > 0 || report?.status === 'warn';
      items.push({
        shot_id: s.id,
        shot_no: s.shot_no,
        shot_title: s.shot_title,
        qc_strip_path: s.qc_strip_path || report?.strip_path || '',
        expect_dialogue: report?.expect_dialogue,
        has_warning: hasWarn,
        warnings,
        report: report || {},
      });
    }
    return items;
  }, [latestResult, episodeId, shots]);

  const overviewSheetPath =
    (latestResult && latestResult.episode_id === episodeId ? latestResult.overview_sheet_path : '') ||
    episode?.qc_overview_path ||
    '';

  const warningCount = displayItems.filter((x) => x.has_warning).length;
  const passedCount = displayItems.length - warningCount;

  const handleRunInspect = async () => {
    if (!episodeId) return;
    setBusy(true);
    try {
      const res = await api.post<QcInspectVideosResponse>(
        `/api/episodes/${episodeId}/qc-inspect-videos`,
        { cols: 1 }
      );
      setLatestResult(res);
      await onUpdated();
      if (res.warning_count > 0) {
        notify(
          `3 帧 + 音轨体检完成：共检 ${res.inspected_count} 镜，发现 ${res.warning_count} 镜存在音画/中频幻觉告警，请复听核查`,
          'err'
        );
      } else {
        notify(`3 帧 + 音轨体检全部通过（共 ${res.inspected_count} 镜正常）！`);
      }
    } catch (err: any) {
      notify(err?.message || '3 帧 + 音轨幻觉体检失败', 'err');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="studio-card rounded-2xl p-4 space-y-3 border-amber-200/80">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-900">
            <Activity className="h-4 w-4 text-amber-600" />
            视频音画体检（3 帧画面抽检 + dB 响度 + 中频人声幻觉告警）
            {displayItems.length > 0 && (
              <span
                className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                  warningCount > 0
                    ? 'bg-rose-50 border-rose-200 text-rose-700'
                    : 'bg-emerald-50 border-emerald-200 text-emerald-700'
                }`}
              >
                {warningCount > 0 ? `告警 ${warningCount} 镜 · 正常 ${passedCount} 镜` : `全部正常 (${passedCount} 镜)`}
              </span>
            )}
          </div>
          <p className="text-[11px] text-slate-500">
            自动抽取每镜视频头/中/尾 3 帧并分析 volumedetect / silencedetect / 300–3400Hz 中频能量，提前揪出黑屏、静音、爆音与非台词镜 AI 乱说话幻觉。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {overviewSheetPath && (
            <button
              type="button"
              onClick={() =>
                setLightboxSrc({
                  url: `${mediaUrl(overviewSheetPath)}&t=${Date.now()}`,
                  title: '全集视频 3 帧 + 音轨体检总览拼图',
                  downloadHref: mediaUrl(overviewSheetPath, true),
                })
              }
              className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-white px-2.5 py-1.5 text-xs font-medium text-sky-700 hover:bg-sky-50 cursor-pointer"
            >
              <Eye className="h-3.5 w-3.5" /> 放大总览拼图
            </button>
          )}
          {overviewSheetPath && onRevealPath && (
            <button
              type="button"
              onClick={() => onRevealPath(overviewSheetPath)}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
            >
              <FolderOpen className="h-3.5 w-3.5" /> 目录
            </button>
          )}
          <button
            type="button"
            onClick={handleRunInspect}
            disabled={busy || !episodeId}
            className="inline-flex items-center gap-1.5 rounded-lg bg-amber-500 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-amber-600 disabled:opacity-45 cursor-pointer"
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Activity className="h-3.5 w-3.5" />}
            {busy ? '体检扫描中...' : '3 帧 + 音轨幻觉体检'}
          </button>
        </div>
      </div>

      {/* 全集体检总览拼图缩略预览 */}
      {overviewSheetPath && (
        <div className="rounded-xl border border-slate-200 bg-slate-900/95 p-2 flex flex-col sm:flex-row items-center gap-3">
          <img
            src={mediaUrl(overviewSheetPath)}
            alt="全集体检总览拼图"
            onClick={() =>
              setLightboxSrc({
                url: `${mediaUrl(overviewSheetPath)}&t=${Date.now()}`,
                title: '全集视频 3 帧 + 音轨体检总览拼图',
                downloadHref: mediaUrl(overviewSheetPath, true),
              })
            }
            className="h-28 sm:w-56 w-full object-cover rounded-lg cursor-zoom-in border border-slate-700"
          />
          <div className="text-xs text-slate-200 space-y-1 flex-1">
            <div className="font-semibold text-white">全集音画体检总览拼图已就绪</div>
            <div className="text-[11px] text-slate-300">
              已检 {displayItems.length} 镜 · 正常 {passedCount} 镜 · 告警 {warningCount} 镜（点击左侧图片可灯箱放大逐帧细看）
            </div>
          </div>
        </div>
      )}

      {/* 逐镜 dB 与中频幻觉告警明细卡片 */}
      {displayItems.length > 0 && (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {displayItems.map((item) => {
            const rep = item.report || {};
            const hasHallucination = item.warnings.some(isMidbandHallucinationWarning);
            return (
              <div
                key={item.shot_id}
                className={`rounded-xl border p-3 space-y-2 text-xs ${
                  item.has_warning
                    ? 'border-rose-200 bg-rose-50/45'
                    : 'border-emerald-200/80 bg-emerald-50/25'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="rounded bg-sky-50 border border-sky-200 px-1.5 py-0.5 font-mono text-[10px] font-bold text-sky-700">
                      S{String(item.shot_no).padStart(2, '0')}
                    </span>
                    <span className="font-semibold text-slate-900 truncate">{item.shot_title}</span>
                    {item.resolution_label && (
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                        {item.resolution_label}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {item.qc_strip_path && (
                      <button
                        type="button"
                        onClick={() =>
                          setLightboxSrc({
                            url: mediaUrl(item.qc_strip_path!),
                            title: `S${String(item.shot_no).padStart(2, '0')} ${item.shot_title} · 3 帧体检条`,
                            downloadHref: mediaUrl(item.qc_strip_path!, true),
                          })
                        }
                        className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-700 hover:bg-slate-100 cursor-pointer"
                      >
                        看 3 帧
                      </button>
                    )}
                    {item.has_warning ? (
                      <span className="inline-flex items-center gap-0.5 rounded-full bg-rose-100 border border-rose-200 px-2 py-0.5 text-[10px] font-semibold text-rose-700">
                        <AlertTriangle className="h-2.5 w-2.5" /> 需复听
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-0.5 rounded-full bg-emerald-100 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
                        <CheckCircle2 className="h-2.5 w-2.5" /> 正常
                      </span>
                    )}
                  </div>
                </div>

                {/* dB 与中频能量标签 */}
                <div className="flex flex-wrap gap-1.5 text-[10px] font-mono">
                  {rep.has_audio === false ? (
                    <span className="rounded bg-amber-100 border border-amber-200 px-1.5 py-0.5 text-amber-800">
                      无音轨
                    </span>
                  ) : (
                    <>
                      {typeof rep.mean_volume_db === 'number' && (
                        <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-slate-700">
                          均值 {rep.mean_volume_db.toFixed(1)} dB
                        </span>
                      )}
                      {typeof rep.max_volume_db === 'number' && (
                        <span
                          className={`rounded border px-1.5 py-0.5 ${
                            rep.max_volume_db > -0.3
                              ? 'bg-rose-100 border-rose-300 text-rose-800 font-semibold'
                              : 'bg-white border-slate-200 text-slate-700'
                          }`}
                        >
                          峰值 {rep.max_volume_db.toFixed(1)} dB
                        </span>
                      )}
                      {typeof rep.voice_band_mean_db === 'number' && (
                        <span
                          className={`rounded border px-1.5 py-0.5 ${
                            hasHallucination
                              ? 'bg-rose-100 border-rose-300 text-rose-800 font-semibold'
                              : 'bg-white border-slate-200 text-slate-700'
                          }`}
                        >
                          中频人声 {rep.voice_band_mean_db.toFixed(1)} dB
                        </span>
                      )}
                      {typeof rep.silence_ratio === 'number' && (
                        <span className="rounded bg-white border border-slate-200 px-1.5 py-0.5 text-slate-600">
                          静音 {Math.round(rep.silence_ratio * 100)}%
                        </span>
                      )}
                    </>
                  )}
                  {hasHallucination && (
                    <span className="rounded bg-rose-600 px-1.5 py-0.5 font-sans font-semibold text-white">
                      疑似中频幻觉乱说话
                    </span>
                  )}
                </div>

                {item.warnings.length > 0 && (
                  <div className="space-y-1">
                    {item.warnings.map((w, idx) => (
                      <div key={idx} className="text-[11px] text-rose-800 leading-snug">
                        • {w}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <ImageLightbox
        src={lightboxSrc?.url || null}
        title={lightboxSrc?.title}
        downloadHref={lightboxSrc?.downloadHref}
        onClose={() => setLightboxSrc(null)}
      />
    </div>
  );
};

/* ============================================================================
 * 3. 三轨配音区：批量中文 TTS 弹窗 + 每镜干声试听/上传/切纯字幕
 *    (POST /api/episodes/{id}/cli-generate-tts & POST /api/shots/{id}/upload-vo)
 * ============================================================================ */

interface BatchTtsAndVoPanelProps {
  episodeId: number | null;
  episode: Episode | null;
  shots: Shot[];
  ttsModelName?: string;
  ttsPricePerShot?: number;
  activeTtsRunning?: boolean;
  onJobSubmitted?: (job?: AsyncJob) => void;
  onUpdated: () => Promise<void>;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

const TTS_VOICE_OPTIONS = [
  { id: 'Kore', label: 'Kore（清晰自然 · 女声默认推荐）' },
  { id: 'Puck', label: 'Puck（活泼轻快 · 少年/解说感）' },
  { id: 'Aoede', label: 'Aoede（温柔叙事 · 故事旁白）' },
  { id: 'Charon', label: 'Charon（沉稳磁性 · 纪录/悬疑）' },
  { id: 'Fenrir', label: 'Fenrir（浑厚有力 · 震撼开场）' },
  { id: 'Zephyr', label: 'Zephyr（清亮明快 · 都市轻喜剧）' },
];

export const BatchTtsAndVoPanel: React.FC<BatchTtsAndVoPanelProps> = ({
  episodeId,
  episode,
  shots,
  ttsModelName = 'gemini-3.8-flash-tts',
  ttsPricePerShot = 0.01,
  activeTtsRunning = false,
  onJobSubmitted,
  onUpdated,
  notify,
}) => {
  const [modalOpen, setModalOpen] = useState(false);
  const [voice, setVoice] = useState('Kore');
  const [stylePrompt, setStylePrompt] = useState('自然温暖、语速适中、有画面感的中文短视频旁白');
  const [submittingTts, setSubmittingTts] = useState(false);
  const [shotBusyMap, setShotBusyMap] = useState<Record<number, boolean>>({});
  const [audioNonce, setAudioNonce] = useState<Record<number, number>>({});

  const shotsWithText = shots.filter((s) => Boolean((s.cn_dialogue || s.subtitle_text || '').trim()));
  const voReadyCount = shots.filter((s) => Boolean((s.vo_file_path || '').trim())).length;

  const handleBatchTts = async () => {
    if (!episodeId) return;
    setSubmittingTts(true);
    try {
      const res = await api.post<{
        ok?: boolean;
        async?: boolean;
        job_id?: number;
        job?: AsyncJob;
        generated_count?: number;
        total_cost_usd?: number;
      }>(`/api/episodes/${episodeId}/cli-generate-tts`, {
        voice,
        style_prompt: stylePrompt.trim(),
        async_mode: true,
      });

      setModalOpen(false);
      if (res.async || res.job_id) {
        notify(`已提交后台批量 TTS 配音任务（音色 ${voice}），可在顶部任务队列实时查看进度！`);
        onJobSubmitted?.(res.job);
      } else {
        await onUpdated();
        notify(
          `批量生成中文 TTS 旁白完成（${res.generated_count || 0} 镜 · 约 $${(res.total_cost_usd || 0).toFixed(2)}）`
        );
      }
    } catch (err: any) {
      notify(err?.message || '批量生成中文 TTS 旁白失败', 'err');
    } finally {
      setSubmittingTts(false);
    }
  };

  const handleUploadShotVo = async (shot: Shot, file: File) => {
    setShotBusyMap((prev) => ({ ...prev, [shot.id]: true }));
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('vo_source', 'human');
      await api.upload(`/api/shots/${shot.id}/upload-vo`, fd);
      setAudioNonce((prev) => ({ ...prev, [shot.id]: Date.now() }));
      await onUpdated();
      notify(`已为 S${String(shot.shot_no).padStart(2, '0')} 绑定真人/克隆干声：${file.name}`);
    } catch (err: any) {
      notify(err?.message || '上传干声失败', 'err');
    } finally {
      setShotBusyMap((prev) => ({ ...prev, [shot.id]: false }));
    }
  };

  const handleClearShotVo = async (shot: Shot) => {
    setShotBusyMap((prev) => ({ ...prev, [shot.id]: true }));
    try {
      await api.post(`/api/shots/${shot.id}/upload-vo`, { clear: true });
      await onUpdated();
      notify(`S${String(shot.shot_no).padStart(2, '0')} 已清除旁白，切回纯字幕模式`);
    } catch (err: any) {
      notify(err?.message || '清除旁白失败', 'err');
    } finally {
      setShotBusyMap((prev) => ({ ...prev, [shot.id]: false }));
    }
  };

  return (
    <div className="studio-card rounded-2xl p-4 space-y-3 border-emerald-200/90">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-900">
            <Mic className="h-4 w-4 text-emerald-600" />
            三轨配音区：旁白干声（TTS 批量配音 / 真人克隆上传 / 切纯字幕）
            <span className="rounded-full bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
              已配旁白 {voReadyCount}/{shots.length} 镜
            </span>
            {episode?.vo_mode && episode.vo_mode !== 'none' && (
              <span className="rounded-full bg-sky-50 border border-sky-200 px-2 py-0.5 text-[10px] font-medium text-sky-700">
                当前模式：{episode.vo_mode === 'tts' ? 'AI TTS 旁白' : '真人/克隆干声'}
              </span>
            )}
          </div>
          <p className="text-[11px] text-slate-500">
            渲染本机 1080p 或 Colab 4K 母带时，将自动执行「原生环境音 + 旁白干声 VO + BGM 自动侧链闪避」三轨混音，并在 VO 超长时自动对齐镜头时长。
          </p>
        </div>

        <button
          type="button"
          onClick={() => setModalOpen(true)}
          disabled={!episodeId || shotsWithText.length === 0 || activeTtsRunning}
          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-45 cursor-pointer"
        >
          {activeTtsRunning ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Sparkles className="h-3.5 w-3.5" />
          )}
          {activeTtsRunning
            ? '后台批量 TTS 配音中...'
            : `一键批量生成中文 TTS 旁白 (${shotsWithText.length}镜 · ~$${(shotsWithText.length * ttsPricePerShot).toFixed(2)})`}
        </button>
      </div>

      {/* 逐镜旁白控件列表 */}
      <div className="space-y-2 max-h-80 overflow-y-auto pr-1">
        {shots.map((s) => {
          const textPreview = (s.cn_dialogue || s.subtitle_text || '').trim();
          const hasVo = Boolean((s.vo_file_path || '').trim());
          const busy = Boolean(shotBusyMap[s.id]);
          const nonce = audioNonce[s.id];
          const voSrc = hasVo
            ? `${mediaUrl(s.vo_file_path!)}${nonce ? `&t=${nonce}` : ''}`
            : '';

          return (
            <div
              key={s.id}
              className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-2.5 flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="font-mono font-bold text-sky-700">
                    S{String(s.shot_no).padStart(2, '0')}
                  </span>
                  <span className="font-medium text-slate-900 truncate">{s.shot_title}</span>
                  {hasVo ? (
                    <span className="inline-flex items-center gap-0.5 rounded bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                      <Volume2 className="h-2.5 w-2.5" />
                      {s.vo_source === 'tts' ? 'AI TTS 旁白' : '真人/克隆干声'}
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-0.5 rounded bg-slate-100 border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-500">
                      <VolumeX className="h-2.5 w-2.5" /> 纯字幕（无旁白）
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-slate-600 truncate">
                  {textPreview || '（本镜暂无台词或字幕文案）'}
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2 shrink-0">
                {hasVo && (
                  <audio
                    key={`${s.id}-${s.vo_file_path}-${nonce || 0}`}
                    src={voSrc}
                    controls
                    className="h-8 w-52 sm:w-60"
                  />
                )}
                <label
                  className={`inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-white px-2.5 py-1.5 text-[11px] font-medium text-sky-700 hover:bg-sky-50 cursor-pointer ${
                    busy ? 'opacity-45 pointer-events-none' : ''
                  }`}
                >
                  {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
                  上传真人/克隆干声
                  <input
                    type="file"
                    accept="audio/*,.wav,.mp3,.m4a,.aac,.flac,.ogg"
                    className="hidden"
                    disabled={busy}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) handleUploadShotVo(s, f);
                      e.target.value = '';
                    }}
                  />
                </label>
                {hasVo && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => handleClearShotVo(s)}
                    className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-medium text-slate-600 hover:bg-rose-50 hover:border-rose-200 hover:text-rose-700 disabled:opacity-45 cursor-pointer"
                  >
                    <Trash2 className="h-3 w-3" /> 切纯字幕（清除旁白）
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {shots.length === 0 && (
          <div className="py-6 text-center text-xs text-slate-400">当前分集还没有分镜。</div>
        )}
      </div>

      {/* 批量中文 TTS 配置弹窗 */}
      {modalOpen && (
        <div
          onClick={() => !submittingTts && setModalOpen(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/55 backdrop-blur-xs p-4"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-5 shadow-xl space-y-4"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                <Mic className="h-4 w-4 text-emerald-600" />
                一键批量生成中文 TTS 旁白
              </div>
              <button
                type="button"
                disabled={submittingTts}
                onClick={() => setModalOpen(false)}
                className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 cursor-pointer"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="rounded-xl bg-emerald-50/70 border border-emerald-200 px-3 py-2 text-[11px] text-emerald-900 leading-relaxed">
              将为本集 <strong>{shotsWithText.length}</strong> 个含台词/字幕的镜头并发生成 24kHz WAV 旁白
              （模型 <code className="font-mono">{ttsModelName}</code>，预估约{' '}
              <strong>${(shotsWithText.length * ttsPricePerShot).toFixed(2)}</strong>）。提交后在后台异步运行，不阻塞页面操作。
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="mb-1 block font-medium text-slate-700">选择配音音色（Voice）</label>
                <select
                  value={voice}
                  onChange={(e) => setVoice(e.target.value)}
                  className="studio-input w-full rounded-lg px-3 py-2 text-xs"
                >
                  {TTS_VOICE_OPTIONS.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="mb-1 block font-medium text-slate-700">
                  导演语气与情绪提示（可选，支持中文）
                </label>
                <textarea
                  value={stylePrompt}
                  onChange={(e) => setStylePrompt(e.target.value)}
                  rows={3}
                  placeholder="例：温暖治愈、略带悬念感、语速稍快、停顿干脆"
                  className="studio-input w-full rounded-lg p-2.5 text-xs leading-relaxed"
                />
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                disabled={submittingTts}
                onClick={() => setModalOpen(false)}
                className="rounded-lg border border-slate-200 bg-white px-3.5 py-1.5 text-xs text-slate-600 hover:bg-slate-100 cursor-pointer"
              >
                取消
              </button>
              <button
                type="button"
                disabled={submittingTts}
                onClick={handleBatchTts}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50 cursor-pointer"
              >
                {submittingTts ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                {submittingTts ? '提交任务中...' : '开始后台批量配音'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
