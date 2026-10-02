import { useState } from 'react';
import type { FormEvent } from 'react';
import { mutate, useAction, useResource } from './api';
import type { Execution, Project, Secret, Snapshot, StorageInfo } from './types';
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
  shortId,
  useNavigation,
} from './ui';
import { ProjectGitHubPanel } from './settings';

export function ProjectCards({ projects }: { projects: Project[] }) {
  if (!projects.length)
    return (
      <Empty title="Give your next project a home">
        Add a goal. Its workspace, tools, and progress will stay here between runs.
      </Empty>
    );
  return (
    <div className="project-grid">
      {projects.map((project) => (
        <Link className="project-card" href={`/projects/${project.id}`} key={project.id}>
          <div className="row between">
            <span className="project-icon" aria-hidden="true">
              ⌘
            </span>
            <Badge status={project.status} />
          </div>
          <h3>{project.name}</h3>
          <p className="project-description">{project.description || 'No description yet.'}</p>
          <TaskProgress taskLog={project.task_log} />
          <div className="project-footer">
            <span>{project.default_model || 'Provider default model'}</span>
            <span>
              {project.last_run_at ? date(project.last_run_at) : 'Ready for its first run'}
            </span>
          </div>
        </Link>
      ))}
    </div>
  );
}

function TaskProgress({ taskLog }: { taskLog: string }) {
  const tasks = [...taskLog.matchAll(/^\s*[-*]\s+\[([ xX])\]/gm)];
  if (!tasks.length) return null;
  const completed = tasks.filter((task) => task[1].toLowerCase() === 'x').length;
  return (
    <p className="tiny muted top-space">
      {completed} / {tasks.length} tasks checked off
    </p>
  );
}

export function ProjectsPage() {
  const projects = useResource<Project[]>('/projects');
  const [creating, setCreating] = useState(false);
  return (
    <>
      <PageTitle
        eyebrow="Persistent work"
        title="Projects"
        description="A lasting workspace for each goal, ready whenever your usage is available."
        actions={
          <button className="primary" onClick={() => setCreating(true)}>
            + New project
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
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const action = useAction();
  const { go } = useNavigation();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const project = await mutate<Project>('/projects', 'POST', { name, description });
      go(`/projects/${project.id}`);
    });
  };
  return (
    <section className="panel create-panel">
      <div className="row between">
        <h2>Create a project</h2>
        <button className="quiet" onClick={close} aria-label="Close new project form">
          ✕
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
        <p className="small muted">
          Each project gets its own persistent environment and workspace. Agents run autonomously
          inside an isolated microVM.
        </p>
        <ActionNotice {...action} />
        <div className="form-actions">
          <button type="button" onClick={close}>
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
  'Description',
  'Tasks',
  'Feedback',
  'Last run',
  'History',
  'Environment',
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
        description="Project state persists between every execution."
        actions={
          <>
            <Badge status={project.status} />
            <Link className="button primary" href={`/prepare?project=${id}`}>
              ▶ Prepare run
            </Link>
          </>
        }
      />
      <nav className="tabs" aria-label="Project sections">
        {tabs.map((name) => (
          <button
            key={name}
            className={name === tab ? 'active' : ''}
            onClick={() => setTab(name)}
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
              Permanently remove this project, its disks, secrets, and snapshots. Active projects
              cannot be deleted.
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
      {tab === 'Tasks' && <ProjectEditor key={`${id}-tasks`} project={project} field="task_log" />}
      {tab === 'Feedback' && (
        <ProjectEditor key={`${id}-feedback`} project={project} field="next_run_feedback" />
      )}
      {tab === 'Last run' && (
        <section className="panel">
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
      {tab === 'Environment' && <EnvironmentPanel id={id} />}
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
  field: 'description' | 'task_log' | 'next_run_feedback';
}) {
  const [text, setText] = useState(project[field]);
  const [name, setName] = useState(project.name);
  const [model, setModel] = useState(project.default_model);
  const [effort, setEffort] = useState(project.default_reasoning_effort);
  const action = useAction();
  const labels = {
    description: 'Project description',
    task_log: 'Shared task log',
    next_run_feedback: 'Feedback for the next run',
  };
  const descriptions = {
    description: 'A clear goal and success criteria guide autonomous work.',
    task_log: 'You and the agent maintain this durable record of completed work and next steps.',
    next_run_feedback: 'These instructions are included when the next execution starts.',
  };
  return (
    <section className="panel">
      <h2>{labels[field]}</h2>
      <p className="muted">{descriptions[field]}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(
            () =>
              mutate(`/projects/${project.id}`, 'PATCH', {
                [field]: text,
                ...(field === 'description'
                  ? { name, default_model: model, default_reasoning_effort: effort || 'medium' }
                  : {}),
              }),
            'Changes saved.',
          );
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
            className={field === 'task_log' ? 'mono' : ''}
            rows={15}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={field === 'task_log' ? '- [ ] First useful step…' : undefined}
          />
        </label>
        {field === 'description' && (
          <div className="form-grid">
            <label>
              Default model
              <input
                value={model ?? ''}
                onChange={(e) => setModel(e.target.value)}
                placeholder="Provider default"
              />
            </label>
            <label>
              Default reasoning effort
              <select value={effort ?? ''} onChange={(e) => setEffort(e.target.value)}>
                <option value="">Default (medium)</option>
                {['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].map(
                  (value) => (
                    <option key={value}>{value}</option>
                  ),
                )}
              </select>
            </label>
          </div>
        )}
        <ActionNotice {...action} />
        <div className="form-actions">
          <button className="primary" disabled={action.busy}>
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

function EnvironmentPanel({ id }: { id: string }) {
  const storage = useResource<StorageInfo>(`/projects/${id}/storage`);
  const snapshots = useResource<Snapshot[]>(`/projects/${id}/snapshots`);
  const [name, setName] = useState('');
  const [scope, setScope] = useState('workspace');
  const [size, setSize] = useState(40);
  const action = useAction();
  return (
    <>
      <section className="panel">
        <h2>Persistent storage</h2>
        <p className="muted">
          Each run boots a fresh VM with these disks attached. The environment keeps installed tools
          and caches; the workspace keeps your source code and data.
        </p>
        <ErrorNotice error={storage.error} />
        {storage.data && (
          <div className="storage-grid">
            {(['environment', 'workspace'] as const).map((domain) => (
              <article className="storage-card" key={domain}>
                <h3>{domain}</h3>
                <strong>{bytes(storage.data![domain].used_bytes)}</strong>
                <p className="muted small">
                  allocated on host · {bytes(storage.data![domain].size_bytes)} virtual capacity
                </p>
              </article>
            ))}
          </div>
        )}
        <div className="form-grid">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action.run(
                () => mutate(`/projects/${id}/storage/resize`, 'POST', { scope, size_gib: size }),
                'Storage resized.',
              );
            }}
          >
            <h3>Grow a disk</h3>
            <div className="row">
              <label>
                Disk
                <select value={scope} onChange={(e) => setScope(e.target.value)}>
                  <option value="workspace">Workspace</option>
                  <option value="environment">Environment</option>
                </select>
              </label>
              <label>
                New size, GiB
                <input
                  type="number"
                  required
                  min={1}
                  max={16384}
                  value={size}
                  onChange={(e) => setSize(Number(e.target.value))}
                />
              </label>
            </div>
            <button disabled={action.busy}>Resize disk</button>
          </form>
          <div>
            <h3>Rebuild the environment</h3>
            <p className="muted small">
              Start with fresh tools and caches while retaining all workspace files. Stop the
              project first.
            </p>
            <button
              className="danger"
              disabled={action.busy}
              onClick={() => {
                if (
                  window.confirm(
                    'Reset this environment? Installed tools, home files, and caches will be removed. Workspace files remain.',
                  )
                )
                  void action.run(
                    () => mutate(`/projects/${id}/environment/reset`, 'POST', {}),
                    'Environment reset.',
                  );
              }}
            >
              Reset environment…
            </button>
          </div>
        </div>
        <ActionNotice {...action} />
      </section>
      <section className="panel">
        <h2>Storage snapshots</h2>
        <p className="muted">
          Snapshots contain persistent disks, including anything the agent wrote to them. Runtime
          credential files are kept separately.
        </p>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              await mutate(`/projects/${id}/snapshots`, 'POST', { name });
              setName('');
            }, 'Snapshot created.');
          }}
        >
          <label className="grow">
            <span className="sr-only">Snapshot name</span>
            <input
              value={name}
              maxLength={200}
              onChange={(e) => setName(e.target.value)}
              placeholder="Snapshot name (optional)"
            />
          </label>
          <button disabled={action.busy}>Create snapshot</button>
        </form>
        <ErrorNotice error={snapshots.error} />
        {snapshots.data?.length ? (
          <div className="snapshot-list">
            {snapshots.data.map((snapshot) => (
              <SnapshotRow key={snapshot.id} projectId={id} snapshot={snapshot} />
            ))}
          </div>
        ) : (
          <p className="muted small">
            No snapshots yet. Runs also create a preparation snapshot when possible.
          </p>
        )}
      </section>
    </>
  );
}

