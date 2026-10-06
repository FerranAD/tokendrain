import { NumberInput } from './number-input';
import { Icon } from './icons';
import { WorkspacePanel } from './workspace';
import { Kanban } from './kanban';
import { ModelSelector } from './model-selector';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { mutate, useAction, useResource } from './api';
import type { AgentStatus, Execution, Project, ProjectGitHub, Secret, StorageInfo } from './types';
import { GitHubFields, emptyGitHub, githubPayload } from './github-fields';
import {
  ActionNotice,
  Badge,
  bytes,
  date,
  Empty,
  ErrorNotice,
  Link,
  Loading,
  PageTitle,
  ReportView,
  ExecutionOutcome,
  shortId,
  useNavigation,
} from './ui';
import { ProjectGitHubPanel } from './settings';
import { confirmDiscardChanges, UnsavedNotice, useUnsavedChanges } from './drafts';

export function ProjectCards({ projects }: { projects: Project[] }) {
  if (!projects.length)
    return (
      <Empty title="No projects yet">
        Create a project, add approved tasks, and prepare a Run.
      </Empty>
    );
  return (
    <div className="project-grid">
      {projects.map((project) => (
        <Link className="project-card" href={`/projects/${project.id}`} key={project.id}>
          <div className="row between">
            <span className="project-icon" aria-hidden="true">
              {project.name.charAt(0).toUpperCase()}
            </span>
            <Badge status={project.status} />
          </div>
          <h3>{project.name}</h3>
          <p className="project-description">{project.description || 'No description yet.'}</p>

          {project.latest_execution?.termination_reason && (
            <div className="project-recent">
              <Icon name={project.latest_execution.status === 'completed' ? 'check' : 'clock'} />
              <span>{project.latest_execution.termination_reason.replaceAll('_', ' ')}</span>
            </div>
          )}
          <div className="project-footer">
            <span className="mono">{project.default_model || 'Provider default model'}</span>
            <span>
              {project.last_run_at ? date(project.last_run_at) : 'Ready for its first run'}
            </span>
          </div>
        </Link>
      ))}
    </div>
  );
}

export function ProjectsPage() {
  const projects = useResource<Project[]>('/projects');
  const [creating, setCreating] = useState(false);
  return (
    <>
      <PageTitle
        title="Projects"

        actions={
          <button className="primary" onClick={() => setCreating(true)}>
            <Icon name="plus" /> New project
          </button>
        }
      />
      {creating && <NewProject close={() => setCreating(false)} />}
      <ErrorNotice error={projects.error} />
      {projects.data ? <ProjectCards projects={projects.data} /> : <Loading />}
    </>
  );
}

