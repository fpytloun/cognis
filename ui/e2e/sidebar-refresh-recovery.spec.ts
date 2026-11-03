import { expect, test, type Page, type Response, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } });

async function openApp(page: Page, path = '/chat/conv-task-chat', initialWaitingStatus = 200) {
  const initialWaiting = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/v1/conversations/waiting'
      && response.status() === initialWaitingStatus);
  await page.goto(path);
  // A quiet network can precede the leader-owned bootstrap read. Observe its
  // expected response before changing fixtures or advancing live deadlines.
  await page.getByTestId(path === '/' ? 'control-center' : 'chat-header')
    .waitFor({ state: 'visible', timeout: 30_000 });
  await (await initialWaiting).finished();
  await page.waitForLoadState('networkidle', { timeout: 30_000 });
}

async function openClockedApp(page: Page, path = '/chat/conv-task-chat', initialWaitingStatus = 200) {
  // Clock.install must precede application timers, including document leases.
  // https://playwright.dev/docs/clock#introduction
  await page.clock.install();
  await openApp(page, path, initialWaitingStatus);
}

async function settleAuthorityStorage(page: Page) {
  // Virtual time must not expire real IndexedDB work's three-second watchdog.
  // This readonly transaction waits behind admitted authority transactions.
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const request = indexedDB.open('cognis-profile-intent-v1', 1);
    request.onupgradeneeded = () => request.transaction?.abort();
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('Authority storage is blocked'));
    request.onsuccess = () => {
      const db = request.result;
      const tx = db.transaction('profiles', 'readonly');
      tx.objectStore('profiles').getAllKeys();
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onabort = tx.onerror = () => { db.close(); reject(tx.error); };
    };
  }));
}

async function advanceClock(page: Page, duration: number, mode: 'runFor' | 'fastForward' = 'runFor') {
  await settleAuthorityStorage(page);
  if (mode === 'fastForward') {
    await page.clock.fastForward(duration);
    await settleAuthorityStorage(page);
    return;
  }
  for (let remaining = duration; remaining > 0;) {
    const step = Math.min(remaining, 500);
    await page.clock.runFor(step);
    await settleAuthorityStorage(page);
    remaining -= step;
  }
}

const row = (id: string, title: string, unread = false) => ({
  conversation_id: id, title, user_email: 'owner@cognis.test', agent_id: 'agent-stage39',
  status: 'active', context: { type: 'web', platform_data: {}, ref: null },
  has_unread: unread, has_active_turn: false, pending_notification_types: [],
  attention_actions: [], active_session_status: null,
  created_at: '2026-10-14T20:00:00Z', updated_at: '2026-10-14T20:00:00Z',
  last_message_at: '2026-10-14T20:00:00Z',
});
const browserAgent = {
  agent_id: 'agent-stage39', name: 'stage39', display_name: 'Stage 39 Agent',
  status: 'active', agent_type: 'primary', avatar_url: null, avatar_image_id: null,
};
const projection = (items: ReturnType<typeof row>[], revision: string) => ({
  agents: [browserAgent], agent_direct_chats: [], conversations: { items, cursor: null, has_more: false },
  context_types: ['web'], sidebar_revision: revision, is_delta: false,
  sync_timestamp: '2026-10-14T20:00:00Z',
  background_work: { items: [], active_count: 0, truncated: false, generated_at: '2026-10-14T20:00:00Z' },
});

async function fixture(page: Page) {
  await installTaskCockpitFixture(page);
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/api/ws', ws => {
    socket = ws;
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  return (event: object) => socket!.send(JSON.stringify(event));
}

test('executor and schedule bursts refresh metadata without rebuilding the active chat', async ({ page }) => {
  const send = await fixture(page);
  let sidebarCalls = 0;
  let timelineCalls = 0;
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    sidebarCalls++;
    return route.fulfill({ json: projection([], '1') });
  });
  page.on('request', request => {
    if (/\/chat\/v2\/conversations\/conv-task-chat\/(snapshot|sync)/.test(request.url())) timelineCalls++;
  });
  await openClockedApp(page);
  // The header can render before the first canonical snapshot is requested.
  // Drain that bootstrap request before attributing traffic to invalidations.
  await expect.poll(() => timelineCalls).toBeGreaterThan(0);
  await page.waitForLoadState('networkidle');
  const sidebarBefore = sidebarCalls;
  const timelineBefore = timelineCalls;
  for (let i = 0; i < 100; i++) {
    send({ type: 'scope_invalidated', reason: 'executor_state_changed' });
    send({ type: 'scope_invalidated', reason: 'schedule_action_changed' });
  }
  await advanceClock(page, 200);
  expect(timelineCalls).toBe(timelineBefore);
  expect(sidebarCalls).toBeLessThanOrEqual(sidebarBefore + 1);
  await advanceClock(page, 4_100);
  await expect.poll(() => sidebarCalls).toBeGreaterThan(sidebarBefore);
  expect(sidebarCalls).toBeLessThanOrEqual(sidebarBefore + 2);
});

