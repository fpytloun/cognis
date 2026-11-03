import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';
import { Buffer } from 'node:buffer';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import type { NativeConfiguration } from '../src/lib/native/host';

test.use({ serviceWorkers: 'block' });
const email = 'owner@cognis.test';
const storageKey = `cognis.conversation-workspace.v1:${encodeURIComponent(email)}`;
const avatarUrl = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#475569"/><circle cx="32" cy="32" r="20" fill="#38bdf8"/></svg>')}`;
const chats = [
  { conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', title: 'Kitchen renovation', project_id: 'proj_home' },
  { conversation_id: 'conv-task-chat-b', agent_id: 'agent-stage39', title: 'Garden planning', project_id: 'proj_other' },
  { conversation_id: 'conv-second-agent', agent_id: 'agent-second', title: 'Editorial notes', project_id: null },
];
const homes = [
  { conversation_id: 'conv-home-butler', agent_id: 'agent-stage39', title: 'Butler', project_id: null, context: { type: 'web', platform_data: { kind: 'agent_direct' } } },
  { conversation_id: 'conv-home-editor', agent_id: 'agent-second', title: 'Editor', project_id: null, context: { type: 'web', platform_data: { kind: 'agent_direct' } } },
];

test.describe('dashboard retained-capacity restore', () => {
  async function capacityFixture(page: Page) {
    const state = await fixture(page, false);
    await page.setViewportSize({ width: 1440, height: 900 });
    const target = { ...chats[0], status: 'idle', context: { type: 'web' }, has_unread: true };
    const fresh = { ...target, conversation_id: 'conv-capacity-new', title: 'New capacity conversation' };
    const reads: string[] = [];
    page.on('request', (request) => {
      const match = new URL(request.url()).pathname.match(/^\/api\/v1\/conversations\/([^/]+)\/read$/);
      if (request.method() === 'POST' && match) reads.push(match[1]);
    });
    const retained = [
      ...Array.from({ length: 9 }, (_, i) => ({ ...target, conversation_id: `conv-capacity-${i}`, title: `Retained ${i}` })),
      target,
    ];
    await page.addInitScript(({ storageKey, retained }) => {
      localStorage.setItem(storageKey, JSON.stringify({
        version: 1, position: 0.6, migrated: [], tucked: false,
        agents: [{ agentId: 'agent-stage39' }],
        entries: retained.map((chat, i) => ({
          projectId: chat.project_id, layouts: {},
          window: {
            kind: 'conversation', entityId: chat.conversation_id, key: `conversation:${chat.conversation_id}`,
            title: chat.title, agentId: chat.agent_id, canonicalHref: `/chat/${chat.conversation_id}`,
            rect: { x: 1000, y: 80, width: 400, height: 300 }, restoreRect: null,
            minimized: i >= 4, zOrder: i + 1, switcherOrder: i + 1, refreshToken: 0, focusRevision: 1,
          },
        })),
      }));
    }, { storageKey, retained });
    await page.route('**/api/v1/conversations?**', (route) => route.fulfill({
      json: { items: [target, fresh], has_more: false, next_cursor: null },
    }));
    await page.route('**/api/v1/conversations/conv-capacity-*', (route) => {
      const parts = new URL(route.request().url()).pathname.split('/');
      const id = parts[parts.length - 1];
      return route.fulfill({ json: retained.find((chat) => chat.conversation_id === id) ?? fresh });
    });
    await page.goto('/');
    await expect(page.getByTestId(`dashboard-conversation-row-${target.conversation_id}`).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('[data-workspace-window][data-minimized="false"], .workspace-window[data-minimized="false"]')).toHaveCount(4);
    await page.getByTestId('workspace-dismiss-backdrop').click({ position: { x: 20, y: 15 } });
    await expect(page.locator('[data-workspace-window][data-minimized="false"], .workspace-window[data-minimized="false"]')).toHaveCount(0);
    // Calibrate the independent recorder against an actual browser request.
    // Route fulfillment and fixture conversation membership must not hide reads.
    await page.evaluate(async (id) => {
      await fetch(`/api/v1/conversations/${id}/read`, { method: 'POST' });
    }, fresh.conversation_id);
    expect(reads).toContain(fresh.conversation_id);
    reads.length = 0;
    return { state: { ...state, reads: () => reads }, target, fresh };
  }

  test('list restores existing minimized identity at ten retained with repeated clicks', async ({ page }) => {
    const { state, target } = await capacityFixture(page);
    const row = page.getByTestId(`dashboard-conversation-row-${target.conversation_id}`).first();
    await row.click({ position: { x: 20, y: 15 } });
    const panel = page.getByTestId(`workspace-window-conversation:${target.conversation_id}`);
    await expect(panel).toHaveAttribute('data-minimized', 'false');
    await page.getByTestId('workspace-dismiss-backdrop').click({ position: { x: 20, y: 15 } });
    await expect(panel).toHaveAttribute('data-minimized', 'true');
    await row.click({ position: { x: 20, y: 15 } });
    await expect(panel).toHaveAttribute('data-minimized', 'false');
    await expect(panel).toHaveAttribute('data-focused', 'true');
    await expect(page.getByText(/limit reached/i)).toHaveCount(0);
    const saved = await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!).entries, storageKey);
    expect(saved).toHaveLength(10);
    expect(saved.filter((entry: { window: { entityId: string } }) => entry.window.entityId === target.conversation_id)).toHaveLength(1);
    expect(state.errors).toEqual([]);
  });

  test('new identity reports true retained cap without false read or attention window', async ({ page }) => {
    const { state, fresh } = await capacityFixture(page);
    await page.getByTestId(`dashboard-conversation-row-${fresh.conversation_id}`).first().click({ position: { x: 20, y: 15 } });
    await expect(page.getByText(/Retained window limit reached \(10\), including minimized windows/)).toBeVisible();
    await expect(page.getByTestId(`workspace-window-conversation:${fresh.conversation_id}`)).toHaveCount(0);
    await expect(page.locator('[data-window-key^="attention:"]')).toHaveCount(0);
    expect(state.reads()).not.toContain(fresh.conversation_id);
    expect(await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!).entries.length, storageKey)).toBe(10);
  });

  test('delayed permission failure stays a failed open and never marks the unopened chat read', async ({ page }) => {
    const { state, fresh } = await capacityFixture(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let requested!: () => void;
    const started = new Promise<void>((resolve) => { requested = resolve; });
    await page.route(`**/api/v1/conversations/${fresh.conversation_id}?**`, async (route) => {
      requested();
      await gate;
      await route.fulfill({ status: 403, json: { detail: 'Synthetic permission denied' } });
    });
    const row = page.getByTestId(`dashboard-conversation-row-${fresh.conversation_id}`).first();
    await row.click({ position: { x: 20, y: 15 } });
    await started;
    await row.click({ position: { x: 20, y: 15 } });
    expect(state.reads()).not.toContain(fresh.conversation_id);
    release();
    // Each click reports its outcome even when the owner request is singleflight.
    const failures = page.getByText(/Could not open workspace window \(/);
    await expect(failures).toHaveCount(2);
    await expect(failures.first()).toBeVisible();
    await expect(page.getByText(/limit reached/i)).toHaveCount(0);
    expect(state.reads()).not.toContain(fresh.conversation_id);
    await expect(page.getByTestId(`workspace-window-conversation:${fresh.conversation_id}`)).toHaveCount(0);
  });
});

