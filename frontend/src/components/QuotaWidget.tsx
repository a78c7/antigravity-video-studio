import React, { useEffect, useState } from 'react';
import { ExternalLink, Gauge, RefreshCw, SlidersHorizontal, X } from 'lucide-react';
import { api } from '../api';

export interface QuotaSummary {
  gemini: {
    key_set: boolean;
    model: string;
    today_calls: number;
    today_tokens: number;
    today_prompt_tokens: number;
    today_completion_tokens: number;
    today_cost_usd?: number;
    month_calls: number;
    month_tokens: number;
    month_cost_usd?: number;
    monthly_credit_usd?: number;
    remaining_credit_usd?: number;
    daily_limit: number;
  };
  flow: {
    monthly_total: number;
    calibrated_remaining: number;
    estimated_spent: number;
    estimated_remaining: number;
    month: string;
    days_left: number;
    calibrated_at: string;
  };
  deepseek: {
    enabled: boolean;
    key_set: boolean;
    month_calls: number;
    month_tokens: number;
    balance_text: string;
    checked_at: string;
  };
}

interface Props {
  notify: (msg: string, type?: 'ok' | 'err') => void;
}

function fmtTok(n: number): string {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function fmtFlowTotal(total: number): string {
  if (total <= 0) return '0';
  if (total >= 1000 && total % 1000 === 0) return `${total / 1000}k`;
  if (total >= 1000) return `${(total / 1000).toFixed(1)}k`;
  return String(total);
}

export const QuotaWidget: React.FC<Props> = ({ notify }) => {
  const [data, setData] = useState<QuotaSummary | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // 校准表单
  const [flowTotal, setFlowTotal] = useState(0);
  const [flowRem, setFlowRem] = useState(0);
  const [geminiLimit, setGeminiLimit] = useState(250);
  const [geminiCreditUsd, setGeminiCreditUsd] = useState(0);

  const loadSummary = async () => {
    try {
      const res = await api.get<QuotaSummary>('/api/quota/summary');
      setData(res);
      setFlowTotal(res.flow.monthly_total ?? 0);
      setFlowRem(res.flow.estimated_remaining ?? 0);
      setGeminiLimit(res.gemini.daily_limit ?? 250);
      setGeminiCreditUsd(res.gemini.monthly_credit_usd ?? 0);
    } catch {
      // 静默忽略偶发轮询异常
    }
  };

  useEffect(() => {
    loadSummary();
    const timer = setInterval(loadSummary, 15000);
    const onExternalRefresh = () => {
      loadSummary();
    };
    window.addEventListener('agvs-quota-refresh', onExternalRefresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener('agvs-quota-refresh', onExternalRefresh);
    };
  }, []);

  const handleRefreshAll = async () => {
    setRefreshing(true);
    try {
      if (data?.deepseek.enabled && data.deepseek.key_set) {
        const res = await api.post<QuotaSummary>('/api/quota/refresh-deepseek');
        setData(res);
        notify('已刷新本地记账与 DeepSeek 官方直连余额');
      } else {
        await loadSummary();
        notify('已刷新 Gemini 记账与 Flow 月度额度状态');
      }
    } catch (err: any) {
      notify(err.message, 'err');
    } finally {
      setRefreshing(false);
    }
  };

  const handleSaveCalibration = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await api.post<QuotaSummary>('/api/quota/calibrate', {
        flow_monthly_total: Math.max(0, Number(flowTotal) || 0),
        flow_remaining: Math.max(0, Number(flowRem) || 0),
        gemini_daily_limit: Math.max(1, Number(geminiLimit) || 250),
        gemini_monthly_credit_usd: Math.max(0, Number(geminiCreditUsd) || 0),
      });
      setData(res);
      setModalOpen(false);
      notify('额度监控校准已保存');
    } catch (err: any) {
      notify(err.message, 'err');
    }
  };

  const monthCostUsd = data?.gemini.month_cost_usd ?? 0;
  const monthlyCreditUsd = data?.gemini.monthly_credit_usd ?? 0;
  const hasCreditBudget = monthlyCreditUsd > 0;
  const remainingCreditUsd = hasCreditBudget
    ? (data?.gemini.remaining_credit_usd ?? Math.max(0, monthlyCreditUsd - monthCostUsd))
    : 0;
  const creditPct = hasCreditBudget ? Math.min(100, Math.round((monthCostUsd / monthlyCreditUsd) * 100)) : 0;

  const flowMonthlyTotal = data?.flow.monthly_total ?? 0;
  const hasFlowBudget = flowMonthlyTotal > 0;
  const fPct =
    data && hasFlowBudget
      ? Math.min(100, Math.round((data.flow.estimated_remaining / flowMonthlyTotal) * 100))
      : 0;

  return (
    <>
      <div className="rounded-xl border border-slate-200 bg-slate-50/90 p-2.5 text-[11px] space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="inline-flex items-center gap-1.5 font-semibold text-slate-800">
            <Gauge className="h-3.5 w-3.5 text-sky-600" /> 额度监控
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={handleRefreshAll}
              title="刷新额度状态"
              className="rounded p-1 text-slate-500 hover:bg-slate-200/70 hover:text-slate-900 cursor-pointer"
            >
              <RefreshCw className={`h-3 w-3 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={() => setModalOpen(true)}
              title="校准 Flow 积分与 API 月度赠金上限"
              className="rounded px-1.5 py-0.5 text-[10px] font-medium text-sky-700 hover:bg-sky-100 cursor-pointer inline-flex items-center gap-0.5"
            >
              <SlidersHorizontal className="h-2.5 w-2.5" /> 校准
            </button>
          </div>
        </div>

        {/* 1. Gemini API 月度预算 / 赠金实时护栏 */}
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-emerald-700 font-semibold">API 月度预算</span>
            <span
              className={`font-mono font-semibold ${
                hasCreditBudget && creditPct >= 95
                  ? 'text-rose-600'
                  : hasCreditBudget && creditPct >= 80
                    ? 'text-amber-700'
                    : 'text-slate-700'
              }`}
            >
              {data
                ? hasCreditBudget
                  ? `$${monthCostUsd.toFixed(2)} / $${monthlyCreditUsd.toFixed(0)}`
                  : `$${monthCostUsd.toFixed(2)} / 免费层`
                : '...'}
            </span>
          </div>
          <div className="h-1.5 w-full rounded-full bg-slate-200 overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${
                creditPct >= 95 ? 'bg-rose-500' : creditPct >= 80 ? 'bg-amber-500' : 'bg-emerald-500'
              }`}
              style={{ width: `${hasCreditBudget ? Math.max(monthCostUsd > 0 ? 3 : 0, creditPct) : 0}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[9px] text-slate-500 gap-1">
            <span className="truncate">
              {data?.gemini.key_set ? `${data.gemini.model} · 今 ${data.gemini.today_calls}次` : '未填 Key（点设置填写）'}
            </span>
            <span className="shrink-0 font-mono text-emerald-700">
              {hasCreditBudget ? `余 $${remainingCreditUsd.toFixed(2)}` : '可在设置设档'}
            </span>
          </div>
          {hasCreditBudget && creditPct >= 80 && (
            <div
              className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${
                creditPct >= 95 ? 'bg-rose-100 text-rose-800' : 'bg-amber-100 text-amber-800'
              }`}
            >
              {creditPct >= 95 ? '⚠️ 月度赠金即将用尽，建议暂停 CLI 防超支' : '⚠️ 已用 ≥80% 月度预算，注意控制生图/视频调用'}
            </div>
          )}
        </div>

        {/* 2. Google Flow 月度积分 */}
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-amber-700 font-semibold">Google Flow</span>
            <span className="text-slate-700 font-mono">
              {data
                ? hasFlowBudget
                  ? `${data.flow.estimated_remaining.toLocaleString()}/${fmtFlowTotal(flowMonthlyTotal)}`
                  : '未校准'
                : '...'}
            </span>
          </div>
          <div className="h-1.5 w-full rounded-full bg-slate-200 overflow-hidden">
            <div
              className="h-full rounded-full bg-amber-500 transition-all"
              style={{ width: `${fPct}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[9px] text-slate-500">
            <span>{hasFlowBudget ? `剩 ${data?.flow.days_left ?? '-'} 天月底清零` : '可在设置选订阅档位'}</span>
            <button onClick={() => setModalOpen(true)} className="text-amber-700 hover:underline cursor-pointer">
              核对实额
            </button>
          </div>
        </div>

        {/* 3. DeepSeek 备用余额 */}
        <div className="pt-1.5 border-t border-slate-200 flex items-center justify-between text-[10px]">
          <span className="text-slate-600 font-medium">DeepSeek</span>
          {data?.deepseek.enabled ? (
            <span className="text-sky-700 font-mono truncate max-w-[115px]" title={data.deepseek.balance_text}>
              {data.deepseek.balance_text || '点刷新查余额'}
            </span>
          ) : (
            <span className="rounded bg-slate-200/80 px-1.5 py-0.5 text-[9px] text-slate-600">备用关闭 · 0花费</span>
          )}
        </div>
      </div>

      {/* 校准与详情浮窗 */}
      {modalOpen && data && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/35 backdrop-blur-xs p-4">
          <div className="studio-card w-full max-w-md rounded-2xl p-5 space-y-4 shadow-2xl border-slate-200">
            <div className="flex items-center justify-between">
              <div className="text-sm font-semibold text-slate-900 inline-flex items-center gap-2">
                <Gauge className="h-4 w-4 text-sky-600" /> 三引擎额度明细与一键校准
              </div>
              <button onClick={() => setModalOpen(false)} className="text-slate-400 hover:text-slate-700 cursor-pointer">
                <X className="h-4 w-4" />
              </button>
            </div>

            {/* 实诚说明 */}
            <div className="rounded-xl bg-slate-50 border border-slate-200 p-3 text-[11px] text-slate-600 space-y-1 leading-relaxed">
              <div>• <strong className="text-slate-800">Gemini API + CLI 预算护栏</strong>：实时累加文本模型 + CLI 生图/生视频/配乐估算花费（本月累计 {data.gemini.month_calls} 次 / {fmtTok(data.gemini.month_tokens)} tokens / 约 <strong>${monthCostUsd.toFixed(2)}</strong>），超 80% 自动预警，防止意外超支。</div>
              <div>• <strong className="text-slate-800">Google Flow</strong>：为守住 0 封号红线绝不爬取网页 Cookie；认领抽卡视频自动估算扣分，你可随时对照官方网页 1 秒校准，或在「系统设置」按 Google 订阅档位一键套用预设。</div>
              <div>• <strong className="text-slate-800">DeepSeek</strong>：默认关闭省钱；开启后走直连查询官方真实余额。</div>
            </div>

            <form onSubmit={handleSaveCalibration} className="space-y-3 text-xs">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-slate-600">API 月度预算/赠金 (USD)</label>
                  <input
                    type="number"
                    min={0}
                    step={1}
                    value={geminiCreditUsd}
                    onChange={(e) => setGeminiCreditUsd(Number(e.target.value))}
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-slate-600">Gemini 日调用参考上限</label>
                  <input
                    type="number"
                    min={1}
                    value={geminiLimit}
                    onChange={(e) => setGeminiLimit(Number(e.target.value))}
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-slate-600">Flow 月度总额（积分）</label>
                  <input
                    type="number"
                    min={0}
                    step={100}
                    value={flowTotal}
                    onChange={(e) => setFlowTotal(Number(e.target.value))}
                    placeholder="如 1000 / 10000 / 25000"
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-slate-600">Flow 当前真实剩余积分</label>
                  <input
                    type="number"
                    min={0}
                    value={flowRem}
                    onChange={(e) => setFlowRem(Number(e.target.value))}
                    className="studio-input w-full rounded-lg px-2.5 py-1.5 text-xs"
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-slate-200">
                <div className="flex gap-3">
                  <a
                    href="https://labs.google/fx/tools/flow"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-700 hover:underline"
                  >
                    Flow 看实额 <ExternalLink className="h-3 w-3" />
                  </a>
                  <a
                    href="https://aistudio.google.com/usage"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-700 hover:underline"
                  >
                    AI Studio 账单 <ExternalLink className="h-3 w-3" />
                  </a>
                </div>
                <button
                  type="submit"
                  className="rounded-lg bg-sky-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-sky-700 cursor-pointer"
                >
                  保存校准
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
};
