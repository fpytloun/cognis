import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';
import { installLibraryRoutes, libraryCatalog } from './library-fixture';
import type { PaletteItem } from '../src/lib/palette/model';
import type { Schedule } from '../src/lib/types/api';

// HTTP and image fixtures must not be bypassed by an activated service worker.
test.use({ serviceWorkers: 'block' });

const common = { project_id: null, agent_id: 'agent-stage39', agent_name: 'LaForge', agent_avatar_url: '/api/v1/images/palette-avatar', status: 'active', format: null, updated_at: '2026-10-23T12:00:00Z' };
const options = (page: Page) => page.getByRole('dialog', { name: 'Command palette', exact: true }).getByRole('option');
const schedule: Schedule = {
  schedule_id: 'palette-review', name: 'Weekly command palette review', description: 'Review palette latency and navigation.',
  schedule_type: 'cron', cron_expr: '0 9 * * 1', interval_seconds: null, one_shot_at: null, timezone: 'UTC',
  agent_id: 'agent-stage39', workflow_id: null, project_id: null, skill_id: null,
  task_template: { title: 'Review command palette', intent: 'Review performance.' },
  enabled: true, max_concurrent_runs: 1, delete_after_run: false, retry_failed_tasks: false,
  fail_paused_task_on_next_fire: false, completion_mode_family: 'default', allow_silent_completion: false,
  interaction_mode_override: null, session_policy: null, last_fired_at: null, next_fire_at: '2026-10-26T09:00:00Z',
  last_run_status: null, consecutive_errors: 0, disabled_reason: null, created_by: 'admin@cognis-e2e.localdev.me',
  created_at: '2026-10-23T12:00:00Z', updated_at: '2026-10-23T12:00:00Z', human_schedule: 'Every Monday at 09:00',
  is_expired: false, expiration_grace_until: null,
};
const catalog: PaletteItem[] = [
  { ...common, kind: 'conversation', id: 'conv-task-chat', title: 'Command palette — design & performance', href: '/chat/conv-task-chat' },
  { ...common, kind: 'deliverable', id: 'dlv_library_design', title: 'Command palette — interaction specification', format: 'markdown', href: '/library/deliverable/dlv_library_design' },
  { ...common, kind: 'artifact', id: 'palette-interface', origin: 'agent', title: 'Command palette — interface exploration.png', format: 'image/png', size_bytes: 12000, href: '/library/artifact/palette-interface' },
  { ...common, kind: 'task', id: TASK_ID, title: 'Implement command palette', status: 'in progress', href: `/tasks/${TASK_ID}` },
  { ...common, kind: 'artifact', id: 'palette-requirements', origin: 'user', producer_name: 'Palette reviewer', agent_name: null, agent_avatar_url: null, title: 'Command palette requirements.pdf', format: 'application/pdf', href: '/library/artifact/palette-requirements' },
  { ...common, kind: 'artifact', id: 'palette-benchmark', title: 'Command palette latency results.csv', format: 'text/csv', href: '/library/artifact/palette-benchmark' },
  { ...common, kind: 'schedule', id: 'palette-review', title: 'Weekly command palette review', status: 'enabled', href: '/schedules/palette-review' },
];