async function fixture(page: Page, seed = true, windows = true, display: Record<string, boolean> = {}, chatCount = 3,
  workspaceOptions: { tucked?: boolean } = {}) {
  const errors: string[] = [];
  page.on('pageerror', (error) => { errors.push(error.message); console.error(error.message); });
  await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: windows });
  let preferences = { display: { theme: 'dark', language: 'en', dashboard_workspace_windows: windows,
    pinned_agents_collapsed: true, pinned_agents_order_by_activity: true, ...display }, chat: {}, notifications: {} };
  const preferenceUpdates: unknown[] = [];
  await page.route('**/api/v1/user-preferences', (route) => {
    if (route.request().method() === 'PUT') {
      preferences = route.request().postDataJSON();
      preferenceUpdates.push(preferences);
    }
    return route.fulfill({ json: preferences });
  });
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email, role: 'user', name: 'Owner' } }));
  await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
  await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
    generated_at: '2026-10-14T08:00:00Z', summary: { total: 0, warning: 0, critical: 0, info: 0, truncated: false }, issues: [],
  } }));
  await page.route('**/api/v1/agents?**', (route) => route.fulfill({ json: { items: [
    { agent_id: 'agent-stage39', name: 'Butler', display_name: 'Butler', avatar_url: avatarUrl, status: 'active', agent_type: 'primary',
      default_agent_profile_id: 'default', agent_profiles: { default: { enabled: true }, developer: { enabled: true, quick_switch: true, quick_switch_color: 'blue' } } },
    { agent_id: 'agent-second', name: 'Editor', display_name: 'Editor', avatar_url: avatarUrl, status: 'active', agent_type: 'primary', profiles: [] },
  ], has_more: false, next_cursor: null } }));
  let readCalls: string[] = [];
  let active = true;
  let lastMessage = '2026-10-14T08:00:00Z';
  let detailCalls = 0;
  const activityTimes = new Map<string, string>();
  const openedTimes = new Map<string, string>();
  let allRead = false;
  const attention = new Set<string>();
  let created = 0;
  const creations: Record<string, unknown>[] = [];
  await page.route('**/api/v1/conversations/resolve', (route) => {
    const body = route.request().postDataJSON();
    return route.fulfill({ json: { ...homes.find((item) => item.agent_id === body.agent_id), status: 'active' } });
  });
  await page.route('**/api/v1/conversations?**', (route) => {
    const url = new URL(route.request().url());
    const query = url.searchParams.get('q') ?? url.searchParams.get('query') ?? '';
    const items = chats.filter((item) => item.agent_id === url.searchParams.get('agent_id') && item.title.toLowerCase().includes(query.toLowerCase()));
    return route.fulfill({ json: { items: [
      ...items.map((item) => ({ ...item, context: { type: 'web' }, starred_at: item === chats[0] ? '2026-10-14T08:00:00Z' : null })),
      { ...chats[0], conversation_id: 'conv-task-control', title: 'Task control excluded', context: { type: 'web', platform_data: { kind: 'task_control' } } },
      { ...chats[0], conversation_id: 'conv-channel', title: 'Channel excluded', context: { type: 'slack' } },
    ], has_more: false, cursor: null } });
  });
  await page.route('**/api/v1/conversations', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = route.request().postDataJSON();
    creations.push(body);
    const chat = { conversation_id: `conv-created-${++created}`, agent_id: body.agent_id, title: 'New conversation', project_id: body.project_id };
    chats.push(chat);
    return route.fulfill({ json: { ...chat, context: { type: 'web' }, status: 'active' } });
  });
  await page.route('**/api/v1/conversations/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const chat = [...chats, ...homes].find((item) => path.split('/')[4] === item.conversation_id);
    if (!chat) return route.fallback();
    if (path.endsWith('/read')) {
      readCalls.push(chat.conversation_id);
      return route.fulfill({ json: { ok: true } });
    }
    if (path.endsWith(chat.conversation_id)) {
      detailCalls++;
      return route.fulfill({ json: {
      ...chat, status: 'active', context: homes.includes(chat as typeof homes[number]) ? { type: 'web', platform_data: { kind: 'agent_direct' } } : { type: 'web' }, user_email: email, active_session_id: null,
      has_active_turn: chat.conversation_id === 'conv-second-agent' && active,
       has_unread: !allRead && !readCalls.includes(chat.conversation_id), pending_notification_types: attention.has(chat.conversation_id) ? ['step_question'] : [],
        last_message_at: activityTimes.get(chat.conversation_id) ?? lastMessage,
        updated_at: openedTimes.get(chat.conversation_id), active_session_updated_at: openedTimes.get(chat.conversation_id),
      } });
    }
    return route.fallback();
  });
  await page.route('**/api/v1/projects/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith('/conversations/page') ? { items: [{ ...chats[0], status: 'active', context: { type: 'web' }, last_message_at: lastMessage }], cursor: null, has_more: false }
      : path.endsWith('/tasks/page') ? { items: [], cursor: null, has_more: false }
      : path.endsWith('/agents') ? []
      : path.endsWith('/activity') ? { deliverables: [], artifacts: [] }
      : { project_id: 'proj_home', name: 'Home', description: '', status: 'active', instructions: '', sources: [], workflow_ids: [], agents: [], grants: [], caller_role: 'owner' };
    return route.fulfill({ json });
  });
  if (seed) await page.addInitScript(({ storageKey, chats, homes, tucked }) => {
    if (localStorage.getItem(storageKey)) return;
    localStorage.setItem(storageKey, JSON.stringify({
      version: 1, position: 0.6, migrated: [], tucked,
      agents: homes.map((home) => ({ agentId: home.agent_id, homeConversationId: home.conversation_id })),
      entries: chats.map((chat, index) => ({
        projectId: chat.project_id, layouts: {},
        window: {
          kind: 'conversation', entityId: chat.conversation_id, key: `conversation:${chat.conversation_id}`,
          title: chat.title, agentId: chat.agent_id, canonicalHref: `/chat/${chat.conversation_id}`,
          rect: { x: 320 + index * 20, y: 70 + index * 20, width: 720, height: 600 },
          restoreRect: null, minimized: false, zOrder: index + 1, switcherOrder: index + 1,
          refreshToken: 0, focusRevision: 1,
        },
      })),
    }));
  }, { storageKey, chats: chats.slice(0, chatCount), homes, tucked: workspaceOptions.tucked ?? false });
  return { errors, reads: () => readCalls, settle: () => { active = false; },
    readAll: () => { allRead = true; },
    opened: (id: string, time: string) => { openedTimes.set(id, time); },
    preferences: () => preferences, preferenceUpdates: () => preferenceUpdates,
    details: () => detailCalls,
    creations: () => creations,
    activity: (id: string, time: string, needsAttention = false) => { activityTimes.set(id, time); if (needsAttention) attention.add(id); },
    newMessage: (time: string) => { readCalls = []; lastMessage = time; } };
}

test('native-pinned root cold-loads preferences without the web dock (synthetic host only)', async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.preferences().chat, { keep_assistant_messages_separate: true, enter_to_send: false });
  await page.addInitScript(() => {
    const host = window as Window & { __cognisNative?: NativeConfiguration; webkit?: { messageHandlers: { cognis: { postMessage(): void } } } };
    host.__cognisNative = { version: 1, generation: crypto.randomUUID(), server: location.origin, surface: 'pinned' };
    host.webkit = { messageHandlers: { cognis: { postMessage() {} } } };
  });
  await page.goto('/native-pinned?agent=agent-stage39&conversation=conv-task-chat');
  await expect.poll(() => page.evaluate(() => {
    const key = Object.keys(localStorage).find((key) => key.startsWith('cognis_user_preferences:account-v2:'));
    return key ? JSON.parse(localStorage.getItem(key)!).chat : null;
  })).toMatchObject({ keep_assistant_messages_separate: true, enter_to_send: false });
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('compact preference parity on cold open, live scoped invalidation and dense expanded layout', async ({ page, context }) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1440, height: 1100 });
  const state = await fixture(page, true, true, {}, 1);
  Object.assign(state.preferences().chat, { keep_assistant_messages_separate: true, group_tool_calls: true });
  const base = { source_refs: [], stable: true, turn_id: 'layout-turn', turn_cycle_index: 0, assistant_phase_index: 0 };
  const tool = (index: number) => ({ ...base, id: `layout-tool-${index}`, kind: 'tool_call', sort_key: `000${index}`,
    call_id: `layout-call-${index}`, tool_name: 'bash', arguments: { command: 'true', description: `Layout command ${index}` },
    status: index % 2 === 0 ? 'failed' : 'complete', is_error: index % 2 === 0, attachments: [], file_diffs: [],
    truncated: false, has_full_output: false });
  await page.route('**/api/v1/chat/v2/conversations/*/snapshot', (route) => {
    const id = new URL(route.request().url()).pathname.split('/')[6];
    return route.fulfill({ json: {
      schema_version: 2, projection_version: 'layout-parity',
      scope: { key: `conversation:${id}`, kind: 'conversation', conversation_id: id },
      conversation: { conversation_id: id, agent_id: 'agent-stage39', status: 'active' },
      timeline: { items: [tool(1), tool(2), tool(3),
        { ...base, id: 'layout-message', kind: 'message', sort_key: '0004', message_id: 'layout-message',
          role: 'assistant', content: 'Layout **Markdown** in order.', attachments: [], partial: false },
        tool(5), tool(6), tool(8)], has_more_before: false, before_cursor: null },
      state: { state_version: 1, capabilities: [], active_turn: {}, pending: {}, active_session: {} },
      queue: { messages: [], queued_count: 0 },
      runtime: { runtime_epoch: 'layout-epoch', runtime_revision: 0, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
      cursor: 'layout-cursor', server_time: '',
    } });
  });
  // Exercise the shared CompactConversationChat without depending on the
  // independently maintained tray launch/expansion lifecycle.
  await page.goto('/');
  const panel = page.getByTestId('workspace-window-conversation:conv-task-chat');
  await expect(panel).toBeVisible({ timeout: 20_000 });
  await expect(panel.locator('strong', { hasText: 'Markdown' })).toHaveCount(1);
  await expect(panel.locator('[data-kind="activity_segment_assistant"]')).toHaveCount(0);
  const key = await page.evaluate(() => Object.keys(localStorage).find((key) => key.startsWith('cognis_user_preferences:account-v2:')));
  expect(key).toBeTruthy();
  // A genuine second document emits a storage event. Its payload is deliberately
  // opposite the canonical API value: only the scoped hint may be consumed.
  const other = await context.newPage();
  await other.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Storage peer</title>' }));
  await other.goto('/storage-peer');
  Object.assign(state.preferences().chat, { keep_assistant_messages_separate: false });
  await other.evaluate(({ key }) => localStorage.setItem(key!, JSON.stringify({ chat: { keep_assistant_messages_separate: true } })), { key });
  await expect(panel.locator('[data-kind="activity_segment"]')).toHaveCount(1);
  const expand = panel.locator('[data-kind="activity_segment"] > button');
  await expand.click();
  await expect(panel.locator('[data-kind="tool_call"]')).toHaveCount(6);
  await expect(panel.locator('[data-tool-status="failed"]')).toHaveCount(3);
  await expect(panel.locator('[data-kind="activity_segment_assistant"] strong')).toHaveText('Markdown');
  const gaps = await panel.locator('[data-kind="activity_segment"] > div').evaluate((body) => {
    const rows = Array.from(body.children).map((node) => node.getBoundingClientRect());
    return rows.slice(1).map((row, index) => row.top - rows[index].bottom);
  });
  expect(gaps.length).toBe(6);
  for (const gap of gaps) expect(gap).toBeLessThanOrEqual(9);
  await other.close();
  expect(state.errors).toEqual([]);
});

