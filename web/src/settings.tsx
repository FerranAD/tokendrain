import { useState } from 'react';
import { mutate, useAction, useResource } from './api';
import type { GitHubStatus, OpenAIStatus, ProjectGitHub, Repository, SystemInfo } from './types';
import { ActionNotice, Badge, ErrorNotice, Link, Loading, PageTitle } from './ui';

export function SettingsPage() {
  return (
    <>
      <PageTitle
        eyebrow="Your infrastructure"
        title="Settings"
        description="Connect accounts and configure the platform that runs your projects."
      />
      <OpenAISettings />
      <GitHubSettings />
      <SystemSettings />
    </>
  );
}

function OpenAISettings() {
  const status = useResource<OpenAIStatus>('/auth/openai');
  const [loginUrl, setLoginUrl] = useState('');
  const [authJson, setAuthJson] = useState('');
  const [importing, setImporting] = useState(false);
  const action = useAction();
  return (
    <section className="panel" id="openai">
      <div className="row between">
        <div>
          <div className="eyebrow">Agent connection</div>
          <h2>OpenAI / Codex</h2>
        </div>
        <Badge status={status.data?.connected ? 'connected' : 'disconnected'} />
      </div>
      <ErrorNotice error={status.error} />
      <p className="muted">
        Connect your ChatGPT account so Codex can use your available allowance. Long-lived
        credentials stay encrypted on this host. Project VMs receive temporary access tokens.
      </p>
      {status.data?.connected && (
        <div className="callout">
          <strong>{status.data.account_label || 'Account connected'}</strong>
          <p className="small">
            Method:{' '}
            {status.data.method === 'import'
              ? 'Imported Codex credentials'
              : 'Sign in with ChatGPT'}
          </p>
        </div>
      )}
      <div className="row wrap">
        <button
          className="primary"
          disabled={action.busy}
          onClick={() => {
            void action.run(async () => {
              const login = await mutate<{ id: string; url: string }>(
                '/auth/openai/login',
                'POST',
                {},
              );
              setLoginUrl(login.url);
            }, 'Sign-in started. Open the secure sign-in link to continue.');
          }}
        >
          {action.busy
            ? 'Starting…'
            : status.data?.connected
              ? 'Reconnect with ChatGPT'
              : 'Sign in with ChatGPT'}{' '}
          <span aria-hidden="true">↗</span>
        </button>
        <button onClick={() => setImporting((value) => !value)}>Import Codex auth.json</button>
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
      {(loginUrl || status.data?.login?.url) && (
        <div className="callout top-space">
          <a
            className="button"
            href={loginUrl || status.data?.login?.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Continue to secure sign-in ↗
          </a>
          <p className="small muted">
            Complete the provider’s flow, then return here. For a remote host, forward the
            configured callback port before opening the sign-in link. With default settings:{' '}
            <code>ssh -N -L 8742:127.0.0.1:8742 user@your-host</code>. Open tokendrain at{' '}
            <code>http://127.0.0.1:8742</code> in the browser on your computer so the loopback
            callback reaches the daemon.
          </p>
          <button className="quiet" onClick={status.reload}>
            Check connection
          </button>
          {status.data?.login?.status && (
            <span className="small muted">{status.data.login.status}</span>
          )}
        </div>
      )}
      <ErrorNotice error={status.data?.login?.error} />
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
              setImporting(false);
            }, 'Codex credentials imported.');
          }}
        >
          <h3>Advanced compatibility import</h3>
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
          <div className="eyebrow">Repository integration</div>
          <h2>Your GitHub App</h2>
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
      <div className="eyebrow">Host configuration</div>
      <h2>System & execution defaults</h2>
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
          <h3 className="top-space">System checks</h3>
          <div className="checks">
            {status.data.checks.map((check) => (
              <div className="check" key={check.name}>
                <span
                  className={`check-mark ${check.ok ? 'ok' : 'bad'}`}
                  aria-label={check.ok ? 'Pass' : 'Fail'}
                >
                  {check.ok ? '✓' : '!'}
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
  return (
    <form
      className="inset"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(
          () =>
            mutate('/system', 'PATCH', {
              concurrency,
              vm_defaults: { vcpus, memory_mib: memory, disk_gib: disk },
            }),
          'Execution defaults saved.',
        );
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
      <ActionNotice {...action} />
      <button disabled={action.busy}>Save defaults</button>
    </form>
  );
}

const permissionNames: Record<string, string> = {
  contents: 'Repository contents',
  pull_requests: 'Pull requests',
  issues: 'Issues',
  actions: 'Actions',
};

export function ProjectGitHubPanel({ id }: { id: string }) {
  const status = useResource<GitHubStatus>('/integrations/github');
  const integration = useResource<ProjectGitHub | null>(`/projects/${id}/github`);
  const [installationId, setInstallationId] = useState('');
  const [repositoryId, setRepositoryId] = useState('');
  const [permissions, setPermissions] = useState<Record<string, string>>({ contents: 'read' });
  const [initialized, setInitialized] = useState(false);
  const repositories = useResource<Repository[]>(
    installationId ? `/integrations/github/installations/${installationId}/repositories` : null,
  );
  const action = useAction();
  if (!initialized && integration.data !== undefined) {
    setInitialized(true);
    if (integration.data) {
      setInstallationId(String(integration.data.installation_id));
      setRepositoryId(String(integration.data.repository_id));
      setPermissions(integration.data.permissions);
    }
  }
  const installation = status.data?.installations.find(
    (item) => String(item.id) === installationId,
  );
  return (
    <section className="panel">
      <h2>GitHub repository</h2>
      <p className="muted">
        Select one repository and the capabilities this project needs. The selected permissions
        authorize the agent to act autonomously.
      </p>
      <ErrorNotice error={status.error || integration.error} />
      {!status.data ? (
        <Loading />
      ) : !status.data.configured ? (
        <div className="callout">
          <p>Configure your GitHub App before attaching a repository.</p>
          <Link className="button" href="/settings">
            Open GitHub settings →
          </Link>
        </div>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(() => {
              const repository = repositories.data?.find(
                (item) => String(item.id) === repositoryId,
              );
              if (!repository && !integration.data?.repository_name)
                throw new Error('Choose an available repository.');
              return mutate(`/projects/${id}/github`, 'PUT', {
                installation_id: installationId,
                repository_id: Number(repositoryId),
                repository_name: repository?.full_name ?? integration.data?.repository_name,
                permissions: Object.fromEntries(
                  Object.entries(permissions).filter(([, value]) => value),
                ),
              });
            }, 'Repository integration saved.');
          }}
        >
          <div className="form-grid">
            <label>
              Installation
              <select
                required
                value={installationId}
                onChange={(e) => {
                  setInstallationId(e.target.value);
                  setRepositoryId('');
                  setPermissions({ contents: 'read' });
                }}
              >
                <option value="">Select an account or organization</option>
                {status.data.installations.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.account}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Repository
              <select
                required
                disabled={!installationId || repositories.loading}
                value={repositoryId}
                onChange={(e) => setRepositoryId(e.target.value)}
              >
                <option value="">
                  {repositories.loading ? 'Loading repositories…' : 'Select a repository'}
                </option>
                {repositories.data?.map((repository) => (
                  <option key={repository.id} value={repository.id}>
                    {repository.full_name}
                    {repository.private ? ' (private)' : ''}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <ErrorNotice error={repositories.error} />
          {!status.data.installations.length && (
            <p className="small muted">
              No installations available. Install your app and refresh its installations in{' '}
              <Link href="/settings">Settings</Link>.
            </p>
          )}
          <h3>Allowed capabilities</h3>
          <p className="small muted">
            The app’s installation permissions are the maximum. GitHub verifies the narrower
            permissions when a token is issued.
          </p>
          <div className="permissions">
            {Object.entries(permissionNames).map(([key, label]) => {
              const maximum = installation?.permissions?.[key];
              const known = !!installation?.permissions;
              const canRead =
                !known || maximum === 'read' || maximum === 'write' || maximum === 'admin';
              const canWrite =
                key !== 'actions' && (!known || maximum === 'write' || maximum === 'admin');
              return (
                <label key={key}>
                  {label}
                  <select
                    value={permissions[key] ?? ''}
                    onChange={(e) => setPermissions((old) => ({ ...old, [key]: e.target.value }))}
                  >
                    <option value="">No access</option>
                    {canRead && <option value="read">Read</option>}
                    {canWrite && <option value="write">Read & write</option>}
                  </select>
                </label>
              );
            })}
          </div>
          <ActionNotice {...action} />
          <div className="form-actions">
            {integration.data && (
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
                      setRepositoryId('');
                    }, 'Repository disconnected.');
                }}
              >
                Disconnect
              </button>
            )}
            <button className="primary" disabled={action.busy || !repositoryId || !installationId}>
              Save repository access
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