test('a single work invalidation after a snapshot updates idle-parent activity and child TODOs', async ({ page }) => {
  const send = await fixture(page);
  let revision = 1;
  let sidebarCalls = 0;
  let timelineCalls = 0;
  const parent = { ...row('conv-task-chat', 'Idle parent'), active_session_id: 'sess-task-chat' };
  await page.route(/\/api\/v1\/conversations\/conv-task-chat(?:\?|$)/, route =>
    route.fulfill({ json: parent }));
  const child = {
    kind: 'delegated_session', work_id: 'unopened-child',
    controller_conversation_id: parent.conversation_id,
    session_id: 'unopened-child', parent_session_id: 'sess-task-chat',
    title: 'Unopened child', agent_id: 'system:explore', status: 'active',
    todos: [{ content: 'Inspect child work', status: 'in_progress', priority: 'normal' }],
  };
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    sidebarCalls++;
    const response = projection(
      revision === 1 ? [parent] : [parent, row('new-unread-chat', 'New unread conversation', true)],
      String(revision),
    );
    return route.fulfill({ json: {
      ...response,
      sync_timestamp: new Date().toISOString(),
      background_work: {
        ...response.background_work,
        items: revision === 1 || revision === 4 ? [] : [{
          ...child,
          todos: revision === 3
            ? [{ content: 'Verify latest child work', status: 'pending', priority: 'normal' }]
            : child.todos,
        }],
        active_count: revision === 1 || revision === 4 ? 0 : 1,
      },
    } });
  });
  page.on('request', request => {
    if (/\/chat\/v2\/conversations\/conv-task-chat\/(snapshot|sync)/.test(request.url())) timelineCalls++;
  });
  await openApp(page);
  await expect.poll(() => sidebarCalls).toBeGreaterThan(0);
  await page.waitForLoadState('networkidle');
  const before = sidebarCalls;
  const timelineBefore = timelineCalls;
  revision = 2;
  send({ type: 'work_invalidated', revision: '2' });
  // One invalidation must be enough; opening a child/inspector is not required.
  await page.waitForTimeout(4_100);
  await expect(page.getByText('New unread conversation', { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId('activity-avatar').filter({ has: page.locator('[data-testid="activity-avatar-orbit"]') }).first())
    .toHaveAttribute('aria-label', 'Background work active');
  const drawer = page.getByRole('button', { name: /^Ongoing work/ });
  await expect(drawer).toContainText('1 running');
  await drawer.click();
  await expect(page.getByRole('button', { name: 'Unopened child', exact: true })).toBeVisible();
  const progress = page.getByRole('button', { name: /Unopened child todo progress/ });
  await progress.click();
  await expect(page.getByText('Inspect child work', { exact: true })).toBeVisible();
  await progress.click();
  revision = 3;
  send({ type: 'work_invalidated', revision: '3' });
  await page.waitForTimeout(4_100);
  await progress.click();
  await expect(page.getByText('Verify latest child work', { exact: true })).toBeVisible();
  await progress.click();
  revision = 4;
  send({ type: 'work_invalidated', revision: '4' });
  await page.waitForTimeout(4_100);
  await expect(drawer).toHaveCount(0);
  await expect(page.getByTestId('activity-avatar-orbit')).toHaveCount(0);
  expect(timelineCalls).toBe(timelineBefore);
  const settled = sidebarCalls;
  await page.waitForTimeout(30_000);
  expect(sidebarCalls).toBe(settled); // No polling after the event-driven work drains.
});

