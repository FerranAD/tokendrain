import { ThemeControl } from './theme';
import { NotificationSettings } from './notifications';
import { Icon } from './icons';
import { GitHubFields, emptyGitHub, githubPayload } from './github-fields';
import { UnsavedNotice, useUnsavedChanges } from './drafts';
import { useEffect, useState } from 'react';
import { mutate, useAction, useResource } from './api';
import type { GitHubStatus, OpenAIStatus, ProjectGitHub, SystemInfo } from './types';
import { ActionNotice, Badge, ErrorNotice, Loading, PageTitle } from './ui';

export function SettingsPage() {
  return (
    <>
      <PageTitle title="Settings" />
      <nav className="settings-nav" aria-label="Settings sections">
        <a href="#openai">Codex account</a>
        <a href="#github">GitHub</a>
        <a href="#notifications">Notifications</a>
        <a href="#system">Execution defaults</a>
        <a href="#appearance">Appearance</a>
      </nav>
      <div className="settings-content">
        <OpenAISettings />
        <GitHubSettings />
        <NotificationSettings />
        <SystemSettings />
        <section className="panel appearance-panel" id="appearance">
          <h2>Appearance</h2>
          <p className="small muted">Follow your system or choose a theme.</p>
          <ThemeControl />
        </section>
      </div>
    </>
  );
}

function OpenAISettings() {
  const status = useResource<OpenAIStatus>('/auth/openai');
  const [authJson, setAuthJson] = useState('');
  const [importing, setImporting] = useState(false);
  const action = useAction();
  const draft = useUnsavedChanges(authJson);
  return (
    <section className="panel" id="openai">
      <div className="row between">
        <div>
          <h2>Codex account</h2>
        </div>
        <Badge
          status={
            status.data?.connected
              ? status.data.valid === false
                ? 'invalid'
                : status.data.connection_ok === false
                  ? 'needs_attention'
                  : status.data.connection_ok === true
                    ? 'connected'
                    : 'imported'
              : 'disconnected'
          }
        />
      </div>
      <ErrorNotice error={status.error || status.data?.credential_error} />
      <p className="muted">
        Import auth.json from your Codex installation. Credentials refresh automatically.
      </p>
      {status.data?.connected && (
        <div className="callout">
          <strong>{status.data.account_label || 'Account connected'}</strong>
          <p className="small">
            {status.data.connection_ok === true
              ? 'Connection checked · usage available'
              : 'Credentials stored · connection not verified'}
          </p>
          {status.data.connection_checked_at && (
            <p className="tiny muted">
              Checked {new Date(status.data.connection_checked_at * 1000).toLocaleString()}
            </p>
          )}
        </div>
      )}
      {status.data?.usage_error && (
        <div className="callout warning">
          <strong>Connection needs attention</strong>
          <p>{status.data.usage_error}</p>
        </div>
      )}
      {status.data?.reauth_required && (
        <div className="callout">
          <strong>Sign in again, then replace the file</strong>
          <p>On the computer where you use Codex:</p>
          <pre>codex logout{'\n'}codex login</pre>
          <p className="small">
            Import the new auth.json from your Codex home (usually ~/.codex/auth.json). Your
            projects and workspace stay intact.
          </p>
        </div>
      )}
      <div className="row wrap">
        {status.data?.connected && (
          <button
            disabled={action.busy}
            onClick={() =>
              void action.run(async () => {
                await mutate('/auth/openai/check', 'POST');
                status.reload();
              }, 'Connection check finished.')
            }
          >
            {action.busy ? 'Checking…' : 'Check connection'}
          </button>
        )}
        <button
          onClick={() => {
            if (draft.discard()) {
              setAuthJson('');
              draft.markSaved('');
              setImporting((value) => !value);
            }
          }}
        >
          {status.data?.connected ? 'Replace Codex auth.json' : 'Import Codex auth.json'}
        </button>
        {status.data?.connected && (
          <button
            className="quiet danger"
            disabled={action.busy}
            onClick={() => {
              if (window.confirm('Disconnect OpenAI? New work cannot start until you reconnect.'))
                void action.run(() => mutate('/auth/openai', 'DELETE'), 'OpenAI disconnected.');
            }}
          >
            Disconnect
          </button>
        )}
      </div>
      {importing && (
        <form
          className="inset top-space"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              let parsed: unknown;
              try {
                parsed = JSON.parse(authJson);
              } catch {
                throw new Error('The file is not valid JSON. Choose your Codex auth.json file.');
              }
              if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
                throw new Error('Expected a Codex authentication JSON object.');
              await mutate('/auth/openai/import', 'POST', { auth_json: parsed });
              status.reload();
              setAuthJson('');
              draft.markSaved('');
              setImporting(false);
            }, 'Credentials imported. See connection status above.');
          }}
        >
          <h3>Import Codex auth.json</h3>
          <p className="small muted">
            Import an existing Codex auth.json. The host interprets and stores it centrally; it is
            never stored in project machines.
          </p>
          <label>
            Choose auth.json
            <input
              type="file"
              accept=".json,application/json"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void file.text().then(setAuthJson);
                e.target.value = '';
              }}
            />
          </label>
          <label>
            Or paste JSON
            <textarea
              rows={5}
              required
              autoComplete="off"
              spellCheck={false}
              className="mono"
              value={authJson}
              onChange={(e) => setAuthJson(e.target.value)}
              placeholder='{ "tokens": … }'
            />
          </label>
          <UnsavedNotice dirty={draft.dirty} />
          <button disabled={action.busy || !authJson}>Import credentials</button>
        </form>
      )}
      <ActionNotice {...action} />
    </section>
  );
}

