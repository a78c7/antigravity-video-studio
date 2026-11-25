import React, { useEffect } from 'react';
import { Download, X } from 'lucide-react';

interface Props {
  src: string | null;
  title?: string;
  downloadHref?: string;
  onClose: () => void;
}

export const ImageLightbox: React.FC<Props> = ({ src, title, downloadHref, onClose }) => {
  useEffect(() => {
    if (!src) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [src, onClose]);

  if (!src) return null;

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-slate-950/90 backdrop-blur-xs p-3 sm:p-6"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-5xl flex items-center justify-between gap-2 pb-3 text-white"
      >
        <div className="text-xs sm:text-sm font-medium truncate">{title || '高清大图预览'}</div>
        <div className="flex items-center gap-2 shrink-0">
          {downloadHref && (
            <a
              href={downloadHref}
              download
              className="inline-flex items-center gap-1 rounded-lg bg-white/15 px-3 py-1.5 text-xs font-medium text-white hover:bg-white/25"
            >
              <Download className="h-3.5 w-3.5" /> 下载原图
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg bg-white/15 p-1.5 text-white hover:bg-white/25 cursor-pointer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div
        onClick={(e) => e.stopPropagation()}
        className="max-h-[82vh] max-w-full overflow-auto rounded-xl bg-black/40 p-1"
      >
        <img src={src} alt={title || '预览图'} className="max-h-[80vh] max-w-full object-contain rounded-lg" />
      </div>
    </div>
  );
};
