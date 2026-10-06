import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

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
    name: 'General usage',
    used_percent: 32.5,
    window_minutes: 720,
    resets_at: '2026-10-03T08:00:00Z',
  },
  {
    limit_id: 'codex',
    name: 'Long-term usage',
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
      if (path.endsWith('/github'))
        return route.fulfill({ json: { ...body, repository_name: 'octocat/telescope' } });
      if (path === '/runs') return route.fulfill({ json: { id: 'run-1234', ...body } });
      if (path === '/schedules') return route.fulfill({ json: { id: 'schedule-new', ...body } });
      return route.fulfill({ json: { ok: true } });
    }
    if (!signedIn)
      return route.fulfill({ status: 401, json: { detail: 'Authentication required' } });
    const resources: Record<string, unknown> = {
      '/session': { authenticated: true, auth_mode: 'token' },
      '/projects/project-a/tasks': [],
      '/system': system,
      '/notifications/ntfy': {
        enabled: false,
        server_url: 'https://ntfy.sh',
        topic: '',
        rules: [],
        has_token: false,
        delivery: {},
      },
      '/projects': [current, second],
      '/projects/project-a': current,
      '/projects/new-project': { ...project, id: 'new-project', name: 'Useful project' },
      '/projects/project-a/executions': [],
      '/projects/new-project/executions': [],
      '/runs': [],
      '/schedules': [],
      '/automations': [],
      '/automation-occurrences': [],
      '/usage': windows,
      '/auth/openai': { connected: true, method: 'import', account_label: 'user@example.test' },
      '/auth/openai/models': [
        { id: 'codex-test', name: 'Codex Test', reasoning_efforts: ['low', 'medium', 'high'] },
        { id: 'codex-other', name: 'Other', reasoning_efforts: ['medium', 'high'] },
      ],
      '/integrations/github': {
        configured: true,
        name: 'tokendrain-example',
        repository_count: 1,
        installation_url: 'https://github.com/apps/tokendrain-example/installations/new',
      },
      '/projects/project-a/github': null,
      '/integrations/github/repositories': [
        { id: 55, full_name: 'octocat/telescope', used_by: [] },
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
        virtual_size_bytes: 40000000000,
        allocated_bytes: 2800000000,
      },
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
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
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
  await expect(page.getByText(/12 hours window/)).toBeVisible();
  await expect(page.getByText(/14 days window/)).toBeVisible();
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
    initial_tasks: [],
    default_model: '',
    default_reasoning_effort: 'medium',
  });
  expect(errors).toEqual([]);
});

test('run preparation serializes per-project models and actual usage windows', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/prepare?project=project-a');
  await expect(page.getByRole('checkbox', { name: /Package telescope/ })).toBeChecked();
  await page.getByRole('checkbox', { name: /Build a todo app/ }).check();
  await page
    .getByRole('combobox', { name: 'Model', exact: true })
    .nth(1)
    .selectOption('codex-other');
  await page.getByRole('combobox', { name: 'Reasoning', exact: true }).nth(1).selectOption('high');
  await page.getByRole('checkbox', { name: /General usage.*12 hours/ }).check();
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
    threshold_mode: 'graceful',
  });
  expect(errors).toEqual([]);
});

test('task edits and described dotenv import reach separate project APIs', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Kanban', exact: true }).click();
  await page.getByRole('button', { name: 'Add task', exact: false }).first().click();
  await page.getByLabel('Title', { exact: true }).fill('Next useful change');
  await page.getByLabel('Details', { exact: true }).fill('Build and verify');
  await page.getByRole('button', { name: 'Save task', exact: true }).click();
  await page.getByRole('button', { name: 'Secrets', exact: true }).click();
  await page
    .getByRole('textbox', { name: '.env content' })
    .fill('TEST_KEY=runtime-value\nSECOND_KEY=second-value');
  await page.getByLabel('Purpose of TEST_KEY').fill('Integration tests only');
  await page.getByLabel('Purpose of SECOND_KEY').fill('Local test service');
  await page.getByRole('button', { name: 'Import 2 secrets' }).click();
  await expect(page.getByRole('textbox', { name: '.env content' })).toHaveValue('');
  expect(writes.find((write) => write.path === '/projects/project-a/tasks')?.body).toEqual([
    { title: 'Next useful change', description: 'Build and verify', column: 'todo' },
  ]);
  expect(writes.find((write) => write.path.endsWith('/secrets/import'))?.body).toEqual({
    dotenv: 'TEST_KEY=runtime-value\nSECOND_KEY=second-value',
    descriptions: { TEST_KEY: 'Integration tests only', SECOND_KEY: 'Local test service' },
  });
  expect(errors).toEqual([]);
});

