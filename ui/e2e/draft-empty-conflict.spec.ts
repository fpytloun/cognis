import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  // Static preview cannot provide the controller's bootstrap CSP nonce.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

test('full chat autosaves and sends over stale empty initialization', async ({ page }) => {
  const state = await installTaskCockpitFixture(page);
  const email = 'owner@cognis.test';
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email, role: 'user', name: 'Owner' },
  }));
  await page.route(/\/api\/v1\/inbox(?:\?.*)?$/, route => route.request().method() !== 'GET'
    ? route.fallback() : route.fulfill({ json: { items: [], unread_count: 0, next_cursor: null } }));
  await page.route('**/api/v1/dashboard/navigation-summary', route => route.request().method() !== 'GET'
    ? route.fallback() : route.fulfill({ json: {
      chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
      schedules: 0, tasks: 0,
    } }));
  await page.route('**/api/v1/feedback/conversations/conv-task-chat/staging', route => route.request().method() !== 'GET'
    ? route.fallback() : route.fulfill({ json: {
      conversation_id: 'conv-task-chat', version: 1, items: [], receipts: [], subject_ids: [],
    } }));
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await page.goto('/chat/conv-task-chat');
  const composer = page.getByRole('textbox', { name: 'Chat message', exact: true });
  await expect(composer).toBeEditable();
  // Recreate the persisted desktop failure: hydration observed no draft, then an
  // empty initialization reached storage before the local edit. Do not reload
  // or send a storage event that would repair the observation before typing.
  const key = await page.evaluate(email => {
    const key = `composer-v3:cognis-chat-draft:conv-task-chat:account-v2:${encodeURIComponent(`${location.origin}\n${email}`)}`;
    if (localStorage.getItem(key) !== null) throw new Error('Untouched composer unexpectedly persisted input');
    localStorage.setItem(key, JSON.stringify({
      version: 3, revision: crypto.randomUUID(), input_revision: crypto.randomUUID(),
      consumed: false, text: '', attachments: [], chat_mode: null,
    }));
    return key;
  }, email);
  await composer.fill('Local input must not conflict with an empty draft');
  await expect.poll(() => page.evaluate(key => JSON.parse(localStorage.getItem(key)!).text, key))
    .toBe('Local input must not conflict with an empty draft');
  await expect(composer).toHaveValue('Local input must not conflict with an empty draft');
  const form = page.locator('form').filter({ has: composer });
  await form.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.messageRequests().length).toBe(1);
  expect(state.messageRequests()[0].body).toMatchObject({
    content: 'Local input must not conflict with an empty draft',
  });
  await expect(composer).toHaveValue('');
  await expect(page.getByText(/Draft changed in another window/)).toHaveCount(0);
  await expect.poll(() => page.evaluate(key => JSON.parse(localStorage.getItem(key)!).consumed, key)).toBe(true);
  expect(state.unmockedRequests()).toEqual([]);
});