test('tablet keeps a child-work invalidation arriving during a sidebar read', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 1024 });
  const send = await fixture(page);
  const parent = { ...row('conv-task-chat', 'Idle parent'), active_session_id: 'sess-task-chat' };
  await page.route(/\/api\/v1\/conversations\/conv-task-chat(?:\?|$)/, route =>
    route.fulfill({ json: parent }));
  let revision = 1;
  let calls = 0;
  const requestUrls: string[] = [];
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let entered!: () => void;
  const reading = new Promise<void>(resolve => { entered = resolve; });
  await page.route('**/api/v1/conversations/sidebar?**', async route => {
    calls++;
    requestUrls.push(route.request().url());
    const captured = revision;
    if (captured === 2) { entered(); await held; }
    const response = projection([parent], String(captured));
    await route.fulfill({ json: {
      ...response,
      background_work: {
        ...response.background_work,
        active_count: captured >= 3 ? 1 : 0,
        items: captured >= 3 ? [{
          kind: 'managed_conversation', work_id: 'tablet-child',
          controller_conversation_id: parent.conversation_id,
          controller_session_id: 'sess-task-chat',
          title: 'Unopened tablet child', status: 'running', agent_id: 'agent-stage39',
          todos: [{ content: 'Latest child TODO', status: 'in_progress', priority: 'normal' }],
        }] : [],
      },
    } });
  });
  await openApp(page);
  await expect.poll(() => calls).toBeGreaterThan(0);
  await page.waitForLoadState('networkidle');
  const before = calls;
  revision = 2;
  send({ type: 'work_invalidated', revision: '2' });
  await reading;
  revision = 3;
  for (let i = 0; i < 100; i++) send({ type: 'work_invalidated', revision: '3' });
  // Let the browser consume the burst while the old HTTP response is still held.
  await page.waitForTimeout(100);
  release();
  await page.waitForLoadState('networkidle');
  const drawer = page.getByRole('button', { name: /^Ongoing work/ });
  await expect(drawer).toContainText('1 running');
  await expect(page.getByTestId('activity-avatar-orbit').first()).toBeVisible();
  await drawer.click();
  await page.getByRole('button', { name: /Unopened tablet child todo progress/ }).click();
  await expect(page.getByText('Latest child TODO', { exact: true })).toBeVisible();
  expect(calls, JSON.stringify(requestUrls)).toBe(before + 2);
  await page.waitForTimeout(30_000);
  expect(calls, JSON.stringify(requestUrls)).toBe(before + 2);
});

test('opening a stale conversation list repairs missed pushes without polling or a timeline reload', async ({ page }) => {
  await fixture(page);
  let updated = false;
  let calls = 0;
  let timelineCalls = 0;
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    calls++;
    return route.fulfill({ json: projection(
      updated ? [row('missed-unread-chat', 'Recovered unread conversation', true)] : [],
      updated ? '2' : '1',
    ) });
  });
  page.on('request', request => {
    if (/\/chat\/v2\/conversations\/conv-task-chat\/(snapshot|sync)/.test(request.url())) timelineCalls++;
  });
  await openClockedApp(page);
  const before = calls;
  await page.getByRole('button', { name: 'Hide conversations', exact: true }).click();
  await advanceClock(page, 31_000);
  expect(calls).toBe(before);
  const timelineBefore = timelineCalls;
  updated = true; // No WebSocket event: the client missed the push while idle.
  await page.getByRole('button', { name: 'Show conversations', exact: true }).click();
  await expect(page.getByText('Recovered unread conversation', { exact: true }).first()).toBeVisible();
  expect(calls).toBe(before + 1);
  await page.getByRole('button', { name: 'Hide conversations', exact: true }).click();
  await page.getByRole('button', { name: 'Show conversations', exact: true }).click();
  await advanceClock(page, 31_000);
  expect(calls).toBe(before + 1);
  expect(timelineCalls).toBe(timelineBefore);
});

