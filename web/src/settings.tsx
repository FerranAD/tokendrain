import { ThemeControl } from './theme';
import { Icon } from './icons';
import { GitHubFields, emptyGitHub, githubPayload } from './github-fields';
import { UnsavedNotice, useUnsavedChanges } from './drafts';
import { useState } from 'react';
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
        <a href="#system">Execution defaults</a>
        <a href="#appearance">Appearance</a>
      </nav>
      <div className="settings-content">
        <OpenAISettings />
        <GitHubSettings />
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
                : 'connected'
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
          <p className="small">Imported Codex credentials · renewed automatically through Codex</p>
        </div>
      )}
      <div className="row wrap">
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
              setAuthJson('');
              draft.markSaved('');
              setImporting(false);
            }, 'Codex credentials imported.');
          }}
        >
          <h3>Import Codex auth.json</h3>
          <p className="small muted">
            Import an existing Codex auth.json. The host interprets and stores it centrally; it is
            never copied onto project disks.
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
  const [installationReturned, setInstallationReturned] = useState(
    new URLSearchParams(window.location.search).get('github') === 'installed',
  );
  const [appId, setAppId] = useState('');
  const [slug, setSlug] = useState('');
  const [key, setKey] = useState('');
  const [editing, setEditing] = useState(false);
  const draft = useUnsavedChanges({ appId, slug, key });
  const action = useAction();
  const origin = window.location.origin;
  return (
    <section className="panel" id="github">
      {installationReturned && (
        <div className="callout">
          <strong>Continue your GitHub setup</strong>
          <p>
            GitHub returned you to tokendrain. Refresh installations to discover repositories
            authorized for your configured App.
          </p>
          <button
            className="top-space"
            disabled={action.busy || !status.data?.configured}
            onClick={() => {
              void action.run(async () => {
                await mutate('/integrations/github/sync', 'POST', {});
                const url = new URL(window.location.href);
                url.searchParams.delete('github');
                window.history.replaceState(null, '', url.pathname + url.search);
                setInstallationReturned(false);
              }, 'Installations refreshed. Select a repository from your project’s GitHub tab.');
            }}
          >
            Discover installed repositories
          </button>
        </div>
      )}
      <div className="row between">
        <div>
          <h2>GitHub App</h2>
        </div>
        <Badge status={status.data?.configured ? 'connected' : 'unconfigured'} />
      </div>
      <ErrorNotice error={status.error} />
      <p className="muted">
        Use a GitHub App you own. Each project receives a short-lived installation token limited to
        its selected repository and permissions.
      </p>
      <details className="setup-guide" open={!status.data?.configured}>
        <summary>Set up a self-hosted GitHub App</summary>
        <ol>
          <li>
            <a
              href="https://github.com/settings/apps/new"
              target="_blank"
              rel="noopener noreferrer"
            >
              Create a GitHub App ↗
            </a>{' '}
            in your personal or organization developer settings. Choose a unique name and use{' '}
            <code>{origin}</code> for its homepage.
          </li>
          <li>
            Set the <strong>Setup URL</strong> to{' '}
            <code>{origin}/api/v1/integrations/github/setup</code>. User authorization is
            unnecessary for the installation-token flow. Leave OAuth callback configuration unused,
            and disable webhooks.
          </li>
          <li>
            Choose repository permissions that set the maximum access you intend to grant. Typical
            settings are <strong>Contents: read and write</strong>,{' '}
            <strong>Pull requests: read and write</strong>, <strong>Issues: read and write</strong>,
            and optionally <strong>Actions: read</strong>. Metadata read access is implicit. Each
            project can select a narrower subset.
          </li>
          <li>
            Create the app, record its <strong>App ID</strong> and URL slug, and generate a{' '}
            <strong>private key</strong>. Enter them below. The key stays encrypted on the host and
            never enters a VM.
          </li>
          <li>
            Install the app onto your account or organization and select repositories. Refresh
            installations here, then connect a repository from a project’s GitHub tab.
          </li>
        </ol>
      </details>
      {status.data?.configured && (
        <div className="inset">
          <div className="row between wrap">
            <div>
              <strong>{status.data.app_slug || `App ${status.data.app_id}`}</strong>
              <p className="small muted">App ID {status.data.app_id}</p>
            </div>
            <div className="row wrap">
              {status.data.installation_url && (
                <a
                  className="button"
                  href={status.data.installation_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Install app ↗
                </a>
              )}
              <button
                disabled={action.busy}
                onClick={() => {
                  void action.run(
                    () => mutate('/integrations/github/sync', 'POST', {}),
                    'Installations refreshed.',
                  );
                }}
              >
                Refresh installations
              </button>
              <button
                className="quiet"
                onClick={() => {
                  if (editing && !draft.discard()) return;
                  draft.markSaved({
                    appId: String(status.data?.app_id ?? ''),
                    slug: status.data?.app_slug ?? '',
                    key: '',
                  });
                  setKey('');
                  setAppId(String(status.data?.app_id ?? ''));
                  setSlug(status.data?.app_slug ?? '');
                  setEditing((value) => !value);
                }}
              >
                Reconfigure
              </button>
            </div>
          </div>
          {status.data.installations.length ? (
            <ul className="plain-list">
              {status.data.installations.map((installation) => (
                <li key={installation.id}>
                  {installation.account}{' '}
                  <span className="muted tiny">Installation {installation.id}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="small muted">
              No installations recorded yet. Install the app, then refresh.
            </p>
          )}
        </div>
      )}
      {(!status.data?.configured || editing) && (
        <form
          className="top-space"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              await mutate('/integrations/github', 'PUT', {
                app_id: appId,
                app_slug: slug,
                private_key: key,
              });
              draft.markSaved({ appId, slug, key: '' });
              setKey('');
              setEditing(false);
            }, 'GitHub App configured. Install it and refresh installations.');
          }}
        >
          <div className="form-grid">
            <label>
              GitHub App ID
              <input
                value={appId}
                inputMode="numeric"
                pattern="[0-9]+"
                onChange={(e) => setAppId(e.target.value)}
                required
              />
            </label>
            <label>
              App slug
              <input
                value={slug}
                pattern="[A-Za-z0-9-]+"
                onChange={(e) => setSlug(e.target.value)}
                placeholder="my-tokendrain"
                required
              />
            </label>
          </div>
          <label>
            Private key file (.pem)
            <input
              type="file"
              accept=".pem,text/plain"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void file.text().then(setKey);
                e.target.value = '';
              }}
            />
          </label>
          <label>
            Private key
            <textarea
              required
              rows={4}
              autoComplete="off"
              spellCheck={false}
              className="mono"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="-----BEGIN RSA PRIVATE KEY-----"
            />
          </label>
          <UnsavedNotice dirty={draft.dirty} />
          <button className="primary" disabled={action.busy}>
            Save GitHub App
          </button>
        </form>
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
          Default disk size, GiB
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
        Leave host memory available beyond concurrency × VM memory. Disk defaults apply to newly
        created project storage.
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
      <p className="muted">Select a repository and the capabilities this project needs.</p>
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
          await mutate(`/projects/${id}/github`, 'PUT', githubPayload(value));
          draft.markSaved();
          setConnected(true);
        }, 'Repository integration saved.');
      }}
    >
      <GitHubFields value={value} onChange={setValue} />
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
