import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const system = {
  version: '0.1.0',
  backend: 'firecracker',
  concurrency: 2,
  vm_defaults: { vcpus: 4, memory_mib: 4096, disk_gib: 40 },
  active_executions: 0,
  uptime_seconds: 122,
  checks: [{ name: 'KVM', ok: true, message: '/dev/kvm is available' }],
};
const stamp = '2026-10-02T08:00:00Z';
const project = {
  id: 'project-a',
  name: 'Package telescope',
  description: 'Package the CLI, run its tests, and prepare a contribution.',
  task_log: '- [ ] Package upstream release',
  next_run_feedback: '',
  status: 'idle',
  default_model: 'codex-test',
  default_reasoning_effort: 'medium',
  created_at: stamp,
  updated_at: stamp,
};
const second = {
  ...project,
  id: 'project-b',
  name: 'Build a todo app',
  description: 'A small useful task list.',
  default_model: '',
};
const windows = [
  {
    limit_id: 'codex',
    name: 'General allowance',
    used_percent: 32.5,
    window_minutes: 720,
    resets_at: '2026-10-03T08:00:00Z',
  },
  {
    limit_id: 'codex',
    name: 'Long allowance',
    used_percent: 62,
    window_minutes: 20160,
    resets_at: '2026-10-16T08:00:00Z',
  },
];

async function fixture(page: Page, options: { signedIn?: boolean } = {}) {
  let signedIn = options.signedIn ?? true;
  let current = { ...project };
  const writes: { path: string; method: string; body: Record<string, unknown> }[] = [];
  const errors: Error[] = [];
  page.on('pageerror', (error) => errors.push(error));
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace('/api/v1', '');
    const method = request.method();
    if (path === '/events') return route.abort();
    if (method !== 'GET') {
      const body = (request.postDataJSON() as Record<string, unknown>) ?? {};
      writes.push({ path, method, body });
      if (path === '/session') {
        signedIn = true;
        return route.fulfill({ json: { ok: true } });
      }
      if (path === '/projects')
        return route.fulfill({ json: { ...project, ...body, id: 'new-project' } });
      if (path === '/projects/project-a' && method === 'PATCH') {
        current = { ...current, ...body };
        return route.fulfill({ json: current });
      }
      if (path === '/runs') return route.fulfill({ json: { id: 'run-1234', ...body } });
      if (path === '/schedules') return route.fulfill({ json: { id: 'schedule-new', ...body } });
      return route.fulfill({ json: { ok: true } });
    }
    if (!signedIn)
      return route.fulfill({ status: 401, json: { detail: 'Authentication required' } });
    const resources: Record<string, unknown> = {
      '/system': system,
      '/projects': [current, second],
      '/projects/project-a': current,
      '/projects/new-project': { ...project, id: 'new-project', name: 'Useful project' },
      '/projects/project-a/executions': [],
      '/projects/new-project/executions': [],
      '/runs': [],
      '/schedules': [],
      '/usage': windows,
      '/auth/openai': { connected: true, method: 'siwc', account_label: 'user@example.test' },
      '/auth/openai/models': [
        { id: 'codex-test', name: 'Codex Test', reasoning_efforts: ['low', 'medium', 'high'] },
      ],
      '/integrations/github': {
        configured: true,
        app_id: '42',
        app_slug: 'tokendrain-example',
        installations: [
          {
            id: '123',
            account: 'octocat',
            permissions: {
              contents: 'write',
              pull_requests: 'write',
              issues: 'read',
              actions: 'read',
            },
          },
        ],
      },
      '/projects/project-a/github': null,
      '/integrations/github/installations/123/repositories': [
        { id: 55, full_name: 'octocat/telescope' },
      ],
      '/projects/project-a/secrets': [],
      '/runs/run-1234': {
        id: 'run-1234',
        status: 'queued',
        created_at: stamp,
        parallel: true,
        stop_conditions: [{ kind: 'provider_limit' }],
        executions: [],
      },
      '/runs/run-1234/events': [],
      '/projects/project-a/storage': {
        environment: { size_bytes: 40000000000, used_bytes: 1800000000 },
        workspace: { size_bytes: 40000000000, used_bytes: 2800000000 },
      },
      '/projects/project-a/snapshots': [],
    };
    if (!(path in resources))
      return route.fulfill({ status: 404, json: { detail: `Unknown test resource ${path}` } });
    return route.fulfill({ json: resources[path] });
  });
  return { writes, errors };
}

