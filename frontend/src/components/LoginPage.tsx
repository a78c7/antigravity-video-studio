import React, { useState } from 'react';
import { Clapperboard, Eye, EyeOff, Lock, LogIn, User } from 'lucide-react';
import { api } from '../api';

interface Props {
  brandName?: string;
  onLoginSuccess: () => Promise<void>;
}

export const LoginPage: React.FC<Props> = ({ brandName = '反重力视频工坊', onLoginSuccess }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const u = username.trim();
    if (!u || !password) {
      setError('请输入账号与密码');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api.post('/api/auth/login', { username: u, password });
      await onLoginSuccess();
    } catch (err: any) {
      setError(err?.message || '登录失败，请检查账号或密码');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-[#f4f7fc] via-[#eef3fb] to-[#f8fafc] px-4 py-8">
      <div className="w-full max-w-md space-y-6">
        {/* 品牌头图 */}
        <div className="text-center space-y-2.5">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 via-blue-600 to-indigo-600 text-white shadow-lg shadow-sky-500/20">
            <Clapperboard className="h-7 w-7" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">{brandName}</h1>
            <p className="mt-1 text-xs sm:text-sm text-slate-500">AI 短剧多人共创与审片看板</p>
          </div>
        </div>

        {/* 登录表单卡片 */}
        <form
          onSubmit={handleSubmit}
          className="studio-card rounded-2xl p-6 sm:p-7 space-y-4 shadow-xl shadow-slate-200/60 border-slate-200/90"
        >
          <div className="space-y-1">
            <h2 className="text-base font-semibold text-slate-900">团队成员登录</h2>
            <p className="text-xs text-slate-500">
              登录后可在线看片审稿、剧本分镜共创留痕、封面投票与评论互动协作。
            </p>
          </div>

          {error && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-700 leading-relaxed">
              {error}
            </div>
          )}

          <div className="space-y-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">成员账号</label>
              <div className="relative">
                <User className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                <input
                  type="text"
                  autoComplete="username"
                  autoCapitalize="none"
                  autoCorrect="off"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="输入你的成员账号"
                  className="studio-input w-full rounded-xl pl-9 pr-3 py-2.5 text-sm"
                  required
                />
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-700">登录密码</label>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                <input
                  type={showPwd ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="输入登录密码"
                  className="studio-input w-full rounded-xl pl-9 pr-10 py-2.5 text-sm"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPwd((v) => !v)}
                  aria-label={showPwd ? '隐藏密码' : '显示密码'}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded-lg p-1.5 text-slate-400 hover:text-slate-700 cursor-pointer"
                >
                  {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>
          </div>

          <button
            type="submit"
            disabled={busy}
            className="w-full inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-sky-600 to-blue-600 py-2.5 text-sm font-semibold text-white shadow-sm hover:from-sky-700 hover:to-blue-700 disabled:opacity-50 transition cursor-pointer"
          >
            <LogIn className="h-4 w-4" />
            {busy ? '正在验证登录...' : `进入${brandName}`}
          </button>

          <div className="pt-2 border-t border-slate-100 text-center text-[11px] text-slate-400">
            自托管团队协作空间 · 支持桌面与手机浏览器直连访问
          </div>
        </form>
      </div>
    </div>
  );
};
