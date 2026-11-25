import React, { useEffect, useState } from 'react';
import {
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  MessageSquarePlus,
  RefreshCw,
  Send,
  XCircle,
} from 'lucide-react';
import { api, roleLabel } from '../api';
import type { AuthUser, RuleSuggestion, SanitizedRule } from '../types';

interface Props {
  user: AuthUser | null;
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

export const RulesCollabPage: React.FC<Props> = ({ user, notify }) => {
  const [rules, setRules] = useState<SanitizedRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedKeys, setExpandedKeys] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [submittingKey, setSubmittingKey] = useState<string | null>(null);
  const [replyInputs, setReplyInputs] = useState<Record<number, string>>({});
  const [handlingId, setHandlingId] = useState<number | null>(null);

  const isAdmin = user?.role === 'admin';

  const loadRules = async (showToast = false) => {
    setLoading(true);
    try {
      const res = await api.get<{ rules: SanitizedRule[] }>('/api/collab/rules');
      setRules(res.rules || []);
      if (showToast) notify('已刷新最新创作规范与建议');
    } catch (err: any) {
      notify(err?.message || '加载创作规范失败', 'err');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRules(false);
  }, []);

  const handleSubmitSuggestion = async (rule: SanitizedRule) => {
    const text = (drafts[rule.key] || '').trim();
    if (!text) {
      notify('请先填写你的修改建议内容', 'err');
      return;
    }
    setSubmittingKey(rule.key);
    try {
      await api.post(`/api/collab/rules/${encodeURIComponent(rule.key)}/suggestions`, {
        suggestion_text: text,
      });
      setDrafts((prev) => ({ ...prev, [rule.key]: '' }));
      await loadRules(false);
      notify(`已提交对「${rule.title}」的修改建议，管理员会收到提醒`);
    } catch (err: any) {
      notify(err?.message || '提交建议失败', 'err');
    } finally {
      setSubmittingKey(null);
    }
  };

  const handleAdminAction = async (sg: RuleSuggestion, status: 'adopted' | 'dismissed') => {
    setHandlingId(sg.id);
    try {
      await api.patch(`/api/collab/suggestions/${sg.id}`, {
        status,
        admin_reply: (replyInputs[sg.id] || '').trim(),
      });
      setReplyInputs((prev) => ({ ...prev, [sg.id]: '' }));
      await loadRules(false);
      notify(status === 'adopted' ? '已标记为「已采纳」并通知提议人' : '已标记为「忽略」并回复说明');
    } catch (err: any) {
      notify(err?.message || '更新建议状态失败', 'err');
    } finally {
      setHandlingId(null);
    }
  };