async function setup(page: Page, platform = 'MacIntel') {
  await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true, dashboardConversation: true });
  await installLibraryRoutes(page, libraryCatalog);
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: {
    email: 'admin@cognis-e2e.localdev.me', name: 'Palette reviewer', role: 'admin',
  } }));
  await page.route('**/api/v1/notifications**', (route) => route.fulfill({
    json: new URL(route.request().url()).pathname === '/api/v1/notifications' ? [] : { count: 0 },
  }));
  await page.route('**/api/v1/inbox**', (route) => route.fulfill({
    json: { items: [], unread_count: 0, next_cursor: null },
  }));
  await page.route('**/api/v1/schedules**', (route) => route.fulfill({
    json: new URL(route.request().url()).pathname === '/api/v1/schedules/palette-review' ? schedule : [],
  }));
  await page.addInitScript((platform) => {
    localStorage.setItem('cognis.theme.palette', 'dark');
    Object.defineProperty(navigator, 'platform', { configurable: true, value: platform });
  }, platform);
  await page.route('**/api/v1/search/palette?**', (route) => {
    const params = new URL(route.request().url()).searchParams;
    const q = params.get('q')?.toLowerCase().replace('pallete', 'palette') ?? '';
    const types = params.getAll('types');
    const items = catalog.filter((row) => (!types.length || types.includes(row.kind))
      && q.split(/\s+/).every((word) => row.title.toLowerCase().includes(word)));
    return route.fulfill({ json: { items, next_cursor: null } });
  });
  await page.route('**/api/v1/artifacts/palette-interface/signed-url**', (route) => route.fulfill({ json: {
    url: '/palette-thumbnail.png', expires_at: null,
  } }));
  await page.route(/\/(?:palette-thumbnail\.png|api\/v1\/images\/palette-avatar)$/, async (route) => {
    // A small, deterministic synthetic raster; never load a remote image for the fixture.
    const image = await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 120; canvas.height = 120;
      const context = canvas.getContext('2d')!;
      const gradient = context.createLinearGradient(0, 0, 120, 120);
      gradient.addColorStop(0, '#d6d3f0'); gradient.addColorStop(.6, '#7c80b9'); gradient.addColorStop(1, '#35394f');
      context.fillStyle = gradient; context.fillRect(0, 0, 120, 120);
      context.fillStyle = '#252a3e'; context.beginPath(); context.roundRect(13, 35, 94, 52, 8); context.fill();
      context.fillStyle = '#e4e7f5'; context.fillRect(24, 47, 62, 3);
      context.fillStyle = '#8894ba'; context.fillRect(24, 58, 70, 2); context.fillRect(24, 68, 48, 2);
      return canvas.toDataURL('image/png').split(',')[1];
    });
    return route.fulfill({ contentType: 'image/png', body: Buffer.from(image, 'base64') });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await expect(page.getByTestId('control-center')).toBeVisible();
}

async function show(page: Page, query = '') {
  await page.keyboard.press('Meta+k');
  const input = page.getByRole('combobox', { name: 'Search Cognis' });
  await expect(input).toBeFocused();
  if (query) await input.fill(query);
  return input;
}

test('search history recalls queries, restores drafts and preserves result navigation', async ({ page }) => {
  await setup(page);
  for (const query of ['command', 'command palette']) {
    const input = await show(page, query);
    await input.press('Escape');
    await expect(input).toBeHidden();
  }
  const input = await show(page);
  await input.press('ArrowUp');
  await expect(input).toHaveValue('command palette');
  await input.press('ArrowUp');
  await expect(input).toHaveValue('command');
  await input.press('ArrowDown');
  await expect(input).toHaveValue('command palette');
  await input.press('ArrowDown');
  await expect(input).toHaveValue('');
  await input.fill('draft phrase');
  await input.press('Alt+ArrowUp');
  await expect(input).toHaveValue('command palette');
  await input.press('Alt+ArrowDown');
  await expect(input).toHaveValue('draft phrase');
  await input.fill('');
  await input.press('ArrowUp');
  await input.press('ArrowRight');
  await expect(options(page)).toHaveCount(7);
  const active = await input.getAttribute('aria-activedescendant');
  await input.press('ArrowDown');
  await expect(input).not.toHaveAttribute('aria-activedescendant', active!);
  await expect(input).toHaveValue('command palette');
  await input.press('Escape');
  await expect(input).toBeHidden();
  await page.reload();
  await expect(page.getByTestId('control-center')).toBeVisible();
  const reopened = await show(page);
  await reopened.press('ArrowUp');
  await expect(reopened).toHaveValue('command palette');
});

test('clearing recalled search history does not save the current recalled query again', async ({ page }) => {
  await setup(page);
  const input = await show(page, 'command');
  await input.press('Escape');
  await expect(input).toBeHidden();
  await show(page);
  await input.press('ArrowUp');
  await expect(input).toHaveValue('command');
  await page.getByRole('button', { name: 'Clear search history', exact: true }).click();
  await expect(input).toBeFocused();
  await input.press('Escape');
  await expect(input).toBeHidden();
  await page.reload();
  await expect(page.getByTestId('control-center')).toBeVisible();
  await show(page);
  await input.press('ArrowUp');
  await expect(input).toHaveValue('');
  await expect(page.getByRole('button', { name: 'Clear search history', exact: true })).toBeHidden();
});