for (const standalone of [false, true]) {
test(`avatar selection persists through tabs and genuine dismissals (standalone=${standalone})`, async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  if (standalone) await page.addInitScript(() => {
    const original = window.matchMedia.bind(window);
    window.matchMedia = (query) => {
      const media = original(query);
      if (query === '(display-mode: standalone)') Object.defineProperty(media, 'matches', { value: true });
      return media;
    };
  });
  const state = await fixture(page, true, true, { pinned_agents_collapsed: false });
  await page.goto('/notifications');
  const avatar = page.locator('[data-pinned-agent="agent-stage39"]');
  const panel = page.getByTestId('pinned-chat-panel');
  await expect(avatar).toHaveAccessibleName('Open pinned chats with Butler');
  await avatar.click();
  await panel.getByRole('tab', { name: /Kitchen renovation/ }).click();
  await panel.locator('textarea').first().fill('Persistent avatar draft');
  await panel.getByRole('tab', { name: /Garden planning/ }).click();
  await panel.getByRole('tab', { name: /Kitchen renovation/ }).click();
  await expect(panel.locator('textarea').first()).toHaveValue('Persistent avatar draft');
  expect(state.reads()).not.toContain('conv-second-agent');
  await page.getByTestId('pinned-chat-minimize').click();
  await expect(panel).toHaveCount(0);
  await avatar.click();
  await expect(panel.locator('textarea').first()).toHaveValue('Persistent avatar draft');
  await panel.getByRole('link', { name: 'Open full conversation' }).click();
  await expect(page).toHaveURL(/\/chat\/conv-task-chat$/);
  await expect(panel).toHaveCount(0);
  expect(state.errors).toEqual([]);
});
}

test('stack expands agents, tabs use native chat, drafts survive, click-away collapses, and drag stays right', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher).toBeVisible();
  await expect(launcher.getByTestId('activity-avatar-orbit')).toBeVisible();
  const avatarButtons = page.locator('[data-pinned-agent]');
  await expect(avatarButtons).toHaveCount(2);
  const sameAvatars = await avatarButtons.elementHandles();
  expect(state.reads()).toEqual([]);
  await launcher.click();
  await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
  await expect(avatarButtons).toHaveCount(2);
  expect(await Promise.all(sameAvatars.map((handle) => handle.evaluate((node) => node.isConnected)))).toEqual([true, true]);
  await expect.poll(async () => {
    const rects = await avatarButtons.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect()));
    return Math.abs(rects[0].y - rects[1].y) - rects[0].height;
  }).toBeGreaterThan(4);
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  const panel = page.getByTestId('pinned-chat-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('pinned-chat-veil')).toHaveCSS('backdrop-filter', 'blur(1px)');
  await expect(panel.getByRole('tab', { name: /Kitchen renovation/ })).toBeVisible();
  expect(state.errors).toEqual([]);
  await expect(panel.getByRole('tab', { name: /Garden planning/ })).toBeVisible();
  await expect(panel.getByRole('tab', { name: 'Home', exact: true })).toBeVisible();
  await panel.getByRole('tab', { name: /Kitchen renovation/ }).click();
  await expect.poll(state.reads).toContain('conv-task-chat');
  expect(state.reads()).not.toContain('conv-task-chat-b');
  const composer = panel.locator('textarea').first();
  await composer.fill('Keep this draft while browsing');
  await panel.getByRole('tab', { name: /Garden planning/ }).click();
  await panel.getByRole('tab', { name: /Kitchen renovation/ }).click();
  await expect(panel.locator('textarea').first()).toHaveValue('Keep this draft while browsing');
  await page.screenshot({ path: test.info().outputPath('pinned-chat-desktop.png') });
  await page.mouse.click(350, 100);
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('pinned-agent-strip')).toHaveCount(0);
  const before = (await launcher.boundingBox())!;
  await expect(launcher.locator('img')).toHaveAttribute('draggable', 'false');
  await page.evaluate(() => {
    document.documentElement.dataset.nativeAvatarDrags = '0';
    document.addEventListener('dragstart', () => {
      document.documentElement.dataset.nativeAvatarDrags = String(Number(document.documentElement.dataset.nativeAvatarDrags) + 1);
    });
  });
  await page.mouse.move(before.x + 24, before.y + 24);
  await page.mouse.down();
  await page.mouse.move(before.x - 250, before.y - 180, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => (await launcher.boundingBox())!.y).toBeLessThan(before.y - 100);
  await expect.poll(async () => launcher.evaluate((node) => node.parentElement!.getAnimations().length)).toBe(0);
  const after = (await launcher.boundingBox())!;
  expect(after.x).toBe(before.x);
  expect(after.y).toBeLessThan(before.y - 100);
  expect(await page.evaluate(() => document.documentElement.dataset.nativeAvatarDrags)).toBe('0');
  await expect(page.getByTestId('pinned-agent-strip')).toHaveCount(0);
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await page.reload();
  await expect(launcher).toBeVisible();
  await expect.poll(async () => Math.abs((await launcher.boundingBox())!.y - after.y)).toBeLessThan(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(launcher).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('opening and metadata reads do not reorder agents, but new messages and notifications do', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, { pinned_agents_collapsed: false });
  state.settle(); state.readAll();
  state.activity('conv-home-butler', '2020-01-01T08:00:00Z');
  state.activity('conv-task-chat', '2020-01-01T08:00:00Z');
  state.activity('conv-task-chat-b', '2020-01-01T08:00:00Z');
  state.activity('conv-home-editor', '2020-01-01T09:00:00Z');
  state.activity('conv-second-agent', '2020-01-01T09:00:00Z');
  await page.routeWebSocket('**/api/ws', (ws) => ws.send(JSON.stringify({ type: 'authenticated' })));
  await page.goto('/notifications');
  const agents = page.locator('[data-pinned-agent]');
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-second');
  state.opened('conv-home-butler', '2099-01-01T00:00:00Z');
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-second');
  await page.keyboard.press('Escape');
  state.activity('conv-home-butler', '2020-01-01T10:00:00Z');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  // Same-priority notifications must use notification arrival, not old chat metadata.
  state.activity('conv-home-editor', '2020-01-01T09:00:00Z', true);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-second');
  state.activity('conv-home-butler', '2020-01-01T10:00:00Z', true);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  state.opened('conv-home-editor', '2100-01-01T00:00:00Z');
  await page.getByRole('button', { name: 'Open pinned chats with Editor' }).click();
  await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  expect(state.errors).toEqual([]);
});