for (const mutation of ['upsert', 'removal'] as const) {
test(`accepted live sidebar ${mutation} fences delayed device hydration`, async ({ page }) => {
  const send = await fixture(page);
  const cached = row('cache-race', 'Older device title');
  const live = row('cache-race', 'Newer live title');
  let holdNetwork = false;
  let releaseNetwork!: () => void;
  const networkGate = new Promise<void>(resolve => { releaseNetwork = resolve; });
  await page.route('**/api/v1/conversations/sidebar?**', async route => {
    if (holdNetwork) await networkGate;
    const rows = holdNetwork ? mutation === 'upsert' ? [live] : [] : [cached];
    await route.fulfill({ json: projection(rows, holdNetwork ? '3' : '1') });
  });
  await openApp(page);
  await expect(page.getByText('Older device title', { exact: true }).first()).toBeVisible();
  await page.evaluate(() => sessionStorage.setItem('hold-sidebar-cache', '1'));
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('hold-sidebar-cache')) return;
    const original = IDBObjectStore.prototype.get;
    const descriptor = Object.getOwnPropertyDescriptor(IDBRequest.prototype, 'onsuccess')!;
    IDBObjectStore.prototype.get = function (key) {
      const request = original.call(this, key);
      if (String(key).includes('sidebar')) {
        Object.defineProperty(request, 'onsuccess', {
          set(handler) {
            descriptor.set!.call(request, function (event: Event) {
              (window as any).releaseSidebarCache = () => handler.call(request, event);
            });
          },
        });
      }
      return request;
    };
  });
  holdNetwork = true;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof (window as any).releaseSidebarCache === 'function');
  send(mutation === 'upsert'
    ? { type: 'sidebar_conversation_upsert', conversation: live, revision: '2' }
    : { type: 'sidebar_conversation_removed', conversation_id: 'cache-race', revision: '2' });
  if (mutation === 'upsert') {
    await expect(page.getByText('Newer live title', { exact: true }).first()).toBeVisible();
  } else {
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
  }
  await page.evaluate(() => (window as any).releaseSidebarCache());
  // Drain the released device-read and Svelte render before checking rollback.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByText('Older device title', { exact: true })).toHaveCount(0);
  if (mutation === 'upsert') await expect(page.getByText('Newer live title', { exact: true }).first()).toBeVisible();
  releaseNetwork();
});
}

test('timeline keeps readable history quiet during an unfinished reconciliation', async ({ page }) => {
  const send = await fixture(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByTestId('task-agent-dock-launcher').click();
  const timeline = page.locator('[data-freshness]').first();
  await expect(timeline).toHaveAttribute('data-freshness', 'current');
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/**', async route => {
    await held;
    await route.fallback();
  });
  send({ type: 'turn_settled', conversation_id: 'conv-task-chat', turn_id: 'freshness-turn' });
  await expect(timeline).toHaveAttribute('data-freshness', 'reconciling');
  await expect(timeline.getByRole('status')).toHaveCount(0);
  await expect(timeline.getByText('What is blocking this release?', { exact: true })).toBeVisible();
  release();
  await expect(timeline).toHaveAttribute('data-freshness', 'current');
});

test('foreground force refresh bypasses ordinary success spacing without a duplicate context-type read', async ({ page }) => {
  await fixture(page);
  let calls = 0;
  let contextCalls = 0;
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    calls++;
    return route.fulfill({ json: projection([], String(calls)) });
  });
  page.on('request', request => {
    if (new URL(request.url()).pathname.endsWith('/conversations/context-types')) contextCalls++;
  });
  await openClockedApp(page);
  const before = calls;
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  await advanceClock(page, 100);
  await expect.poll(() => calls).toBe(before + 1);
  expect(contextCalls).toBe(0);
});

test('Waiting retains its snapshot and recovers while sidebar remains unavailable', async ({ page }) => {
  const send = await fixture(page);
  let failing = false;
  let waitingFailing = false;
  let sidebarCalls = 0;
  let waitingCalls = 0;
  const waitingRow = row('historical-waiting', 'Retained Waiting snapshot', true);
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    sidebarCalls++;
    return failing
      ? route.fulfill({ status: 503, json: { detail: 'Sidebar unavailable' } })
      : route.fulfill({ json: projection([], '1') });
  });
  await page.route('**/api/v1/conversations/waiting?**', route => {
    waitingCalls++;
    return waitingFailing
      ? route.fulfill({ status: 429, headers: { 'Retry-After': '1' }, json: { detail: 'Waiting limited' } })
      : route.fulfill({ json: {
        conversations: { items: [waitingRow], cursor: null, has_more: false },
        active_conversation_ids: [],
      } });
  });
  await openClockedApp(page);
  await expect(page.getByText('Retained Waiting snapshot', { exact: true }).first()).toBeVisible();
  failing = waitingFailing = true;
  send({ type: 'work_invalidated', revision: '2' });
  for (let call = 2; call <= 4; call++) {
    await advanceClock(page, 4_100);
    await expect.poll(() => waitingCalls).toBe(call);
  }
  // Independent response completions can put the sidebar one cooldown behind.
  await advanceClock(page, 4_100);
  await expect.poll(() => sidebarCalls).toBe(4);
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText(/Waiting could not refresh/)).toHaveCount(0);
  await expect(page.getByText('Retained Waiting snapshot', { exact: true }).first()).toBeVisible();
  expect(sidebarCalls).toBe(4);
  for (let i = 0; i < 50; i++) {
    send({ type: 'work_invalidated', revision: String(i + 3) });
    send({ type: 'sidebar_conversation_upsert', conversation_id: `missing-${i}` });
  }
  await advanceClock(page, 60_000, 'fastForward');
  await expect(page.getByTestId('waiting-refresh-stale')).toContainText('Waiting updates are delayed.');
  await expect(page.getByTestId('waiting-refresh-stale')).not.toHaveAttribute('role', 'alert');
  const sidebarBeforeRetry = sidebarCalls;
  const waitingBeforeRetry = waitingCalls;
  waitingFailing = false;
  await page.getByTestId('waiting-refresh-stale').getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText('Retained Waiting snapshot', { exact: true }).first()).toBeVisible();
  expect(sidebarCalls).toBe(sidebarBeforeRetry);
  expect(waitingCalls).toBe(waitingBeforeRetry + 1);
});