test('Ctrl+Shift+number opens a match and leaves browser tab shortcuts unhandled', async ({ page }) => {
  await setup(page, 'Win32');
  await page.keyboard.press('Control+k');
  const input = page.getByRole('combobox', { name: 'Search Cognis' });
  await input.fill('command');
  await expect(options(page)).toHaveCount(7);
  await expect(page.locator('.number-hint').first()).toHaveText('Ctrl+Shift+1');
  expect(await input.evaluate(node => node.dispatchEvent(new KeyboardEvent('keydown', {
    key: '1', code: 'Digit1', ctrlKey: true, bubbles: true, cancelable: true,
  })))).toBe(true);
  expect(await input.evaluate(node => node.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'k', code: 'KeyK', metaKey: true, altKey: true, bubbles: true, cancelable: true,
  })))).toBe(true);
  await expect(input).toBeVisible();
  await input.press('Control+Shift+Digit1');
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
});

test.describe('mobile touch launcher', () => {
  test.use({ hasTouch: true });

  test('metadata, producer attribution, stable realtime updates and multi-select type filtering', async ({ page }, testInfo) => {
    let socket: WebSocketRoute | undefined;
    await page.routeWebSocket('**/api/ws', ws => {
      socket = ws;
      ws.send(JSON.stringify({ type: 'authenticated' }));
      ws.onMessage(message => {
        if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
      });
    });
    await setup(page);
    await page.setViewportSize({ width: 390, height: 844 });
    let revoked = false;
    const searches: URL[] = [];
    await page.route('**/api/v1/search/palette?**', route => {
      const url = new URL(route.request().url());
      searches.push(url);
      const q = url.searchParams.get('q')?.toLowerCase() ?? '';
      const types = url.searchParams.getAll('types');
      const rows = catalog.filter(row => (!revoked || row.kind !== 'conversation')
        && (!types.length || types.includes(row.kind))
        && q.split(/\s+/).every(word => row.title.toLowerCase().includes(word)));
      return route.fulfill({ json: { items: rows, next_cursor: null } });
    });
    await page.getByRole('button', { name: 'Open navigation', exact: true }).tap();
    await page.getByRole('dialog', { name: 'Navigation menu', exact: true })
      .getByRole('button', { name: 'Search Cognis', exact: true }).tap();
    const input = page.getByRole('combobox', { name: 'Search Cognis' });
    await input.fill('command');
    await expect(options(page)).toHaveCount(7);
    await expect(options(page).locator('time')).toHaveCount(7);
    await expect(options(page).filter({ hasText: 'Implement command palette' }).getByText('LaForge', { exact: true })).toBeVisible();
    await expect(options(page).first().locator('.result-icon img')).toBeVisible();
    await expect(options(page).filter({ hasText: 'requirements.pdf' })).toContainText('Uploaded by Palette reviewer');
    await expect(options(page).filter({ hasText: 'requirements.pdf' }).locator('.result-agent img')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('spotlight-mobile-metadata.png'), animations: 'disabled' });
    await expect.poll(() => Boolean(socket)).toBe(true);
    const count = searches.filter(url => url.searchParams.get('q') === 'command').length;
    for (const reason of ['executor_state_changed', 'notification_state_changed', 'task_progress_changed', 'schedule_action_changed', 'event_store_session_invalidated', 'chat_scope_changed', 'sidebar_changed']) {
      socket!.send(JSON.stringify({ type: 'scope_invalidated', reason, metadata_only: true }));
    }
    await page.waitForTimeout(400);
    await expect(options(page)).toHaveCount(7);
    expect(searches.filter(url => url.searchParams.get('q') === 'command')).toHaveLength(count);
    await expect(page.getByText('Results changed. Refresh your search.')).toHaveCount(0);
    revoked = true;
    socket!.send(JSON.stringify({ type: 'sidebar_conversation_removed', conversation_id: 'conv-task-chat' }));
    await expect(options(page)).toHaveCount(6);
    await expect(page.getByText('Results changed. Refresh your search.')).toHaveCount(0);
    const filter = page.getByRole('button', { name: 'Filter result types' });
    await filter.tap();
    for (const type of ['Main agents', 'Projects', 'App pages', 'Conversations', 'Schedules', 'Documents', 'Actions', 'Settings']) {
      await page.getByRole('menuitemcheckbox', { name: type, exact: true }).tap();
    }
    await expect(options(page)).toHaveCount(4);
    await expect(filter).toHaveText(/2 types/);
    await page.screenshot({ path: testInfo.outputPath('spotlight-mobile-type-filter.png'), animations: 'disabled' });
    await expect.poll(() => searches[searches.length - 1]!.searchParams.getAll('types').sort())
      .toEqual(['artifact', 'task']);
    await filter.press('ArrowDown');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('menu', { name: 'Result types' })).toBeHidden();
    await expect(page.getByRole('dialog', { name: 'Command palette', exact: true })).toBeVisible();
    await filter.tap();
    await page.getByRole('menuitem', { name: 'All types', exact: true }).tap();
    for (const type of ['Main agents', 'Projects', 'Conversations', 'Tasks', 'Schedules', 'Documents', 'Files']) {
      await page.getByRole('menuitemcheckbox', { name: type, exact: true }).tap();
    }
    const localCount = searches.length;
    await input.tap();
    await input.fill('new');
    await expect(options(page)).toHaveCount(1);
    await expect(options(page)).toContainText('New conversation');
    await page.waitForTimeout(400);
    expect(searches).toHaveLength(localCount);
    await page.touchscreen.tap(4, 820);
    await expect(page.getByRole('dialog', { name: 'Command palette', exact: true })).toBeHidden();
  });

  for (const route of ['/', '/chat/conv-task-chat']) {
    test(`menu search opens and dismisses by touch on ${route}`, async ({ page }, testInfo) => {
      await setup(page);
      await page.setViewportSize({ width: 390, height: 844 });
      if (route !== '/') {
        // The static preview supports dynamic chat routes through client navigation.
        await page.getByRole('button', { name: 'Open navigation', exact: true }).tap();
        await page.getByRole('dialog', { name: 'Navigation menu', exact: true })
          .getByRole('button', { name: 'Search Cognis', exact: true }).tap();
        await page.getByRole('combobox', { name: 'Search Cognis' }).fill('design & performance');
        await expect(options(page)).toHaveCount(1);
        await options(page).first().getByRole('link').tap();
        await expect(page).toHaveURL((url) => url.pathname === route);
      }
      const opener = page.getByRole('button', { name: 'Open navigation', exact: true });
      await opener.tap();
      const menu = page.getByRole('dialog', { name: 'Navigation menu', exact: true });
      await expect(menu).toBeVisible();
      const search = menu.getByRole('button', { name: 'Search Cognis', exact: true });
      await expect(search).toBeVisible();
      if (route === '/') {
        await page.screenshot({ path: testInfo.outputPath('spotlight-mobile-menu.png'), animations: 'disabled' });
      }
      await search.tap();
      await expect(menu).toBeHidden();
      const palette = page.getByRole('dialog', { name: 'Command palette', exact: true });
      const input = palette.getByRole('combobox', { name: 'Search Cognis' });
      await expect(palette).toBeVisible();
      await expect(input).toBeFocused();
      await input.tap();
      await expect(palette).toBeVisible();
      await input.fill('command');
      await expect(options(page)).toHaveCount(7);
      if (route === '/') {
        await expect(options(page).first().locator('.result-icon img')).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath('spotlight-mobile-touch-search.png'), animations: 'disabled' });
      }
      // Cover blank centering space as well as the outer backdrop.
      await page.touchscreen.tap(route === '/' ? 195 : 4, route === '/' ? 12 : 820);
      await expect(palette).toBeHidden();
      await expect(opener).toBeFocused();
      await expect(menu).toBeHidden();
      await expect(page).toHaveURL((url) => url.pathname === route);
    });
  }
});

