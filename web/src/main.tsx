import { Icon as NavIcon } from './icons';
import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, EventsProvider, mutate, useAction, useResource, useEvents } from './api';
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
import './design.css';
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
        <div className="app-context">
          <span>tokendrain</span>
          <NavIcon name="chevron" />
          <span>
            {route === '/'
              ? 'Dashboard'
              : route.startsWith('/prepare')
                ? 'Prepare run'
                : navigation.find((item) => item.path !== '/' && route.startsWith(item.path))
                    ?.label}
          </span>
        </div>
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
  const { events } = useEvents();
  const active =
    runs.data?.filter(
      (run) => !['completed', 'failed', 'cancelled', 'blocked', 'stopped'].includes(run.status),
    ).length ?? 0;
  return (
    <>
      <PageTitle
        title="Dashboard"
        actions={
          <Link className="button primary" href="/prepare">
            <NavIcon name="play" /> Prepare run
          </Link>
        }
      />
      <section className="dashboard-section dashboard-usage">
        <div className="section-heading">
          <h2>Usage limits</h2>
          <Link className="text-link" href="/settings">
            Account settings
          </Link>
        </div>
        <ErrorNotice error={usage.error} />
        <UsageCards windows={usage.data ?? []} />
        <p className="tiny muted">Includes usage from your other Codex sessions.</p>
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
        <section className="dashboard-section dashboard-active">
          <div className="section-heading">
            <h2>
              <span className="live-dot" /> Working now
            </h2>
            <span className="tiny muted">
              {active} active {active === 1 ? 'run' : 'runs'}
            </span>
          </div>
          {runs.data
            ?.filter(
              (run) =>
                !['completed', 'failed', 'cancelled', 'blocked', 'stopped'].includes(run.status),
            )
            .map((run) => {
              const latest = events
                .filter(
                  (event) =>
                    event.run_id === run.id &&
                    ['agent.progress', 'command', 'execution.state'].includes(event.type),
                )
                .at(-1);
              return (
                <Link className="active-run-card" key={run.id} href={`/runs/${run.id}`}>
                  <span className="active-run-icon">
                    <NavIcon name="terminal" />
                  </span>
                  <div>
                    <strong>
                      {run.executions
                        .map(
                          (execution) => execution.project_name || execution.project_id.slice(0, 8),
                        )
                        .join(' · ') || 'Preparing projects'}
                    </strong>
                    <p>
                      {latest?.message ||
                        run.executions.find((execution) => execution.report)?.report?.summary ||
                        'Agent is working. Open the run to follow its activity.'}
                    </p>
                    <span className="mono tiny muted">
                      {run.id.slice(0, 8)} · Started {date(run.started_at)}
                    </span>
                  </div>
                  <Badge status={run.status} />
                  <NavIcon name="chevron" />
                </Link>
              );
            })}
        </section>
      )}
      <div className="dashboard-bottom">
        <section className="dashboard-section">
          <div className="section-heading">
            <h2>
              <Link href="/projects">Projects</Link>{' '}
              <span className="count">{projects.data?.length ?? 0}</span>
            </h2>
            <button className="quiet" onClick={() => setCreating((value) => !value)}>
              <NavIcon name="plus" /> New project
            </button>
          </div>
          {creating && <NewProject close={() => setCreating(false)} />}
          <ErrorNotice error={projects.error} />
          {projects.data ? <ProjectCards projects={projects.data.slice(0, 6)} /> : <Loading />}
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
              <p className="muted small">No scheduled runs. Create one from Prepare run.</p>
            )}
          </section>
        </section>
      </div>
    </>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
