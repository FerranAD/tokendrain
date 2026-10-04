import type { ReactNode } from 'react';

const shapes: Record<string, ReactNode> = {
  grid: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  folder: <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3Z" />,
  file: (
    <>
      <path d="M14 3H5v18h14V8Z" />
      <path d="M14 3v5h5M8 12h8m-8 4h6" />
    </>
  ),
  files: (
    <>
      <path d="M9 3h10v14H9Z" />
      <path d="M5 7H3v14h10v-2" />
    </>
  ),
  play: <path d="m8 4 12 8-12 8Z" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  settings: (
    <>
      <path d="M4 6h16M4 12h16M4 18h16" />
      <circle cx="8" cy="6" r="2" />
      <circle cx="16" cy="12" r="2" />
      <circle cx="10" cy="18" r="2" />
    </>
  ),
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="m7 9 3 3-3 3m6 0h4" />
    </>
  ),
  agent: (
    <>
      <path d="M4 5h16v12H9l-5 4Z" />
      <path d="M8 9h8m-8 4h5" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  checkpoint: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="m7 12 3 3 7-7" />
    </>
  ),
  alert: (
    <>
      <path d="m12 3 10 18H2Z" />
      <path d="M12 9v5m0 3v.1" />
    </>
  ),
  stop: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="4" />
      <path d="M9 9v6m6-6v6" />
    </>
  ),
  usage: (
    <>
      <path d="M4 18V6m5 12V9m5 9V4m5 14v-6" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  download: (
    <>
      <path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4" />
    </>
  ),
  copy: (
    <>
      <rect x="8" y="8" width="12" height="13" rx="2" />
      <path d="M16 5V3H3v13h2" />
    </>
  ),
  wrap: (
    <>
      <path d="M3 6h18M3 11h14a4 4 0 0 1 0 8h-5m3-3-3 3 3 3M3 16h4" />
    </>
  ),
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  back: <path d="M20 12H4m6-6-6 6 6 6" />,
  chevron: <path d="m9 5 7 7-7 7" />,
};
export function Icon({ name, className = '' }: { name: string; className?: string }) {
  return (
    <svg className={`icon ${className}`} viewBox="0 0 24 24" aria-hidden="true">
      {shapes[name] || shapes.file}
    </svg>
  );
}
