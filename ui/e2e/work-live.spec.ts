/**
 * Work Activity live-projection browser conformance.
 *
 * Proves docs/specs/42-work-live-projection.md through the rendered UI
 * against a real local Cognis API/DB (no mocked Work responses). Fixtures
 * are seeded once, deterministically, by:
 *
 *   make e2e-up
 *   make e2e-seed
 *   make e2e-seed-work-conformance
 *
 * See scripts/seed_work_conformance.py for the exact conversations/sessions
 * created (real Intaris tool_call/tool_result events, real session rows).
 */

import { expect, test, type Page } from '@playwright/test';

import { ADMIN_EMAIL, ADMIN_PASSWORD, login } from './helpers';

const SEEDED_ACTIVITIES = [
  ['Work conformance: completed root with file evidence', 'completed'],
  ['Work conformance: idle execution label', 'idle'],
  ['Work conformance: running execution label', 'running'],
  ['Work conformance: waiting execution label', 'waiting'],
  ['Work conformance: completed execution label', 'completed'],
  ['Work conformance: failed execution label', 'failed'],
] as const;
const FILES_CONVERSATION_ID = 'conv_work_conformance_files';
const EMPTY_CONVERSATION_ID = 'conv_work_conformance_empty';
const ROTATION_CONVERSATION_ID = 'conv_work_conformance_rotation';
const REFRESH_CONVERSATION_ID = 'conv_work_conformance_refresh';
const REFRESH_OLD_COMMAND = 'printf work-conformance-old';
const REFRESH_NEW_COMMAND = 'printf work-conformance-newest';
const LABEL_CONVERSATION_IDS: Record<string, string> = {
  idle: 'conv_work_conformance_label_idle',
  running: 'conv_work_conformance_label_running',
  waiting: 'conv_work_conformance_label_waiting',
  completed: 'conv_work_conformance_label_completed',
  failed: 'conv_work_conformance_label_failed',
};

