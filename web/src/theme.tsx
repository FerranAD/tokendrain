import { useEffect, useState } from 'react';

type Theme = 'system' | 'light' | 'dark';
function readTheme(): Theme {
  try {
    const saved = localStorage.getItem('tokendrain.theme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    /* Theme preferences are optional in restricted browsers. */
  }
  return 'system';
}
function apply(theme: Theme) {
  const dark =
    theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
}
apply(readTheme());

export function ThemeControl() {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    apply(theme);
    const media = matchMedia('(prefers-color-scheme: dark)');
    const changed = () => apply(theme);
    const storage = () => setTheme(readTheme());
    media.addEventListener('change', changed);
    window.addEventListener('storage', storage);
    return () => {
      media.removeEventListener('change', changed);
      window.removeEventListener('storage', storage);
    };
  }, [theme]);
  return (
    <div className="theme-control">
      <span>Appearance</span>
      <div className="theme-options" role="group" aria-label="Appearance">
        {(['light', 'dark', 'system'] as const).map((value) => {
          const label = `${value.charAt(0).toUpperCase()}${value.slice(1)} theme`;
          return (
            <button
              key={value}
              type="button"
              aria-label={label}
              title={label}
              aria-pressed={theme === value}
              onClick={() => {
                setTheme(value);
                try {
                  if (value === 'system') localStorage.removeItem('tokendrain.theme');
                  else localStorage.setItem('tokendrain.theme', value);
                } catch {
                  /* Keep the in-memory preference. */
                }
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                {value === 'light' ? (
                  <>
                    <circle cx="12" cy="12" r="4" />
                    <path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" />
                  </>
                ) : value === 'dark' ? (
                  <path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z" />
                ) : (
                  <>
                    <rect x="3" y="4" width="18" height="13" rx="2" />
                    <path d="M8 21h8m-4-4v4" />
                  </>
                )}
              </svg>
            </button>
          );
        })}
      </div>
    </div>
  );
}