test('block hover rings have paint room and expanded agents use a six-pixel gap', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, { pinned_agents_collapsed: false });
  await page.goto('/notifications');
  const scroll = page.getByTestId('pinned-agent-block-scroll');
  const faces = scroll.getByTestId('pinned-avatar-face');
  await expect(faces).toHaveCount(2);
  await faces.first().hover();
  await expect.poll(() => scroll.evaluate(node => node.getAnimations({ subtree: true })
    .filter(animation => animation.playState === 'running' && animation.effect?.getTiming().iterations !== Infinity).length)).toBe(0);
  const viewport = (await scroll.boundingBox())!;
  const first = (await faces.nth(0).boundingBox())!;
  const last = (await faces.nth(1).boundingBox())!;
  expect(first.y - viewport.y).toBeGreaterThanOrEqual(4);
  expect(viewport.y + viewport.height - last.y - last.height).toBeGreaterThanOrEqual(4);
  expect(first.x - viewport.x).toBeGreaterThanOrEqual(12);
  expect(last.y - first.y - first.height).toBe(6);
  await scroll.screenshot({ path: testInfo.outputPath('unclipped-hover-rings.png') });
  expect(state.errors).toEqual([]);
});

test('click-away consumes the first click without activating underlying background controls', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await page.evaluate(() => {
    const target = document.createElement('button');
    target.textContent = 'Background action'; target.dataset.testid = 'background-action';
    target.style.cssText = 'position:fixed;left:200px;top:150px;width:160px;height:50px;z-index:40';
    for (const kind of ['pointerdown', 'click']) target.addEventListener(kind, () => {
      target.dataset[kind] = String(Number(target.dataset[kind] ?? 0) + 1);
    });
    document.body.append(target);
  });
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveAttribute('data-pinned-agent', 'agent-second');
  await openButler(page);
  const panel = page.getByTestId('pinned-chat-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('pinned-chat-dismiss-backdrop')).toBeVisible();
  await page.mouse.click(250, 175);
  await expect(panel).toHaveCount(0);
  const target = page.getByTestId('background-action');
  expect(await target.getAttribute('data-click')).toBeNull();
  expect(await target.getAttribute('data-pointerdown')).toBeNull();
  await page.mouse.click(250, 175);
  await expect(target).toHaveAttribute('data-click', '1');
  await expect(target).toHaveAttribute('data-pointerdown', '1');
  expect(state.errors).toEqual([]);
});

async function openButler(page: Page): Promise<void> {
  await page.getByTestId('pinned-chat-launcher').click();
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
}

for (const { block, tucked } of [
  { block: false, tucked: true }, { block: true, tucked: true }, { block: false, tucked: false },
]) {
  test(`backdrop dismissal restores saved tuck after typing (${block ? 'block' : 'stack'}, tucked=${tucked})`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await fixture(page, true, true, { pinned_agents_collapsed: !block });
    await page.goto('/notifications');
    const panel = page.getByTestId('pinned-chat-panel');
    const open = async () => {
      if (block) await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
      else await openButler(page);
      await expect(panel).toBeVisible();
    };
    await open();
    if (tucked) {
      await panel.getByRole('button', { name: 'Tuck pinned avatars', exact: true }).click();
      await page.mouse.move(300, 100);
      await open();
    }
    const textarea = panel.locator('textarea').first();
    await textarea.fill('Draft remains after click-away');
    await textarea.press('End'); // Establish keyboard focus, not just pointer hover.
    await page.mouse.click(250, 175);
    await expect(panel).toHaveCount(0);
    const avatars = page.getByTestId(block ? 'pinned-agent-block' : 'pinned-agent-stack');
    await expect(avatars).toHaveAttribute('data-tucked', String(tucked));
    await expect(avatars).toHaveAttribute('data-visually-tucked', String(tucked));
    const launcher = page.getByTestId('pinned-chat-launcher');
    await launcher.hover();
    await expect(avatars).toHaveAttribute('data-visually-tucked', 'false');
    await page.mouse.move(300, 100);
    await expect(avatars).toHaveAttribute('data-visually-tucked', String(tucked));
    await open();
    await expect(textarea).toHaveValue('Draft remains after click-away');
    expect(state.errors).toEqual([]);
  });
}

