import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, EventsProvider, mutate, useAction, useResource } from './api';
import { NewProject, ProjectCards, ProjectPage, ProjectsPage } from './projects';
import { PrepareRunPage, RunPage, RunsPage, RunTable, SchedulesPage } from './runs';
import { SettingsPage } from './settings';
import type { Project, Run, Schedule, SystemInfo, UsageWindow } from './types';
import {
  ActionNotice,
  Badge,
  date,
  ErrorNotice,
  Link,
  Loading,
  Navigation,
  PageTitle,
  UsageCards,
} from './ui';
import './style.css';
import { ThemeControl } from './theme';
import { confirmDiscardChanges } from './drafts';

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [path, setPath] = useState(window.location.pathname + window.location.search);
  const go = useCallback((next: string) => {
    if (!confirmDiscardChanges()) return;
    window.history.pushState(null, '', next);
    setPath(next);
    window.scrollTo(0, 0);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void api<SystemInfo>('/system', { signal: controller.signal })
      .then(() => {
        setError('');
        setAuthenticated(true);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        if (e instanceof Error && 'status' in e && e.status === 401) setAuthenticated(false);
        else setError(e instanceof Error ? e.message : 'Cannot connect to the daemon.');
      });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    const onPop = () => {
      if (confirmDiscardChanges()) setPath(window.location.pathname + window.location.search);
      else window.history.pushState(null, '', path);
    };
    const unauthorized = () => setAuthenticated(false);
    window.addEventListener('popstate', onPop);
    window.addEventListener('tokendrain:unauthorized', unauthorized);
    return () => {
      window.removeEventListener('popstate', onPop);
      window.removeEventListener('tokendrain:unauthorized', unauthorized);
    };
  }, [path]);
  if (authenticated === null)
    return (
      <div className="login-page">
        <div className="login-card">
          <Brand />
          <h1>Connecting to your host</h1>
          {error ? (
            <>
              <ErrorNotice error={error} />
              <button
                onClick={() => {
                  setError('');
                  setAttempt((v) => v + 1);
                }}
              >
                Try again
              </button>
            </>
          ) : (
            <Loading />
          )}
        </div>
      </div>
    );
  if (!authenticated) return <Login onAuthenticated={() => setAuthenticated(true)} />;
  return (
    <Navigation.Provider value={{ path, go }}>
      <EventsProvider>
        <Shell path={path} onSignOut={() => setAuthenticated(false)} />
      </EventsProvider>
    </Navigation.Provider>
  );
}

function Brand() {
  return (
    <span className="brand">
      <img
        src="/branding/tokendrain-logo-horizontal.png"
        alt="tokendrain"
        className="brand-lockup logo-light"
      />
      <img
        src="/branding/tokendrain-logo-horizontal-dark.png"
        alt="tokendrain"
        className="brand-lockup logo-dark"
      />
    </span>
  );
}

function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [token, setToken] = useState('');
  const action = useAction();
  return (
    <div className="login-page">
      <div className="login-card">
        <Brand />
        <h1>Open your workspace</h1>
        <p className="muted">Enter your host’s administration token to manage projects and runs.</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void action.run(async () => {
              await mutate('/session', 'POST', { token });
              setToken('');
              onAuthenticated();
            });
          }}
        >
          <label>
            Administration token
            <input
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              required
              autoFocus
            />
          </label>
          <ActionNotice {...action} />
          <button className="primary full-width" disabled={action.busy}>
            {action.busy ? 'Connecting…' : 'Open tokendrain →'}
          </button>
        </form>
        <p className="tiny muted">Use the administration token configured on your host.</p>
        <ThemeControl />
      </div>
    </div>
  );
}

const navigation = [
  { path: '/', label: 'Dashboard', icon: 'grid' },
  { path: '/projects', label: 'Projects', icon: 'folder' },
  { path: '/runs', label: 'Runs', icon: 'play' },
  { path: '/schedules', label: 'Schedules', icon: 'clock' },
  { path: '/settings', label: 'Settings', icon: 'settings' },
];

function NavIcon({ name }: { name: string }) {
  const shapes: Record<string, React.ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    folder: <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3Z" />,
    play: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="m10 8 6 4-6 4Z" />
      </>
    ),
    clock: (
      <>
        <rect x="3" y="5" width="18" height="16" rx="2" />
        <path d="M7 3v4m10-4v4M3 11h18m-9 3v3h3" />
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
  };
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {shapes[name]}
    </svg>
  );
}

