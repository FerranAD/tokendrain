import { useId } from 'react';
import { useResource } from './api';
import type { GitHubStatus, ProjectGitHub, Repository } from './types';
import { ErrorNotice, Link, Loading } from './ui';

export const emptyGitHub: ProjectGitHub = {
  installation_id: '',
  repository_id: 0,
  repository_name: '',
  permissions: { contents: 'read' },
};
const permissionNames: Record<string, string> = {
  contents: 'Repository contents',
  pull_requests: 'Pull requests',
  issues: 'Issues',
  actions: 'GitHub Actions',
  workflows: 'Workflow files',
};

export function GitHubFields({
  value,
  onChange,
}: {
  value: ProjectGitHub;
  onChange: (value: ProjectGitHub) => void;
}) {
  const status = useResource<GitHubStatus>('/integrations/github');
  const repositories = useResource<Repository[]>(
    value.installation_id
      ? `/integrations/github/installations/${value.installation_id}/repositories`
      : null,
  );
  const listId = useId();
  const installation = status.data?.installations.find(
    (i) => String(i.id) === String(value.installation_id),
  );
  return (
    <>
      <ErrorNotice error={status.error || repositories.error} />
      {!status.data ? (
        <Loading />
      ) : !status.data.configured ? (
        <div className="callout">
          Configure your GitHub App in <Link href="/settings">Settings</Link> before attaching a
          repository.
        </div>
      ) : (
        <>
          <div className="form-grid">
            <label>
              Installation
              <select
                aria-label="Installation"
                required
                value={value.installation_id}
                onChange={(e) => onChange({ ...emptyGitHub, installation_id: e.target.value })}
              >
                <option value="">Select an account or organization</option>
                {status.data.installations.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.account}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Repository
              <input
                role="combobox"
                aria-autocomplete="list"
                list={listId}
                disabled={!value.installation_id || repositories.loading}
                value={value.repository_name}
                placeholder="Search repositories…"
                required
                onChange={(e) =>
                  onChange({
                    ...value,
                    repository_name: e.target.value,
                    repository_id:
                      repositories.data?.find((r) => r.full_name === e.target.value)?.id || 0,
                  })
                }
              />
              <datalist id={listId}>
                {repositories.data?.map((r) => (
                  <option key={r.id} value={r.full_name} />
                ))}
              </datalist>
            </label>
          </div>
          {!status.data.installations.length && (
            <p className="small muted">
              No installations available. Install your app and refresh installations in{' '}
              <Link href="/settings">Settings</Link>.
            </p>
          )}
          <h3>Allowed capabilities</h3>
          <p className="small muted">
            Choose the access this project needs, within the installation’s permissions.
          </p>
          <div className="permissions">
            {Object.entries(permissionNames).map(([key, label]) => {
              const maximum = installation?.permissions?.[key];
              const known = !!installation?.permissions;
              const read =
                key !== 'workflows' &&
                (!known || ['read', 'write', 'admin'].includes(maximum || ''));
              const write = !known || ['write', 'admin'].includes(maximum || '');
              return (
                <label key={key}>
                  {label}
                  <select
                    aria-label={label}
                    value={value.permissions[key] ?? ''}
                    onChange={(e) =>
                      onChange({
                        ...value,
                        permissions: { ...value.permissions, [key]: e.target.value },
                      })
                    }
                  >
                    <option value="">No access</option>
                    {read && <option value="read">Read</option>}
                    {write && (
                      <option value="write">
                        {key === 'workflows' ? 'Write' : 'Read & write'}
                      </option>
                    )}
                  </select>
                </label>
              );
            })}
          </div>
          {value.permissions.workflows === 'write' && value.permissions.contents !== 'write' && (
            <div className="notice warning" role="alert">
              Publishing workflow file changes also needs Repository contents write access.
            </div>
          )}
          {value.permissions.pull_requests === 'write' &&
            value.permissions.contents !== 'write' && (
              <div className="notice warning" role="alert">
                This project can create pull requests, but it has nowhere to publish the source
                branch. Give the target repository Contents write access.
              </div>
            )}
        </>
      )}
    </>
  );
}
export function githubPayload(value: ProjectGitHub) {
  if (!value.installation_id || !value.repository_id)
    throw new Error('Choose an available GitHub repository.');
  return {
    ...value,
    permissions: Object.fromEntries(
      Object.entries(value.permissions).filter(([, permission]) => permission),
    ),
  };
}
