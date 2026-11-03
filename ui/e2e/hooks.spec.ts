import { expect, test, type Page } from '@playwright/test';
import { emptyHook, type AgentHook } from '../src/lib/hooks';

async function setup(page: Page, role = 'user') {
  const hooks: AgentHook[] = [
    { ...emptyHook(), hook_id: 'system:recall-reminder', revision: 1, bundled: true,
      name: 'Recall relevant memories', description: 'Consider memory search when past decisions or preferences matter.',
      prompt: 'Search relevant memories before continuing. Do not search unrelated context.' },
    { ...emptyHook(), hook_id: 'system:working-reminder', revision: 1, bundled: true,
      name: 'Periodic working reminder', description: 'Recheck scope, assumptions, and verification every ten model cycles.',
      trigger: 'before_model_cycle', condition: '{{ cycle.index is divisibleby(10) }}',
      prompt: 'Recheck scope, assumptions, authorization, and validation before continuing.' },
    { ...emptyHook(), hook_id: 'system:memory-consolidation', revision: 1, bundled: true,
      name: 'Consolidate useful memories', description: 'Preserve durable knowledge without duplicating existing memories.',
      trigger: 'conversation_idle', mode: 'fork', idle_seconds: 900,
      prompt: 'Review the conversation and preserve durable memories. Avoid secrets and transient status.' },
    { ...emptyHook(), hook_id: 'system:self-improvement', revision: 1, bundled: true,
      name: 'Learn reusable skills', description: 'Turn verified discoveries into reusable Cognis skills. Creates or updates versioned skills.',
      trigger: 'conversation_idle', mode: 'fork', idle_seconds: 1800,
      condition: '{{ work.has_work or work.successful_tool_calls > 0 }}',
      prompt: 'Identify verified, reusable lessons from the completed work.\n\nInspect existing skills before creating a new one. Prefer focused updates, preserve safeguards, and verify the published version. Make no changes when there is nothing useful to learn.' }
  ];
  const state = { hooks, failSave: false, failRuns: false, failRegistry: false, runRequests: 0, writes: 0 };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    let status = 200;
    if (path === '/api/auth/me') body = { email: 'hooks@cognis.local', role, name: 'Hooks' };
    else if (path === '/api/bootstrap-status') body = { has_users: true, needs_bootstrap: false };
    else if (path === '/api/health') body = { status: 'ok', services: {}, llm_available: true };
    else if (path.endsWith('/sidebar')) body = {
      agents: [], agent_direct_chats: [], conversations: { items: [], next_cursor: null, has_more: false }
    };
    else if (path === '/api/v1/hooks' && route.request().method() === 'GET') {
      status = state.failRegistry ? 500 : 200;
      body = state.failRegistry ? { detail: 'Registry unavailable' } : { items: state.hooks };
    } else if (path === '/api/v1/hooks/runs') {
      state.runRequests++;
      status = state.failRuns ? 500 : 200;
      body = state.failRuns ? { detail: 'History unavailable' } : { items: [] };
    } else if (path.startsWith('/api/v1/hooks') && route.request().method() !== 'GET') {
      state.writes++;
      if (state.failSave) { status = 409; body = { detail: 'Hook changed. Reload before saving again.' }; }
      else if (route.request().method() === 'DELETE') {
        state.hooks = state.hooks.filter(hook => hook.hook_id !== path.split('/').slice(-1)[0]);
        body = { ok: true };
      } else {
        const index = state.hooks.findIndex(hook => hook.hook_id === path.split('/').slice(-1)[0]);
        const saved = {
          ...route.request().postDataJSON(), hook_id: index < 0 ? 'hook-created' : state.hooks[index].hook_id,
          revision: index < 0 ? 1 : state.hooks[index].revision + 1, bundled: false
        };
        if (index < 0) state.hooks.push(saved); else state.hooks[index] = saved;
        body = saved;
      }
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  return state;
}

for (const width of [390, 1280]) {
  test(`Tools hooks: cards, preview, customize, edit and save at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 960 });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const state = await setup(page);
    await page.goto('/hooks');
    await expect(page).toHaveURL(/\/tools\?tab=hooks$/);
    await expect(page.getByRole('navigation', { name: 'Tool registries' }).getByRole('link', { name: 'Hooks' })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('a[href="/hooks"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Learn reusable skills', exact: true })).toBeVisible();
    expect(state.runRequests).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`hooks-cards-${width}.png`), fullPage: true });
    await page.getByRole('button', { name: 'Learn reusable skills', exact: true }).click();
    let dialog = page.getByRole('dialog', { name: 'Learn reusable skills', exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('After 30 min idle', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('textbox')).toHaveCount(0);
    await expect(dialog.getByRole('button', { name: 'Save hook' })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`hooks-preview-${width}.png`), fullPage: true });
    await dialog.getByRole('button', { name: 'Customize', exact: true }).click();
    dialog = page.getByRole('dialog', { name: 'New hook', exact: true });
    await dialog.getByLabel('Name', { exact: true }).fill('My learning hook');
    await dialog.getByLabel('Instructions', { exact: true }).fill('Check verified lessons and update existing skills.');
    await dialog.getByLabel('Idle time (minutes)', { exact: true }).fill('60');
    await page.screenshot({ path: test.info().outputPath(`hooks-editor-${width}.png`), fullPage: true });
    await dialog.getByRole('button', { name: 'Save hook', exact: true }).click();
    dialog = page.getByRole('dialog', { name: 'My learning hook', exact: true });
    await expect(dialog.getByText('After 60 min idle', { exact: true })).toBeVisible();
    expect(state.hooks.find(hook => hook.hook_id === 'hook-created')?.idle_seconds).toBe(3600);
    expect(state.hooks.find(hook => hook.hook_id === 'system:self-improvement')?.idle_seconds).toBe(1800);
    await dialog.getByRole('button', { name: 'Edit hook', exact: true }).click();
    dialog = page.getByRole('dialog', { name: 'Edit hook', exact: true });
    await dialog.getByRole('combobox', { name: 'Trigger', exact: true }).selectOption('turn_completed');
    await dialog.getByRole('combobox', { name: 'Execution', exact: true }).selectOption('follow_up');
    await dialog.getByRole('button', { name: 'Save hook', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'My learning hook', exact: true })).toBeVisible();
    expect(state.hooks.find(hook => hook.hook_id === 'hook-created')?.mode).toBe('follow_up');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByLabel('Search hooks', { exact: true }).fill('My learning');
    await expect(page.getByRole('button', { name: 'My learning hook', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Learn reusable skills', exact: true })).toHaveCount(0);
    await page.getByLabel('Search hooks', { exact: true }).fill('');
    await page.getByRole('navigation', { name: 'Tool registries' }).getByRole('link', { name: /Skills/ }).click();
    await expect(page).toHaveURL(/tab=skills/);
    await page.goBack();
    await expect(page.getByRole('button', { name: 'Learn reusable skills', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('button', { name: 'My learning hook', exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test('draft survives failed save, Escape, and tab navigation; history errors do not hide hooks', async ({ page }) => {
  const state = await setup(page);
  await page.goto('/tools?tab=skills');
  await expect(page.getByPlaceholder('Search skills...')).toBeVisible();
  await page.getByRole('navigation', { name: 'Tool registries' }).getByRole('link', { name: 'Hooks', exact: true }).click();
  await expect(page).toHaveURL(/tab=hooks/);
  await page.getByRole('button', { name: 'New hook', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New hook', exact: true });
  await dialog.getByLabel('Name', { exact: true }).fill('Draft reminder');
  await dialog.getByLabel('Instructions', { exact: true }).fill('Keep my draft.');
  await expect(dialog.getByText('Unsaved changes', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  const confirmation = page.getByRole('dialog', { name: 'Discard unsaved changes?', exact: true });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog.getByLabel('Instructions', { exact: true })).toHaveValue('Keep my draft.');
  page.once('dialog', nativeDialog => nativeDialog.dismiss());
  await page.evaluate(() => history.back());
  await expect(page).toHaveURL(/tab=hooks/);
  await expect(dialog.getByLabel('Instructions', { exact: true })).toHaveValue('Keep my draft.');
  state.failSave = true;
  await dialog.getByRole('button', { name: 'Save hook', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Hook changed');
  await expect(dialog.getByLabel('Name', { exact: true })).toHaveValue('Draft reminder');
  state.failSave = false;
  await dialog.getByRole('button', { name: 'Save hook', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Draft reminder', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  state.failRuns = true;
  await page.getByRole('button', { name: 'Recent runs', exact: false }).click();
  await expect(page.getByRole('alert')).toContainText('History unavailable');
  await expect(page.getByRole('button', { name: 'Draft reminder', exact: true })).toBeVisible();
  state.failRuns = false;
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByText('No runs yet.', { exact: false })).toBeVisible();
});

test('viewer can inspect bundled hooks but cannot modify them', async ({ page }) => {
  const state = await setup(page, 'viewer');
  await page.goto('/tools?tab=hooks');
  await page.getByRole('button', { name: 'Learn reusable skills', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Learn reusable skills', exact: true });
  await expect(dialog.getByRole('button', { name: 'Customize' })).toHaveCount(0);
  await expect(dialog.getByRole('button', { name: 'Edit hook' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'New hook', exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape');
  expect(state.writes).toBe(0);
});