test('Home remains without a close button after regular tabs close; agent X unpins the whole group', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await openButler(page);
  const panel = page.getByTestId('pinned-chat-panel');
  await expect(panel.getByRole('tab', { name: 'Home', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(panel.getByRole('button', { name: 'Unpin Home', exact: true })).toHaveCount(0);
  await expect(panel.getByRole('link', { name: 'Open full conversation' })).toHaveAttribute('href', '/chat/conv-home-butler');
  await panel.getByRole('button', { name: 'Unpin Kitchen renovation' }).click();
  await panel.getByRole('button', { name: 'Unpin Garden planning' }).click();
  await expect(panel.getByRole('tab')).toHaveCount(1);
  await page.reload();
  await openButler(page);
  await expect(panel.getByRole('tab')).toHaveCount(1);
  await panel.getByRole('button', { name: 'Unpin agent', exact: true }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.locator('[data-pinned-agent="agent-stage39"]')).toHaveCount(0);
  await expect(page.locator('[data-pinned-agent="agent-second"]')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('new-chat plus creates a conversation and the full-area picker filters web chats only', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/projects/proj_home');
  await openButler(page);
  const panel = page.getByTestId('pinned-chat-panel');
  await panel.getByRole('button', { name: 'New conversation', exact: true }).click();
  await expect(panel.getByRole('tab', { name: /New conversation/ })).toHaveAttribute('aria-selected', 'true');
  expect(state.creations()[0].project_id).toBe('proj_home');
  await panel.getByRole('tab', { name: /Garden planning/ }).click();
  await panel.getByRole('button', { name: 'New conversation', exact: true }).click();
  await expect.poll(() => state.creations().length).toBe(2);
  expect(state.creations()[1].project_id).toBe('proj_home');
  await panel.getByRole('button', { name: 'Find conversation' }).click();
  const picker = panel.getByTestId('pinned-conversation-picker');
  await expect(picker.getByRole('button', { name: /Kitchen renovation/ })).toBeVisible();
  await expect(picker.getByRole('button', { name: /Task control excluded|Channel excluded/ })).toHaveCount(0);
  await picker.getByRole('textbox', { name: 'Filter recent conversations' }).fill('Garden');
  await expect(picker.getByRole('button', { name: /Garden planning/ })).toBeVisible();
  await expect(picker.getByRole('button', { name: /Kitchen renovation/ })).toHaveCount(0);
  await picker.getByRole('button', { name: /Garden planning/ }).click();
  await expect(picker).toHaveCount(0);
  await expect(panel.getByRole('tab', { name: /Garden planning/ })).toHaveAttribute('aria-selected', 'true');
  await expect(panel.getByRole('button', { name: 'Find conversation' })).toBeVisible();
  await panel.getByRole('button', { name: 'Find conversation' }).click();
  await picker.getByRole('textbox', { name: 'Filter recent conversations' }).fill('no such conversation');
  await expect(picker.getByText('No matching conversations.')).toBeVisible();
  expect(state.errors).toEqual([]);
});

for (const surface of ['dashboard', 'full chat'] as const) {
  test(`shared composer sends attachments from ${surface} with an untouched sibling view`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const state = await fixture(page, true, true, {}, 1);
    const sends: Record<string, unknown>[] = [];
    await page.route('**/api/v1/artifacts/upload', (route) => route.fulfill({ json: {
      artifact_id: 'att_shared_composer_file', filename: 'shared.txt', kind: 'file', mime_type: 'text/plain', size_bytes: 5,
    } }));
    await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/messages/*', (route) => {
      if (route.request().method() !== 'PUT') return route.fallback();
      const body = route.request().postDataJSON();
      sends.push(body);
      return route.fulfill({ json: {
        status: 'accepted', conversation_id: 'conv-task-chat',
        client_txn_id: new URL(route.request().url()).pathname.split('/').pop(),
        client_message_id: body.client_message_id, server_time: '2026-10-23T08:00:00Z',
      } });
    });
    await page.goto(surface === 'dashboard' ? '/' : '/chat/conv-task-chat');
    const source = surface === 'dashboard'
      ? page.getByTestId('workspace-window-conversation:conv-task-chat')
      : page.locator('form').filter({ has: page.getByRole('textbox', { name: 'Chat message', exact: true }) });
    const input = source.locator('textarea').first();
    await expect(input).toBeEnabled({ timeout: 20_000 });
    // Dashboard workspace mode intentionally hides the dock. Pair it with
    // full chat in a second document; full chat also pairs with the web dock.
    const peer = surface === 'dashboard' ? await page.context().newPage() : page;
    const peerState = peer === page ? state : await fixture(peer, true, true, {}, 1);
    if (peer !== page) {
      await peer.goto('/chat/conv-task-chat');
    } else await openButler(peer);
    const pinned = peer === page ? peer.getByTestId('pinned-chat-panel')
      : peer.locator('form').filter({ has: peer.getByRole('textbox', { name: 'Chat message', exact: true }) });
    if (peer === page) await pinned.getByRole('tab', { name: /Kitchen renovation/ }).click();
    await expect(pinned.locator('textarea')).toHaveValue('');
    await input.fill('shared attachment');
    await expect(pinned.locator('textarea')).toHaveValue('shared attachment');
    await source.locator('input[type="file"]').setInputFiles({
      name: 'shared.txt', mimeType: 'text/plain', buffer: Buffer.from('hello'),
    });
    await expect(pinned.getByText('shared.txt', { exact: true })).toBeVisible();
    // Both views stay mounted; dispatch bypasses only the dock's click-away backdrop.
    await source.getByRole('button', { name: /^(Send|Send message|Queue message)$/ }).dispatchEvent('click');
    await expect.poll(() => sends.length).toBe(1);
    await expect(input).toHaveValue('');
    await expect(pinned.locator('textarea')).toHaveValue('');
    expect(sends[0]).toMatchObject({ content: 'shared attachment', attachments: [{ artifact_id: 'att_shared_composer_file' }] });
    await expect(page.getByText(/Draft changed in another window/)).toHaveCount(0);
    await expect(source.getByTestId('scoped-timeline-status-row')).toHaveCount(0);
    expect(state.errors).toEqual([]);
    expect(peerState.errors).toEqual([]);
    if (peer !== page) await peer.close();
  });
}

for (const editDuringRequest of [false, true]) {
  test(`failed full-chat command restores shared input without replacing newer edits (${editDuringRequest})`, async ({ page }) => {
    const state = await fixture(page);
    await page.setViewportSize({ width: 1440, height: 1000 });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let attempts = 0;
    await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/commands/*', async (route) => {
      attempts++;
      await gate;
      await route.fulfill({ status: 503, json: { error: { code: 'service_unavailable', message: 'Command transport unavailable' } } });
    });
    await page.goto('/chat/conv-task-chat');
    const input = page.getByRole('textbox', { name: 'Chat message', exact: true });
    await expect(input).toBeEnabled();
    await openButler(page);
    const pinned = page.getByTestId('pinned-chat-panel');
    await pinned.getByRole('tab', { name: /Kitchen renovation/ }).click();
    await input.fill('/info');
    await expect(pinned.locator('textarea')).toHaveValue('/info');
    const form = page.locator('form').filter({ has: input });
    await form.getByRole('button', { name: 'Send', exact: true }).dispatchEvent('click');
    await expect.poll(() => attempts).toBe(1);
    await expect(input).toHaveValue('');
    if (editDuringRequest) {
      await pinned.locator('textarea').fill('newer draft');
      await expect(input).toHaveValue('newer draft');
    }
    release();
    // A newer input invalidates the captured direct intent, so it must not retry.
    await expect.poll(() => attempts).toBe(editDuringRequest ? 1 : 2);
    if (!editDuringRequest) await expect(page.getByText('Command transport unavailable', { exact: true })).toBeVisible();
    else await expect(form.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
    const expected = editDuringRequest ? 'newer draft' : '/info';
    await expect(input).toHaveValue(expected);
    if (await pinned.locator('textarea').count() === 0) {
      await openButler(page);
      await pinned.getByRole('tab', { name: /Kitchen renovation/ }).click();
    }
    await expect(pinned.locator('textarea')).toHaveValue(expected);
    expect(state.errors).toEqual([]);
  });
}

test('saved tuck temporarily reveals on hover and chat opening, and toggle works both ways', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await openButler(page);
  await page.getByRole('button', { name: 'Tuck pinned avatars' }).click();
  await page.mouse.move(300, 100);
  const stack = page.getByTestId('pinned-agent-stack');
  await expect(stack).toHaveAttribute('data-tucked', 'true');
  await testInfo.attach('tucked-avatar-geometry', {
    contentType: 'application/json',
    body: Buffer.from(JSON.stringify(await page.getByTestId('pinned-chat-launcher')
      .getByTestId('pinned-avatar-face').evaluate(async (node) => {
        const button = node.closest('button')!;
        await Promise.all([...node.getAnimations(), ...button.getAnimations()]
          .filter(animation => animation.effect?.getTiming().iterations !== Infinity)
          .map(animation => animation.finished.catch(() => {})));
        return {
          viewportWidth: innerWidth, face: node.getBoundingClientRect().toJSON(),
          button: button.getBoundingClientRect().toJSON(),
          buttonBorder: getComputedStyle(button).borderLeftWidth,
          faceTransform: getComputedStyle(node).transform,
        };
      }))),
  });
  await expect.poll(async () => {
    const rect = (await page.getByTestId('pinned-chat-launcher').getByTestId('pinned-avatar-face').boundingBox())!;
    return Math.abs(rect.x + rect.width / 2 - 1440);
  }).toBeLessThan(2);
  const tuckedPreview = testInfo.outputPath('tucked-avatars-synthetic-preview.png');
  await page.screenshot({ path: tuckedPreview });
  await testInfo.attach('tucked-avatars-synthetic-preview', { path: tuckedPreview });
  await page.reload();
  await expect(stack).toHaveAttribute('data-tucked', 'true');
  await page.keyboard.press('Tab');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await launcher.focus();
  await launcher.hover();
  await expect(stack).toHaveAttribute('data-tucked', 'true');
  await expect(stack).toHaveAttribute('data-visually-tucked', 'false');
  await expect.poll(async () => {
    const face = (await launcher.getByTestId('pinned-avatar-face').boundingBox())!;
    return face.x + face.width;
  }).toBeLessThan(1440);
  await page.mouse.move(300, 100);
  await expect(stack).toHaveAttribute('data-visually-tucked', 'true');
  await launcher.press('Enter');
  await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
  await expect(page.getByTestId('pinned-agent-strip')).toHaveAttribute('data-visually-tucked', 'false');
  const butler = page.getByRole('button', { name: 'Open pinned chats with Butler' });
  await butler.click();
  await expect(page.getByTestId('pinned-agent-strip')).toHaveAttribute('data-tucked', 'true');
  await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
  await page.mouse.move(300, 100);
  await expect(page.getByTestId('pinned-agent-strip')).toHaveAttribute('data-visually-tucked', 'false');
  await page.getByRole('button', { name: 'Untuck pinned avatars', exact: true }).click();
  await expect(page.getByTestId('pinned-agent-strip')).toHaveAttribute('data-tucked', 'false');
  await page.getByRole('button', { name: 'Tuck pinned avatars', exact: true }).click();
  await page.mouse.move(300, 100);
  await expect(stack).toHaveAttribute('data-visually-tucked', 'true');
  await launcher.click();
  await butler.click();
  const before = (await butler.boundingBox())!;
  await page.mouse.move(1440 - 2, before.y + before.height / 2);
  await page.mouse.down();
  await page.mouse.move(1440 - 20, before.y + before.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId('pinned-agent-strip')).toHaveAttribute('data-tucked', 'false');
  expect(state.errors).toEqual([]);
});

test('each agent reopens its last selected tab across collapse, navigation and reload, with safe fallback', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await openButler(page);
  const panel = page.getByTestId('pinned-chat-panel');
  await panel.getByRole('tab', { name: 'Garden planning' }).click();
  await page.keyboard.press('Escape');
  await openButler(page);
  await expect(panel.getByRole('tab', { name: 'Garden planning' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', { name: 'Open pinned chats with Editor' }).click();
  await panel.getByRole('tab', { name: 'Editorial notes' }).click();
  await page.keyboard.press('Escape');
  await page.reload();
  await openButler(page);
  await expect(panel.getByRole('tab', { name: 'Garden planning' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', { name: 'Open pinned chats with Editor' }).click();
  await expect(panel.getByRole('tab', { name: 'Editorial notes' })).toHaveAttribute('aria-selected', 'true');
  await panel.getByRole('button', { name: 'Unpin Editorial notes' }).click();
  await page.keyboard.press('Escape');
  await page.getByTestId('pinned-chat-launcher').click();
  await page.getByRole('button', { name: 'Open pinned chats with Editor' }).click();
  await expect(panel.getByRole('tab', { name: 'Home', exact: true })).toHaveAttribute('aria-selected', 'true');
  expect(state.errors).toEqual([]);
});

test('right-edge gutter stays an interactive, stable target and short swipes tuck the dock', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher).toBeVisible();
  const before = (await launcher.boundingBox())!;
  await page.mouse.move(1438, before.y + before.height / 2);
  expect(await launcher.evaluate((node) => node.matches(':hover'))).toBe(true);
  await page.mouse.click(1438, before.y + before.height / 2);
  await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
  await page.keyboard.press('Escape');
  const current = (await launcher.boundingBox())!;
  await page.mouse.move(current.x + current.width / 2, current.y + current.height / 2);
  await page.mouse.down();
  await page.mouse.move(current.x + current.width / 2 + 14, current.y + current.height / 2, { steps: 4 });
  await page.mouse.up();
  const stack = page.getByTestId('pinned-agent-stack');
  await expect(stack).toHaveAttribute('data-tucked', 'true');
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await page.mouse.move(1439, current.y + current.height / 2);
  await expect(stack).toHaveAttribute('data-tucked', 'true');
  expect(await launcher.evaluate((node) => node.matches(':hover'))).toBe(true);
  expect(state.errors).toEqual([]);
});

test('Display preferences independently control collapse and activity order and survive reload', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/settings?tab=display');
  const collapse = page.getByRole('checkbox', { name: /Collapse pinned agent avatars/ });
  const ordering = page.getByRole('checkbox', { name: /Order pinned agents by activity/ });
  async function showDisplay() {
    await page.getByRole('tab', { name: 'display', exact: true }).click();
    const discard = page.getByRole('button', { name: 'Discard changes', exact: true });
    await expect.poll(async () => await collapse.isVisible() || await discard.isVisible()).toBe(true);
    if (await discard.isVisible()) await discard.click();
  }
  await showDisplay();
  await expect(collapse).toBeChecked();
  await expect(ordering).toBeChecked();
  await collapse.uncheck();
  await expect(page.getByTestId('pinned-agent-block')).toBeVisible();
  await ordering.uncheck();
  await expect.poll(() => state.preferenceUpdates().length).toBe(2);
  expect(state.preferences().display.pinned_agents_collapsed).toBe(false);
  expect(state.preferences().display.pinned_agents_order_by_activity).toBe(false);
  await page.reload();
  await showDisplay();
  await expect(collapse).not.toBeChecked();
  await expect(ordering).not.toBeChecked();
  await expect(page.getByTestId('pinned-agent-block')).toBeVisible();
  await collapse.check();
  await expect(page.getByTestId('pinned-agent-stack')).toBeVisible();
  await expect(ordering).not.toBeChecked();
  expect(state.errors).toEqual([]);
});

for (const collapsed of [true, false]) {
  test(`stable pin order ignores activity when disabled in ${collapsed ? 'stack' : 'block'}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await fixture(page, true, true, {
      pinned_agents_collapsed: collapsed, pinned_agents_order_by_activity: false,
    });
    await page.goto('/notifications');
    const agents = page.locator('[data-pinned-agent]');
    await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
    const calls = state.details();
    state.activity('conv-second-agent', '2026-10-15T15:00:00Z', true);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect.poll(state.details).toBeGreaterThan(calls);
    await expect(agents.first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
    await expect(agents.nth(1).getByTestId('activity-avatar-attention')).toHaveAttribute('title', 'Waiting for answer');
    expect(state.errors).toEqual([]);
  });
}

test('persistent block orders recent activity first, keeps tucked state and moves as a unit', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, { pinned_agents_collapsed: false }, 3, { tucked: true });
  state.settle();
  await page.goto('/notifications');
  const block = page.getByTestId('pinned-agent-block');
  await expect(block).toBeVisible();
  await expect(page.locator('[data-pinned-agent]').first()).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  const calls = state.details();
  state.activity('conv-second-agent', '2026-10-15T15:00:00Z', true);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(state.details).toBeGreaterThan(calls);
  await expect(page.locator('[data-pinned-agent]').first()).toHaveAttribute('data-pinned-agent', 'agent-second');
  await expect(page.getByTestId('pinned-agent-block-grip')).toHaveCount(0);
  const avatar = page.getByRole('button', { name: 'Open pinned chats with Butler' });
  await avatar.focus();
  const before = (await block.boundingBox())!;
  await avatar.press('Alt+ArrowUp');
  await expect.poll(async () => (await block.boundingBox())!.y).toBeLessThan(before.y);
  const rect = (await avatar.boundingBox())!;
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width / 2 + 14, rect.y + rect.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect(block).toHaveAttribute('data-tucked', 'true');
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
  await expect(block).toHaveAttribute('data-tucked', 'true');
  await page.keyboard.press('Escape');
  await expect(block).toBeVisible();
  await page.reload();
  await expect(block).toHaveAttribute('data-tucked', 'true');
  expect(state.errors).toEqual([]);
});

test('open agent keeps a circular highlight without outlining the edge hit area', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, { pinned_agents_collapsed: false });
  await page.goto('/notifications');
  const butler = page.getByRole('button', { name: 'Open pinned chats with Butler' });
  const editor = page.getByRole('button', { name: 'Open pinned chats with Editor' });
  await butler.click();
  await page.mouse.move(300, 100);
  await expect(butler).toHaveAttribute('data-selected', 'true');
  expect(await butler.evaluate(node => getComputedStyle(node).boxShadow)).toBe('none');
  const face = butler.getByTestId('pinned-avatar-face');
  expect(await face.evaluate(node => getComputedStyle(node).boxShadow)).not.toBe('none');
  const bounds = (await face.boundingBox())!;
  expect(bounds.width).toBe(bounds.height);
  await editor.click();
  await page.mouse.move(300, 100);
  await expect(editor).toHaveAttribute('data-selected', 'true');
  await expect(butler).toHaveAttribute('data-selected', 'false');
  await editor.focus();
  await editor.press('Tab');
  await page.keyboard.press('Shift+Tab');
  expect(await editor.evaluate(node => getComputedStyle(node).boxShadow)).toBe('none');
  expect(state.errors).toEqual([]);
});

test('every block avatar drags the whole block without opening a chat', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, {
    pinned_agents_collapsed: false, pinned_agents_order_by_activity: false,
  });
  await page.goto('/notifications');
  const block = page.getByTestId('pinned-agent-block');
  await expect(block).toBeVisible();
  for (const agentId of ['agent-stage39', 'agent-second']) {
    const target = page.locator(`[data-pinned-agent="${agentId}"]`);
    const previous = (await block.boundingBox())!;
    const rect = (await target.boundingBox())!;
    await page.mouse.move(rect.x + 24, rect.y + 24);
    await page.mouse.down();
    await page.mouse.move(rect.x + 24, rect.y - 36, { steps: 6 });
    await page.mouse.up();
    await expect.poll(async () => (await block.boundingBox())!.y).toBeLessThan(previous.y - 40);
    await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  }
  expect(state.errors).toEqual([]);
});

test('persistent block bounds rows to display height and scrolls surplus agents horizontally', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, true, {
    pinned_agents_collapsed: false, pinned_agents_order_by_activity: false,
  });
  const extra = Array.from({ length: 6 }, (_, index) => ({
    agent_id: `agent-block-${index + 1}`, name: `Block agent ${index + 1}`,
    display_name: `Block agent ${index + 1}`, avatar_url: avatarUrl, status: 'active', agent_type: 'primary',
  }));
  await page.route('**/api/v1/agents?**', (route) => route.fulfill({ json: {
    items: [{ ...extra[0], agent_id: 'agent-stage39', display_name: 'Butler' },
      { ...extra[0], agent_id: 'agent-second', display_name: 'Editor' }, ...extra],
    has_more: false, next_cursor: null,
  } }));
  await page.addInitScript(({ storageKey, extra }) => {
    const workspace = JSON.parse(localStorage.getItem(storageKey)!);
    for (const agent of extra) if (!workspace.agents.some((item: { agentId: string }) => item.agentId === agent.agent_id)) {
      workspace.agents.push({ agentId: agent.agent_id, homeConversationId: `home-${agent.agent_id}` });
    }
    localStorage.setItem(storageKey, JSON.stringify(workspace));
  }, { storageKey, extra });
  await page.route('**/api/v1/conversations/home-agent-block-*?**', (route) => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!;
    return route.fulfill({ json: { conversation_id: id, agent_id: id.slice(5), title: id,
      context: { type: 'web', platform_data: { kind: 'agent_direct' } }, status: 'active' } });
  });
  await page.goto('/notifications');
  const scroll = page.getByTestId('pinned-agent-block-scroll');
  await expect(page.locator('[data-pinned-agent]')).toHaveCount(8);
  for (const [height, rows] of [[900, 6], [600, 4], [400, 2]]) {
    await page.setViewportSize({ width: 1440, height });
    await expect.poll(() => scroll.evaluate((node) => getComputedStyle(node).gridTemplateRows.split(' ').length)).toBe(rows);
    const bounds = (await scroll.boundingBox())!;
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(height);
    expect(await scroll.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
  }
  await scroll.evaluate((node) => { node.scrollLeft = node.scrollWidth; });
  await expect(page.getByRole('button', { name: 'Open pinned chats with Block agent 6' })).toBeInViewport();
  await page.getByRole('button', { name: 'Open pinned chats with Block agent 6' }).click();
  await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('compact composer reuses profile settings and switches without losing a draft', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  const commands: string[] = [];
  await page.route('**/api/v1/chat/v2/conversations/*/commands/*', async (route) => {
    const body = route.request().postDataJSON();
    commands.push(body.content);
    const parts = new URL(route.request().url()).pathname.split('/');
    await route.fulfill({ json: {
      conversation_id: parts[6], client_txn_id: parts[8], status: 'completed', result_type: 'profile',
      text: 'Profile switched', data: { resolved_agent_profile_id: 'developer' }, server_time: '2026-10-15T08:00:00Z',
    } });
  });
  await page.goto('/notifications');
  await openButler(page);
  const panel = page.getByTestId('pinned-chat-panel');
  const textarea = panel.locator('textarea').first();
  await textarea.fill('Keep this draft while switching profile');
  await expect(panel.getByRole('button', { name: 'Chat settings', exact: true }).locator('.settings-icon')).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Chat settings', exact: true }).locator('.trigger-label')).toBeHidden();
  await expect(panel.getByRole('button', { name: 'Chat settings', exact: true }).locator('.trigger-accent')).toBeHidden();
  await expect(panel.getByRole('button', { name: 'Chat settings', exact: true }).locator('.trigger-chevron')).toBeHidden();
  await panel.getByRole('button', { name: 'Chat settings', exact: true }).click();
  await page.getByRole('switch', { name: 'Use developer profile' }).click();
  await expect.poll(() => commands).toEqual(['/profile developer']);
  await expect(textarea).toHaveValue('Keep this draft while switching profile');
  await expect(page.getByRole('switch', { name: 'Use developer profile' })).toHaveAttribute('aria-checked', 'true');
  expect(state.errors).toEqual([]);
});

test('rightward throw tucks the dock without opening it and tab strip scrolls independently of actions', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher).toBeVisible();
  const rect = (await launcher.boundingBox())!;
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width + 30, rect.y + rect.height / 2, { steps: 4 });
  await page.mouse.up();
  await page.mouse.move(300, 100);
  await expect(page.getByTestId('pinned-agent-stack')).toHaveAttribute('data-tucked', 'true');
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await launcher.focus();
  await launcher.press('Enter');
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  const panel = page.getByTestId('pinned-chat-panel');
  for (let index = 0; index < 5; index++) {
    await panel.getByRole('button', { name: 'New conversation', exact: true }).click();
    await expect(panel.getByRole('tab')).toHaveCount(index + 4);
  }
  expect(await panel.getByRole('tablist').evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
  await expect(panel.getByRole('button', { name: 'New conversation', exact: true })).toBeInViewport();
  await expect(panel.getByRole('button', { name: 'Find conversation' })).toBeInViewport();
  expect(state.errors).toEqual([]);
});