export function NewProject({ close }: { close: () => void }) {
  const account = useResource<AgentStatus>('/agent');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('medium');
  const [attachGitHub, setAttachGitHub] = useState(false);
  const [github, setGitHub] = useState<ProjectGitHub>(emptyGitHub);
  const [initialTasks, setInitialTasks] = useState<
    { title: string; description: string; column: string }[]
  >([]);
  const draft = useUnsavedChanges({
    name,
    description,
    model,
    effort,
    initialTasks,
    attachGitHub,
    github,
  });
  const dismiss = () => {
    if (draft.discard()) close();
  };
  const action = useAction();
  const { go } = useNavigation();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const integration = attachGitHub ? githubPayload(github) : undefined;
      const project = await mutate<Project>('/projects', 'POST', {
        name,
        description,
        initial_tasks: initialTasks.filter((t) => t.title.trim()),
        default_model: model,
        default_reasoning_effort: effort,
        ...(integration ? { github: integration } : {}),
      });
      draft.markSaved();
      go(`/projects/${project.id}`);
    });
  };
  return (
    <section className="panel create-panel">
      <div className="row between">
        <h2>Create a project</h2>
        <button className="quiet" onClick={dismiss} aria-label="Close new project form">
          <Icon name="close" />
        </button>
      </div>
      <form onSubmit={submit}>
        <label>
          Project name
          <input
            autoFocus
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Build a useful little thing"
          />
        </label>
        <label>
          What should the agent achieve?
          <textarea
            rows={5}
            required
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Describe the goal, success criteria, and any constraints…"
          />
        </label>
        <fieldset className="initial-tasks">
          <legend>Agent defaults</legend>
          <ErrorNotice error={account.error} />
          {!account.data ? (
            <Loading />
          ) : !account.data.connected ? (
            <div className="callout">
              Configure {account.data.label || 'Codex'} in{' '}
              <Link
                href={account.data.name === 'claude_code' ? '/settings#claude' : '/settings#openai'}
              >
                Settings
              </Link>{' '}
              to choose a model.
            </div>
          ) : (
            <ModelSelector
              defaults
              model={model}
              effort={effort}
              onChange={(values) => {
                setModel(values.model);
                setEffort(values.reasoning_effort);
              }}
            />
          )}
        </fieldset>
        <fieldset className="initial-tasks">
          <legend>GitHub · optional</legend>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={attachGitHub}
              onChange={(e) => setAttachGitHub(e.target.checked)}
            />
            <span>Attach a GitHub repository</span>
          </label>
          {attachGitHub && <GitHubFields value={github} onChange={setGitHub} />}
        </fieldset>
        <fieldset className="initial-tasks">
          <legend>Initial tasks</legend>
          {initialTasks.map((task, i) => (
            <div className="inset" key={i}>
              <div className="row">
                <input
                  aria-label={`Task ${i + 1} title`}
                  placeholder="Task title"
                  required
                  value={task.title}
                  onChange={(e) =>
                    setInitialTasks(
                      initialTasks.map((t, j) => (j === i ? { ...t, title: e.target.value } : t)),
                    )
                  }
                />
                <select
                  aria-label={`Task ${i + 1} column`}
                  value={task.column}
                  onChange={(e) =>
                    setInitialTasks(
                      initialTasks.map((t, j) => (j === i ? { ...t, column: e.target.value } : t)),
                    )
                  }
                >
                  <option value="todo">Todo — approved</option>
                  <option value="backlog">Backlog — review later</option>
                </select>
                <button
                  type="button"
                  aria-label="Remove initial task"
                  onClick={() => setInitialTasks(initialTasks.filter((_, j) => j !== i))}
                >
                  ×
                </button>
              </div>
              <textarea
                aria-label={`Task ${i + 1} details`}
                rows={2}
                placeholder="Details (optional)"
                value={task.description}
                onChange={(e) =>
                  setInitialTasks(
                    initialTasks.map((t, j) =>
                      j === i ? { ...t, description: e.target.value } : t,
                    ),
                  )
                }
              />
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              setInitialTasks([...initialTasks, { title: '', description: '', column: 'todo' }])
            }
          >
            + Add initial task
          </button>
        </fieldset>
        <p className="small muted">
          Add approved tasks now, or refine the board after creating the project.
        </p>
        <UnsavedNotice dirty={draft.dirty} />
        <ActionNotice {...action} />
        <div className="form-actions">
          <button type="button" onClick={dismiss}>
            Cancel
          </button>
          <button className="primary" disabled={action.busy}>
            {action.busy ? 'Creating storage…' : 'Create project'}
          </button>
        </div>
      </form>
    </section>
  );
}

const tabs = [
  'Overview',
  'Kanban',
  'Workspace',
  'Last run',
  'History',
  'Description',
  'Feedback',
  'VM storage',
  'GitHub',
  'Secrets',
] as const;
type Tab = (typeof tabs)[number];

export function ProjectPage({ id }: { id: string }) {
  const resource = useResource<Project>(`/projects/${id}`);
  const history = useResource<Execution[]>(`/projects/${id}/executions`);
  const [tab, setTab] = useState<Tab>('Overview');
  const action = useAction();
  const { go } = useNavigation();
  if (!resource.data)
    return (
      <>
        <ErrorNotice error={resource.error} />
        {resource.loading && <Loading />}
      </>
    );
  const project = resource.data;
  const report =
    project.latest_report ?? history.data?.find((execution) => execution.report)?.report;
  return (
    <>
      <Link className="back-link" href="/projects">
        ← Projects
      </Link>
      <PageTitle
        title={project.name}
        description={project.description || undefined}
        actions={
          <>
            <Badge status={project.status} />
            <Link className="button primary" href={`/prepare?project=${id}`}>
              <Icon name="play" /> Prepare run
            </Link>
          </>
        }
      />
      <nav className="tabs project-tabs" aria-label="Project sections">
        {tabs.map((name) => (
          <button
            key={name}
            className={name === tab ? 'active' : ''}
            onClick={() => {
              if (name !== tab && confirmDiscardChanges()) setTab(name);
            }}
            aria-current={name === tab ? 'page' : undefined}
          >
            {name === 'Feedback' ? 'Next run feedback' : name}
          </button>
        ))}
      </nav>
      <ErrorNotice error={resource.error} />
      <ActionNotice {...action} />
      {tab === 'Overview' && (
        <div className="two-columns">
          <section className="panel">
            <div className="row between">
              <h2>The goal</h2>
              <button className="quiet" onClick={() => setTab('Description')}>
                Edit
              </button>
            </div>
            <p className="preserve">
              {project.description || 'Add a description to give the agent a clear goal.'}
            </p>
            <dl className="facts">
              <div>
                <dt>Model</dt>
                <dd>{project.default_model || 'Provider default'}</dd>
              </div>
              <div>
                <dt>Reasoning</dt>
                <dd>{project.default_reasoning_effort || 'Provider default'}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{date(project.created_at)}</dd>
              </div>
              <div>
                <dt>Last run</dt>
                <dd>{date(project.last_run_at ?? history.data?.[0]?.started_at)}</dd>
              </div>
            </dl>
          </section>
          <section className="panel">
            <h2>Latest progress</h2>
            {report ? (
              <>
                <p>{report.summary}</p>
                {!!report.blockers?.length && (
                  <div className="callout warning">
                    <strong>Blockers</strong>
                    <ul>
                      {report.blockers.map((blocker, i) => (
                        <li key={i}>{blocker}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <button onClick={() => setTab('Last run')}>Read full report →</button>
              </>
            ) : (
              <Empty title="Ready to begin">Prepare a run to turn this goal into work.</Empty>
            )}
          </section>
        </div>
      )}
      {tab === 'Description' && (
        <>
          <ProjectEditor key={id} project={project} field="description" />
          <section className="panel danger-zone">
            <h3>Delete project</h3>
            <p className="muted small">
              Permanently remove this project, its machine and secrets. Active projects cannot be
              deleted.
            </p>
            <button
              className="danger"
              disabled={action.busy}
              onClick={() => {
                if (
                  window.confirm(`Permanently delete “${project.name}” and all of its stored work?`)
                )
                  void action.run(async () => {
                    await mutate(`/projects/${id}`, 'DELETE');
                    go('/projects');
                  });
              }}
            >
              Delete project…
            </button>
          </section>
        </>
      )}
      {tab === 'Kanban' && <Kanban id={id} />}
      {tab === 'Workspace' && <WorkspacePanel project={project} executions={history.data ?? []} />}
      {tab === 'Feedback' && (
        <ProjectEditor key={`${id}-feedback`} project={project} field="next_run_feedback" />
      )}
      {tab === 'Last run' && (
        <section className="panel">
          <ExecutionOutcome execution={project.latest_execution} />
          <ReportView report={report} />
        </section>
      )}
      {tab === 'History' && (
        <section className="panel">
          <h2>Execution history</h2>
          <ErrorNotice error={history.error} />
          {history.data ? <ExecutionTable executions={history.data} /> : <Loading />}
        </section>
      )}
      {tab === 'VM storage' && <StoragePanel id={id} />}
      {tab === 'GitHub' && <ProjectGitHubPanel id={id} />}
      {tab === 'Secrets' && <SecretsPanel id={id} />}
    </>
  );
}

function ProjectEditor({
  project,
  field,
}: {
  project: Project;
  field: 'description' | 'next_run_feedback';
}) {
  const [text, setText] = useState(project[field]);
  const [name, setName] = useState(project.name);
  const [model, setModel] = useState(project.default_model);
  const [effort, setEffort] = useState(project.default_reasoning_effort);
  const draft = useUnsavedChanges({ text, name, model, effort });
  const action = useAction();
  const labels = {
    description: 'Project description',
    next_run_feedback: 'Feedback for the next run',
  };
  const descriptions = {
    description: 'What should the agent build, and what counts as done?',
    next_run_feedback: 'These instructions are included when the next execution starts.',
  };
  return (
    <section className="panel">
      <h2>{labels[field]}</h2>
      <p className="muted">{descriptions[field]}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            await mutate(`/projects/${project.id}`, 'PATCH', {
              [field]: text,
              ...(field === 'description'
                ? { name, default_model: model, default_reasoning_effort: effort || 'medium' }
                : {}),
            });
            draft.markSaved();
          }, 'Changes saved.');
        }}
      >
        {field === 'description' && (
          <label>
            Name
            <input
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
        )}
        <label>
          <span className="sr-only">{labels[field]}</span>
          <textarea
            aria-label={labels[field]}
            rows={15}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        {field === 'description' && (
          <ModelSelector
            defaults
            model={model}
            effort={effort}
            onChange={(values) => {
              setModel(values.model);
              setEffort(values.reasoning_effort);
            }}
          />
        )}
        <UnsavedNotice dirty={draft.dirty} />
        <ActionNotice {...action} notice={draft.dirty ? undefined : action.notice} />
        <div className="form-actions">
          <button className="primary" disabled={action.busy || !draft.dirty}>
            {action.busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>
    </section>
  );
}

export function ExecutionTable({ executions }: { executions: Execution[] }) {
  if (!executions.length)
    return (
      <Empty title="No executions yet">
        Each run will appear here with its outcome and report.
      </Empty>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Execution / run</th>
            <th>Status</th>
            <th>Model</th>
            <th>Started</th>
            <th>Finished</th>
          </tr>
        </thead>
        <tbody>
          {executions.map((execution) => (
            <tr key={execution.id}>
              <td>
                <Link href={`/runs/${execution.run_id}`}>
                  {execution.project_name || shortId(execution.id)} <span className="muted">↗</span>
                </Link>
                <div className="tiny muted mono">{shortId(execution.run_id)}</div>
              </td>
              <td>
                <Badge status={execution.status} />
              </td>
              <td>
                {execution.model || 'Default'}
                <div className="tiny muted">{execution.reasoning_effort || 'Default effort'}</div>
              </td>
              <td>{date(execution.started_at)}</td>
              <td>{date(execution.finished_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StoragePanel({ id }: { id: string }) {
  const storage = useResource<StorageInfo>(`/projects/${id}/storage`);
  const currentSize = storage.data
    ? Math.ceil(storage.data.virtual_size_bytes / 1024 ** 3)
    : undefined;
  const [size, setSize] = useState<number | ''>('');
  useEffect(() => {
    setSize(currentSize ?? '');
  }, [id, currentSize]);
  const action = useAction();
  return (
    <section className="panel">
      <h2>VM storage</h2>
      <p className="muted">
        This project's development machine persists between runs, including installed tools,
        configuration, caches, and Workspace files. Stop the project before resizing storage.
      </p>
      <ErrorNotice error={storage.error} />
      {storage.data && (
        <article className="storage-card">
          <strong>{bytes(storage.data.virtual_size_bytes)}</strong>
          <p className="muted small">{bytes(storage.data.allocated_bytes)} allocated</p>
        </article>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(
            () => mutate(`/projects/${id}/storage/resize`, 'POST', { size_gib: size }),
            'VM storage resized.',
          );
        }}
      >
        <h3>Resize VM storage</h3>
        <p className="muted small">Storage can grow; shrinking is unsupported.</p>
        <label>
          New capacity, GiB
          <NumberInput
            required
            min={currentSize ?? 1}
            max={4096}
            value={size}
            onValueChange={(value) => setSize(value)}
          />
        </label>
        <button disabled={action.busy || currentSize === undefined}>Resize</button>
      </form>
      <ActionNotice {...action} />
    </section>
  );
}

function SecretsPanel({ id }: { id: string }) {
  const secrets = useResource<Secret[]>(`/projects/${id}/secrets`);
  const [editing, setEditing] = useState<Secret | 'new' | null>(null);
  const [dotenv, setDotenv] = useState('');
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const action = useAction();
  const draft = useUnsavedChanges({ dotenv, descriptions });
  const names = [
    ...new Set(
      Array.from(
        dotenv.matchAll(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/gm),
        (match) => match[1],
      ),
    ),
  ];
  return (
    <>
      <div className="callout warning">
        <strong>Available to everything inside this project’s VM</strong>
        <p>
          Agents execute arbitrary code. Any software they run can read these values or send them
          over the Internet. Add only credentials you intend to grant this project. The agent sees
          each name and explanation in its context.
        </p>
      </div>
      <section className="panel">
        <div className="row between">
          <h2>Project secrets</h2>
          <button
            onClick={() => {
              if (confirmDiscardChanges()) setEditing('new');
            }}
          >
            + Add secret
          </button>
        </div>
        <ErrorNotice error={secrets.error} />
        {editing && (
          <SecretEditor
            key={editing === 'new' ? 'new' : editing.name}
            projectId={id}
            secret={editing === 'new' ? undefined : editing}
            close={() => setEditing(null)}
          />
        )}
        {secrets.data?.length ? (
          <div className="secret-list">
            {secrets.data.map((secret) => (
              <div className="row between secret-row" key={secret.name}>
                <div>
                  <code>{secret.name}</code>
                  <p className="small muted">{secret.description}</p>
                </div>
                <div className="row">
                  <button
                    className="quiet"
                    onClick={() => {
                      if (confirmDiscardChanges()) setEditing(secret);
                    }}
                  >
                    Edit
                  </button>
                  <button
                    className="quiet danger"
                    disabled={action.busy}
                    onClick={() => {
                      if (window.confirm(`Remove ${secret.name} from future executions?`))
                        void action.run(() =>
                          mutate(
                            `/projects/${id}/secrets/${encodeURIComponent(secret.name)}`,
                            'DELETE',
                          ),
                        );
                    }}
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">No generic secrets configured.</p>
        )}
        <ActionNotice {...action} />
      </section>
      <section className="panel">
        <h2>Import a .env bundle</h2>
        <p className="muted">
          Paste or upload a .env file, then explain what each credential is for. Existing names will
          be replaced.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              await mutate(`/projects/${id}/secrets/import`, 'POST', { dotenv, descriptions });
              setDotenv('');
              draft.markSaved({ dotenv: '', descriptions: {} });
              setDescriptions({});
            }, 'Secrets imported.');
          }}
        >
          <label>
            Choose .env file
            <input
              type="file"
              accept=".env,text/plain"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void file.text().then(setDotenv);
                e.target.value = '';
              }}
            />
          </label>
          <label>
            .env content
            <textarea
              className="mono"
              rows={5}
              autoComplete="off"
              spellCheck={false}
              value={dotenv}
              onChange={(e) => setDotenv(e.target.value)}
              placeholder="SERVICE_TEST_KEY=…"
            />
          </label>
          {names.map((name) => (
            <label key={name}>
              Purpose of <code>{name}</code>
              <input
                required
                value={descriptions[name] ?? ''}
                onChange={(e) =>
                  setDescriptions((values) => ({ ...values, [name]: e.target.value }))
                }
                placeholder="Which account, permitted use, and any important limits"
              />
            </label>
          ))}
          <UnsavedNotice dirty={draft.dirty} />
          <button className="primary" disabled={action.busy || !names.length}>
            Import {names.length || ''} secret{names.length === 1 ? '' : 's'}
          </button>
        </form>
      </section>
    </>
  );
}

function SecretEditor({
  projectId,
  secret,
  close,
}: {
  projectId: string;
  secret?: Secret;
  close: () => void;
}) {
  const [name, setName] = useState(secret?.name ?? '');
  const [value, setValue] = useState('');
  const [description, setDescription] = useState(secret?.description ?? '');
  const action = useAction();
  const draft = useUnsavedChanges({ name, value, description });
  return (
    <form
      className="inset"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          await mutate(`/projects/${projectId}/secrets/${encodeURIComponent(name)}`, 'PUT', {
            ...(value || !secret ? { value } : {}),
            description,
          });
          setValue('');
          draft.markSaved();
          close();
        });
      }}
    >
      <div className="form-grid">
        <label>
          Variable name
          <input
            pattern="[A-Za-z_][A-Za-z0-9_]*"
            required
            readOnly={!!secret}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          {secret ? 'Replacement value (leave empty to retain)' : 'Secret value'}
          <input
            type="password"
            autoComplete="new-password"
            required={!secret}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
      </div>
      <label>
        Purpose and allowed use
        <textarea
          rows={2}
          required
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <UnsavedNotice dirty={draft.dirty} />
      <ActionNotice {...action} />
      <div className="form-actions">
        <button
          type="button"
          onClick={() => {
            if (draft.discard()) close();
          }}
        >
          Cancel
        </button>
        <button className="primary" disabled={action.busy}>
          Save secret
        </button>
      </div>
    </form>
  );
}