test('exhausted Waiting recovers a complete snapshot automatically while sidebar still fails', async ({ page }) => {
  test.setTimeout(120_000);
  const send = await fixture(page);
  let failing = false;
  let waitingFails = false;
  let waitingCalls = 0;
  const failedResponses: Response[] = [];
  page.on('response', response => {
    if (response.status() === 503 && new URL(response.url()).pathname.endsWith('/conversations/waiting')) {
      failedResponses.push(response);
    }
  });
  const cursors: Array<string | null> = [];
  await page.route('**/api/v1/conversations/sidebar?**', route => failing
    ? route.fulfill({ status: 503, json: { detail: 'Sidebar unavailable' } })
    : route.fulfill({ json: projection([], '1') }));
  await page.route('**/api/v1/conversations/waiting?**', route => {
    waitingCalls++;
    if (waitingFails) return route.fulfill({ status: 503, json: { detail: 'Waiting unavailable' } });
    const cursor = new URL(route.request().url()).searchParams.get('cursor');
    cursors.push(cursor);
    return route.fulfill({ json: {
      conversations: {
        items: [row(cursor ? 'recovered-page-two' : 'recovered-page-one',
          cursor ? 'Recovered Waiting page two' : 'Recovered Waiting page one', true)],
        cursor: cursor ? null : 'waiting-page-two', has_more: !cursor,
      },
      active_conversation_ids: [],
    } });
  });
  await openApp(page);
  await expect(page.getByText('Recovered Waiting page two', { exact: true }).first()).toBeVisible();
  failing = waitingFails = true;
  send({ type: 'work_invalidated', revision: '2' });
  for (let i = 0; i < 3; i++) {
    await expect.poll(() => failedResponses.length).toBe(i + 1);
    expect(await failedResponses[i].finished()).toBeNull();
    await expect.poll(() => waitingCalls).toBe(2 + i + 1);
  }
  await expect.poll(() => waitingCalls).toBe(5); // Two initial pages, three failures.
  for (let i = 0; i < 50; i++) send({ type: 'work_invalidated', revision: String(i + 3) });
  // Negative policy assertion: storms cannot restart the fast burst.
  await page.waitForTimeout(40_000);
  expect(waitingCalls).toBe(5);
  waitingFails = false;
  // Only recovery gets a longer deadline: one real 60s half-open window.
  await expect.poll(() => waitingCalls, { timeout: 65_000 }).toBe(7);
  expect(cursors.slice(-2)).toEqual([null, 'waiting-page-two']);
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText('Recovered Waiting page one', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Recovered Waiting page two', { exact: true }).first()).toBeVisible();
});

test('offline recovery retains Waiting and foreground storms do not restart its burst', async ({ page }) => {
  test.setTimeout(210_000);
  const send = await fixture(page);
  let fails = false;
  let calls = 0;
  const failedResponses: Response[] = [];
  page.on('response', response => {
    if (response.status() === 503 && new URL(response.url()).pathname.endsWith('/conversations/waiting')) {
      failedResponses.push(response);
    }
  });
  await page.route('**/api/v1/conversations/sidebar?**', route => route.fulfill({
    json: projection([], '1'),
  }));
  await page.route('**/api/v1/conversations/waiting?**', route => {
    calls++;
    return fails
      ? route.fulfill({ status: 503, json: { detail: 'Waiting unavailable' } })
      : route.fulfill({ json: {
        conversations: { items: [row('offline-retained', 'Waiting preserved offline', true)], cursor: null, has_more: false },
        active_conversation_ids: [],
      } });
  });
  await openApp(page);
  await expect(page.getByText('Waiting preserved offline', { exact: true }).first()).toBeVisible();
  fails = true;
  send({ type: 'work_invalidated', revision: '2' });
  for (let i = 0; i < 3; i++) {
    await expect.poll(() => failedResponses.length).toBe(i + 1);
    expect(await failedResponses[i].finished()).toBeNull();
    await expect.poll(() => calls).toBe(1 + i + 1);
  }
  await expect.poll(() => calls).toBe(4);
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
    window.dispatchEvent(new Event('offline'));
  });
  // Cross a real probe deadline while offline: no automatic request is allowed.
  await page.waitForTimeout(65_000);
  expect(calls).toBe(4);
  await expect(page.getByText('Waiting preserved offline', { exact: true }).first()).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true });
    for (let i = 0; i < 50; i++) {
      window.dispatchEvent(new Event('online'));
      window.dispatchEvent(new Event('focus'));
    }
  });
  await expect.poll(() => calls).toBe(5);
  await expect.poll(() => failedResponses.length).toBe(4);
  expect(await failedResponses[3].finished()).toBeNull();
  // A failed foreground probe must not replenish the fast retry burst.
  await page.waitForTimeout(4_100);
  expect(calls).toBe(5);
  fails = false;
  // Only recovery gets a longer deadline: the next real 60s probe window.
  await expect.poll(() => calls, { timeout: 65_000 }).toBe(6);
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
});