test('typed profile and override commands update the same cached settings without remount', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  const runtime = {
    revision: 1, profile_id: 'default', profile_source: 'conversation',
    model: 'gpt-5', provider_id: 'openai', model_source: 'agent_profile',
    reasoning_effort: 'medium', reasoning_effort_source: 'agent_profile',
    fast_mode: false, fast_mode_source: 'agent_profile',
  };
  await page.route('**/api/v1/conversations/conv-home-butler?**', (route) => route.fulfill({
    json: { ...homes[0], status: 'active', active_session_id: 'session-home-profile' },
  }));
  let runtimeLoaded = false;
  await page.route('**/api/v1/sessions/session-home-profile/intaris', (route) => {
    runtimeLoaded = true;
    return route.fulfill({ json: { session_id: 'session-home-profile', runtime_selection: runtime } });
  });
  let revision = 1;
  await page.route('**/api/v1/chat/v2/conversations/*/commands/*', (route) => {
    const content = route.request().postDataJSON().content as string;
    const parts = new URL(route.request().url()).pathname.split('/');
    return route.fulfill({ json: {
      conversation_id: parts[6], client_txn_id: parts[8], status: 'completed', result_type: 'profile',
      text: 'Settings updated', data: { resolved_agent_profile_id: 'developer',
        runtime_selection: { ...runtime, revision: ++revision, profile_id: 'developer',
          fast_mode_source: content.startsWith('/fast') ? 'session_override' : 'agent_profile' } },
      server_time: '2026-10-15T08:00:00Z',
    } });
  });
  await page.goto('/notifications');
  await openButler(page);
  await expect.poll(() => runtimeLoaded).toBe(true);
  const panel = page.getByTestId('pinned-chat-panel');
  const textarea = panel.locator('textarea').first();
  await textarea.fill('/profile developer');
  await textarea.press('Enter');
  await expect(panel.getByRole('button', { name: 'Chat settings' })).toHaveAttribute('title', 'Profile: developer');
  await textarea.fill('/fast off');
  await textarea.press('Enter');
  await expect(panel.getByLabel('Profile settings overridden', { exact: true })).toBeVisible();
  await textarea.fill('/profile developer');
  await textarea.press('Enter');
  await expect(panel.getByLabel('Profile settings overridden', { exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('project local summary refresh honors notification invalidation without changing global pins', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, false);
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/api/ws', (ws) => {
    socket = ws; ws.send(JSON.stringify({ type: 'authenticated' }));
  });
  await page.goto('/projects/proj_home');
  await page.getByRole('tab', { name: /^Conversations/ }).click();
  await page.getByRole('button', { name: /Kitchen renovation/ }).first().click();
  await expect.poll(state.reads).toContain('conv-task-chat');
  const before = state.details();
  state.newMessage('2026-10-15T08:20:00Z');
  expect(socket).toBeDefined();
  socket!.send(JSON.stringify({ type: 'scope_invalidated', reason: 'notification_state_changed', conversation_id: 'conv-task-chat' }));
  await expect.poll(state.details).toBeGreaterThan(before);
  await expect.poll(state.reads).toContain('conv-task-chat');
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('collapsed stack animates latest activity to the front and nudges for new attention', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await expect.poll(state.details).toBeGreaterThanOrEqual(5);
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-second');
  state.settle();
  state.activity('conv-task-chat', '2026-10-15T08:00:00Z');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  state.activity('conv-second-agent', '2026-10-15T08:05:00Z');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-second');
  state.activity('conv-task-chat', '2026-10-15T08:06:00Z', true);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-stage39');
  await expect(launcher.locator('.nudge')).toHaveCount(1);
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('stack separates through transforms and animates back without a second launcher or list', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const avatars = page.locator('[data-pinned-agent]');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher.locator('img')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open pinned chats with Butler' })).toHaveCount(0);
  const handles = await avatars.elementHandles();
  const collapsedTransforms = await avatars.evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).transform));
  await launcher.click();
  expect(await avatars.evaluateAll((nodes) => nodes.some((node) => node.getAnimations().some((animation) => animation.playState === 'running')))).toBe(true);
  await expect(page.getByRole('button', { name: 'Open pinned chats with Butler' })).toBeVisible();
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('avatars-separated.png') });
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('pinned-agent-strip')).toHaveCount(0);
  await expect.poll(() => avatars.evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).transform))).toEqual(collapsedTransforms);
  expect(await Promise.all(handles.map((handle) => handle.evaluate((node) => node.isConnected)))).toEqual([true, true]);
  await expect(launcher).toBeFocused();
  expect(state.errors).toEqual([]);
});

