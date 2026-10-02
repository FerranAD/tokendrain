import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { mutate, useAction, useEvents, useResource } from './api';
import type {
  CodexModel,
  LiveEvent,
  Project,
  ProjectConfig,
  Run,
  RunTemplate,
  Schedule,
  StopCondition,
  UsageWindow,
} from './types';
import {
  ActionNotice,
  Badge,
  conditionLabel,
  date,
  duration,
  Empty,
  ErrorNotice,
  Link,
  Loading,
  PageTitle,
  ReportView,
  shortId,
  UsageCards,
  useNavigation,
} from './ui';

const terminal = new Set(['completed', 'cancelled', 'failed', 'blocked']);

export function RunTable({ runs }: { runs: Run[] }) {
  if (!runs.length)
    return (
      <Empty
        title="No runs yet"
        action={
          <Link className="button primary" href="/prepare">
            Prepare your first run
          </Link>
        }
      >
        Give one or more projects some time with an agent.
      </Empty>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Status</th>
            <th>Projects</th>
            <th>Created</th>
            <th>Execution</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <Link href={`/runs/${run.id}`} className="mono">
                  {shortId(run.id)} ↗
                </Link>
              </td>
              <td>
                <Badge status={run.status} />
              </td>
              <td>
                {run.executions
                  ?.map((execution) => execution.project_name || shortId(execution.project_id))
                  .join(', ') || 'Preparing'}
              </td>
              <td>{date(run.created_at)}</td>
              <td>{run.parallel ? 'Parallel' : 'Sequential'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RunsPage() {
  const runs = useResource<Run[]>('/runs');
  return (
    <>
      <PageTitle
        eyebrow="Autonomous work"
        title="Runs"
        description="Every execution has a workspace, a budget, and a record of what happened."
        actions={
          <Link className="button primary" href="/prepare">
            ▶ Prepare run
          </Link>
        }
      />
      <section className="panel">
        <ErrorNotice error={runs.error} />
        {runs.data ? <RunTable runs={runs.data} /> : <Loading />}
      </section>
    </>
  );
}

export function PrepareRunPage({ selectedProject }: { selectedProject?: string }) {
  const { go } = useNavigation();
  const [mode, setMode] = useState<'run' | 'schedule'>('run');
  const [name, setName] = useState('Daily project work');
  const [cron, setCron] = useState('0 3 * * *');
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  return (
    <>
      <PageTitle
        eyebrow="Put available usage to work"
        title="Prepare a run"
        description="Choose projects, tune their models, and define when work should stop."
      />
      <RunBuilder
        selectedProject={selectedProject}
        submitLabel={mode === 'run' ? 'Start run' : 'Create schedule'}
        onSubmit={async (template) => {
          if (mode === 'run') {
            const result = await mutate<Run>('/runs', 'POST', template);
            go(`/runs/${result.id}`);
          } else {
            await mutate('/schedules', 'POST', {
              name,
              cron,
              timezone,
              enabled: true,
              run_template: template,
            });
            go('/schedules');
          }
        }}
      >
        <section className="panel">
          <h2>When to run</h2>
          <div className="segmented">
            <button
              type="button"
              className={mode === 'run' ? 'selected' : ''}
              onClick={() => setMode('run')}
            >
              Start now
            </button>
            <button
              type="button"
              className={mode === 'schedule' ? 'selected' : ''}
              onClick={() => setMode('schedule')}
            >
              Save a schedule
            </button>
          </div>
          {mode === 'schedule' && (
            <ScheduleFields {...{ name, setName, cron, setCron, timezone, setTimezone }} />
          )}
        </section>
      </RunBuilder>
    </>
  );
}

function sameWindow(condition: StopCondition, window: UsageWindow) {
  return (
    condition.kind === 'usage' &&
    condition.window_minutes === window.window_minutes &&
    (!condition.limit_id || condition.limit_id === window.limit_id)
  );
}

export function RunBuilder({
  initial,
  selectedProject,
  children,
  onSubmit,
  submitLabel = 'Save',
}: {
  initial?: RunTemplate;
  selectedProject?: string;
  children?: ReactNode;
  onSubmit: (template: RunTemplate) => Promise<void>;
  submitLabel?: string;
}) {
  const projects = useResource<Project[]>('/projects');
  const models = useResource<CodexModel[]>('/auth/openai/models');
  const usage = useResource<UsageWindow[]>('/usage');
  const [configs, setConfigs] = useState<ProjectConfig[]>(initial?.projects ?? []);
  const [conditions, setConditions] = useState<StopCondition[]>(
    initial?.stop_conditions ?? [{ kind: 'provider_limit' }, { kind: 'project_completed' }],
  );
  const [parallel, setParallel] = useState(initial?.parallel ?? true);
  const [initialized, setInitialized] = useState(false);
  const action = useAction();
  // Apply the URL selection once after projects arrive without resetting edits on live updates.
  if (!initialized && projects.data) {
    setInitialized(true);
    const project = projects.data.find((item) => item.id === selectedProject);
    if (!initial && project)
      setConfigs([
        {
          project_id: project.id,
          model: project.default_model || '',
          reasoning_effort: project.default_reasoning_effort || '',
        },
      ]);
  }
  const toggleProject = (project: Project, checked: boolean) =>
    setConfigs((old) =>
      checked
        ? [
            ...old,
            {
              project_id: project.id,
              model: project.default_model || '',
              reasoning_effort: project.default_reasoning_effort || '',
            },
          ]
        : old.filter((item) => item.project_id !== project.id),
    );
  const toggleCondition = (kind: 'provider_limit' | 'project_completed', checked: boolean) =>
    setConditions((old) =>
      checked
        ? [...old.filter((item) => item.kind !== kind), { kind }]
        : old.filter((item) => item.kind !== kind),
    );
  const elapsed = conditions.find((condition) => condition.kind === 'elapsed');
  const observed =
    usage.data?.filter((window) => window.window_minutes != null && window.window_minutes > 0) ??
    [];
  const unavailable = conditions.filter(
    (condition) =>
      condition.kind === 'usage' && !observed.some((window) => sameWindow(condition, window)),
  );
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (configs.length && conditions.length)
          void action.run(() =>
            onSubmit({
              projects: configs.map((config) => ({
                ...config,
                reasoning_effort: config.reasoning_effort || 'medium',
              })),
              stop_conditions: conditions,
              parallel,
            }),
          );
      }}
    >
      <section className="panel">
        <div className="row between">
          <h2>
            <span className="step-number">1</span> Choose projects
          </h2>
          <span className="muted small">{configs.length} selected</span>
        </div>
        <ErrorNotice error={projects.error} />
        {!projects.data && <Loading />}
        {projects.data?.length === 0 && (
          <Empty
            title="Create a project first"
            action={
              <Link className="button" href="/projects">
                Go to projects
              </Link>
            }
          >
            Projects keep their source code, environment, and progress between runs.
          </Empty>
        )}
        <div className="run-projects">
          {projects.data?.map((project) => {
            const config = configs.find((item) => item.project_id === project.id);
            const selectedModel = models.data?.find((item) => item.id === config?.model);
            const efforts = selectedModel?.reasoning_efforts?.length
              ? selectedModel.reasoning_efforts
              : ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
            const patch = (values: Partial<ProjectConfig>) =>
              setConfigs((old) =>
                old.map((item) => (item.project_id === project.id ? { ...item, ...values } : item)),
              );
            return (
              <div className={`run-project ${config ? 'selected' : ''}`} key={project.id}>
                <label className="checkbox project-select">
                  <input
                    type="checkbox"
                    checked={!!config}
                    onChange={(e) => toggleProject(project, e.target.checked)}
                  />
                  <span>
                    <strong>{project.name}</strong>
                    <span className="small muted clipped">{project.description}</span>
                  </span>
                </label>
                {config && (
                  <div className="model-settings">
                    <label>
                      Model
                      <input
                        list="codex-models"
                        value={config.model}
                        onChange={(e) => patch({ model: e.target.value })}
                        placeholder="Provider default"
                      />
                    </label>
                    <label>
                      Reasoning
                      <select
                        value={config.reasoning_effort}
                        onChange={(e) => patch({ reasoning_effort: e.target.value })}
                      >
                        <option value="">Default (medium)</option>
                        {efforts.map((effort) => (
                          <option key={effort}>{effort}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <datalist id="codex-models">
          {models.data?.map((model) => (
            <option value={model.id} key={model.id}>
              {model.name || model.id}
            </option>
          ))}
        </datalist>
        {models.error && (
          <p className="small muted">
            Model discovery is unavailable: {models.error}. You can enter a model ID or use the
            provider default.
          </p>
        )}
        <label className="checkbox top-space">
          <input
            type="checkbox"
            checked={parallel}
            onChange={(e) => setParallel(e.target.checked)}
          />
          <span>Run projects in parallel, within the configured concurrency limit</span>
        </label>
      </section>
      <section className="panel">
        <h2>
          <span className="step-number">2</span> Stop when any condition is met
        </h2>
        <p className="muted">
          Usage thresholds are checked between useful turns. Projects stop starting new work at the
          boundary.
        </p>
        <ErrorNotice error={usage.error} />
        {!!usage.data?.length && <UsageCards windows={usage.data} />}
        <div className="conditions">
          {observed.map((window, index) => {
            const condition = conditions.find((item) => sameWindow(item, window));
            return (
              <div className="condition-row" key={`${window.limit_id}-${index}`}>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={!!condition}
                    onChange={(e) =>
                      setConditions((old) =>
                        e.target.checked
                          ? [
                              ...old,
                              {
                                kind: 'usage',
                                limit_id: window.limit_id,
                                window_minutes: window.window_minutes!,
                                used_percent: 95,
                              },
                            ]
                          : old.filter((item) => !sameWindow(item, window)),
                      )
                    }
                  />
                  <span>
                    {window.name || window.limit_id} · {duration(window.window_minutes)} window
                    reaches
                  </span>
                </label>
                <label className="number-inline">
                  <span className="sr-only">Usage threshold percent</span>
                  <input
                    type="number"
                    min={1}
                    max={100}
                    step={0.1}
                    required={!!condition}
                    disabled={!condition}
                    value={condition?.kind === 'usage' ? condition.used_percent : 95}
                    onChange={(e) =>
                      setConditions((old) =>
                        old.map((item) =>
                          sameWindow(item, window)
                            ? { ...item, used_percent: Number(e.target.value) }
                            : item,
                        ),
                      )
                    }
                  />
                  <span>% used</span>
                </label>
              </div>
            );
          })}
          {!observed.length && (
            <p className="small muted">
              Usage rules become available after Codex reports your account’s window metadata. You
              can use a runtime limit in the meantime.
            </p>
          )}
          {unavailable.map((condition, index) => (
            <div className="condition-row" key={index}>
              <span>
                {conditionLabel(condition)}{' '}
                <span className="muted small">(not currently observed)</span>
              </span>
              <button
                type="button"
                className="quiet"
                onClick={() => setConditions((old) => old.filter((item) => item !== condition))}
              >
                Remove
              </button>
            </div>
          ))}
          <div className="condition-row">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={!!elapsed}
                onChange={(e) =>
                  setConditions((old) =>
                    e.target.checked
                      ? [
                          ...old.filter((item) => item.kind !== 'elapsed'),
                          { kind: 'elapsed', seconds: 14400 },
                        ]
                      : old.filter((item) => item.kind !== 'elapsed'),
                  )
                }
              />
              <span>Elapsed runtime reaches</span>
            </label>
            <label className="number-inline">
              <span className="sr-only">Runtime limit in hours</span>
              <input
                type="number"
                min={0.0167}
                max={168}
                step="any"
                disabled={!elapsed}
                required={!!elapsed}
                value={elapsed?.kind === 'elapsed' ? elapsed.seconds / 3600 : 4}
                onChange={(e) =>
                  setConditions((old) =>
                    old.map((item) =>
                      item.kind === 'elapsed'
                        ? { kind: 'elapsed', seconds: Math.round(Number(e.target.value) * 3600) }
                        : item,
                    ),
                  )
                }
              />
              <span>hours</span>
            </label>
          </div>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={conditions.some((item) => item.kind === 'provider_limit')}
              onChange={(e) => toggleCondition('provider_limit', e.target.checked)}
            />
            <span>Provider usage limit prevents further work</span>
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={conditions.some((item) => item.kind === 'project_completed')}
              onChange={(e) => toggleCondition('project_completed', e.target.checked)}
            />
            <span>Project is complete</span>
          </label>
        </div>
      </section>
      {children}
      <ActionNotice {...action} />
      <div className="run-submit">
        <p className="small muted">
          Agents have full administrative access inside their VMs and work without command
          approvals.
        </p>
        <button className="primary" disabled={action.busy || !configs.length || !conditions.length}>
          {action.busy ? 'Saving…' : submitLabel} <span aria-hidden="true">→</span>
        </button>
      </div>
    </form>
  );
}

export function RunPage({ id }: { id: string }) {
  const run = useResource<Run>(`/runs/${id}`);
  const history = useResource<LiveEvent[]>(`/runs/${id}/events`);
  const usage = useResource<UsageWindow[]>('/usage');
  const { events, connected } = useEvents();
  const action = useAction();
  const [tab, setTab] = useState<'executions' | 'events'>('executions');
  const log = useMemo(() => {
    const collected = [...(history.data ?? []), ...events.filter((event) => event.run_id === id)];
    const seen = new Set<string>();
    return collected
      .filter((event) => {
        const key = String(event.id || `${event.timestamp}-${event.type}-${event.message}`);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(-1000);
  }, [history.data, events, id]);
  if (!run.data)
    return (
      <>
        <ErrorNotice error={run.error} />
        {run.loading && <Loading />}
      </>
    );
  const data = run.data;
  return (
    <>
      <Link href="/runs" className="back-link">
        ← Runs
      </Link>
      <PageTitle
        eyebrow="Run detail"
        title={`Run ${shortId(id)}`}
        description={`Created ${date(data.created_at)}`}
        actions={
          <>
            <Badge status={data.status} />
            {!terminal.has(data.status) && (
              <button
                className="danger"
                disabled={action.busy}
                onClick={() => {
                  void action.run(
                    () => mutate(`/runs/${id}/cancel`, 'POST', {}),
                    'Cancellation requested. Executions are shutting down.',
                  );
                }}
              >
                Cancel run
              </button>
            )}
          </>
        }
      />
      <ActionNotice {...action} />
      <ErrorNotice error={run.error} />
      <section className="panel run-meta">
        <dl className="facts horizontal">
          <div>
            <dt>Started</dt>
            <dd>{date(data.started_at)}</dd>
          </div>
          <div>
            <dt>Finished</dt>
            <dd>{date(data.finished_at)}</dd>
          </div>
          <div>
            <dt>Execution</dt>
            <dd>{data.parallel ? 'Parallel' : 'Sequential'}</dd>
          </div>
          <div>
            <dt>Projects</dt>
            <dd>{data.executions.length}</dd>
          </div>
        </dl>
        <div className="stop-chips">
          <span className="small muted">Stop when ANY:</span>
          {data.stop_conditions.map((condition, index) => (
            <span className="chip" key={index}>
              {conditionLabel(condition)}
            </span>
          ))}
        </div>
      </section>
      {!!usage.data?.length && <UsageCards windows={usage.data} />}
      <nav className="tabs" aria-label="Run sections">
        <button
          className={tab === 'executions' ? 'active' : ''}
          onClick={() => setTab('executions')}
        >
          Executions & reports
        </button>
        <button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>
          Live events <span className={`status-dot ${connected ? 'online' : ''}`} />
        </button>
      </nav>
      {tab === 'executions' ? (
        <div className="execution-list">
          {data.executions.map((execution) => (
            <section className="panel" key={execution.id}>
              <div className="row between wrap">
                <div>
                  <Link className="execution-title" href={`/projects/${execution.project_id}`}>
                    {execution.project_name || `Project ${shortId(execution.project_id)}`} ↗
                  </Link>
                  <p className="small muted">
                    {execution.model || 'Provider default model'} ·{' '}
                    {execution.reasoning_effort || 'Default reasoning'} · Started{' '}
                    {date(execution.started_at)}
                  </p>
                </div>
                <Badge status={execution.status} />
              </div>
              {execution.error && <ErrorNotice error={execution.error} />}
              {execution.report ? (
                <ReportView report={execution.report} />
              ) : (
                <p className="muted small">
                  {terminal.has(execution.status)
                    ? 'No final agent report was produced. Check the execution events for details.'
                    : 'The agent’s report will appear here when this execution ends.'}
                </p>
              )}
              {execution.thread_id && (
                <details>
                  <summary>Execution identifiers</summary>
                  <p className="mono tiny">
                    Execution: {execution.id}
                    <br />
                    Codex thread: {execution.thread_id}
                  </p>
                </details>
              )}
            </section>
          ))}
        </div>
      ) : (
        <section className="panel">
          <div className="row between">
            <h2>Execution events</h2>
            <span className="tiny muted">
              {connected ? 'Live connection' : 'Reconnecting…'} · Last {log.length} events
            </span>
          </div>
          <ErrorNotice error={history.error} />
          <div className="event-log" role="log" aria-live="off">
            {log.length ? (
              log.map((event, index) => (
                <div className="log-row" key={`${event.id}-${index}`}>
                  <time>
                    {event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '—'}
                  </time>
                  <span className="log-type">{event.type}</span>
                  <span>{event.message || (event.data ? JSON.stringify(event.data) : '')}</span>
                </div>
              ))
            ) : (
              <p className="muted">Waiting for execution events…</p>
            )}
          </div>
        </section>
      )}
    </>
  );
}

function ScheduleFields({
  name,
  setName,
  cron,
  setCron,
  timezone,
  setTimezone,
}: {
  name: string;
  setName: (value: string) => void;
  cron: string;
  setCron: (value: string) => void;
  timezone: string;
  setTimezone: (value: string) => void;
}) {
  return (
    <div className="top-space">
      <label>
        Schedule name
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required />
      </label>
      <div className="form-grid">
        <label>
          Cron expression
          <input
            className="mono"
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            required
            placeholder="0 3 * * *"
          />
          <span className="hint">
            Minute · hour · day · month · weekday. “0 3 * * *” runs daily at 03:00.
          </span>
        </label>
        <label>
          Timezone
          <input
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            required
            placeholder="Europe/Madrid"
          />
          <span className="hint">
            IANA timezone name. Scheduling follows local daylight saving time.
          </span>
        </label>
      </div>
    </div>
  );
}

export function SchedulesPage() {
  const schedules = useResource<Schedule[]>('/schedules');
  const [editing, setEditing] = useState<Schedule>();
  const action = useAction();
  return (
    <>
      <PageTitle
        eyebrow="Keep work moving"
        title="Schedules"
        description="Recurring triggers create ordinary runs with the same project settings and limits."
        actions={
          <Link className="button primary" href="/prepare">
            + New schedule
          </Link>
        }
      />
      <ActionNotice {...action} />
      <ErrorNotice error={schedules.error} />
      {editing ? (
        <ScheduleEditor key={editing.id} schedule={editing} close={() => setEditing(undefined)} />
      ) : !schedules.data ? (
        <Loading />
      ) : !schedules.data.length ? (
        <section className="panel">
          <Empty
            title="Work while you’re away"
            action={
              <Link className="button" href="/prepare">
                Prepare a scheduled run
              </Link>
            }
          >
            A timezone-aware schedule can put unused capacity to work every day.
          </Empty>
        </section>
      ) : (
        <div className="schedule-list">
          {schedules.data.map((schedule) => (
            <section className="panel" key={schedule.id}>
              <div className="row between wrap">
                <div>
                  <h2>{schedule.name}</h2>
                  <p className="small muted">
                    <code>{schedule.cron}</code> · {schedule.timezone}
                  </p>
                </div>
                <div className="row">
                  <Badge status={schedule.enabled ? 'enabled' : 'paused'} />
                  <button
                    disabled={action.busy}
                    onClick={() => {
                      void action.run(() =>
                        mutate(`/schedules/${schedule.id}`, 'PATCH', {
                          enabled: !schedule.enabled,
                        }),
                      );
                    }}
                  >
                    {schedule.enabled ? 'Pause' : 'Enable'}
                  </button>
                  <button onClick={() => setEditing(schedule)}>Edit</button>
                  <button
                    className="quiet danger"
                    disabled={action.busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete schedule “${schedule.name}”? Existing runs will remain.`,
                        )
                      )
                        void action.run(() => mutate(`/schedules/${schedule.id}`, 'DELETE'));
                    }}
                  >
                    Delete
                  </button>
                </div>
              </div>
              <dl className="facts horizontal">
                <div>
                  <dt>Next trigger</dt>
                  <dd>{schedule.enabled ? date(schedule.next_run_at) : 'Paused'}</dd>
                </div>
                <div>
                  <dt>Last trigger</dt>
                  <dd>{date(schedule.last_run_at)}</dd>
                </div>
                <div>
                  <dt>Projects</dt>
                  <dd>{schedule.run_template.projects.length}</dd>
                </div>
              </dl>
              <div className="stop-chips">
                {schedule.run_template.stop_conditions.map((condition, index) => (
                  <span className="chip" key={index}>
                    {conditionLabel(condition)}
                  </span>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </>
  );
}

function ScheduleEditor({ schedule, close }: { schedule: Schedule; close: () => void }) {
  const [name, setName] = useState(schedule.name);
  const [cron, setCron] = useState(schedule.cron);
  const [timezone, setTimezone] = useState(schedule.timezone);
  return (
    <>
      <div className="row between">
        <h2>Edit {schedule.name}</h2>
        <button onClick={close}>Close editor</button>
      </div>
      <RunBuilder
        initial={schedule.run_template}
        submitLabel="Save schedule"
        onSubmit={async (template) => {
          await mutate(`/schedules/${schedule.id}`, 'PATCH', {
            name,
            cron,
            timezone,
            run_template: template,
          });
          close();
        }}
      >
        <section className="panel">
          <h2>Timing</h2>
          <ScheduleFields {...{ name, setName, cron, setCron, timezone, setTimezone }} />
        </section>
      </RunBuilder>
    </>
  );
}
