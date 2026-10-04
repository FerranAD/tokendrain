import { Icon } from './icons';
import { ModelSelector } from './model-selector';
import { confirmDiscardChanges, UnsavedNotice, useUnsavedChanges } from './drafts';
import { ActivityTimeline } from './activity';
import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { mutate, useAction, useEvents, useResource } from './api';
import type {
  ProjectGitHub,
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
  ExecutionOutcome,
  shortId,
  UsageCards,
  useNavigation,
} from './ui';

const terminal = new Set(['completed', 'cancelled', 'failed', 'blocked', 'stopped']);

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
                  {shortId(run.id)}
                </Link>
              </td>
              <td>
                <Badge status={run.status} />
                {run.executions?.find((execution) => execution.termination_reason)
                  ?.termination_reason && (
                  <span className="table-subtext">
                    {run.executions
                      .find((execution) => execution.termination_reason)
                      ?.termination_reason?.replaceAll('_', ' ')}
                  </span>
                )}
              </td>
              <td>
                {run.executions
                  ?.map((execution) => execution.project_name || shortId(execution.project_id))
                  .join(', ') || 'Preparing'}
              </td>
              <td>{date(run.created_at)}</td>
              <td>
                <span className="run-policy">
                  {run.threshold_mode === 'hard' ? 'Hard limit' : 'Graceful stop'}
                </span>
                <span className="table-subtext">{run.parallel ? 'Parallel' : 'Sequential'}</span>
              </td>
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
        title="Runs"
        actions={
          <Link className="button primary" href="/prepare">
            <Icon name="play" /> Prepare run
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
      <PageTitle title="Prepare a run" />
      <RunBuilder
        selectedProject={selectedProject}
        draftContext={{ mode, name, cron, timezone }}
        submitLabel={mode === 'run' ? 'Start run' : 'Create schedule'}
        onSubmit={async (template, saved) => {
          if (mode === 'run') {
            const result = await mutate<Run>('/runs', 'POST', template);
            saved();
            go(`/runs/${result.id}`);
          } else {
            await mutate('/schedules', 'POST', {
              name,
              cron,
              timezone,
              enabled: true,
              run_template: template,
            });
            saved();
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
              aria-pressed={mode === 'run'}
              onClick={() => setMode('run')}
            >
              Start now
            </button>
            <button
              type="button"
              className={mode === 'schedule' ? 'selected' : ''}
              aria-pressed={mode === 'schedule'}
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

function GitHubRunWarning({ id }: { id: string }) {
  const integration = useResource<ProjectGitHub | null>(`/projects/${id}/github`);
  return integration.data?.permissions.pull_requests === 'write' &&
    integration.data.permissions.contents !== 'write' ? (
    <div className="notice warning">
      This project can create pull requests, but cannot publish the source branch. Give the target
      repository Contents write access.
    </div>
  ) : null;
}

export function RunBuilder({
  initial,
  selectedProject,
  children,
  onSubmit,
  submitLabel = 'Save',
  draftContext,
}: {
  initial?: RunTemplate;
  selectedProject?: string;
  children?: ReactNode;
  onSubmit: (template: RunTemplate, saved: () => void) => Promise<void>;
  submitLabel?: string;
  draftContext?: unknown;
}) {
  const projects = useResource<Project[]>('/projects');
  const usage = useResource<UsageWindow[]>('/usage');
  const [configs, setConfigs] = useState<ProjectConfig[]>(initial?.projects ?? []);
  const [conditions, setConditions] = useState<StopCondition[]>(
    initial?.stop_conditions ?? [{ kind: 'provider_limit' }, { kind: 'project_completed' }],
  );
  const [thresholdMode, setThresholdMode] = useState<'graceful' | 'hard'>(
    initial?.threshold_mode ?? 'graceful',
  );
  const [parallel, setParallel] = useState(initial?.parallel ?? true);
  const [initialized, setInitialized] = useState(false);
  const action = useAction();
  const configuration = { configs, conditions, thresholdMode, parallel, draftContext };
  // Preparation is a new configuration, not edits to a saved Run or schedule.
  const draft = useUnsavedChanges(configuration, configuration, !!initial);
  // Apply the URL selection once after projects arrive without resetting edits on live updates.
  if (!initialized && projects.data) {
    setInitialized(true);
    const project = projects.data.find((item) => item.id === selectedProject);
    if (!initial && project) {
      const selected = [
        {
          project_id: project.id,
          model: project.default_model || '',
          reasoning_effort: project.default_reasoning_effort || '',
        },
      ];
      setConfigs(selected);
      draft.markSaved({ configs: selected, conditions, thresholdMode, parallel, draftContext });
    }
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
      className="run-builder"
      onSubmit={(e) => {
        e.preventDefault();
        if (configs.length && conditions.length)
          void action.run(() =>
            onSubmit(
              {
                projects: configs.map((config) => ({
                  ...config,
                  reasoning_effort: config.reasoning_effort || 'medium',
                })),
                stop_conditions: conditions,
                parallel,
                threshold_mode: thresholdMode,
              },
              () => draft.markSaved(),
            ),
          );
      }}
    >
      <div className="builder-layout">
        <div className="builder-main">
          <section className="panel builder-projects">
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
                const patch = (values: Partial<ProjectConfig>) =>
                  setConfigs((old) =>
                    old.map((item) =>
                      item.project_id === project.id ? { ...item, ...values } : item,
                    ),
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
                    <Badge status={project.status} />
                    {config && (
                      <>
                        <ModelSelector
                          model={config.model}
                          effort={config.reasoning_effort}
                          onChange={patch}
                        />
                        <GitHubRunWarning id={project.id} />
                      </>
                    )}
                  </div>
                );
              })}
            </div>
            <label className="checkbox top-space">
              <input
                type="checkbox"
                checked={parallel}
                onChange={(e) => setParallel(e.target.checked)}
              />
              <span>Run projects in parallel</span>
            </label>
          </section>
          <div className="builder-timing">{children}</div>
        </div>
        <div className="builder-side">
          <section className="panel builder-limits">
            <h2>
              <span className="step-number">2</span> When to stop
            </h2>
            <p className="muted">The first condition reached stops the run.</p>
            <ErrorNotice error={usage.error} />
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
                        <small className="condition-observation">
                          {window.used_percent.toFixed(1)}% used now
                          {window.resets_at && ` · Resets ${date(window.resets_at)}`}
                        </small>
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
                  Usage rules become available after Codex reports your account’s window metadata.
                  You can use a runtime limit in the meantime.
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
                            ? {
                                kind: 'elapsed',
                                seconds: Math.round(Number(e.target.value) * 3600),
                              }
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
          <section className="panel builder-policy">
            <h3>At the usage boundary</h3>
            <div className="threshold-options">
              <label className="checkbox">
                <input
                  type="radio"
                  name="threshold-mode"
                  checked={thresholdMode === 'graceful'}
                  onChange={() => setThresholdMode('graceful')}
                />
                <span>
                  <strong>Graceful stop</strong>
                  <span className="hint">
                    Save a checkpoint and settle current work. Up to 90 seconds to finish; may go
                    slightly over the limit.
                  </span>
                </span>
              </label>
              <label className="checkbox">
                <input
                  type="radio"
                  name="threshold-mode"
                  checked={thresholdMode === 'hard'}
                  onChange={() => setThresholdMode('hard')}
                />
                <span>
                  <strong>Hard limit</strong>
                  <span className="hint">
                    Interrupt as soon as the limit is observed. No extra model work. Provider usage
                    updates may arrive late.
                  </span>
                </span>
              </label>
            </div>
          </section>
        </div>
      </div>

      <UnsavedNotice dirty={draft.dirty} />
      <ActionNotice {...action} />
      <div className="run-submit">
        <p className="small muted">
          {configs.length} {configs.length === 1 ? 'project' : 'projects'} selected ·{' '}
          {thresholdMode === 'hard' ? 'Hard limit' : 'Graceful stop'}
        </p>
        <button className="primary" disabled={action.busy || !configs.length || !conditions.length}>
          {action.busy ? 'Saving…' : submitLabel} <Icon name="arrow" />
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
  const [tab, setTab] = useState<'executions' | 'events'>();
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
  const selectedTab = tab ?? (terminal.has(data.status) ? 'executions' : 'events');
  return (
    <>
      <Link href="/runs" className="back-link">
        ← Runs
      </Link>
      <PageTitle
        title={
          <>
            Run <code className="heading-id">{shortId(id)}</code>
          </>
        }
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
          {data.finished_at && (
            <div>
              <dt>Finished</dt>
              <dd>{date(data.finished_at)}</dd>
            </div>
          )}
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
          <span className="small muted">Stop policy</span>
          <span className="chip">
            {data.threshold_mode === 'hard' ? 'Hard limit' : 'Graceful stop'}
          </span>
          {data.stop_conditions.map((condition, index) => (
            <span className="chip" key={index}>
              {conditionLabel(condition)}
            </span>
          ))}
        </div>
      </section>
      {!!usage.data?.length && (
        <div className="run-usage">
          <UsageCards windows={usage.data} />
        </div>
      )}
      <nav className="tabs run-tabs" aria-label="Run sections">
        <button
          className={selectedTab === 'executions' ? 'active' : ''}
          onClick={() => setTab('executions')}
        >
          Executions & reports
        </button>
        <button
          className={selectedTab === 'events' ? 'active' : ''}
          onClick={() => setTab('events')}
        >
          Activity <span className={`status-dot ${connected ? 'online' : ''}`} />
        </button>
      </nav>
      {selectedTab === 'executions' ? (
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
              <ExecutionOutcome execution={execution} />
              {execution.checkpoint_from_execution_id &&
                execution.checkpoint_from_execution_id !== execution.id && (
                  <p className="small muted">
                    Checkpoint from a previous execution. This execution produced no new valid
                    report.
                  </p>
                )}
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
        <section className="panel activity-panel">
          <div className="row between">
            <h2>Activity</h2>
            <span className="tiny muted activity-connection">
              {connected ? 'Live' : !terminal.has(data.status) ? 'Reconnecting…' : 'Saved activity'}{' '}
              · {log.length} events
            </span>
          </div>
          <ErrorNotice error={history.error} />
          <ActivityTimeline events={log} executions={data.executions} />
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
        title="Schedules"
        actions={
          <Link className="button primary" href="/prepare">
            <Icon name="plus" /> New schedule
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
            title="No schedules yet"
            action={
              <Link className="button" href="/prepare">
                Prepare a scheduled run
              </Link>
            }
          >
            Set a time and reuse your run configuration.
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
  const timing = useUnsavedChanges({ name, cron, timezone });
  return (
    <>
      <div className="row between">
        <h2>Edit {schedule.name}</h2>
        <button
          onClick={() => {
            if (confirmDiscardChanges()) close();
          }}
        >
          Close editor
        </button>
      </div>
      <RunBuilder
        initial={schedule.run_template}
        draftContext={{ name, cron, timezone }}
        submitLabel="Save schedule"
        onSubmit={async (template, saved) => {
          await mutate(`/schedules/${schedule.id}`, 'PATCH', {
            name,
            cron,
            timezone,
            run_template: template,
          });
          saved();
          timing.markSaved();
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