test('expanded avatar image dragging moves the whole dock without opening a chat', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  await page.getByTestId('pinned-chat-launcher').click();
  const avatar = page.getByRole('button', { name: 'Open pinned chats with Butler' });
  await expect(avatar.locator('img')).toHaveAttribute('draggable', 'false');
  await expect.poll(() => avatar.evaluate((node) => node.getAnimations().length)).toBe(0);
  const before = (await avatar.boundingBox())!;
  await page.mouse.move(before.x + before.width / 2, before.y + before.height / 2);
  await page.mouse.down();
  await page.mouse.move(before.x - 100, before.y - 100, { steps: 10 });
  await page.mouse.up();
  await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
  const after = (await avatar.boundingBox())!;
  expect(after.x).toBe(before.x);
  expect(after.y).toBeLessThan(before.y - 80);
  await page.mouse.click(300, 100);
  await expect(page.getByTestId('pinned-agent-strip')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('reduced motion keeps expansion instant while preserving keyboard movement', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-second');
  await launcher.click();
  expect(await page.locator('[data-pinned-agent]').evaluateAll((nodes) => nodes.every((node) => !node.getAnimations().length))).toBe(true);
  const before = (await launcher.boundingBox())!;
  await page.keyboard.press('Alt+ArrowUp');
  expect((await launcher.boundingBox())!.y).toBeLessThan(before.y);
  await page.keyboard.press('Escape');
  await expect(launcher).toBeFocused();
  expect(state.errors).toEqual([]);
});

test('touch dragging on the avatar does not cancel or expand the collapsed dock', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1024, height: 900 }, hasTouch: true, serviceWorkers: 'block' });
  const page = await context.newPage();
  try {
    const state = await fixture(page);
    await page.goto('/notifications');
    const launcher = page.getByTestId('pinned-chat-launcher');
    await expect(launcher.locator('img')).toBeVisible();
    await launcher.tap();
    await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
    await page.keyboard.press('Escape');
    const before = (await launcher.boundingBox())!;
    const x = before.x + before.width / 2;
    const y = before.y + before.height / 2;
    const session = await context.newCDPSession(page);
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
    for (let step = 1; step <= 8; step++) {
      await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - step * 15 }] });
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    const after = (await launcher.boundingBox())!;
    expect(after.x).toBe(before.x);
    expect(after.y).toBeLessThan(before.y - 100);
    await expect(page.getByTestId('pinned-agent-strip')).toHaveCount(0);
    await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
    await session.detach();
    await launcher.tap();
    await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
    expect(state.errors).toEqual([]);
  } finally {
    await context.close();
  }
});