function GitHubSettings() {
  const status = useResource<GitHubStatus>('/integrations/github');
  const action = useAction();
  const [syncStarted, setSyncStarted] = useState(false);
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('github') === 'installed' && !syncStarted) {
      setSyncStarted(true);
      void action.run(async () => {
        await mutate('/integrations/github/sync', 'POST', {});
        const url = new URL(window.location.href);
        url.searchParams.delete('github');
        window.history.replaceState(null, '', url);
        status.reload();
      }, 'Repositories updated. Choose a repository in your project.');
    }
  }, [syncStarted]);
  const connect = () =>
    action.run(async () => {
      const setup = await mutate<{ action: string; manifest: unknown; state: string }>(
        '/integrations/github/connect',
        'POST',
        {},
      );
      const form = document.createElement('form');
      form.method = 'POST';
      form.action = setup.action;
      for (const [name, value] of Object.entries({
        manifest: JSON.stringify(setup.manifest),
        state: setup.state,
      })) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        form.appendChild(input);
      }
      document.body.appendChild(form);
      form.submit();
    });
  return (
    <section className="panel" id="github">
      <div className="section-heading">
        <h2>GitHub</h2>
        <span>{status.data?.configured ? 'Connected ✓' : 'Not connected'}</span>
      </div>
      <ErrorNotice error={status.error} />
      {status.data?.configured ? (
        <>
          <p>{status.data.name}</p>
          <p className="muted">{status.data.repository_count} repositories available</p>
          <a className="button" href={status.data.installation_url}>
            Manage repositories on GitHub ↗
          </a>
          <details className="top-space">
            <summary>Advanced</summary>
            <div className="actions top-space">
              <button
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate('/integrations/github/sync', 'POST', {});
                    status.reload();
                  }, 'Repositories refreshed.')
                }
              >
                Refresh repositories
              </button>
              <button disabled={action.busy} onClick={() => void connect()}>
                Reconnect GitHub
              </button>
              <button
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate('/integrations/github', 'DELETE');
                    status.reload();
                  }, 'GitHub disconnected.')
                }
              >
                Disconnect GitHub
              </button>
            </div>
          </details>
          {status.data.policy_errors?.map((error) => (
            <ErrorNotice key={error} error={error} />
          ))}
        </>
      ) : (
        <>
          <p className="small muted">
            Tokendrain needs repository administration access to enforce PR-only mode. Autonomous
            VMs never receive that permission.
          </p>
          <button className="primary" disabled={action.busy} onClick={() => void connect()}>
            Connect GitHub
          </button>
        </>
      )}
      <ActionNotice {...action} />
    </section>
  );
}

