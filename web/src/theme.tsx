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
    <label className="theme-control">
      <span>Appearance</span>
      <select
        aria-label="Appearance"
        value={theme}
        onChange={(e) => {
          const value = e.target.value as Theme;
          setTheme(value);
          try {
            if (value === 'system') localStorage.removeItem('tokendrain.theme');
            else localStorage.setItem('tokendrain.theme', value);
          } catch {
            /* Keep the in-memory preference. */
          }
        }}
      >
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  );
}