test('a delayed snapshot applies under churn without regressing pushed rows or removals', async ({ page }) => {
  const send = await fixture(page);
  const original = row('race-row', 'Before pushed title');
  const removed = row('race-removed', 'Removed during refresh');
  let calls = 0;
  let release: (() => void) | undefined;
  await page.route('**/api/v1/conversations/sidebar?**', async route => {
    calls++;
    if (calls === 2) await new Promise<void>(resolve => { release = resolve; });
    return route.fulfill({ json: projection(
      calls === 1 ? [original, removed] : [
        original, removed, row('snapshot-only', 'Snapshot progress under churn'),
      ], calls === 1 ? '1' : '2',
    ) });
  });
  await page.route('**/api/v1/conversations/waiting?**', route => route.fulfill({ json: {
    conversations: { items: [], cursor: null, has_more: false }, active_conversation_ids: [],
  } }));
  await openApp(page);
  await expect(page.getByText('Before pushed title', { exact: true }).first()).toBeVisible();
  send({ type: 'scope_invalidated', reason: 'sidebar_changed' });
  await expect.poll(() => Boolean(release)).toBe(true);
  send({ type: 'sidebar_conversation_upsert', conversation_id: original.conversation_id,
    conversation: { ...original, title: 'New pushed title' }, revision: '3' });
  send({ type: 'sidebar_conversation_removed', conversation_id: removed.conversation_id, revision: '4' });
  await expect(page.getByText('New pushed title', { exact: true }).first()).toBeVisible();
  release!();
  await expect(page.getByText('Snapshot progress under churn', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Before pushed title', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Removed during refresh', { exact: true })).toHaveCount(0);
  await expect(page.getByText('New pushed title', { exact: true }).first()).toBeVisible();
});