async function openConversation(page: Page, conversationId: string): Promise<void> {
  await page.goto(`/chat/${conversationId}`, { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL(new RegExp(`/chat/${conversationId}$`));
  const reloadApp = page.getByRole('button', { name: 'Reload app' });
  if (await reloadApp.isVisible()) {
    await reloadApp.click();
    await expect(page).toHaveURL(new RegExp(`/chat/${conversationId}$`));
  }
}

async function openInspector(page: Page): Promise<void> {
  const overviewTab = page.getByRole('tab', { name: 'Overview' });
  const compactHeaderButton = page.getByTestId('chat-header-info');
  await expect(compactHeaderButton).toBeVisible();
  // The open store can be restored before the drawer's tabs mount. Testing
  // tab visibility here would toggle an already-open inspector closed.
  if (await compactHeaderButton.getAttribute('aria-expanded') !== 'true') {
    await compactHeaderButton.click();
  }
  await expect(compactHeaderButton).toHaveAttribute('aria-expanded', 'true');
  await expect(overviewTab).toBeVisible();
  await overviewTab.click();
  await expect(page.getByTestId('inspector-overview')).toBeVisible();
}

async function openWorkTab(page: Page): Promise<void> {
  await openInspector(page);
  await page.getByRole('tab', { name: 'Work' }).click();
  await expect(page.getByTestId('work-view')).toBeVisible();
}

test.use({ serviceWorkers: 'block' });

test.describe('Work Activity live projection (real API/DB)', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      const events: unknown[] = [];
      Object.defineProperty(window, '__workBoundaryAudit', { value: events });
      const NativeWebSocket = window.WebSocket;
      window.WebSocket = class extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          this.addEventListener('message', (event) => {
            try {
              const frame = JSON.parse(String(event.data));
              if (frame.type === 'work_invalidated') {
                events.push({ at: performance.now(), frame });
              }
            } catch { /* Non-JSON frames are not Work invalidations. */ }
          });
        }
      };
      const nativeFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const url = String(args[0] instanceof Request ? args[0].url : args[0]);
        const relevant = url.includes('/work') || url.includes('/snapshot');
        const id = events.length;
        if (relevant) events.push({ id, at: performance.now(), phase: 'start', url });
        const response = await nativeFetch(...args);
        if (relevant) {
          const body = await response.clone().json().catch(() => null);
          events.push({
            id, at: performance.now(), phase: 'complete', url, status: response.status,
            revision: body?.work_revision, materialization: body?.materialization,
          });
        }
        return response;
      };
    });
  });
  test.afterEach(async ({ page, request }, testInfo) => {
    if (testInfo.title.includes('visible refresh repairs')) {
      const loginResponse = await request.post('/api/auth/login', {
        data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
      });
      if (loginResponse.ok()) {
        await request.post('/api/v1/chat/v2/e2e/work-background-repair/resume', {
          data: { conversation_id: REFRESH_CONVERSATION_ID },
        });
      }
    }
    const audit = await page.evaluate(() => ({
      url: location.href,
      tabs: Array.from(document.querySelectorAll('[role="tab"]')).map((node) => ({
        text: node.textContent, selected: node.getAttribute('aria-selected'),
      })),
      inspectorVisible: !!document.querySelector('[data-testid="inspector-overview"]'),
      events: (window as Window & { __workBoundaryAudit?: unknown[] }).__workBoundaryAudit,
    })).catch(() => ({ pageClosed: true }));
    await testInfo.attach('work-boundary-audit', {
      body: JSON.stringify(audit, null, 2), contentType: 'application/json',
    });
  });
  test('visible refresh repairs delayed Work ahead of historical backlog', async ({ page }) => {
    await proveVisibleRefresh(page);
  });

  test('activity list loads real seeded conversations', async ({ page }) => {
    await login(page);
    for (const [title, state] of SEEDED_ACTIVITIES) {
      const conversationId = title.includes('completed root')
        ? FILES_CONVERSATION_ID : LABEL_CONVERSATION_IDS[state];
      await openConversation(page, conversationId);
      await openInspector(page);
      const overview = page.getByTestId('inspector-overview');
      await expect(overview).toBeVisible();
      await expect(overview.getByRole('heading', { name: 'Execution sessions' })).toBeVisible();
      await expect(overview.getByTestId('activity-tree-row')).toHaveCount(1);
      await expect(overview.getByTestId('activity-tree-row')).toContainText(
        state[0].toUpperCase() + state.slice(1),
      );
      await expect(overview.getByTestId('activity-overview-empty')).toHaveCount(0);
    }
  });

  test('sessionless conversation renders empty Work without an error', async ({ page }) => {
    await login(page);
    await openConversation(page, EMPTY_CONVERSATION_ID);
    await openWorkTab(page);
    await expect(page.getByTestId('work-empty')).toBeVisible();
    await expect(page.getByTestId('work-panel-error')).toHaveCount(0);
    await expect(page.getByText('Unable to load')).toHaveCount(0);
  });

  test('completed root with a null active session renders retained real Work evidence', async ({ page }) => {
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    await openWorkTab(page);
    await expect(page.getByTestId('work-file-explorer')).toBeVisible();
    await expect(page.getByTestId('work-files-tree')).toContainText('work_conformance_target.py');
    await expect(page.getByTestId('work-panel-error')).toHaveCount(0);
  });

  for (const state of Object.keys(LABEL_CONVERSATION_IDS)) {
    const expectedLabel = state;
    test(`renders the server execution state "${expectedLabel}" in its conversation inspector`, async ({ page }) => {
      await login(page);
      await openConversation(page, LABEL_CONVERSATION_IDS[state]);
      await openInspector(page);
      const card = page.getByTestId('inspector-overview').getByTestId('activity-tree-row');
      await expect(card).toHaveCount(1);
      await expect(card).toContainText(expectedLabel[0].toUpperCase() + expectedLabel.slice(1));
      await expect(card.locator('.animate-spin')).toHaveCount(0);
    });
  }

  test('rotated physical sessions collapse into one logical node without duplicated evidence', async ({ page }) => {
    await login(page);
    await openConversation(page, ROTATION_CONVERSATION_ID);
    const projection = await page.evaluate(async (conversationId) => {
      const response = await fetch(
        `/api/v1/chat/v2/conversations/${conversationId}/work?category=files`,
        { credentials: 'include' },
      );
      if (!response.ok) throw new Error(`Work projection failed: ${response.status}`);
      return response.json();
    }, ROTATION_CONVERSATION_ID);
    expect(projection.workstreams).toHaveLength(1);
    expect(projection.workstreams[0].backing_session_count).toBe(2);
    expect([...projection.workstreams[0].backing_session_ids].sort()).toEqual([
      'sess_work_conformance_rotation_a_v2',
      'sess_work_conformance_rotation_b_v2',
    ]);

    await openWorkTab(page);
    await expect(page.getByTestId('work-file-explorer')).toBeVisible();
    await expect(
      page.getByTestId('work-files-tree').getByText('work_conformance_rotation_a.py'),
    ).toHaveCount(1);
    await expect(
      page.getByTestId('work-files-tree').getByText('work_conformance_rotation_b.py'),
    ).toHaveCount(1);
  });

  test('owner-wide invalidation converges without overlapping fetches or unbounded confirmation polling', async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWebSocket = window.WebSocket;
      const frames: unknown[] = [];
      Object.defineProperty(window, '__workConformanceFrames', { value: frames });
      class RecordingWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          this.addEventListener('message', (event) => {
            if (typeof event.data !== 'string') return;
            try {
              const payload = JSON.parse(event.data);
              if (payload.type === 'work_invalidated') frames.push(payload);
            } catch {
              // Ignore non-JSON protocol frames.
            }
          });
        }
      }
      window.WebSocket = RecordingWebSocket;
    });
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    await openWorkTab(page);
    await expect(page.getByTestId('work-file-explorer')).toBeVisible();

    const initialRevision = await page.evaluate(async (conversationId) => {
      const response = await fetch(
        `/api/v1/chat/v2/conversations/${conversationId}/work?category=files`,
        { credentials: 'include' },
      );
      return Number((await response.json()).work_revision);
    }, FILES_CONVERSATION_ID);
    let workRequests = 0;
    const inFlight = new Set<unknown>();
    let peakInFlight = 0;
    let latestCompletedRevision = initialRevision;
    const pendingResponses: Promise<void>[] = [];
    const isFilesRequest = (url: string) => {
      const parsed = new URL(url);
      return parsed.pathname === `/api/v1/chat/v2/conversations/${FILES_CONVERSATION_ID}/work`
        && parsed.searchParams.get('category') === 'files';
    };
    page.on('request', (request) => {
      if (isFilesRequest(request.url())) {
        workRequests += 1;
        inFlight.add(request);
        peakInFlight = Math.max(peakInFlight, inFlight.size);
      }
    });
    page.on('requestfinished', (request) => inFlight.delete(request));
    page.on('requestfailed', (request) => inFlight.delete(request));
    page.on('response', (response) => {
      if (!isFilesRequest(response.url())) return;
      pendingResponses.push((async () => {
        expect(response.ok()).toBe(true);
        const body = await response.json();
        latestCompletedRevision = Math.max(latestCompletedRevision, Number(body.work_revision));
      })());
    });

    const triggerId = `work-conformance-trigger-${crypto.randomUUID()}`;
    let createdId: string | null = null;
    try {
      createdId = await page.evaluate(async ({ agentId, title }) => {
        const response = await fetch('/api/v1/conversations', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            agent_id: agentId,
            title,
            context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
          }),
        });
        if (!response.ok) throw new Error(`Topology mutation failed: ${response.status}`);
        return (await response.json()).conversation_id as string;
      }, { agentId: 'e2e-test-agent', title: triggerId });

      await page.waitForFunction(
        ({ revision }) => {
          const frames = (window as Window & { __workConformanceFrames?: Array<Record<string, unknown>> })
            .__workConformanceFrames ?? [];
          return frames.some(
            (frame) =>
              frame.work_scope_key === '*' &&
              Number(frame.revision) > revision,
          );
        },
        { revision: initialRevision },
      );
      const wildcardFrame = await page.evaluate((revision) => {
        const frames = (window as Window & { __workConformanceFrames?: Array<Record<string, unknown>> })
          .__workConformanceFrames ?? [];
        return frames.find(
          (frame) => frame.work_scope_key === '*' && Number(frame.revision) > revision,
        );
      }, initialRevision);
      expect(wildcardFrame).toMatchObject({ type: 'work_invalidated', work_scope_key: '*' });
      expect(Number(wildcardFrame?.revision)).toBeGreaterThan(initialRevision);
      await expect.poll(() => latestCompletedRevision, { timeout: 5_000 })
        .toBeGreaterThanOrEqual(Number(wildcardFrame?.revision));
      // Recovery confirmation is intentionally bounded by WorkView's 15s
      // reconciliation window. Observe beyond that window, not an arbitrary
      // count that mistakes confirmation reads for duplicate invalidations.
      await page.waitForTimeout(20_000);
      await Promise.all(pendingResponses);
      await expect.poll(() => inFlight.size).toBe(0);
      expect(peakInFlight).toBeLessThanOrEqual(1);
      const settledRequests = workRequests;
      await page.waitForTimeout(3_000);
      expect(workRequests).toBe(settledRequests);
      await Promise.all(pendingResponses);
    } finally {
      if (createdId) {
        await page.evaluate(async (conversationId) => {
          const response = await fetch(`/api/v1/conversations/${conversationId}`, {
            method: 'DELETE',
            credentials: 'include',
          });
          if (!response.ok) throw new Error(`Trigger cleanup failed: ${response.status}`);
        }, createdId);
      }
    }
  });

  test('failed refresh retains the last good snapshot and selection', async ({ page }) => {
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    await openWorkTab(page);
    await expect(page.getByTestId('work-file-explorer')).toBeVisible();
    const fileRow = page
      .getByTestId('work-files-tree')
      .getByText('work_conformance_target.py')
      .locator('..');
    await fileRow.click();
    await expect(page.getByTestId('work-diff-pane')).toContainText('handler');

    await page.route('**/api/v1/chat/v2/**/work**', (route) =>
      route.fulfill({ status: 500, body: 'seeded failure' }),
    );
    const refreshButton = page.getByRole('button', { name: 'Refresh work' });
    await expect(refreshButton).toBeVisible();
    const failedResponse = page.waitForResponse(
      (response) => response.url().includes('/work') && response.status() === 500,
    );
    await refreshButton.click();
    await failedResponse;
    await expect(page.getByTestId('work-file-explorer')).toBeVisible();
    await expect(fileRow).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('work-diff-pane')).toContainText('handler');
    await page.unroute('**/api/v1/chat/v2/**/work**');
  });

  async function proveVisibleRefresh(page: Page): Promise<void> {
    await page.addInitScript(() => {
      window.localStorage.setItem('cognis:work-command-label-mode', 'command');
    });
    await page.addInitScript(() => {
      const NativeWebSocket = window.WebSocket;
      const frames: Array<Record<string, unknown>> = [];
      Object.defineProperty(window, '__workRefreshFrames', { value: frames });
      class RecordingWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          this.addEventListener('message', (event) => {
            if (typeof event.data !== 'string') return;
            try {
              const payload = JSON.parse(event.data);
              if (payload.type === 'work_invalidated') frames.push(payload);
            } catch {
              // Ignore non-JSON protocol frames.
            }
          });
        }
      }
      window.WebSocket = RecordingWebSocket;
    });
    let releaseAutomaticRecovery!: () => void;
    const automaticRecoveryReleased = new Promise<void>((resolve) => {
      releaseAutomaticRecovery = resolve;
    });
    let initialAutomaticRecovery = 0;
    await page.route('**/api/v1/work/refresh', async (route) => {
      initialAutomaticRecovery += 1;
      await automaticRecoveryReleased;
      await route.continue();
    });
    let browserIntarisRequests = 0;
    page.on('request', (request) => {
      if (request.url().includes(':8060/')) browserIntarisRequests += 1;
    });
    await login(page);
    const fixture = await page.evaluate(async (conversationId) => {
      const prepared = await fetch('/api/v1/chat/v2/e2e/work-catching-up', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ conversation_id: conversationId }),
      });
      if (!prepared.ok) throw new Error(`Catching-up fixture failed: ${prepared.status}`);
      const state = await prepared.json();
      const projection = await fetch(
        `/api/v1/chat/v2/conversations/${conversationId}/work?category=commands`,
        { credentials: 'include', headers: { 'x-e2e-direct': '1' } },
      );
      if (!projection.ok) throw new Error(`Work precondition failed: ${projection.status}`);
      return { state, projection: await projection.json() };
    }, REFRESH_CONVERSATION_ID);
    expect(fixture.state.covered_through_seq).toBeLessThan(fixture.state.target_seq);
    expect(fixture.projection.materialization.state).toBe('catching_up');
    await openConversation(page, REFRESH_CONVERSATION_ID);
    await openWorkTab(page);
    await expect(page.getByText('Catching up activity…', { exact: true })).toBeVisible();
    expect(initialAutomaticRecovery).toBe(1);
    const commandMode = page.getByRole('button', { name: 'Command', exact: true });
    if (await commandMode.count()) await commandMode.click();
    await page.getByTestId('work-tab-commands').click();
    const retainedCommand = page.getByRole('button', { name: /Running commands/ }).first();
    if (await retainedCommand.count()) await retainedCommand.click();
    await expect(page.getByText(REFRESH_OLD_COMMAND, { exact: true })).toBeVisible();
    await expect(page.getByText(REFRESH_NEW_COMMAND, { exact: true })).toHaveCount(0);

    const initialRevision = await page.evaluate(async (conversationId) => {
      const response = await fetch(
        `/api/v1/chat/v2/conversations/${conversationId}/work?category=commands`,
        { credentials: 'include' },
      );
      return Number((await response.json()).work_revision);
    }, REFRESH_CONVERSATION_ID);
    let workRefetches = 0;
    const uiSnapshots: Array<{ state: string; commands: number; revision: number; url: string }> = [];
    const snapshotReads: Promise<void>[] = [];
    page.on('request', (request) => {
      if (
        request.url().includes(`/conversations/${REFRESH_CONVERSATION_ID}/work`)
        && request.url().includes('category=commands')
      ) workRefetches += 1;
    });
    const recordWorkSnapshot = (response: import('@playwright/test').Response) => {
      const request = response.request();
      if (
        !response.ok()
        || request.headers()['x-e2e-direct'] === '1'
        || !response.url().includes(`/conversations/${REFRESH_CONVERSATION_ID}/work`)
        || !response.url().includes('category=commands')
      ) return;
      snapshotReads.push((async () => {
        const projection = await response.json();
        uiSnapshots.push({
          state: projection.materialization.state,
          commands: projection.summary.commands,
          revision: projection.work_revision,
          url: response.url(),
        });
      })());
    };
    page.on('response', recordWorkSnapshot);
    const refreshResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith('/api/v1/work/refresh')
        && response.request().method() === 'POST',
    );
    releaseAutomaticRecovery();
    const accepted = await refreshResponse;
    await page.unroute('**/api/v1/work/refresh');
    expect(accepted.status()).toBe(202);
    expect(await accepted.json()).toMatchObject({
      scope: {
        key: `conversation:${REFRESH_CONVERSATION_ID}`,
        conversation_id: REFRESH_CONVERSATION_ID,
      },
      session_count: 1,
    });
    expect(browserIntarisRequests).toBe(0);

    await expect(page.getByText('Catching up activity…', { exact: true })).toHaveCount(0, {
      timeout: 30_000,
    });
    await expect(page.getByRole('tab', { name: /Commands 2/ })).toBeVisible();
    page.off('response', recordWorkSnapshot);
    await Promise.all(snapshotReads);
    await expect.poll(
      () => uiSnapshots.some((snapshot) => snapshot.state === 'live' && snapshot.commands === 2),
      { timeout: 20_000 },
    ).toBe(true);
    await expect(
      page.getByRole('button', { name: new RegExp(REFRESH_NEW_COMMAND) }),
    ).toBeVisible();
    const liveSnapshot = uiSnapshots.find(
      (snapshot) => snapshot.state === 'live' && snapshot.commands === 2,
    );
    expect(liveSnapshot?.revision).toBeGreaterThanOrEqual(initialRevision);
    await expect.poll(async () => page.evaluate((revision) => {
      const frames = (
        window as Window & { __workRefreshFrames?: Array<Record<string, unknown>> }
      ).__workRefreshFrames ?? [];
      return frames.find(
        (frame) => frame.work_scope_key === 'conversation:conv_work_conformance_refresh' && Number(frame.revision) >= revision,
      ) ?? null;
    }, initialRevision)).not.toBeNull();
    const conversationFrame = await page.evaluate((revision) => {
      const frames = (
        window as Window & { __workRefreshFrames?: Array<Record<string, unknown>> }
      ).__workRefreshFrames ?? [];
      return frames.find(
        (frame) => frame.work_scope_key === 'conversation:conv_work_conformance_refresh' && Number(frame.revision) >= revision,
      );
    }, initialRevision);
    expect(conversationFrame).toMatchObject({
      type: 'work_invalidated', work_scope_key: `conversation:${REFRESH_CONVERSATION_ID}`,
    });
    expect(Number(conversationFrame?.revision)).toBeGreaterThanOrEqual(initialRevision);
    expect(workRefetches).toBeGreaterThanOrEqual(1);
    expect(workRefetches).toBeLessThanOrEqual(10);
  }

  test('selecting a file carries exact history identity into the real file-history request and diff', async ({ page }) => {
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    const historyRequest = page.waitForRequest(
      (request) =>
        request.url().includes('/api/v1/work/file-history') && request.method() === 'POST',
    );
    await openWorkTab(page);
    await page.getByTestId('work-files-tree').getByText('work_conformance_target.py').click();
    const request = await historyRequest;
    const body = request.postDataJSON() as { path_generation_id?: string };
    expect(typeof body.path_generation_id).toBe('string');
    expect(body.path_generation_id?.length).toBeGreaterThan(0);
    await expect(page.getByTestId('work-combined-diff')).toContainText('handler');
  });

  test('selected exact diff remains mounted across real owner-wide invalidations', async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWebSocket = window.WebSocket;
      const frames: Array<Record<string, unknown>> = [];
      Object.defineProperty(window, '__diffStabilityFrames', { value: frames });
      class RecordingWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          this.addEventListener('message', (event) => {
            if (typeof event.data !== 'string') return;
            try {
              const payload = JSON.parse(event.data);
              if (payload.type === 'work_invalidated') frames.push(payload);
            } catch {
              // Ignore non-JSON protocol frames.
            }
          });
        }
      }
      window.WebSocket = RecordingWebSocket;
    });
    let historyRequests = 0;
    page.on('request', (request) => {
      if (
        request.url().includes('/api/v1/work/file-history')
        && request.method() === 'POST'
      ) historyRequests += 1;
    });
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    await openWorkTab(page);
    const fileRow = page.getByTestId('work-files-tree').getByText('work_conformance_target.py');
    await fileRow.click();
    const diff = page.getByTestId('work-combined-diff');
    await expect(diff).toContainText('handler');
    await expect(page.getByText('truncated', { exact: true })).toHaveCount(0);
    await page.evaluate(() => {
      const target = document.querySelector('[data-testid="work-combined-diff"]');
      const state = { removed: false, empty: false, mutations: 0 };
      Object.defineProperty(window, '__diffStabilityDom', { value: state });
      if (!target) {
        state.removed = true;
        return;
      }
      new MutationObserver(() => {
        state.mutations += 1;
        state.removed ||= !target.isConnected;
        state.empty ||= !target.textContent?.includes('handler');
      }).observe(document.body, { childList: true, subtree: true, characterData: true });
    });

    const created: string[] = [];
    try {
      for (let index = 0; index < 3; index += 1) {
        const priorFrames = await page.evaluate(() =>
          (window as Window & { __diffStabilityFrames?: unknown[] })
            .__diffStabilityFrames?.length ?? 0
        );
        const conversationId = await page.evaluate(async (title) => {
          const response = await fetch('/api/v1/conversations', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              agent_id: 'e2e-test-agent',
              title,
              context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
            }),
          });
          if (!response.ok) throw new Error(`Invalidation trigger failed: ${response.status}`);
          return (await response.json()).conversation_id as string;
        }, `Work diff stability ${index}`);
        created.push(conversationId);
        await expect.poll(() => page.evaluate((count) =>
          ((window as Window & { __diffStabilityFrames?: unknown[] })
            .__diffStabilityFrames?.length ?? 0) > count
        , priorFrames)).toBe(true);
        await expect(diff).toContainText('handler');
      }
      const screenshot = await page.screenshot();
      expect(screenshot.byteLength).toBeGreaterThan(0);
      const domState = await page.evaluate(() =>
        (window as Window & {
          __diffStabilityDom?: { removed: boolean; empty: boolean; mutations: number };
        }).__diffStabilityDom
      );
      expect(domState).toMatchObject({ removed: false, empty: false });
      expect(historyRequests).toBeLessThanOrEqual(2);
      await expect(fileRow.locator('..')).toHaveAttribute('aria-selected', 'true');
    } finally {
      for (const conversationId of created) {
        await page.evaluate(async (id) => {
          await fetch(`/api/v1/conversations/${id}`, {
            method: 'DELETE',
            credentials: 'include',
          });
        }, conversationId);
      }
    }
  });

  test('mobile recent file row shows its filename tail and compact controls', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await openConversation(page, FILES_CONVERSATION_ID);
    await openInspector(page);

    const path = page.getByTestId('recent-file-path').first();
    await expect(path).toContainText('work_conformance_target.py');
    await expect(path).toHaveAttribute('title', /work_conformance_target\.py$/);
    const scroll = await path.evaluate((element) => ({
      left: element.scrollLeft,
      tail: Math.max(0, element.scrollWidth - element.clientWidth),
    }));
    expect(scroll.left).toBe(scroll.tail);
    await expect(page.getByTestId('recent-file-type').first()).toBeVisible();
    const expand = page.getByRole('button', { name: /Expand .*work_conformance_target\.py/ }).first();
    await expect(expand).toBeVisible();
    await expand.click();
    await expect(page.getByTestId('recent-file-time').first()).toHaveText(/\d{2}:\d{2} · .+/);
    await expect(page.getByRole('button', { name: /Open .*work_conformance_target\.py in Work/ }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: /Open .*work_conformance_target\.py in Work/ }).first()).toHaveAttribute('title', 'Open in Work');
    await expect(page.getByText('Open in Work', { exact: true })).toHaveCount(0);
    await path.evaluate((element) => { element.scrollLeft = 0; });
    expect(await path.evaluate((element) => element.scrollLeft)).toBe(0);
  });
});
