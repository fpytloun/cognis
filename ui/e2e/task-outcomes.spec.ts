import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture, TASK_ID, HEAVY_STEP_RUN_ID } from './task-cockpit-fixture';
import type { Deliverable } from '../src/lib/types/api';

test.beforeEach(async ({ page }) => {
  await page.routeWebSocket('**/api/ws', socket => {
    socket.send(JSON.stringify({ type: 'authenticated' }));
    socket.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
    });
  });
});

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`task output freshness at ${viewport.width}px`, async ({ page }, info) => {
    await page.setViewportSize(viewport);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/auth/me', route => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@cognis-e2e.localdev.me', name: 'Admin', role: 'admin',
        is_active: true, force_password_change: false, mfa_enabled: false }),
    }));
    let document: Deliverable = {
      deliverable_id: 'dlv-stage39-final', step_run_id: HEAVY_STEP_RUN_ID,
      version: 1, attempt_number: 1, lineage_id: 'dlv-stage39-final', revision: 1,
      content: '# Release decision\n\nThe previous release report remains readable.',
      title: 'Release decision', format: 'markdown', target: 'none', outputs: {},
      status: 'approved', evaluator_feedback: null, created_at: null, updated_at: null,
      task_freshness: { state: 'current', replacement_status: null, label: 'Current output' },
    };
    const fixture = await installTaskCockpitFixture(page, { taskOutput: () => document });
    fixture.setStatus('running');
    await page.goto(`/tasks/${TASK_ID}`);
    const result = page.getByTestId('task-final-result');
    await expect(result).toBeVisible();
    await result.getByRole('button', { name: 'Expand document' }).click();
    await expect(result.getByText('The previous release report remains readable.')).toBeVisible();
    document = { ...document, task_freshness: {
      state: 'stale', replacement_status: 'running', label: 'Stale — revision 1, new revision in progress',
    } };
    await expect(result.getByText('Stale — revision 1, new revision in progress')).toBeVisible({ timeout: 20_000 });
    await expect(result.getByText('The previous release report remains readable.')).toHaveCount(0);
    await expect(result.getByRole('button', { name: 'Expand document' })).toHaveAttribute('aria-expanded', 'false');
    await result.scrollIntoViewIfNeeded();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await page.screenshot({ path: info.outputPath(`stale-${viewport.width}.png`) });
    await result.getByRole('button', { name: 'Expand document' }).click();
    await expect(result.getByText('The previous release report remains readable.')).toBeVisible();
    await result.scrollIntoViewIfNeeded();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await page.screenshot({ path: info.outputPath(`expanded-${viewport.width}.png`) });
    document = { ...document, deliverable_id: 'dlv-stage39-replacement', lineage_id: 'dlv-stage39-replacement', revision: 1,
      content: '# Release decision\n\nThe replacement report is ready.',
      task_freshness: { state: 'current', replacement_status: null, label: 'Current output' },
    };
    await expect(result.getByText(/Stale —/)).toHaveCount(0, { timeout: 20_000 });
    await result.getByRole('button', { name: 'Expand document' }).click();
    await expect(page.getByText('The replacement report is ready.')).toBeVisible();
    await result.scrollIntoViewIfNeeded();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await page.screenshot({ path: info.outputPath(`current-${viewport.width}.png`) });
    expect(await page.evaluate(() => window.document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(fixture.unmockedRequests()).toEqual([]);
    expect(errors).toEqual([]);
  });
}

test('task settings preserve finish instructions and enforce a direct-only inbox', async ({ page }) => {
  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      email: 'admin@cognis-e2e.localdev.me', name: 'Admin', role: 'admin',
      is_active: true, force_password_change: false, mfa_enabled: false,
    }),
  }));
  const fixture = await installTaskCockpitFixture(page);
  fixture.setStatus('draft');
  let submitted: Record<string, unknown> | null = null;
  await page.route(`**/api/v1/tasks/${TASK_ID}`, async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    submitted = route.request().postDataJSON();
    return route.fulfill({ contentType: 'application/json', body: '{}' });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: 'Configure', exact: true }).click();
  const completion = page.locator('select').filter({ has: page.locator('option[value="direct"]') });
  await completion.selectOption('default');
  const instructions = page.getByRole('textbox', { name: /Finish instructions/ });
  await instructions.fill('On failure, inspect and verify one authorized repair.');
  const delivery = page.getByRole('combobox').filter({
    has: page.locator('option[value="in_app_notifications"]'),
  });
  await delivery.selectOption('in_app_notifications');
  await expect(instructions).toHaveCount(0);
  await expect(completion).toHaveValue('direct');
  await expect(completion).toBeDisabled();
  await delivery.selectOption('preferred_channel');
  await completion.selectOption('default');
  await expect(instructions).toHaveValue('On failure, inspect and verify one authorized repair.');
  await page.getByRole('button', { name: 'Save task', exact: true }).click();
  await expect.poll(() => submitted).toMatchObject({
    on_finish_instructions: 'On failure, inspect and verify one authorized repair.',
    completion_mode_family: 'default',
    delivery_mode: 'preferred_channel',
  });
});

test('task attention fetch failure is visible and retry restores freshness', async ({ page }) => {
  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      email: 'admin@cognis-e2e.localdev.me', name: 'Admin', role: 'admin',
      is_active: true, force_password_change: false, mfa_enabled: false,
    }),
  }));
  await installTaskCockpitFixture(page);
  let unavailable = true;
  await page.route('**/api/v1/notifications?*', (route) => route.fulfill({
    status: unavailable ? 503 : 200,
    contentType: 'application/json',
    body: JSON.stringify(unavailable ? { error: { code: 'unavailable', message: 'Attention service unavailable' } } : []),
  }));
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByText(/Task requests may be unavailable or stale/)).toBeVisible();
  unavailable = false;
  await page.getByRole('button', { name: 'Retry requests', exact: true }).click();
  await expect(page.getByText(/Task requests may be unavailable or stale/)).toHaveCount(0);
});