for (const mutation of ['removal', 'out-of-scope-upsert'] as const) {
test(`scope round trips preserve Waiting cache after ${mutation} without bypass requests`, async ({ page }) => {
  const send = await fixture(page);
  let waitingFails = false;
  let directCalls = 0;
  let archivedLoaded = false;
  let waitingFailures = 0;
  await page.route('**/api/v1/conversations/agent-direct**', route => {
    directCalls++;
    return route.fulfill({ json: [] });
  });
  await page.route('**/api/v1/conversations/context-types**', route => route.fulfill({
    json: ['web'],
  }));
  await page.route('**/api/v1/conversations/sidebar?**', route => {
    if (new URL(route.request().url()).searchParams.get('status') === 'archived') archivedLoaded = true;
    return route.fulfill({ json: projection([], '3') });
  });
  await page.route('**/api/v1/conversations/waiting?**', route => {
    if (waitingFails) {
      waitingFailures++;
      return route.fulfill({ status: 503, json: { detail: 'Waiting offline' } });
    }
    return route.fulfill({ json: {
      conversations: { items: [row('removed-cache-row', 'Do not resurrect Waiting', true)], cursor: null, has_more: false },
      active_conversation_ids: [],
    } });
  });
  await openApp(page);
  await expect(page.getByText('Do not resurrect Waiting', { exact: true }).first()).toBeVisible();
  if (mutation === 'removal') {
    send({ type: 'sidebar_conversation_removed', conversation_id: 'removed-cache-row', revision: '4' });
    await expect(page.getByText('Do not resurrect Waiting', { exact: true })).toHaveCount(0);
  }
  waitingFails = true;
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await page.getByRole('button', { name: 'Archived', exact: true }).click();
  await expect.poll(() => archivedLoaded).toBe(true);
  if (mutation === 'out-of-scope-upsert') {
    send({ type: 'sidebar_conversation_upsert', conversation_id: 'removed-cache-row', revision: '4',
      conversation: row('removed-cache-row', 'Updated active Waiting snapshot', true) });
    await expect(page.getByText('Updated active Waiting snapshot', { exact: true })).toHaveCount(0);
  }
  await page.getByRole('button', { name: 'Active', exact: true }).click();
  await expect.poll(() => waitingFailures).toBeGreaterThan(0);
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText(/Waiting could not refresh/)).toHaveCount(0);
  await expect(page.getByText('Do not resurrect Waiting', { exact: true })).toHaveCount(0);
  if (mutation === 'out-of-scope-upsert') {
    await expect(page.getByText('Updated active Waiting snapshot', { exact: true }).first()).toBeVisible();
  }
  expect(directCalls).toBe(0);
});
}

test('unread updates continue quietly during incomplete executor telemetry', async ({ page }) => {
  const send = await fixture(page);
  let calls = 0;
  let incomplete = false;
  const running = row('known-shell', 'Known active shell', true);
  const initialUnread = row('initial-unread', 'Unread before executor outage', true);
  const unread = row('new-unread-during-outage', 'Fresh unread during executor outage', true);
  await page.route('**/api/v1/conversations/sidebar?**', route => route.fulfill({ json: {
    ...projection([], '1'), background_work_changed: !incomplete,
  } }));
  await page.route('**/api/v1/conversations/waiting?**', route => {
    calls++;
    return route.fulfill({ json: {
      conversations: { items: incomplete ? [running, unread] : [running, initialUnread], cursor: null, has_more: false },
      active_conversation_ids: incomplete ? [] : ['known-shell'], activity_complete: !incomplete,
    } });
  });
  await openClockedApp(page);
  await expect(page.getByText(initialUnread.title, { exact: true })).toBeVisible();
  await expect(page.getByText(running.title, { exact: true })).toHaveCount(0);
  await expect.poll(() => calls).toBe(1);
  incomplete = true;
  send({ type: 'work_invalidated', revision: '2' });
  await advanceClock(page, 4_100);
  await expect(page.getByText('Fresh unread during executor outage', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(initialUnread.title, { exact: true })).toHaveCount(0);
  await expect(page.getByText('Known active shell', { exact: true })).toHaveCount(0);
  await advanceClock(page, 5 * 60_000, 'fastForward');
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText(/Waiting could not refresh/)).toHaveCount(0);
});

test('Dashboard also keeps transient Waiting failures quiet', async ({ page }) => {
  await fixture(page);
  let failures = 0;
  await page.route('**/api/v1/conversations/waiting?**', route => {
    failures++;
    return route.fulfill({ status: 503, json: { detail: 'Database temporarily unavailable' } });
  });
  await openClockedApp(page, '/', 503);
  await expect.poll(() => failures).toBeGreaterThan(0);
  await expect(page.getByText(/Waiting could not refresh/)).toHaveCount(0);
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await advanceClock(page, 1_000);
  await advanceClock(page, 60_000, 'fastForward');
  await expect(page.getByTestId('waiting-refresh-stale')).toContainText('Waiting updates are delayed.');
  await expect(page.getByTestId('waiting-refresh-stale')).not.toHaveAttribute('role', 'alert');
});