test('administration login uses a session and leaves no token in browser storage', async ({
  page,
}) => {
  const { writes, errors } = await fixture(page, { signedIn: false });
  await page.goto('/');
  await page.getByLabel('Administration token').fill('test-only-admin-token');
  await page.getByRole('button', { name: 'Open tokendrain' }).click();
  await expect(page.getByRole('heading', { name: 'Your work, moving forward.' })).toBeVisible();
  expect(writes[0]).toEqual({
    path: '/session',
    method: 'POST',
    body: { token: 'test-only-admin-token' },
  });
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(errors).toEqual([]);
});

test('dashboard renders provider metadata and creates a persistent project', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/');
  await expect(page.getByText('12 hours window', { exact: true })).toBeVisible();
  await expect(page.getByText('14 days window', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/dashboard.png', fullPage: true });
  await page.getByRole('button', { name: 'New project' }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Useful project');
  await page
    .getByLabel('What should the agent achieve?')
    .fill('Build something useful and test it.');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/new-project$/);
  expect(writes.find((write) => write.path === '/projects')?.body).toEqual({
    name: 'Useful project',
    description: 'Build something useful and test it.',
  });
  expect(errors).toEqual([]);
});

test('run preparation serializes per-project models and actual usage windows', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/prepare?project=project-a');
  await expect(page.getByRole('checkbox', { name: /Package telescope/ })).toBeChecked();
  await page.getByRole('checkbox', { name: /Build a todo app/ }).check();
  await page.getByLabel('Model', { exact: true }).nth(1).fill('codex-other');
  await page.getByRole('combobox', { name: 'Reasoning', exact: true }).nth(1).selectOption('high');
  await page.getByRole('checkbox', { name: /General allowance.*12 hours/ }).check();
  await page.getByRole('spinbutton', { name: 'Usage threshold percent' }).first().fill('88');
  await page.getByRole('checkbox', { name: 'Elapsed runtime reaches' }).check();
  await page.getByRole('spinbutton', { name: 'Runtime limit in hours' }).fill('2.5');
  await page.getByRole('checkbox', { name: /Run projects in parallel/ }).uncheck();
  await page.screenshot({ path: 'test-results/prepare-run.png', fullPage: true });
  await page.getByRole('button', { name: 'Start run', exact: false }).click();
  await expect(page).toHaveURL(/\/runs\/run-1234$/);
  expect(writes.find((write) => write.path === '/runs')?.body).toEqual({
    projects: [
      { project_id: 'project-a', model: 'codex-test', reasoning_effort: 'medium' },
      { project_id: 'project-b', model: 'codex-other', reasoning_effort: 'high' },
    ],
    stop_conditions: [
      { kind: 'provider_limit' },
      { kind: 'project_completed' },
      { kind: 'usage', limit_id: 'codex', window_minutes: 720, used_percent: 88 },
      { kind: 'elapsed', seconds: 9000 },
    ],
    parallel: false,
  });
  expect(errors).toEqual([]);
});

test('task edits and described dotenv import reach separate project APIs', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page
    .getByRole('textbox', { name: 'Shared task log' })
    .fill('- [x] First change\n- [ ] Next change');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('status')).toContainText('Changes saved');
  await page.getByRole('button', { name: 'Secrets', exact: true }).click();
  await page
    .getByRole('textbox', { name: '.env content' })
    .fill('TEST_KEY=runtime-value\nSECOND_KEY=second-value');
  await page.getByLabel('Purpose of TEST_KEY').fill('Integration tests only');
  await page.getByLabel('Purpose of SECOND_KEY').fill('Local test service');
  await page.getByRole('button', { name: 'Import 2 secrets' }).click();
  await expect(page.getByRole('textbox', { name: '.env content' })).toHaveValue('');
  expect(writes.find((write) => write.path === '/projects/project-a')?.body).toEqual({
    task_log: '- [x] First change\n- [ ] Next change',
  });
  expect(writes.find((write) => write.path.endsWith('/secrets/import'))?.body).toEqual({
    dotenv: 'TEST_KEY=runtime-value\nSECOND_KEY=second-value',
    descriptions: { TEST_KEY: 'Integration tests only', SECOND_KEY: 'Local test service' },
  });
  expect(errors).toEqual([]);
});