test('touch dragging either block avatar moves the group and the next tap still opens chat', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1024, height: 900 }, hasTouch: true, serviceWorkers: 'block' });
  const page = await context.newPage();
  try {
    const state = await fixture(page, true, true, {
      pinned_agents_collapsed: false, pinned_agents_order_by_activity: false,
    });
    await page.goto('/notifications');
    const block = page.getByTestId('pinned-agent-block');
    await expect(block).toBeVisible();
    const session = await context.newCDPSession(page);
    for (const agentId of ['agent-stage39', 'agent-second']) {
      const target = page.locator(`[data-pinned-agent="${agentId}"]`);
      const rect = (await target.boundingBox())!;
      const before = (await block.boundingBox())!.y;
      const x = rect.x + 24, y = rect.y + 24;
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
      for (let step = 1; step <= 6; step++) {
        await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - step * 10 }] });
      }
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await expect.poll(async () => (await block.boundingBox())!.y).toBeLessThan(before - 40);
      await expect(page.getByTestId('pinned-chat-panel')).toHaveCount(0);
    }
    await session.detach();
    await page.getByRole('button', { name: 'Open pinned chats with Butler' }).tap();
    await expect(page.getByTestId('pinned-chat-panel')).toBeVisible();
    expect(state.errors).toEqual([]);
  } finally { await context.close(); }
});

test('removing or cancelling the captured avatar does not block the next interaction', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/notifications');
  const launcher = page.getByTestId('pinned-chat-launcher');
  await expect(launcher.locator('img')).toBeVisible();
  const before = (await launcher.boundingBox())!;
  await page.mouse.move(before.x + before.width / 2, before.y + before.height / 2);
  await page.mouse.down();
  await page.mouse.move(before.x + before.width / 2, before.y - 50, { steps: 5 });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(launcher).toHaveCount(0);
  await page.mouse.up();
  await page.setViewportSize({ width: 1440, height: 900 });
  await launcher.click();
  await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
  await page.keyboard.press('Escape');
  const next = (await launcher.boundingBox())!;
  await launcher.evaluate((node) => node.addEventListener('gotpointercapture', (event) => {
    node.dataset.capturedPointer = String((event as PointerEvent).pointerId);
  }, { once: true }));
  await page.mouse.move(next.x + next.width / 2, next.y + next.height / 2);
  await page.mouse.down();
  await page.mouse.move(next.x + next.width / 2, next.y - 20, { steps: 4 });
  await launcher.evaluate((node) => {
    node.releasePointerCapture(Number(node.dataset.capturedPointer));
  });
  await page.mouse.up();
  await launcher.click();
  await expect(page.getByTestId('pinned-agent-strip')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('project windows do not create or remove global agent pins', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, false);
  await page.goto('/projects/proj_home');
  await page.getByRole('tab', { name: /^Conversations/ }).click();
  await page.getByRole('button', { name: /Kitchen renovation/ }).first().click();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  await page.getByTestId('workspace-window-minimize-conversation:conv-task-chat').click();
  await page.getByRole('link', { name: 'Notifications', exact: true }).click();
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveCount(0);
  await page.goto('/projects/proj_home');
  await page.getByTestId('workspace-dock-restore-conversation:conv-task-chat').click();
  await page.getByTestId('workspace-window-close-conversation:conv-task-chat').click();
  await page.goto('/notifications');
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

for (const width of [768, 2560]) {
  test(`dock remains right aligned and viewport-contained at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await fixture(page);
    await page.goto('/notifications');
    const launcher = page.getByTestId('pinned-chat-launcher');
    await expect(launcher).toHaveAttribute('data-pinned-agent', 'agent-second');
    await launcher.click();
    await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
    const panel = page.getByTestId('pinned-chat-panel');
    await expect(panel).toBeVisible();
    const usableWidth = await page.evaluate(() => document.documentElement.clientWidth);
    const edgeInset = await panel.evaluate((node) => parseFloat(getComputedStyle(node.parentElement!).right));
    await expect.poll(async () => {
      const rect = (await panel.boundingBox())!;
      return Math.abs(rect.x + rect.width - (usableWidth - edgeInset));
    }).toBeLessThan(0.5);
    const bounds = (await panel.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(Math.abs(bounds.x + bounds.width - (usableWidth - edgeInset))).toBeLessThan(0.5);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(900);
    const background = page.locator('[data-app-content="true"] > div').first();
    if (width === 2560) {
      const content = (await background.boundingBox())!;
      expect(bounds.x).toBeGreaterThan(content.x + content.width);
    }
    await page.keyboard.press('Escape');
    await expect(panel).toHaveCount(0);
    await expect(launcher).toBeFocused();
    await page.screenshot({ path: test.info().outputPath(`pinned-chat-${width}.png`) });
    expect(state.errors).toEqual([]);
  });
}

test('global pins stay visible independently of project windows and share main dashboard windows', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page);
  await page.goto('/projects/proj_home');
  await expect(page.getByRole('heading', { name: 'Home', exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
  await expect(page.getByTestId('pinned-chat-launcher')).toBeVisible();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toHaveCount(0);
  await page.getByRole('tab', { name: /^Conversations/ }).click();
  await page.getByRole('button', { name: /Kitchen renovation/ }).first().click();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  await expect.poll(state.reads).toContain('conv-task-chat');
  expect(state.reads()).not.toContain('conv-task-chat-b');
  state.newMessage('2026-10-14T08:10:00Z');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(state.reads).toContain('conv-task-chat');
  await page.getByTestId('workspace-window-minimize-conversation:conv-task-chat').click();
  const detailCount = state.details();
  state.newMessage('2026-10-14T08:15:00Z');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(state.details).toBeGreaterThan(detailCount);
  expect(state.reads()).not.toContain('conv-task-chat');
  await page.getByTestId('workspace-dock-restore-conversation:conv-task-chat').click();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat-b')).toHaveCount(0);
  await expect(page.getByTestId('pinned-chat-launcher')).toBeVisible();
  await page.goto('/');
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat-b')).toBeVisible();
  await expect(page.getByTestId('pinned-chat-launcher')).toHaveCount(0);
  await page.goto('/notifications');
  await page.getByTestId('pinned-chat-launcher').click();
  await page.getByRole('button', { name: 'Open pinned chats with Butler' }).click();
  await page.getByRole('button', { name: 'Unpin Kitchen renovation' }).click();
  await page.goto('/projects/proj_home');
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeVisible();
  const saved = await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!).entries, storageKey);
  expect(saved.map((item: { window: { entityId: string } }) => item.window.entityId)).toEqual(['conv-task-chat-b', 'conv-second-agent']);
  expect(state.errors).toEqual([]);
});

test('dock stays accessible on dashboards when workspace windows are disabled', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, true, false);
  await page.goto('/');
  await expect(page.getByTestId('pinned-chat-launcher')).toBeVisible();
  await page.goto('/projects/proj_home');
  await expect(page.getByTestId('pinned-chat-launcher')).toBeVisible();
  expect(state.errors).toEqual([]);
});