test('GitHub access modes and workflow toggle save a simple repository binding', async ({
  page,
}) => {
  const { writes, errors } = await fixture(page);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'GitHub', exact: true }).click();
  await page.getByRole('combobox', { name: 'Repository', exact: true }).fill('octocat/telescope');
  await expect(page.getByRole('radio', { name: 'Pull requests', exact: true })).toBeChecked();
  await expect(
    page.getByRole('radio', { name: 'Pull requests', exact: true }),
  ).toHaveAccessibleDescription(/Updating the default branch requires a pull request/);
  await expect(
    page.getByText(/GitHub enforces these settings through GitHub App token permissions/),
  ).toBeVisible();
  await page.getByRole('checkbox', { name: 'Allow workflow file changes' }).check();
  await page.getByRole('button', { name: 'Save repository access' }).click();
  await expect(page.getByRole('status')).toContainText('Repository integration saved');
  expect(writes.find((write) => write.path.endsWith('/github'))?.body).toEqual({
    repository_id: 55,
    access_mode: 'pull_requests',
    allow_workflows: true,
  });
  await expect(page.getByLabel('Installation', { exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('GitHub repository conflicts disable only the incompatible writable mode', async ({
  page,
}) => {
  await fixture(page);
  await page.route('**/api/v1/integrations/github/repositories*', (route) =>
    route.fulfill({
      json: [
        {
          id: 55,
          full_name: 'octocat/telescope',
          used_by: [
            { project_id: 'other', project_name: 'Project B', access_mode: 'pull_requests' },
          ],
        },
      ],
    }),
  );
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'GitHub', exact: true }).click();
  await page.getByRole('combobox', { name: 'Repository', exact: true }).fill('octocat/telescope');
  await expect(page.getByText('Used by Project B · Pull requests')).toBeVisible();
  await expect(page.getByRole('radio', { name: 'Direct write', exact: true })).toBeDisabled();
  await expect(page.getByRole('radio', { name: 'Pull requests', exact: true })).toBeEnabled();
  await page.getByRole('radio', { name: 'Read only', exact: true }).check();
  await expect(page.getByRole('checkbox', { name: 'Allow workflow file changes' })).toBeDisabled();
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
      threshold_mode: 'graceful',
    },
  });
  expect(errors).toEqual([]);
});

test('mobile project navigation and VM storage controls do not overflow', async ({ page }) => {
  const { errors } = await fixture(page);
  await page.route('**/api/v1/projects/project-a/storage', (route) =>
    route.fulfill({ json: { virtual_size_bytes: 80 * 1024 ** 3, allocated_bytes: 2800000000 } }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'VM storage', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'VM storage', exact: true })).toBeVisible();
  await expect(page.getByLabel('New capacity, GiB')).toHaveValue('80');
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
  await page.getByRole('button', { name: 'Activity' }).click();
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
  await expect(
    page.getByText('Repositories updated. Choose a repository in your project.'),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/settings$/);
  expect(writes).toEqual([{ path: '/integrations/github/sync', method: 'POST', body: {} }]);
  expect(errors).toEqual([]);
});

test('Kanban drag moves cards and preserves ordering', async ({ page }) => {
  const { errors } = await fixture(page);
  let tasks = [
    {
      id: 'task-a',
      project_id: 'project-a',
      title: 'First task',
      description: '',
      column: 'todo',
      position: 0,
      origin: 'user',
    },
    {
      id: 'task-b',
      project_id: 'project-a',
      title: 'Second task',
      description: 'Discovered work',
      column: 'todo',
      position: 1,
      origin: 'agent',
    },
  ];
  await page.route('**/api/v1/projects/project-a/tasks', async (route) => {
    if (route.request().method() === 'POST') {
      const mutation = route.request().postDataJSON()[0];
      const task = tasks.find((t) => t.id === mutation.id)!;
      const others = tasks.filter((t) => t.id !== task.id);
      const column = mutation.column;
      const ordered = others
        .filter((t) => t.column === column)
        .sort((a, b) => a.position - b.position);
      ordered.splice(mutation.position, 0, { ...task, column });
      tasks = [
        ...others.filter((t) => t.column !== column),
        ...ordered.map((t, i) => ({ ...t, position: i })),
      ];
    }
    await route.fulfill({ json: tasks });
  });
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Kanban', exact: true }).click();
  await expect(page.locator('.ai-tag')).toHaveCount(1);
  await page
    .locator('.task-card')
    .filter({ hasText: 'Second task' })
    .dragTo(page.locator('.task-card').filter({ hasText: 'First task' }), {
      targetPosition: { x: 20, y: 2 },
    });
  await expect(page.locator('.kanban-column').nth(1).locator('.task-card').first()).toContainText(
    'Second task',
  );
  await page
    .locator('.task-card')
    .filter({ hasText: 'First task' })
    .dragTo(page.locator('.kanban-column').nth(3));
  await expect(page.locator('.kanban-column').nth(3)).toContainText('First task');
  await page.screenshot({ path: 'test-results/kanban.png', fullPage: true });
  expect(tasks.find((t) => t.id === 'task-a')?.column).toBe('done');
  expect(errors).toEqual([]);
});

