import React from 'react';

export const LocalProjectsBanner: React.FC = () => (
  <div className="studio-card rounded-xl p-4 text-xs text-slate-600 flex flex-wrap items-center justify-between gap-3">
    <div className="space-y-1">
      <div className="font-semibold text-slate-800">Google Flow 省积分与三油箱协作策略（已内置在每集预算器中）</div>
      <div>
        Nano Banana 定首帧（<span className="text-emerald-700 font-semibold">0 积分</span>）→ Omni Flash 1.2 360p 抽卡（1分/秒）或 Veo 3.1 Lite（5分/0分）→ 选定即
        <span className="text-emerald-700 font-semibold">免费 1080p 超分</span> → 全片 4K/30fps HLG HDR 交给 Google Colab GPU，或本机直出 1080p 三轨母带。
      </div>
    </div>
    <a
      href="https://labs.google/fx/tools/flow"
      target="_blank"
      rel="noreferrer"
      className="shrink-0 rounded-lg border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs font-semibold text-sky-700 hover:bg-sky-100"
    >
      打开官方 Google Flow ↗
    </a>
  </div>
);

export default LocalProjectsBanner;