test('pinned desktop search preserves server diversity and an idle blank snapshot', async ({ page }, testInfo) => {
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/api/ws', ws => {
    socket = ws;
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await setup(page);
  const footer = page.getByTestId('pinned-search-and-notifications');
  const search = footer.getByRole('button', { name: 'Search Cognis', exact: true });
  const notifications = footer.getByRole('link', { name: 'Notifications', exact: true });
  await expect(search).toBeVisible();
  expect((await search.boundingBox())!.y).toBeLessThan((await notifications.boundingBox())!.y);
  let requests = 0;
  const file = (index: number): PaletteItem => ({
    ...common, kind: 'artifact', id: `coffee-file-${index}`, title: 'coffee', format: 'text/plain',
    href: `/library/artifact/coffee-file-${index}`,
  });
  const task: PaletteItem = { ...common, kind: 'task', id: TASK_ID, title: 'Smart coffee replenishment', href: `/tasks/${TASK_ID}` };
  await page.route('**/api/v1/search/palette?**', route => {
    requests++;
    return route.fulfill({ json: {
      items: [file(0), file(1), task, ...Array.from({ length: 9 }, (_, i) => file(i + 2))],
      next_cursor: 'coffee-continuation',
    } });
  });
  const input = await show(page);
  await expect(options(page)).toHaveCount(13); // One local action and twelve server results.
  await input.press('ArrowDown');
  const active = await input.getAttribute('aria-activedescendant');
  await expect.poll(() => Boolean(socket)).toBe(true);
  const before = requests;
  for (const reason of ['chat_scope_changed', 'sidebar_changed'])
    socket!.send(JSON.stringify({ type: 'scope_invalidated', reason, metadata_only: true }));
  await page.waitForTimeout(450);
  expect(requests).toBe(before); // No idle continuation or metadata-triggered reorder.
  await expect(input).toHaveAttribute('aria-activedescendant', active!);
  await input.fill('coffee');
  await expect(options(page)).toHaveCount(12);
  await expect(options(page).nth(2)).toContainText('Smart coffee replenishment');
  await page.screenshot({ path: testInfo.outputPath('spotlight-balanced-launcher.png'), animations: 'disabled' });
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click();
  await expect(search).toBeVisible();
  expect((await search.boundingBox())!.y).toBeLessThan((await notifications.boundingBox())!.y);
  await search.click();
  await expect(page.getByRole('combobox', { name: 'Search Cognis' })).toBeFocused();
});