function Shell({ path, onSignOut }: { path: string; onSignOut: () => void }) {
  const session = useResource<{ auth_mode: string }>('/session');
  const action = useAction();
  const url = new URL(path, window.location.origin);
  const route = url.pathname;
  let content;
  if (route === '/') content = <Dashboard />;
  else if (route === '/projects') content = <ProjectsPage />;
  else if (/^\/projects\/[^/]+$/.test(route))
    content = <ProjectPage key={route} id={route.split('/')[2]} />;
  else if (route === '/runs') content = <RunsPage />;
  else if (/^\/runs\/[^/]+$/.test(route))
    content = <RunPage key={route} id={route.split('/')[2]} />;
  else if (route === '/prepare')
    content = (
      <PrepareRunPage key={path} selectedProject={url.searchParams.get('project') ?? undefined} />
    );
  else if (route === '/schedules') content = <SchedulesPage />;
  else if (route === '/settings') content = <SettingsPage />;
  else
    content = (
      <>
        <PageTitle
          title="Page not found"
          description="This page may have moved or the link may be incomplete."
        />
        <Link className="button" href="/">
          Return to dashboard
        </Link>
      </>
    );
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <Link href="/" className="brand-link" aria-label="tokendrain dashboard">
          <Brand />
        </Link>
        <nav aria-label="Main navigation">
          {navigation.map((item) => {
            const active = item.path === '/' ? route === '/' : route.startsWith(item.path);
            return (
              <Link
                href={item.path}
                key={item.path}
                className={active ? 'active' : ''}
                aria-current={active ? 'page' : undefined}
              >
                <NavIcon name={item.icon} />
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          <ThemeControl />
          {session.data?.auth_mode === 'token' && (
            <button
              className="sidebar-button"
              disabled={action.busy}
              onClick={() => {
                if (!confirmDiscardChanges()) return;
                void action.run(async () => {
                  await mutate('/session', 'DELETE');
                  onSignOut();
                });
              }}
            >
              Sign out
            </button>
          )}
          {action.error && <p className="tiny">{action.error}</p>}
        </div>
      </aside>
      <main id="main" tabIndex={-1}>
        {content}
      </main>
    </div>
  );
}

function Dashboard() {
  const projects = useResource<Project[]>('/projects');
  const runs = useResource<Run[]>('/runs');
  const schedules = useResource<Schedule[]>('/schedules');
  const usage = useResource<UsageWindow[]>('/usage');
  const system = useResource<SystemInfo>('/system');
  const [creating, setCreating] = useState(false);
  const active =
    runs.data?.filter(
      (run) => !['completed', 'failed', 'cancelled', 'blocked', 'stopped'].includes(run.status),
    ).length ?? 0;
  return (
    <>
      <PageTitle
        title="Usage & runs"
        description="Your allowance, active work, and what’s next."
        actions={
          <Link className="button primary" href="/prepare">
            <NavIcon name="play" /> Prepare run
          </Link>
        }
      />
      <section className="dashboard-section">
        <div className="section-heading">
          <h2>Provider allowance</h2>
          <Link className="text-link" href="/settings">
            Manage connection ↗
          </Link>
        </div>
        <ErrorNotice error={usage.error} />
        <UsageCards windows={usage.data ?? []} />
        <p className="tiny muted">
          Reported by Codex. Windows use provider metadata; usage may be shared with your other
          Codex sessions.
        </p>
      </section>
      <div className="dashboard-stats">
        <div>
          <span className="muted small">Projects</span>
          <strong>{projects.data?.length ?? '—'}</strong>
        </div>
        <div>
          <span className="muted small">Active runs</span>
          <strong>{active}</strong>
        </div>
        <div>
          <span className="muted small">Enabled schedules</span>
          <strong>{schedules.data?.filter((item) => item.enabled).length ?? '—'}</strong>
        </div>
        <div>
          <span className="muted small">Execution capacity</span>
          <strong>
            {system.data
              ? `${system.data.active_executions ?? 0} / ${system.data.concurrency}`
              : '—'}
          </strong>
        </div>
      </div>
      {active > 0 && (
        <section className="panel">
          <h2>Active Runs</h2>
          <RunTable
            runs={
              runs.data?.filter(
                (r) =>
                  !['completed', 'failed', 'cancelled', 'blocked', 'stopped'].includes(r.status),
              ) || []
            }
          />
        </section>
      )}
      <section className="dashboard-section">
        <div className="section-heading">
          <h2>
            Projects <span className="count">{projects.data?.length ?? 0}</span>
          </h2>
          <button className="quiet" onClick={() => setCreating((value) => !value)}>
            + New project
          </button>
        </div>
        {creating && <NewProject close={() => setCreating(false)} />}
        <ErrorNotice error={projects.error} />
        {projects.data ? <ProjectCards projects={projects.data} /> : <Loading />}
      </section>
      <section className="dashboard-section">
        <div className="section-heading">
          <h2>Recent runs</h2>
          <Link className="text-link" href="/runs">
            View all runs →
          </Link>
        </div>
        <section className="panel compact">
          <ErrorNotice error={runs.error} />
          {runs.data ? <RunTable runs={runs.data.slice(0, 5)} /> : <Loading />}
        </section>
      </section>
      <section className="dashboard-section">
        <div className="section-heading">
          <h2>Upcoming schedules</h2>
          <Link className="text-link" href="/schedules">
            Manage schedules →
          </Link>
        </div>
        <ErrorNotice error={schedules.error} />
        <section className="panel compact">
          {schedules.data?.some((item) => item.enabled) ? (
            schedules.data
              .filter((item) => item.enabled)
              .slice(0, 5)
              .map((schedule) => (
                <div className="row between schedule-preview" key={schedule.id}>
                  <div>
                    <Link href="/schedules">{schedule.name}</Link>
                    <p className="tiny muted">
                      {schedule.cron} · {schedule.timezone}
                    </p>
                  </div>
                  <div className="align-right">
                    <Badge status="scheduled" />
                    <p className="tiny muted">{date(schedule.next_run_at)}</p>
                  </div>
                </div>
              ))
          ) : (
            <p className="muted small">
              No enabled schedules. Save a run configuration as a schedule to keep projects moving
              automatically.
            </p>
          )}
        </section>
      </section>
    </>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