test('GitHub repository access narrows installation permissions', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'GitHub', exact: true }).click();
  await page.getByRole('combobox', { name: 'Installation', exact: true }).selectOption('123');
  await page.getByRole('combobox', { name: 'Repository', exact: true }).selectOption('55');
  await page
    .getByRole('combobox', { name: 'Repository contents', exact: true })
    .selectOption('write');
  await page.getByRole('combobox', { name: 'Pull requests', exact: true }).selectOption('write');
  expect(
    await page
      .getByRole('combobox', { name: 'Issues', exact: true })
      .locator('option')
      .allTextContents(),
  ).toEqual(['No access', 'Read']);
  await page.getByRole('button', { name: 'Save repository access' }).click();
  await expect(page.getByRole('status')).toContainText('Repository integration saved');
  expect(writes.find((write) => write.path.endsWith('/github'))?.body).toEqual({
    installation_id: '123',
    repository_id: 55,
    repository_name: 'octocat/telescope',
    permissions: { contents: 'write', pull_requests: 'write' },
  });
  expect(errors).toEqual([]);
});

test('schedules use the ordinary run template with timezone-aware timing', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/prepare?project=project-a');
  await page.getByRole('button', { name: 'Save a schedule' }).click();
  await page.getByLabel('Schedule name').fill('Nightly telescope');
  await page.getByLabel('Cron expression').fill('0 3 * * *');
  await page.getByRole('textbox', { name: /^Timezone/ }).fill('Europe/Madrid');
  await page.getByRole('button', { name: 'Create schedule' }).click();
  await expect(page).toHaveURL(/\/schedules$/);
  expect(writes.find((write) => write.path === '/schedules')?.body).toEqual({
    name: 'Nightly telescope',
    cron: '0 3 * * *',
    timezone: 'Europe/Madrid',
    enabled: true,
    run_template: {
      projects: [{ project_id: 'project-a', model: 'codex-test', reasoning_effort: 'medium' }],
      stop_conditions: [{ kind: 'provider_limit' }, { kind: 'project_completed' }],
      parallel: true,
    },
  });
  expect(errors).toEqual([]);
});

test('mobile project navigation and environment controls do not overflow', async ({ page }) => {
  const { errors } = await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Environment', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Persistent storage' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: 'test-results/project-mobile.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('live SSE execution events are rendered as text', async ({ page }) => {
  const { errors } = await fixture(page);
  await page.route('**/api/v1/events', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `id: 1\ndata: ${JSON.stringify({ id: 'event-1', type: 'execution.log', run_id: 'run-1234', timestamp: stamp, message: 'Tests passed. <script>alert("unsafe")</script>' })}\n\n`,
    }),
  );
  await page.goto('/runs/run-1234');
  await page.getByRole('button', { name: 'Live events' }).click();
  await expect(page.getByRole('log')).toContainText(
    'Tests passed. <script>alert("unsafe")</script>',
  );
  await expect(page.getByRole('log').locator('script')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('cancelling an active run requests orderly shutdown', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.route('**/api/v1/runs/run-1234', (route) =>
    route.fulfill({
      json: {
        id: 'run-1234',
        status: 'running',
        created_at: stamp,
        started_at: stamp,
        parallel: true,
        stop_conditions: [{ kind: 'elapsed', seconds: 3600 }],
        executions: [
          {
            id: 'execution-1',
            project_id: 'project-a',
            project_name: project.name,
            run_id: 'run-1234',
            status: 'running',
            model: 'codex-test',
            reasoning_effort: 'medium',
            started_at: stamp,
          },
        ],
      },
    }),
  );
  await page.goto('/runs/run-1234');
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Cancellation requested');
  expect(writes.find((write) => write.path === '/runs/run-1234/cancel')).toEqual({
    path: '/runs/run-1234/cancel',
    method: 'POST',
    body: {},
  });
  expect(errors).toEqual([]);
});

test('GitHub setup return requires authenticated discovery instead of trusting query data', async ({
  page,
}) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/settings?github=installed');
  await expect(page.getByText('Continue your GitHub setup', { exact: true })).toBeVisible();
  expect(writes).toEqual([]);
  await page.getByRole('button', { name: 'Discover installed repositories' }).click();
  await expect(page).toHaveURL(/\/settings$/);
  expect(writes).toEqual([{ path: '/integrations/github/sync', method: 'POST', body: {} }]);
  expect(errors).toEqual([]);
});