test('main-agent activation resolves only on selection and app pages use canonical navigation', async ({ page }) => {
  await setup(page);
  let resolutions = 0;
  await page.route('**/api/v1/conversations/resolve', route => {
    resolutions++;
    expect(route.request().postDataJSON()).toEqual({
      agent_id: 'agent-stage39', context_type: 'web', scope: 'agent_direct',
    });
    return route.fulfill({ json: {
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39',
      agent_profile_id: null, context: { type: 'web', scope: 'agent_direct' },
    } });
  });
  await page.route('**/api/v1/search/palette?**', route => route.fulfill({ json: {
    items: new URL(route.request().url()).searchParams.get('q') === 'laforge' ? [{
      ...common, kind: 'agent', id: 'agent-stage39', title: 'LaForge',
      href: '/chat?agent_id=agent-stage39&scope=agent_direct',
    }] : [], next_cursor: null,
  } }));
  await show(page, 'laforge');
  await expect(options(page)).toHaveCount(1);
  await expect(options(page).first().locator('.result-icon img')).toBeVisible();
  expect(resolutions).toBe(0);
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/chat\/conv-task-chat$/);
  expect(resolutions).toBe(1);
  await show(page, 'dashboard');
  await expect(options(page)).toHaveCount(1);
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL((url) => url.pathname === '/');
  await expect(page.getByTestId('control-center')).toBeVisible();
});

test('project activation overrides the previously selected project tab with Overview', async ({ page }, testInfo) => {
  await setup(page);
  const project = {
    project_id: 'palette-project', name: 'Palette project', description: '', status: 'active',
    instructions: '', sources: [], workflow_ids: [], agents: [], grants: [], caller_role: 'owner',
    is_readonly_for_caller: false, is_shared_with_me: false, active_schedule_count: 0,
    icon: 'cognis', color: '#fbbf24', avatar_url: null,
  };
  await page.route('**/api/v1/projects/**', route => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith('/page') ? { items: [], cursor: null, has_more: false }
      : path.endsWith('/agents') ? [] : path.endsWith('/activity') ? { deliverables: [], artifacts: [] } : project });
  });
  await page.route('**/api/v1/search/palette?**', route => route.fulfill({ json: {
    items: [{
      ...common, kind: 'project', id: 'palette-project', project_id: 'palette-project',
      title: 'Palette project', href: '/projects/palette-project?tab=overview',
      project_avatar_url: null, project_icon: 'cognis', project_color: '#fbbf24',
    }], next_cursor: null,
  } }));
  await show(page, 'palette project');
  await expect(options(page)).toHaveCount(1);
  await expect(options(page).first().getByTestId('project-library-icon')).toHaveCSS(
    'background-color', 'rgb(251, 191, 36)',
  );
  await page.getByTestId('command-palette').screenshot({
    path: testInfo.outputPath('spotlight-project-icon.png'), animations: 'disabled',
  });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/palette-project\?tab=overview$/);
  await expect(page.getByRole('heading', { name: 'Palette project', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/palette-project\?tab=settings$/);
  await show(page, 'palette project');
  await expect(options(page)).toHaveCount(1);
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/palette-project\?tab=overview$/);
  await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
});

