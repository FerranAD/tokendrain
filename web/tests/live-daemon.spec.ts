import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const origin = process.env.TOKENDRAIN_LIVE_URL;
const tokenFile = process.env.TOKENDRAIN_LIVE_TOKEN_FILE;

test.use({ trace: 'off' });

test('live daemon: persistent project, secrets, disks, run report, schedule, and settings', async ({
  page,
}) => {
  test.skip(!origin || !tokenFile, 'Opt in with a dedicated mock daemon URL and admin-token file.');
  test.setTimeout(90_000);
  const base = new URL(origin!).origin;
  const headers = { Origin: base, 'X-Tokendrain-Request': '1' };
  const login = await page.request.post(`${base}/api/v1/session`, {
    headers,
    data: { token: readFileSync(tokenFile!, 'utf8').trim() },
  });
  expect(login.status()).toBe(200);
  const system = await page.request.get(`${base}/api/v1/system`);
  expect(
    (await system.json()).backend,
    'Live smoke only operates against explicit mock state',
  ).toBe('mock');
  const errors: Error[] = [];
  page.on('pageerror', (error) => errors.push(error));
  const name = `Browser integration ${Date.now()}`;
  let projectId: string | undefined;
  let scheduleId: string | undefined;
  let runId: string | undefined;
  try {
    await page.goto(base);
    await expect(page.getByRole('heading', { name: 'Usage & runs' })).toBeVisible();
    await page.getByRole('button', { name: 'New project' }).click();
    await page.getByLabel('Project name', { exact: true }).fill(name);
    await page
      .getByLabel('What should the agent achieve?')
      .fill('Verify the real UI and API integration using the explicit mock backend.');
    await page.getByRole('button', { name: 'Create project', exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+$/);
    projectId = new URL(page.url()).pathname.split('/').at(-1)!;

    await page.getByRole('button', { name: 'Kanban', exact: true }).click();
    await page.getByRole('button', { name: 'Add task', exact: false }).first().click();
    await page.getByLabel('Title', { exact: true }).fill('Verify run checkpoint');
    await page.getByRole('button', { name: 'Save task' }).click();
    await expect(page.locator('.task-card')).toContainText('Verify run checkpoint');
    await page.getByRole('button', { name: 'Next run feedback', exact: true }).click();
    await page
      .getByRole('textbox', { name: 'Feedback for the next run' })
      .fill('Inspect current state and preserve user edits.');
    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect(page.getByRole('status')).toContainText('Changes saved');

    await page.getByRole('button', { name: 'Secrets', exact: true }).click();
    await page.getByRole('button', { name: 'Add secret' }).click();
    await page.getByLabel('Variable name').fill('TD_BROWSER_TEST');
    await page.getByLabel('Secret value', { exact: true }).fill('test-only-no-provider-access');
    await page
      .getByLabel('Purpose and allowed use')
      .fill('A synthetic value for UI integration checks.');
    await page.getByRole('button', { name: 'Save secret' }).click();
    await expect(page.locator('.secret-list code')).toContainText('TD_BROWSER_TEST');
    await page
      .getByRole('textbox', { name: '.env content' })
      .fill('TD_BROWSER_ENV=test-only-env-value');
    await page.getByLabel('Purpose of TD_BROWSER_ENV').fill('Synthetic dotenv import check.');
    await page.getByRole('button', { name: 'Import 1 secret' }).click();
    await expect(page.getByRole('textbox', { name: '.env content' })).toHaveValue('');

    await page.getByRole('button', { name: 'VM storage', exact: true }).click();
    await page.getByLabel('New capacity, GiB').fill('50');
    await page.getByRole('button', { name: 'Resize', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: 'VM storage resized' })).toBeVisible();

    await page.getByRole('link', { name: 'Prepare run' }).click();
    await page.getByRole('checkbox', { name: 'Elapsed runtime reaches' }).check();
    await page.getByRole('spinbutton', { name: 'Runtime limit in hours' }).fill('0.1');
    await page.getByRole('button', { name: 'Start run', exact: false }).click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/);
    runId = new URL(page.url()).pathname.split('/').at(-1)!;
    await expect(page.locator('.page-heading .status-blocked')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { name: 'Last agent checkpoint' })).toBeVisible();
    await expect(page.locator('.report')).toContainText(/simulation|simulated|mock/i);
    await page.getByRole('button', { name: 'Activity' }).click();
    await expect(page.locator('.log-row').first()).toBeVisible();
    await page.screenshot({ path: 'test-results/live-daemon-run.png', fullPage: true });

    await page.goto(`${base}/projects/${projectId}`);
    await page.getByRole('button', { name: 'History', exact: true }).click();
    await expect(page.locator('tbody .status-blocked')).toBeVisible();
    await page.getByRole('button', { name: 'Tasks', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Shared task log' })).toHaveValue(
      /Create persistent project/,
    );
    await page.getByRole('button', { name: 'GitHub', exact: true }).click();
    await expect(
      page.getByText('Configure your GitHub App before attaching a repository.'),
    ).toBeVisible();

    await page.goto(`${base}/prepare?project=${projectId}`);
    await page.getByRole('button', { name: 'Save a schedule' }).click();
    await page.getByLabel('Schedule name').fill(name);
    await page.getByRole('textbox', { name: /^Timezone/ }).fill('Europe/Madrid');
    await page.getByRole('button', { name: 'Create schedule' }).click();
    await expect(page).toHaveURL(/\/schedules$/);
    const schedule = page.locator('.schedule-list > section').filter({ hasText: name });
    await schedule.getByRole('button', { name: 'Pause', exact: true }).click();
    await expect(schedule.locator('.status-paused')).toBeVisible();
    const schedules = await (await page.request.get(`${base}/api/v1/schedules`)).json();
    scheduleId = schedules.find((item: { name: string }) => item.name === name).id;
    await schedule.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.getByRole('textbox', { name: /^Cron expression/ }).fill('0 4 * * *');
    await page.getByRole('button', { name: 'Save schedule' }).click();
    await expect(schedule.getByText('0 4 * * *', { exact: true })).toBeVisible();

    await page.goto(`${base}/settings`);
    await expect(page.getByRole('heading', { name: 'System & execution defaults' })).toBeVisible();
    await page.getByRole('button', { name: 'Save defaults' }).click();
    await expect(page.getByRole('status')).toContainText('Execution defaults saved');
    expect(errors).toEqual([]);
  } finally {
    if (scheduleId)
      await page.request.delete(`${base}/api/v1/schedules/${scheduleId}`, { headers });
    if (runId)
      await page.request.post(`${base}/api/v1/runs/${runId}/cancel`, { headers, data: {} });
    if (projectId) await page.request.delete(`${base}/api/v1/projects/${projectId}`, { headers });
  }
});