  const statusBadge = (status: string) => {
    if (status === 'adopted') {
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 border border-emerald-200 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
          <CheckCircle2 className="h-3 w-3" /> 已采纳
        </span>
      );
    }
    if (status === 'dismissed') {
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 px-2 py-0.5 text-[10px] font-medium text-slate-500">
          <XCircle className="h-3 w-3" /> 已忽略
        </span>
      );
    }
    return (
      <span className="rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
        待处理
      </span>
    );
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold tracking-tight text-slate-900">创作规范与建议</h1>
          <p className="mt-1 text-xs sm:text-sm text-slate-500">
            团队共享的选品打分、剧本创作、画面审美与多平台运营标准（只读展示）。发现哪条不顺手，直接用大白话提修改建议留痕！
          </p>
        </div>
        <button
          type="button"
          onClick={() => loadRules(true)}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin text-sky-600' : ''}`} /> 刷新规范
        </button>
      </div>

      {loading && rules.length === 0 ? (
        <div className="studio-card rounded-2xl py-16 text-center text-xs text-slate-400">正在加载团队创作规范...</div>
      ) : (
        <div className="space-y-4">
          {rules.map((rule) => {
            const expanded = Boolean(expandedKeys[rule.key]);
            const openCount = (rule.suggestions || []).filter((s) => s.status === 'open').length;
            return (
              <div key={rule.key} className="studio-card rounded-2xl overflow-hidden">
                {/* 卡片头部 */}
                <div className="p-4 sm:p-5 border-b border-slate-100 bg-slate-50/50 space-y-2">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="space-y-1 min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <BookOpen className="h-4 w-4 text-sky-600 shrink-0" />
                        <h2 className="text-sm sm:text-base font-semibold text-slate-900">{rule.title}</h2>
                        {openCount > 0 && (
                          <span className="rounded-full bg-amber-100 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                            {openCount} 条待处理建议
                          </span>
                        )}
                      </div>
                      {rule.description && (
                        <p className="text-xs text-slate-600 leading-relaxed">{rule.description}</p>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => setExpandedKeys((prev) => ({ ...prev, [rule.key]: !prev[rule.key] }))}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 cursor-pointer shrink-0"
                    >
                      {expanded ? (
                        <>
                          收起全文 <ChevronUp className="h-3.5 w-3.5" />
                        </>
                      ) : (
                        <>
                          展开规范全文 <ChevronDown className="h-3.5 w-3.5" />
                        </>
                      )}
                    </button>
                  </div>
                </div>

                {/* 正文与提建议区 */}
                <div className="p-4 sm:p-5 grid grid-cols-1 gap-4 lg:grid-cols-12">
                  {/* 左：只读规范内容 */}
                  <div className="lg:col-span-7 space-y-2 min-w-0">
                    <div
                      className={`rounded-xl border border-slate-200 bg-[#fbfcfe] p-3.5 text-xs leading-relaxed text-slate-700 whitespace-pre-wrap break-words ${
                        expanded ? '' : 'max-h-48 overflow-hidden relative'
                      }`}
                    >
                      {rule.content}
                      {!expanded && (
                        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-[#fbfcfe] to-transparent" />
                      )}
                    </div>
                    {!expanded && (
                      <button
                        type="button"
                        onClick={() => setExpandedKeys((prev) => ({ ...prev, [rule.key]: true }))}
                        className="text-xs font-medium text-sky-600 hover:underline cursor-pointer"
                      >
                        查看完整细则 →
                      </button>
                    )}
                  </div>

                  {/* 右：提修改建议与历史建议流 */}
                  <div className="lg:col-span-5 space-y-3 min-w-0">
                    <div className="rounded-xl border border-sky-200/80 bg-sky-50/40 p-3 space-y-2">
                      <div className="inline-flex items-center gap-1.5 text-xs font-semibold text-sky-900">
                        <MessageSquarePlus className="h-3.5 w-3.5 text-sky-600" /> 提一条修改建议（大白话即可）
                      </div>
                      <textarea
                        value={drafts[rule.key] || ''}
                        onChange={(e) => setDrafts((prev) => ({ ...prev, [rule.key]: e.target.value }))}
                        rows={2}
                        placeholder="例：希望剧情反转节奏更快，或者结尾互动语更口语化一些..."
                        className="studio-input w-full rounded-lg p-2.5 text-xs leading-relaxed"
                      />
                      <div className="flex justify-end">
                        <button
                          type="button"
                          disabled={submittingKey === rule.key || !(drafts[rule.key] || '').trim()}
                          onClick={() => handleSubmitSuggestion(rule)}
                          className="inline-flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 disabled:opacity-45 cursor-pointer"
                        >
                          <Send className="h-3 w-3" />
                          {submittingKey === rule.key ? '提交中...' : '提交建议留痕'}
                        </button>
                      </div>
                    </div>

                    {/* 该规则的历史建议列表 */}
                    <div className="space-y-2 max-h-72 overflow-y-auto pr-0.5">
                      {(rule.suggestions || []).map((sg) => (
                        <div key={sg.id} className="rounded-xl border border-slate-200 bg-white p-3 space-y-2 text-xs">
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <div className="flex items-center gap-1.5">
                              <span className="font-semibold text-slate-900">{sg.actor_name || sg.actor_username}</span>
                              <span className="text-[10px] text-slate-500">({roleLabel(sg.actor_role)})</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                              {statusBadge(sg.status)}
                              <span className="text-[10px] text-slate-400 font-mono">{sg.created_at.slice(5, 16)}</span>
                            </div>
                          </div>
                          <div className="text-slate-700 leading-relaxed whitespace-pre-wrap break-words">
                            {sg.suggestion_text}
                          </div>

                          {sg.admin_reply && (
                            <div className="rounded-lg bg-emerald-50/70 border border-emerald-200/70 px-2.5 py-1.5 text-[11px] text-emerald-900">
                              管理员回复（{sg.handled_by || '管理员'}）：{sg.admin_reply}
                            </div>
                          )}

                          {isAdmin && sg.status === 'open' && (
                            <div className="pt-2 border-t border-slate-100 space-y-1.5">
                              <input
                                value={replyInputs[sg.id] || ''}
                                onChange={(e) => setReplyInputs((prev) => ({ ...prev, [sg.id]: e.target.value }))}
                                placeholder="可选：给协作成员的处理回复（如：好主意，下版同步更新）"
                                className="studio-input w-full rounded-lg px-2.5 py-1.5 text-[11px]"
                              />
                              <div className="flex items-center justify-end gap-1.5">
                                <button
                                  type="button"
                                  disabled={handlingId === sg.id}
                                  onClick={() => handleAdminAction(sg, 'dismissed')}
                                  className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-100 cursor-pointer"
                                >
                                  忽略
                                </button>
                                <button
                                  type="button"
                                  disabled={handlingId === sg.id}
                                  onClick={() => handleAdminAction(sg, 'adopted')}
                                  className="rounded-lg bg-emerald-600 px-3 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700 cursor-pointer"
                                >
                                  标记已采纳
                                </button>
                              </div>
                            </div>
                          )}
                        </div>
                      ))}
                      {(!rule.suggestions || rule.suggestions.length === 0) && (
                        <div className="py-3 text-center text-[11px] text-slate-400">暂无修改建议</div>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
