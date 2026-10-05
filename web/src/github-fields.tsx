import { useId } from 'react';
import { useResource } from './api';
import type { GitHubStatus, ProjectGitHub, Repository } from './types';
import { ErrorNotice, Link, Loading } from './ui';

export const emptyGitHub: ProjectGitHub = {
  repository_id: 0,
  repository_name: '',
  access_mode: 'pull_requests',
  allow_workflows: false,
};

const modeNames = {
  read_only: 'Read only',
  pull_requests: 'Pull requests',
  direct_write: 'Direct write',
};

export function GitHubFields({
  value,
  onChange,
  projectId,
}: {
  value: ProjectGitHub;
  onChange: (value: ProjectGitHub) => void;
  projectId?: string;
}) {
  const status = useResource<GitHubStatus>('/integrations/github');
  const repositories = useResource<Repository[]>(
    `/integrations/github/repositories${projectId ? `?exclude_project=${encodeURIComponent(projectId)}` : ''}`,
  );
  const listId = useId();
  const selected = repositories.data?.find((repo) => repo.id === value.repository_id);
  const writableUse = selected?.used_by?.find((use) => use.access_mode !== 'read_only');
  const blocked =
    writableUse?.access_mode === 'pull_requests'
      ? 'direct_write'
      : writableUse?.access_mode === 'direct_write'
        ? 'pull_requests'
        : undefined;
  return (
    <>
      <ErrorNotice error={status.error || repositories.error} />
      {!status.data ? (
        <Loading />
      ) : !status.data.configured ? (
        <div className="callout">
          Connect GitHub in <Link href="/settings">Settings</Link> before choosing a repository.
        </div>
      ) : (
        <>
          <label>
            Repository
            <input
              role="combobox"
              aria-autocomplete="list"
              list={listId}
              value={value.repository_name}
              placeholder="Search repositories…"
              required
              onChange={(event) => {
                const repo = repositories.data?.find(
                  (item) => item.full_name === event.target.value,
                );
                onChange({
                  ...value,
                  repository_name: event.target.value,
                  repository_id: repo?.id || 0,
                });
              }}
            />
            <datalist id={listId}>
              {repositories.data?.map((repo) => (
                <option key={repo.id} value={repo.full_name} />
              ))}
            </datalist>
          </label>
          {!repositories.loading && !repositories.data?.length && (
            <p className="small muted">
              No repositories available.{' '}
              <a href={status.data.installation_url}>Choose repositories on GitHub ↗</a>
            </p>
          )}
          {selected?.used_by?.map((use) => (
            <p className="small muted" key={use.project_id}>
              Used by {use.project_name} · {modeNames[use.access_mode]}
            </p>
          ))}
          <fieldset>
            <legend>Access</legend>
            {Object.entries(modeNames).map(([mode, label]) => (
              <label className="check-row" key={mode}>
                <input
                  type="radio"
                  name={listId + '-access'}
                  value={mode}
                  checked={value.access_mode === mode}
                  disabled={mode === blocked}
                  onChange={() =>
                    onChange({
                      ...value,
                      access_mode: mode as ProjectGitHub['access_mode'],
                      allow_workflows: mode === 'read_only' ? false : value.allow_workflows,
                    })
                  }
                />
                <span>{label}</span>
              </label>
            ))}
          </fieldset>
          {blocked && (
            <p className="small muted">
              Pull requests and Direct write cannot coexist for the same repository because PR-only
              protection applies to the repository as a whole.
            </p>
          )}
          {value.access_mode === blocked && (
            <ErrorNotice error="Choose a compatible access mode before saving." />
          )}
          <label className="check-row">
            <input
              type="checkbox"
              checked={value.allow_workflows}
              disabled={value.access_mode === 'read_only'}
              onChange={(event) => onChange({ ...value, allow_workflows: event.target.checked })}
            />
            <span>Allow workflow file changes</span>
          </label>
          <ErrorNotice error={selected?.policy_error || value.policy_error || undefined} />
          {selected?.policy_error && (
            <p className="small muted">
              Pull requests access is unavailable until GitHub allows the required default-branch
              rule. Read only remains available. Direct write must be explicitly selected.
            </p>
          )}
        </>
      )}
    </>
  );
}

export function githubPayload(value: ProjectGitHub) {
  if (!value.repository_id) throw new Error('Choose an available GitHub repository.');
  return {
    repository_id: value.repository_id,
    access_mode: value.access_mode,
    allow_workflows: value.allow_workflows,
  };
}