test('Spotlight appearance, typo search, arrows and instant dashboard activation', async ({ page }, testInfo) => {
  await setup(page);
  const requests: string[] = [];
  page.on('request', (request) => { if (request.url().includes('/search/palette')) requests.push(request.url()); });
  const input = await show(page, 'command pallete');
  await expect(options(page)).toHaveCount(7);
  await expect(page.locator('.palette-option.selected')).toContainText('Command palette — design');
  await expect(options(page).first().locator('.result-icon img')).toBeVisible();
  await expect.poll(() => page.getByTestId('command-palette')
    .evaluate((node) => getComputedStyle(node).backdropFilter)).not.toBe('none');
  await page.screenshot({ path: testInfo.outputPath('spotlight-search-dark.png'), animations: 'disabled' });
  await input.press('ArrowDown');
  await expect(page.locator('.palette-option.selected')).toContainText('interaction specification');
  await input.press('Enter');
  await expect(page).toHaveURL((url) => url.pathname === '/');
  await expect(page.getByTestId('workspace-window-deliverable:dlv_library_design')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Release readiness', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('spotlight-dashboard-document.png') });
  await show(page, 'command');
  await expect(options(page)).toHaveCount(7);
  await expect(page.locator('.number-hint').first()).toHaveText('⌃⇧1');
  const reserved = await page.getByRole('combobox', { name: 'Search Cognis' }).evaluate(node => [
    node.dispatchEvent(new KeyboardEvent('keydown', {
      key: '1', code: 'Digit1', metaKey: true, bubbles: true, cancelable: true,
    })),
    node.dispatchEvent(new KeyboardEvent('keydown', {
      key: '#', code: 'Digit3', metaKey: true, shiftKey: true, bubbles: true, cancelable: true,
    })),
  ]);
  expect(reserved).toEqual([true, true]);
  await page.keyboard.press('Control+Shift+Digit1');
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  await expect(page).toHaveURL((url) => url.pathname === '/');
  // Warm cached results render without a new transport request.
  await show(page, 'command');
  const before = requests.length;
  await expect(options(page)).toHaveCount(7);
  await page.keyboard.press('Escape');
  expect(requests.length).toBe(before);
});

test('forced canonical route and settings destinations', async ({ page }, testInfo) => {
  await setup(page);
  await show(page, 'interaction specification');
  await expect(options(page)).toHaveCount(1);
  await page.keyboard.press('Meta+Enter');
  await expect(page).toHaveURL(/\/library\/deliverable\/dlv_library_design$/);
  await show(page, 'providers');
  await expect(options(page)).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath('spotlight-settings.png') });
  await page.evaluate(() => {
    document.documentElement.dataset.themeFamily = 'porcelain';
    document.documentElement.dataset.resolvedTheme = 'light';
  });
  await page.screenshot({ path: testInfo.outputPath('spotlight-settings-light.png') });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/settings\?tab=providers$/);
});