test('Dashboard Waiting completes and recovers independently of failed history', async ({ page }) => {
  await fixture(page);
  let waitingFails = false;
  let waitingCalls = 0;
  let title = 'Fresh Waiting despite failed history';
  // Application errors stay resource-local; 503 belongs to global connectivity.
  await page.route('**/api/v1/conversations?**', route => route.fulfill({
    status: 500, json: { detail: 'History unavailable' },
  }));
  await page.route('**/api/v1/conversations/waiting?**', route => {
    waitingCalls++;
    return waitingFails
      ? route.fulfill({ status: 500, json: { detail: 'Waiting unavailable' } })
      : route.fulfill({ json: {
        conversations: { items: [row('independent-waiting', title, true)], cursor: null, has_more: false },
        active_conversation_ids: [],
      } });
  });
  await openClockedApp(page, '/');
  await expect(page.getByText(title, { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId('dashboard-conversations-error')).toContainText('History unavailable');
  waitingFails = true;
  await page.getByTestId('dashboard-conversations-error').getByRole('button', { name: 'Try again' }).click();
  await expect.poll(() => waitingCalls).toBeGreaterThan(1);
  await advanceClock(page, 1_000);
  await advanceClock(page, 60_000, 'fastForward');
  await expect(page.getByTestId('waiting-refresh-stale')).toBeVisible();
  waitingFails = false;
  title = 'Recovered Waiting while history still fails';
  await page.getByTestId('waiting-refresh-stale').getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  await expect(page.getByText(title, { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId('dashboard-conversations-error')).toContainText('History unavailable');
});

test('a long cooldown cannot carry an old scope failure into a new scope', async ({ page }) => {
  const send = await fixture(page);
  let fails = false;
  let waitingCalls = 0;
  await page.route('**/api/v1/conversations/context-types**', route => route.fulfill({ json: ['web'] }));
  // Let Waiting establish the shared cooldown; a sidebar rejection first could
  // correctly prevent the failing Waiting request from being admitted at all.
  await page.route('**/api/v1/conversations/sidebar?**', route => route.fulfill({
    json: projection([], '1'),
  }));
  await page.route('**/api/v1/conversations/waiting?**', route => {
    waitingCalls++;
    return fails
      ? route.fulfill({ status: 429, headers: { 'Retry-After': '120' }, json: { detail: 'Waiting limited' } })
      : route.fulfill({ json: {
        conversations: { items: [row('scope-stale', 'Initial Waiting snapshot', true)], cursor: null, has_more: false },
        active_conversation_ids: [],
      } });
  });
  await openClockedApp(page);
  await expect(page.getByText('Initial Waiting snapshot', { exact: true }).first()).toBeVisible();
  fails = true;
  send({ type: 'work_invalidated', revision: '2' });
  await advanceClock(page, 4_100);
  await expect.poll(() => waitingCalls).toBe(2);
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await page.getByRole('button', { name: 'Archived', exact: true }).click();
  await advanceClock(page, 61_000, 'fastForward');
  await expect(page.getByTestId('waiting-refresh-stale')).toHaveCount(0);
  expect(waitingCalls).toBe(2);
});

test('project selection immediately hides projectless direct chats even from a stale projection', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/v1/projects', route => route.fulfill({ json: [{
    project_id: 'project-sidebar', name: 'Sidebar Project', status: 'active',
  }] }));
  await page.route('**/api/v1/conversations/sidebar?**', route => route.fulfill({ json: {
    ...projection([], '1'),
    agent_direct_chats: [{
      agent: browserAgent,
      conversation: { ...row('projectless-direct', 'Projectless Direct Chat'),
        context: { type: 'web', platform_data: { kind: 'agent_direct' }, ref: null } },
    }],
  } }));
  await openApp(page);
  const directSection = page.locator('section[aria-labelledby="sidebar-agents-heading"]');
  await expect(directSection).toBeVisible();
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await page.locator('select#conversation-project-filter').selectOption('project-sidebar');
  await expect(directSection).toHaveCount(0);
});