test('Activity uses structured commands and filters persisted and live projects', async ({
  page,
}) => {
  const { errors } = await fixture(page);
  const report = {
    status: 'blocked',
    summary: 'README committed locally; publishing needs Contents write.',
    completed: ['Updated README'],
    remaining: ['Publish the branch'],
    blockers: ['GitHub denied push'],
  };
  const executions = [
    { id: 'exec-a', project_id: 'project-a', project_name: 'Package telescope', status: 'running' },
    { id: 'exec-b', project_id: 'project-b', project_name: 'Build a todo app', status: 'running' },
  ];
  await page.route('**/api/v1/runs/run-1234', (route) =>
    route.fulfill({
      json: {
        id: 'run-1234',
        status: 'running',
        created_at: stamp,
        parallel: true,
        stop_conditions: [],
        executions,
      },
    }),
  );
  await page.route('**/api/v1/runs/run-1234/events', (route) =>
    route.fulfill({
      json: [
        {
          id: 21,
          run_id: 'run-1234',
          project_id: 'project-a',
          execution_id: 'exec-a',
          type: 'execution.log',
          timestamp: stamp,
          message: JSON.stringify({
            type: 'commandExecution',
            command: 'git push origin docs/readme',
            cwd: '/workspace',
            status: 'failed',
            exitCode: 1,
            durationMs: 977,
            aggregatedOutput: 'GitHub rejected push (HTTP 403)',
          }),
        },
        {
          id: 22,
          run_id: 'run-1234',
          project_id: 'project-a',
          execution_id: 'exec-a',
          type: 'execution.log',
          timestamp: stamp,
          message: JSON.stringify({
            type: 'agentMessage',
            phase: 'final_answer',
            text: JSON.stringify(report),
          }),
        },
        {
          id: 25,
          run_id: 'run-1234',
          project_id: 'project-a',
          execution_id: 'exec-a',
          type: 'execution.error',
          timestamp: stamp,
          message: 'Connection to the project VM was lost',
          data: {
            title: 'Connection to the project VM was lost',
            explanation: 'The VM or its supervisor may have stopped or run out of memory.',
            workspace: 'Files already written remain in the workspace.',
            recovery: '512 MiB RAM is very little. Increase Memory in Settings to 4096 MiB.',
            technical_detail: 'ConnectionRefusedError: [Errno 111] Connection refused',
          },
        },
        {
          id: 23,
          run_id: 'run-1234',
          project_id: 'project-a',
          execution_id: 'exec-a',
          type: 'execution.log',
          timestamp: stamp,
          message: JSON.stringify({
            type: 'userMessage',
            content: [{ text: 'Continue autonomous work' }],
          }),
        },
      ],
    }),
  );
  await page.route('**/api/v1/events', (route) =>
    route.fulfill({
      contentType: 'text/event-stream',
      body: `id: 24\ndata: ${JSON.stringify({ id: 24, run_id: 'run-1234', project_id: 'project-b', execution_id: 'exec-b', type: 'agent.progress', message: 'Building the utility', timestamp: stamp })}\n\n`,
    }),
  );
  await page.goto('/runs/run-1234');
  await page.getByRole('button', { name: 'Activity' }).click();
  await expect(page.getByRole('log')).toContainText('Command failed');
  await expect(page.getByRole('log')).toContainText(report.summary);
  await expect(page.getByRole('log')).toContainText('Building the utility');
  await expect(page.getByRole('log')).not.toContainText('Continue autonomous work');
  await expect(page.getByRole('log')).toContainText('Connection to the project VM was lost');
  await expect(page.getByRole('log')).toContainText('4096 MiB');
  await expect(page.getByText('ConnectionRefusedError:', { exact: false })).not.toBeVisible();
  await page.getByText('Technical details', { exact: true }).click();
  await expect(page.getByRole('log')).toContainText('ConnectionRefusedError');
  await expect(page.getByRole('log')).toContainText('sudo journalctl');
  await page.getByRole('button', { name: 'Build a todo app', exact: true }).click();
  await expect(page.getByRole('log')).not.toContainText('git push');
  await expect(page.getByRole('log')).toContainText('Building the utility');
  await page.getByRole('button', { name: 'Package telescope', exact: true }).click();
  await expect(page.getByRole('log')).not.toContainText('Building the utility');
  await page.getByText('Show full output', { exact: true }).click();
  await expect(page.getByRole('log')).toContainText('GitHub rejected push (HTTP 403)');
  await page.screenshot({ path: 'test-results/activity.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('appearance follows system and persists an explicit theme', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await fixture(page);
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('.logo-dark').first()).toBeVisible();
  await page.getByRole('button', { name: 'Light theme', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: 'Dark theme', exact: true }).click();
  await page.screenshot({
    path: 'test-results/dashboard-dark.png',
    fullPage: true,
    animations: 'disabled',
  });
  await page.goto('/prepare?project=project-a');
  const radio = page.getByRole('radio', { name: /Graceful stop/ });
  const size = await radio.boundingBox();
  expect(size?.width).toBeLessThanOrEqual(18);
  expect(size?.height).toBeLessThanOrEqual(18);
  await page.screenshot({
    path: 'test-results/prepare-dark.png',
    fullPage: true,
    animations: 'disabled',
  });
  await page.getByRole('button', { name: 'System theme', exact: true }).click();
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('mobile appearance stays in the top-right and cycles through all modes', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: 'light' });
  const { errors } = await fixture(page);
  await page.goto('/');
  const control = page.getByRole('button', { name: /^Appearance:/ });
  await expect(control).toHaveAttribute('data-mode', 'system');
  await expect(page.getByRole('group', { name: 'Appearance', exact: true })).toBeHidden();
  const bounds = await control.boundingBox();
  expect(bounds?.x).toBeGreaterThan(320);
  expect(bounds?.y).toBeLessThan(30);
  await control.click();
  await expect(control).toHaveAttribute('data-mode', 'light');
  await control.click();
  await expect(control).toHaveAttribute('data-mode', 'dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(control).toHaveAttribute('data-mode', 'dark');
  await page.screenshot({ path: 'test-results/mobile-theme.png', fullPage: true });
  await control.click();
  await expect(control).toHaveAttribute('data-mode', 'system');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('unsaved changes warn on tabs and navigation and clear after saving', async ({ page }) => {
  await fixture(page);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Description', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  await page.getByLabel('Project description', { exact: true }).fill('Unsaved description');
  await expect(page.getByText('Unsaved changes', { exact: true })).toBeVisible();
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('button', { name: 'Next run feedback', exact: true }).click();
  await expect(page.getByLabel('Project description', { exact: true })).toHaveValue(
    'Unsaved description',
  );
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('link', { name: 'Dashboard', exact: true }).click();
  await expect(page).toHaveURL(/projects\/project-a$/);
  await page.route('**/api/v1/projects/project-a', (route) =>
    route.request().method() === 'PATCH'
      ? route.fulfill({ status: 409, json: { detail: 'Save failed. Try again.' } })
      : route.fallback(),
  );
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('alert')).toContainText('Save failed');
  await expect(page.getByText('Unsaved changes', { exact: true })).toBeVisible();
  await page.unroute('**/api/v1/projects/project-a');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText('Changes saved.', { exact: true })).toBeVisible();
  await expect(page.getByText('Unsaved changes', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Next run feedback', exact: true }).click();
  await expect(page.getByLabel('Feedback for the next run', { exact: true })).toBeVisible();
});

test('project creation saves the model and GitHub integration together', async ({ page }) => {
  const { writes } = await fixture(page);
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project' }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Integrated project');
  await page.getByLabel('What should the agent achieve?').fill('Complete approved work.');
  await page.getByLabel('Default model', { exact: true }).selectOption('codex-other');
  await page.getByLabel('Default reasoning effort', { exact: true }).selectOption('high');
  await page.getByRole('checkbox', { name: 'Attach a GitHub repository' }).check();
  await expect(
    page.getByRole('radio', { name: 'Read only', exact: true }),
  ).toHaveAccessibleDescription(/cannot be published to GitHub/);
  await expect(
    page.getByText(/GitHub enforces these settings through GitHub App token permissions/),
  ).toBeVisible();
  await page.getByRole('combobox', { name: 'Repository', exact: true }).fill('octocat/telescope');
  await page.screenshot({ path: 'test-results/create-project.png', fullPage: true });
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await expect(page).toHaveURL(/projects\/new-project$/);
  expect(writes.find((w) => w.path === '/projects')?.body).toMatchObject({
    default_model: 'codex-other',
    default_reasoning_effort: 'high',
    github: {
      repository_id: 55,
      access_mode: 'pull_requests',
      allow_workflows: false,
    },
  });
});

test('Run preparation has no unsaved warning and dark controls show selection', async ({
  page,
}) => {
  await fixture(page);
  await page.goto('/prepare?project=project-a');
  await page.getByRole('button', { name: 'Dark theme', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Dark theme', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('radio', { name: /Hard limit/ }).check();
  await page.getByRole('radio', { name: /Graceful stop/ }).check();
  await expect(page.getByText('Unsaved changes', { exact: true })).toHaveCount(0);
  const now = page.getByRole('button', { name: 'Start now', exact: true });
  const schedule = page.getByRole('button', { name: 'Save a schedule', exact: true });
  await expect(now).toHaveAttribute('aria-pressed', 'true');
  await schedule.click();
  await expect(schedule).toHaveAttribute('aria-pressed', 'true');
  await expect(now).toHaveAttribute('aria-pressed', 'false');
  const colors = await page
    .locator('.segmented button')
    .evaluateAll((buttons) => buttons.map((button) => getComputedStyle(button).backgroundColor));
  expect(colors[0]).not.toBe(colors[1]);
  await expect(page.getByText('Unsaved changes', { exact: true })).toHaveCount(0);
  let warned = false;
  page.on('dialog', (dialog) => {
    warned = true;
    void dialog.dismiss();
  });
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page).toHaveURL(/settings$/);
  expect(warned).toBe(false);
  await expect(page.getByRole('link', { name: 'Manage repositories on GitHub ↗' })).toBeVisible();
  await page.screenshot({
    path: 'test-results/settings-dark.png',
    fullPage: true,
    animations: 'disabled',
  });
});

test('workspace browses lazily, sanitizes previews, gates large files, and works on mobile', async ({
  page,
}) => {
  const { errors } = await fixture(page);
  const requested: string[] = [];
  const entry = (
    name: string,
    path: string,
    kind = 'file',
    size = 100,
    mime_type = 'text/plain',
  ) => ({
    name,
    path,
    kind,
    size: kind === 'directory' ? null : size,
    mime_type,
    modified_at: stamp,
  });
  const root = [
    entry('src', 'src', 'directory'),
    entry('README.md', 'README.md'),
    entry('server.log', 'server.log', 'file', 3 * 1024 ** 2),
    entry('huge.log', 'huge.log', 'file', 40 * 1024 ** 2),
    entry('data.bin', 'data.bin', 'file', 1024, 'application/octet-stream'),
    entry('pixel.png', 'pixel.png', 'file', 70, 'image/png'),
  ];
  const nested = [entry('main.py', 'src/main.py')];
  const workspaceRoute = (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.searchParams.get('path') || '';
    const operation = url.pathname.split('/').pop();
    requested.push(`${operation}:${path}`);
    if (operation === 'tree')
      return route.fulfill({ json: { path, entries: path === 'src' ? nested : root } });
    if (operation === 'archive' || operation === 'download')
      return route.fulfill({
        body: 'download',
        headers: {
          'Content-Disposition': `attachment; filename="${operation === 'archive' ? 'workspace.zip' : 'main.py'}"`,
        },
      });
    if (path === 'pixel.png')
      return route.fulfill({
        contentType: 'image/png',
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=',
          'base64',
        ),
      });
    return route.fulfill({
      contentType: 'text/plain',
      body:
        path === 'README.md'
          ? '# Notes\n\n<script>window.workspaceInjected = true</script>\n\n<a href="javascript:alert(1)">Bad link</a><img src="x" onerror="alert(1)">'
          : path === 'server.log'
            ? 'Large log contents'
            : "print('hello')\n",
    });
  };
  await page.route('**/api/v1/projects/project-a/workspace/**', workspaceRoute);
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  const files = page.getByRole('list', { name: 'Workspace files' });
  await expect(files.getByRole('listitem')).toHaveCount(6);
  expect([...new Set(requested)]).toEqual(['tree:']);
  await page.getByLabel('Filter filenames').fill('readme');
  await expect(files.getByRole('listitem')).toHaveCount(1);
  await files.getByRole('button', { name: /README.md/ }).click();
  await expect(page.getByRole('heading', { name: 'Notes', exact: true })).toBeVisible();
  await expect(page.locator('.workspace-markdown script, .workspace-markdown img')).toHaveCount(0);
  await expect(page.locator('.workspace-markdown a')).not.toHaveAttribute('href', /javascript/);
  expect(await page.evaluate(() => 'workspaceInjected' in window)).toBe(false);
  await page.getByRole('button', { name: 'Source', exact: true }).click();
  await expect(page.locator('.workspace-code')).toContainText('# Notes');
  await page.getByLabel('Filter filenames').fill('');
  await files.getByRole('button', { name: /server.log/ }).click();
  await expect(page.getByRole('heading', { name: 'Large file', exact: true })).toBeVisible();
  expect(requested).not.toContain('file:server.log');
  await page.getByRole('button', { name: 'Load anyway' }).click();
  await expect(page.locator('.workspace-code')).toContainText('Large log contents');
  await files.getByRole('button', { name: /huge.log/ }).click();
  await expect(page.getByRole('heading', { name: 'Download only' })).toBeVisible();
  expect(requested).not.toContain('file:huge.log');
  await files.getByRole('button', { name: /data.bin/ }).click();
  await expect(page.getByRole('heading', { name: 'Preview unavailable' })).toBeVisible();
  expect(requested).not.toContain('file:data.bin');
  await files.getByRole('button', { name: /pixel.png/ }).click();
  await expect(page.getByRole('img', { name: 'pixel.png' })).toBeVisible();
  expect(
    await page
      .getByRole('img', { name: 'pixel.png' })
      .evaluate((element) => (element as HTMLImageElement).naturalWidth),
  ).toBe(1);
  await files.getByRole('button', { name: /src/ }).click();
  await expect(files.getByRole('button', { name: /main.py/ })).toBeVisible();
  await files.getByRole('button', { name: /main.py/ }).click();
  await expect(page.locator('.workspace-code')).toContainText("print('hello')");
  await expect(page.locator('.workspace-line-numbers')).toContainText('1');
  await page.getByRole('button', { name: 'Line wrap' }).click();
  await expect(page.locator('.workspace-code')).toHaveCSS('white-space', 'pre-wrap');
  await expect(page.getByRole('link', { name: 'Download file', exact: true })).toHaveAttribute(
    'href',
    /\/download\?path=src%2Fmain\.py$/,
  );
  await expect(
    page.getByRole('link', { name: 'Download folder as ZIP', exact: true }),
  ).toHaveAttribute('href', /\/archive\?path=src$/);
  await expect(
    page.getByRole('link', { name: 'Download workspace as ZIP', exact: true }),
  ).toHaveAttribute('href', /\/archive\?path=$/);
  await page.screenshot({ path: 'test-results/workspace-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(files).toBeHidden();
  await expect(page.locator('.workspace-code')).toBeVisible();
  await page.screenshot({ path: 'test-results/workspace-mobile.png', fullPage: true });
  await page.getByRole('button', { name: 'Back to files' }).click();
  await expect(files).toBeVisible();
  await page
    .getByRole('navigation', { name: 'Workspace path' })
    .getByRole('button', { name: 'Workspace', exact: true })
    .click();
  await expect(files.getByRole('listitem')).toHaveCount(6);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('active project workspace points to its Run without accessing files', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/v1/projects/project-a/executions', (route) =>
    route.fulfill({
      json: [{ id: 'execution-1', project_id: 'project-a', run_id: 'run-1234', status: 'running' }],
    }),
  );
  let workspaceRequests = 0;
  await page.route('**/api/v1/projects/project-a/workspace/**', (route) => {
    workspaceRequests++;
    return route.abort();
  });
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Workspace currently in use' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Run', exact: true })).toHaveAttribute(
    'href',
    '/runs/run-1234',
  );
  expect(workspaceRequests).toBe(0);
});

test('Codex connection checks show reauthentication guidance and recover', async ({ page }) => {
  await fixture(page);
  let status: Record<string, unknown> = {
    connected: true,
    valid: true,
    account_label: 'user@example.test',
  };
  let attempts = 0;
  await page.route('**/api/v1/auth/openai', (route) => route.fulfill({ json: status }));
  await page.route('**/api/v1/auth/openai/check', (route) => {
    attempts++;
    status = {
      ...status,
      connection_checked_at: Date.now() / 1000,
      connection_ok: attempts > 1,
      reauth_required: attempts === 1,
      usage_error:
        attempts === 1 ? 'Codex could not authenticate with the imported credentials.' : null,
    };
    return route.fulfill({ json: status });
  });
  await page.goto('/settings');
  await expect(page.getByText('Credentials stored · connection not verified')).toBeVisible();
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await expect(page.getByText('Connection needs attention', { exact: true })).toBeVisible();
  await expect(page.locator('pre').filter({ hasText: 'codex logout' })).toContainText(
    'codex login',
  );
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await expect(page.getByText('Connection checked · usage available')).toBeVisible();
  await expect(page.getByText('Connection needs attention', { exact: true })).toHaveCount(0);
});

test('ntfy settings save reminders and send a test using saved settings', async ({ page }) => {
  await fixture(page);
  let config = {
    enabled: false,
    server_url: 'https://ntfy.sh',
    topic: '',
    rules: [],
    has_token: false,
    delivery: {},
  };
  let saved: Record<string, unknown> = {};
  let testCount = 0;
  await page.route('**/api/v1/notifications/ntfy**', async (route) => {
    const request = route.request();
    if (request.method() === 'PUT') {
      saved = request.postDataJSON() as Record<string, unknown>;
      config = { ...config, ...saved, has_token: !!saved.access_token };
      return route.fulfill({ json: config });
    }
    if (request.method() === 'POST') {
      testCount++;
      return route.fulfill({ json: { sent: true } });
    }
    return route.fulfill({ json: config });
  });
  await page.goto('/settings');
  const panel = page.locator('#notifications');
  await panel.getByLabel('Enable usage reminders').check();
  await panel.getByLabel('ntfy server URL').fill('https://ntfy.example');
  await panel.getByLabel('Topic', { exact: true }).fill('drain-alerts');
  await panel.getByLabel('Access token (optional)', { exact: true }).fill('fake-token');
  await panel.getByRole('button', { name: 'Add usage reminder' }).click();
  await panel.getByLabel('Reset within (hours)').fill('10');
  await panel.getByLabel('At least this much allowance remaining (%)').fill('85');
  await expect(panel.getByRole('button', { name: 'Send test notification' })).toBeDisabled();
  await panel.getByRole('button', { name: 'Save notifications' }).click();
  await expect(panel.getByText('Notification settings saved.')).toBeVisible();
  expect(saved).toMatchObject({
    enabled: true,
    server_url: 'https://ntfy.example',
    topic: 'drain-alerts',
    access_token: 'fake-token',
    rules: [{ window_minutes: 10080, hours_before_reset: 10, min_remaining_percent: 85 }],
  });
  await expect(panel.getByLabel('Replace access token (optional)')).toHaveValue('');
  await panel.getByRole('button', { name: 'Send test notification' }).click();
  await expect(panel.getByText('Test notification sent.')).toBeVisible();
  expect(testCount).toBe(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

const automationTemplate = {
  projects: [{ project_id: 'project-a', model: 'codex-test', reasoning_effort: 'high' }],
  parallel: true,
  threshold_mode: 'graceful',
  stop_conditions: [
    { kind: 'usage', window_minutes: 10080, used_percent: 100 },
    { kind: 'provider_limit' },
    { kind: 'project_completed' },
  ],
};
const automationRule = {
  id: 'automation-a',
  name: 'Weekly drain',
  enabled: true,
  mode: 'approval',
  trigger: {
    window_minutes: 10080,
    limit_id: null,
    hours_before_reset: 12,
    min_remaining_percent: 20,
  },
  run_template: automationTemplate,
};
function pendingAutomation() {
  return {
    id: 'occurrence-a',
    automation_id: 'automation-a',
    automation_name: 'Weekly drain',
    limit_id: 'codex',
    window_minutes: 10080,
    resets_at: new Date(Date.now() + 3600000).toISOString(),
    matched_window: { limit_id: 'codex', window_minutes: 10080, used_percent: 20 },
    current_usage: windows,
    run_template: automationTemplate,
    mode: 'approval',
    status: 'pending',
    created_at: stamp,
    notified_at: stamp,
    delivery_error: null,
    last_error: null,
    run_id: null,
  };
}

for (const mode of ['automatic', 'approval'] as const) {
  test(`create usage automation in ${mode} mode with exhaustion defaults`, async ({ page }) => {
    const { writes, errors } = await fixture(page);
    await page.goto('/automations');
    await expect(page.getByText(/Conditions are checked every 15 minutes/)).toBeVisible();
    await page.getByRole('button', { name: 'New automation', exact: true }).click();
    await page.getByLabel('Name', { exact: true }).fill('Spend allowance');
    await page.getByRole('checkbox', { name: /Package telescope/ }).check();
    await page.getByLabel('Usage window (minutes)', { exact: true }).fill('720');
    await page.getByLabel('Minimum remaining (%)', { exact: true }).fill('40');
    if (mode === 'automatic')
      await page.getByRole('radio', { name: /^Launch automatically/ }).check();
    else {
      await expect(
        page.getByRole('radio', { name: /^Notify and wait for approval/ }),
      ).toBeChecked();
      await expect(page.getByRole('link', { name: 'Notification settings →' })).toBeVisible();
    }
    await expect(page.getByLabel('Usage threshold percent').first()).toHaveValue('100');
    await page.getByRole('button', { name: 'Save automation' }).click();
    await expect(page.getByRole('heading', { name: 'No automations yet' })).toBeVisible();
    expect(writes.find((w) => w.path === '/automations')?.body).toMatchObject({
      name: 'Spend allowance',
      mode,
      enabled: true,
      trigger: {
        window_minutes: 720,
        limit_id: null,
        hours_before_reset: 12,
        min_remaining_percent: 40,
      },
      run_template: {
        projects: [{ project_id: 'project-a', model: 'codex-test', reasoning_effort: 'medium' }],
        stop_conditions: [
          { kind: 'usage', window_minutes: 720, used_percent: 100 },
          { kind: 'provider_limit' },
          { kind: 'project_completed' },
        ],
      },
    });
    expect(errors).toEqual([]);
  });
}

test('automation editing retains configuration and shows pending requests and history', async ({
  page,
}) => {
  const { writes, errors } = await fixture(page);
  const pending = pendingAutomation();
  await page.route('**/api/v1/automations', (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({ json: [automationRule] })
      : route.fallback(),
  );
  await page.route('**/api/v1/automation-occurrences', (route) =>
    route.fulfill({
      json: [pending, { ...pending, id: 'old', status: 'launched', run_id: 'run-1234' }],
    }),
  );
  await page.goto('/automations');
  await expect(page.getByRole('link', { name: 'Review run' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'View run', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.getByLabel('Minimum remaining (%)', { exact: true })).toHaveValue('20');
  await expect(page.getByRole('checkbox', { name: /Package telescope/ })).toBeChecked();
  await page.getByLabel('Name', { exact: true }).fill('Updated drain');
  await page.getByRole('button', { name: 'Save automation' }).click();
  await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
  expect(writes.find((w) => w.path === '/automations/automation-a')?.body).toMatchObject({
    name: 'Updated drain',
    run_template: automationTemplate,
  });
  expect(errors).toEqual([]);
});

test('notification deep link survives login and requires explicit authorization', async ({
  page,
}) => {
  const { writes, errors } = await fixture(page, { signedIn: false });
  const pending = pendingAutomation();
  await page.route('**/api/v1/automation-occurrences/occurrence-a', (route) =>
    route.fulfill({ json: pending }),
  );
  await page.route('**/api/v1/automation-occurrences/occurrence-a/authorize', (route) =>
    route.fulfill({ json: { ...pending, status: 'launched', run_id: 'run-1234' } }),
  );
  await page.goto('/automation-occurrences/occurrence-a');
  await page.getByLabel('Administration token').fill('a'.repeat(40));
  await page.getByRole('button', { name: /Open tokendrain/ }).click();
  await expect(page.getByRole('button', { name: 'Authorize run', exact: true })).toBeVisible();
  expect(writes.map((w) => w.path)).toEqual(['/session']);
  await expect(page.getByText('Package telescope', { exact: true })).toBeVisible();
  await expect(page.getByText(/Stops at reset with hard interruption/)).toBeVisible();
  await page.getByRole('button', { name: 'Authorize run', exact: true }).click();
  await expect(page).toHaveURL(/runs\/run-1234$/);
  expect(errors).toEqual([]);
});

test('expired automation requests cannot be authorized', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  const expired = { ...pendingAutomation(), resets_at: new Date(Date.now() - 1000).toISOString() };
  await page.route('**/api/v1/automation-occurrences/occurrence-a', (route) =>
    route.fulfill({ json: expired }),
  );
  await page.goto('/automation-occurrences/occurrence-a');
  await expect(
    page.getByText('This request expired at reset. It cannot launch a run.'),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Authorize run' })).toHaveCount(0);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('automation admission errors keep the review page available for explicit retry', async ({
  page,
}) => {
  const { errors } = await fixture(page);
  const pending = pendingAutomation();
  await page.route('**/api/v1/automation-occurrences/occurrence-a', (route) =>
    route.fulfill({ json: pending }),
  );
  await page.route('**/api/v1/automation-occurrences/occurrence-a/authorize', (route) =>
    route.fulfill({
      status: 409,
      json: { detail: 'Project already has an active or queued execution' },
    }),
  );
  await page.goto('/automation-occurrences/occurrence-a');
  await page.getByRole('button', { name: 'Authorize run' }).click();
  await expect(page.getByRole('alert')).toContainText('active or queued');
  await expect(page.getByRole('button', { name: 'Authorize run' })).toBeEnabled();
  await expect(page).toHaveURL(/automation-occurrences\/occurrence-a$/);
  expect(errors).toEqual([]);
});

test('dismissing an approval retains it in history without launching a run', async ({ page }) => {
  const { writes, errors } = await fixture(page);
  let item = pendingAutomation();
  await page.route('**/api/v1/automation-occurrences/occurrence-a', (route) =>
    route.fulfill({ json: item }),
  );
  await page.route('**/api/v1/automation-occurrences/occurrence-a/dismiss', (route) => {
    writes.push({ path: '/automation-occurrences/occurrence-a/dismiss', method: 'POST', body: {} });
    item = { ...item, status: 'dismissed' };
    return route.fulfill({ json: item });
  });
  await page.goto('/automation-occurrences/occurrence-a');
  await page.getByRole('button', { name: 'Dismiss', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Authorize run' })).toHaveCount(0);
  await expect(page.getByText('dismissed', { exact: true })).toBeVisible();
  expect(writes.map((w) => w.path)).toEqual(['/automation-occurrences/occurrence-a/dismiss']);
  expect(errors).toEqual([]);
});