test('action-only modal focus trap, dismissal and reduced motion', async ({ page }, testInfo) => {
  await setup(page);
  await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' });
  const trigger = page.getByRole('button', { name: 'Search Cognis', exact: true });
  await trigger.click();
  const input = page.getByRole('combobox', { name: 'Search Cognis' });
  await input.fill('new conversation');
  await expect(options(page)).toHaveCount(1);
  await input.press('Tab');
  await expect(page.getByRole('button', { name: 'Close command palette' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Filter result types' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(input).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button', { name: 'Filter result types' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button', { name: 'Close command palette' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await show(page, 'command');
  await expect(options(page)).toHaveCount(7);
  await expect(options(page).first().locator('.result-icon img')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('spotlight-mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('sparse pagination pauses and failures never retry on scroll', async ({ page }) => {
  await setup(page);
  let requests = 0;
  await page.route('**/api/v1/search/palette?**', (route) => {
    if (new URL(route.request().url()).searchParams.get('q') !== 'sparse')
      return route.fulfill({ json: { items: [], next_cursor: null } });
    requests++;
    return route.fulfill({ json: { items: [], next_cursor: `scan-${requests}` } });
  });
  await show(page, 'sparse');
  await expect(page.getByRole('button', { name: 'Search more names' })).toBeVisible();
  expect(requests).toBe(2);
  await page.keyboard.press('ArrowDown');
  expect(requests).toBe(2);
  await page.route('**/api/v1/search/palette?**', (route) => { requests++; return route.fulfill({ status: 503, json: { error: { message: 'Search took too long.' } } }); });
  await page.getByRole('button', { name: 'Search more names' }).click();
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  const failedAt = requests;
  await page.keyboard.press('ArrowDown');
  expect(requests).toBe(failedAt);
});

test('virtualized continuation has bounded DOM and ends at the client retention limit', async ({ page }) => {
  await setup(page);
  let requests = 0;
  await page.route('**/api/v1/search/palette?**', (route) => {
    if (new URL(route.request().url()).searchParams.get('q') !== 'virtual')
      return route.fulfill({ json: { items: [], next_cursor: null } });
    requests++;
    const cursor = Number(new URL(route.request().url()).searchParams.get('cursor') ?? '0');
    const items = Array.from({ length: 24 }, (_, index) => ({
      ...catalog[0], id: `virtual-conversation-${cursor + index}`,
      title: `Virtual conversation ${cursor + index}`, href: `/chat/virtual-conversation-${cursor + index}`,
    }));
    return route.fulfill({ json: { items, next_cursor: String(cursor + 24) } });
  });
  const input = await show(page, 'virtual');
  await expect(options(page)).toHaveCount(18);
  const list = page.locator('.palette-results');
  for (let index = 0; index < 16; index++) {
    const before = requests;
    await list.evaluate((node) => { node.scrollTop = node.scrollHeight; });
    await expect.poll(() => requests).toBeGreaterThan(before);
    await expect(page.locator('.search-progress')).not.toHaveClass(/loading/);
    expect(await options(page).count()).toBeLessThanOrEqual(18);
  }
  await expect(page.getByText('Refine your search to find more results.')).toBeVisible();
  expect(requests).toBe(17);
  await input.press('ArrowDown');
  await expect(input).toHaveAttribute('aria-activedescendant', /.+/);
  const active = await input.getAttribute('aria-activedescendant');
  expect(active).toBeTruthy();
  await expect(page.locator(`[id="${active}"]`)).toBeAttached();
});

test('schedule activation and warm shortcut focus timing', async ({ page }, testInfo) => {
  await setup(page);
  await show(page, 'weekly');
  await expect(options(page)).toHaveCount(1);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('workspace-window-schedule:palette-review')).toBeVisible();
  await expect(page).toHaveURL((url) => url.pathname === '/');
  const timings: number[] = [];
  const focusTimings: number[] = [];
  for (let index = 0; index < 5; index++) {
    const elapsed = await page.evaluate(async () => {
      const started = performance.now();
      let focusedAt: number | null = null;
      const focused = (event: FocusEvent) => {
        if ((event.target as HTMLElement)?.getAttribute('role') === 'combobox')
          focusedAt = performance.now() - started;
      };
      document.addEventListener('focusin', focused);
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true, bubbles: true }));
      while (document.activeElement?.getAttribute('role') !== 'combobox') {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      }
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      document.removeEventListener('focusin', focused);
      return { focus: focusedAt, twoFrameReady: performance.now() - started };
    });
    expect(elapsed.focus).not.toBeNull();
    focusTimings.push(elapsed.focus!);
    timings.push(elapsed.twoFrameReady);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: 'Command palette', exact: true })).toBeHidden();
  }
  const timingPath = testInfo.outputPath('palette-warm-focus-latency.json');
  const summarize = (samples: number[]) => ({ samples, median: [...samples].sort((a, b) => a - b)[2], max: Math.max(...samples) });
  await writeFile(timingPath, JSON.stringify({ unit: 'ms', keyboardFocus: summarize(focusTimings), twoFrameReady: summarize(timings) }));
  await testInfo.attach('palette-warm-focus-latency.json', {
    path: timingPath,
    contentType: 'application/json',
  });
});