function SnapshotRow({ projectId, snapshot }: { projectId: string; snapshot: Snapshot }) {
  const [scope, setScope] = useState('workspace');
  const action = useAction();
  const path = `/projects/${projectId}/snapshots/${snapshot.id}`;
  return (
    <div className="snapshot-row">
      <div className="row between wrap">
        <div>
          <strong>{snapshot.name || shortId(snapshot.id)}</strong>
          <p className="tiny muted">{date(snapshot.created_at)}</p>
        </div>
        <div className="row wrap">
          <select
            aria-label="What to restore"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
          >
            <option value="workspace">Workspace only</option>
            <option value="environment">Environment only</option>
            <option value="all">Both disks</option>
          </select>
          <button
            disabled={action.busy}
            onClick={() => {
              if (
                window.confirm(
                  `Replace ${scope === 'all' ? 'both disks' : `the ${scope}`} with this snapshot? Changes since the snapshot will be lost.`,
                )
              )
                void action.run(
                  () => mutate(`${path}/restore`, 'POST', { scope }),
                  'Snapshot restored.',
                );
            }}
          >
            Restore…
          </button>
          <button
            className="quiet danger"
            disabled={action.busy}
            onClick={() => {
              if (window.confirm('Delete this snapshot?'))
                void action.run(() => mutate(path, 'DELETE'));
            }}
          >
            Delete
          </button>
        </div>
      </div>
      <ActionNotice {...action} />
    </div>
  );
}

function SecretsPanel({ id }: { id: string }) {
  const secrets = useResource<Secret[]>(`/projects/${id}/secrets`);
  const [editing, setEditing] = useState<Secret | 'new' | null>(null);
  const [dotenv, setDotenv] = useState('');
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const action = useAction();
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
          <button onClick={() => setEditing('new')}>+ Add secret</button>
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
                  <button className="quiet" onClick={() => setEditing(secret)}>
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
      <ActionNotice {...action} />
      <div className="form-actions">
        <button type="button" onClick={close}>
          Cancel
        </button>
        <button className="primary" disabled={action.busy}>
          Save secret
        </button>
      </div>
    </form>
  );
}
