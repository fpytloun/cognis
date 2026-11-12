import { expect, test, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

test('compaction-tail recovery keeps an idle chat renderable and navigable', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const message = (id: string, sortKey: string, content: string) => ({
    id, kind: 'message', role: 'user', message_id: id, client_message_id: id,
    content, sort_key: sortKey, source_refs: [], stable: true, attachments: [],
    partial: false, created_at: '2026-01-01T00:00:00Z',
  });
  const pages = [
    [message('older', '0022:0001', 'Earlier history'), message('copied', '0022:0002', 'Original copied response')],
    [],
  ];
  const fixture = await installTaskCockpitFixture(page, { conversationHistoryPages: pages });
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  let socket!: WebSocketRoute;
  await page.routeWebSocket('**/api/ws', ws => {
    socket = ws;
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await page.route(/\/api\/v1\/inbox(?:\?.*)?$/, route => route.fulfill({
    json: { items: [], unread_count: 0, next_cursor: null },
  }));
  await page.route('**/api/v1/dashboard/navigation-summary', route => route.fulfill({ json: {
    chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
    schedules: 0, tasks: 0,
  } }));
  await page.route(/\/api\/v1\/feedback\/conversations\/[^/]+\/staging$/, route => route.fulfill({
    json: { conversation_id: new URL(route.request().url()).pathname.split('/')[5], version: 1, items: [], receipts: [], subject_ids: [] },
  }));
  await page.goto('/chat/conv-task-chat');
  await expect(page.getByText('Original copied response', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Chat message', exact: true })).toBeEditable();
  pages[0] = [
    message('rotation-boundary', '0023:0001', 'New session boundary'),
    message('copied', '0023:0002', 'Canonical copied response'),
  ];
  let resetPending = true;
  await page.route(/\/api\/v1\/chat\/v2\/conversations\/conv-task-chat\/sync(?:\?.*)?$/, route => {
    if (!resetPending) return route.fallback();
    resetPending = false;
    const cursor = new URL(route.request().url()).searchParams.get('cursor');
    return route.fulfill({ json: {
      schema_version: 2, projection_version: 'stage39-cockpit-e2e',
      conversation_id: 'conv-task-chat', cursor_before: cursor, cursor_after: cursor,
      ops: [], runtime: null, reset_required: true, reset_reason: 'history_compacted',
      has_more: false, server_time: '2026-01-01T00:01:00Z',
    } });
  });
  socket.send(JSON.stringify({
    type: 'scope_invalidated', reason: 'history_compacted', conversation_id: 'conv-task-chat',
  }));
  await expect(page.getByText('Canonical copied response', { exact: true })).toBeVisible();
  await expect(page.getByText('Canonical copied response', { exact: true })).toHaveCount(1);
  await expect(page.getByText('Earlier history', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Chat message', exact: true })).toBeEditable();
  if (await page.getByRole('button', { name: 'Open conversations', exact: true }).isVisible()) {
    await page.getByRole('button', { name: 'Open conversations', exact: true }).click();
  }
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await page.getByRole('button', { name: 'Task', exact: true }).click();
  await page.getByRole('link', { name: /Release safety review B$/ }).click();
  await expect(page).toHaveURL(url => url.pathname === '/chat/conv-task-chat-b' && url.searchParams.get('status') === 'task');
  await expect(page.getByRole('textbox', { name: 'Chat message', exact: true })).toBeEditable();
  expect(errors).toEqual([]);
  expect(fixture.unmockedRequests()).toEqual([]);
});
