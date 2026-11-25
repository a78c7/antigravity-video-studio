/**
 * 统一 API 与媒体地址封装：
 * - 自动兼容本地根路径（http://127.0.0.1:8765/）与自托管反代子路径（如 https://your-domain.example/studio/）；
 * - 所有请求默认携带 same-origin Cookie；
 * - 遇到 401 自动派发 agvs-unauthorized 事件，引导切回登录页。
 */

function detectApiBase(): string {
  if (typeof window === 'undefined') return '';
  try {
    const modPath = new URL(import.meta.url).pathname;
    const assetsIdx = modPath.indexOf('/assets/');
    if (assetsIdx > 0) {
      return modPath.slice(0, assetsIdx).replace(/\/+$/, '');
    }
    if (assetsIdx === 0) {
      return '';
    }
    const srcIdx = modPath.indexOf('/src/');
    if (srcIdx >= 0) {
      return modPath.slice(0, srcIdx).replace(/\/+$/, '');
    }
  } catch {
    // 兜底按当前路径前缀推断
  }
  const pathname = window.location.pathname || '/';
  if (pathname === '/studio' || pathname.startsWith('/studio/')) {
    return '/studio';
  }
  return '';
}

export const API_BASE = detectApiBase();

/** 将 /api/... 路径自动拼接当前部署子路径前缀（如 /studio/api/... 或 /api/...） */
export function apiUrl(path: string): string {
  if (!path) return API_BASE || '/';
  if (/^https?:\/\//i.test(path)) return path;
  const normalized = path.startsWith('/') ? path : `/${path}`;
  if (API_BASE && (normalized === API_BASE || normalized.startsWith(`${API_BASE}/`))) {
    return normalized;
  }
  return `${API_BASE}${normalized}`;
}

/** 构造受控媒体文件预览/下载 URL（兼容根路径与 /studio 子路径） */
export function mediaUrl(filePath?: string, download = false): string {
  if (!filePath) return '';
  const base = apiUrl('/api/media');
  return `${base}?path=${encodeURIComponent(filePath)}${download ? '&download=1' : ''}`;
}

export const UNAUTHORIZED_EVENT = 'agvs-unauthorized';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const fullUrl = apiUrl(url);
  const res = await fetch(fullUrl, {
    ...init,
    credentials: 'same-origin',
  });
  if (!res.ok) {
    let detail = `请求失败 (${res.status})`;
    try {
      const err = await res.json();
      detail = err.detail || JSON.stringify(err);
    } catch {
      const txt = await res.text();
      if (txt) detail = txt;
    }
    if (
      res.status === 401 &&
      typeof window !== 'undefined' &&
      !url.includes('/api/auth/login') &&
      !url.includes('/api/auth/me')
    ) {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail }));
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, body?: unknown) =>
    request<T>(url, {
      method: 'POST',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
  patch: <T>(url: string, body: unknown) =>
    request<T>(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  del: <T>(url: string) => request<T>(url, { method: 'DELETE' }),
  upload: <T>(url: string, formData: FormData) =>
    request<T>(url, {
      method: 'POST',
      body: formData,
    }),
};

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  }
}

export function fmtBytes(bytes: number): string {
  if (!bytes) return '0 B';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function fmtHot(n: number): string {
  if (!n) return '';
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n);
}

export function roleLabel(role?: string): string {
  if (role === 'admin') return '管理员';
  if (role === 'partner') return '联合制片';
  return role || '成员';
}