function SystemSettings() {
  const status = useResource<SystemInfo>('/system');
  return (
    <section className="panel" id="system">
      <h2>Execution defaults</h2>
      <ErrorNotice error={status.error} />
      {status.data ? (
        <>
          <dl className="facts horizontal">
            <div>
              <dt>Version</dt>
              <dd>{status.data.version}</dd>
            </div>
            <div>
              <dt>VM backend</dt>
              <dd>{status.data.backend}</dd>
            </div>
            <div>
              <dt>Active executions</dt>
              <dd>{status.data.active_executions ?? 0}</dd>
            </div>
            <div>
              <dt>Uptime</dt>
              <dd>
                {status.data.uptime_seconds != null
                  ? `${Math.floor(status.data.uptime_seconds / 60)} minutes`
                  : '—'}
              </dd>
            </div>
          </dl>
          <SystemForm system={status.data} />
          <details className="system-diagnostics">
            <summary>System status & diagnostics</summary>{' '}
            <h3 className="top-space">System checks</h3>
            <div className="checks">
              {status.data.checks.map((check) => (
                <div className="check" key={check.name}>
                  <span
                    className={`check-mark ${check.ok ? 'ok' : 'bad'}`}
                    aria-label={check.ok ? 'Pass' : 'Fail'}
                  >
                    <Icon name={check.ok ? 'check' : 'alert'} />
                  </span>
                  <div>
                    <strong>{check.name}</strong>
                    <p className="small muted">
                      {check.scope && `${check.scope}: `}
                      {check.message}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </details>{' '}
          <button className="quiet" onClick={status.reload}>
            Refresh system status
          </button>
        </>
      ) : (
        <Loading />
      )}
    </section>
  );
}

function SystemForm({ system }: { system: SystemInfo }) {
  const [concurrency, setConcurrency] = useState(system.concurrency);
  const [vcpus, setVcpus] = useState(system.vm_defaults.vcpus);
  const [memory, setMemory] = useState(system.vm_defaults.memory_mib);
  const [disk, setDisk] = useState(system.vm_defaults.disk_gib);
  const action = useAction();
  const draft = useUnsavedChanges({ concurrency, vcpus, memory, disk });
  return (
    <form
      className="inset"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          await mutate('/system', 'PATCH', {
            concurrency,
            vm_defaults: { vcpus, memory_mib: memory, disk_gib: disk },
          });
          draft.markSaved();
        }, 'Execution defaults saved.');
      }}
    >
      <div className="form-grid four">
        <label>
          Concurrency
          <input
            type="number"
            min={1}
            max={64}
            required
            value={concurrency}
            onChange={(e) => setConcurrency(Number(e.target.value))}
          />
        </label>
        <label>
          vCPUs per VM
          <input
            type="number"
            min={1}
            max={32}
            required
            value={vcpus}
            onChange={(e) => setVcpus(Number(e.target.value))}
          />
        </label>
        <label>
          Memory per VM, MiB
          <input
            type="number"
            min={512}
            step={128}
            max={262144}
            required
            value={memory}
            onChange={(e) => setMemory(Number(e.target.value))}
          />
        </label>
        <label>
          Default VM storage, GiB
          <input
            type="number"
            min={1}
            max={16384}
            required
            value={disk}
            onChange={(e) => setDisk(Number(e.target.value))}
          />
        </label>
      </div>
      <p className="small muted">
        Leave host memory available beyond concurrency × VM memory. VM storage defaults apply to
        newly created project storage.
      </p>
      <UnsavedNotice dirty={draft.dirty} />
      <ActionNotice {...action} notice={draft.dirty ? undefined : action.notice} />
      <button disabled={action.busy || !draft.dirty}>Save defaults</button>
    </form>
  );
}

export function ProjectGitHubPanel({ id }: { id: string }) {
  const resource = useResource<ProjectGitHub | null>(`/projects/${id}/github`);
  return (
    <section className="panel">
      <h2>GitHub repository</h2>
      <p className="muted">Choose a repository and how the AI may write.</p>
      <ErrorNotice error={resource.error} />
      {resource.data === undefined ? (
        <Loading />
      ) : (
        <GitHubEditor key={id} id={id} initial={resource.data} />
      )}
    </section>
  );
}

function GitHubEditor({ id, initial }: { id: string; initial: ProjectGitHub | null }) {
  const [value, setValue] = useState(initial || emptyGitHub);
  const [connected, setConnected] = useState(!!initial);
  const draft = useUnsavedChanges(value);
  const action = useAction();
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const saved = await mutate<ProjectGitHub>(
            `/projects/${id}/github`,
            'PUT',
            githubPayload(value),
          );
          setValue(saved);
          draft.markSaved(saved);
          setConnected(true);
        }, 'Repository integration saved.');
      }}
    >
      <GitHubFields value={value} onChange={setValue} projectId={id} />
      <UnsavedNotice dirty={draft.dirty} />
      <ActionNotice {...action} notice={draft.dirty ? undefined : action.notice} />
      <div className="form-actions">
        {connected && (
          <button
            type="button"
            className="danger"
            disabled={action.busy}
            onClick={() => {
              if (
                window.confirm(
                  'Disconnect this repository from future runs? Workspace files will remain.',
                )
              )
                void action.run(async () => {
                  await mutate(`/projects/${id}/github`, 'DELETE');
                  setValue(emptyGitHub);
                  setConnected(false);
                  draft.markSaved(emptyGitHub);
                }, 'Repository disconnected.');
            }}
          >
            Disconnect
          </button>
        )}
        <button className="primary" disabled={action.busy || !draft.dirty || !value.repository_id}>
          Save repository access
        </button>
      </div>
    </form>
  );
}
