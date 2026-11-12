import { expect, test, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block' });
const now = '2026-10-28T09:25:38Z';
const mainId = 'conv-task-chat';
const managedId = 'conv-managed-architect';
const nestedId = 'sess_nested-implementer';
const summary = { commands: 0, changed_files: 0, mutations: 0, artifacts: 0, deliverables: 0 };
const nodes = [
  { key: 'root', root_key: 'root', parent_key: null, kind: 'conversation', edge_kind: 'root', ordinal: 0,
    conversation_id: mainId, session_id: 'sess-task-chat', event_store_session_id: 'sess-task-chat',
    agent_id: 'agent-stage39', title: 'Main agent', status: 'active', current: true, superseded: false },
  { key: 'managed', root_key: 'root', parent_key: 'root', kind: 'managed', edge_kind: 'managed_conversation', ordinal: 1,
    conversation_id: managedId, session_id: 'managed-architect', event_store_session_id: 'managed-architect',
    agent_id: 'laforge', title: 'Architect session', status: 'active', current: true, superseded: false },
];

function delegation(sessionId: string) {
  return { id: `delegation:${sessionId}`, kind: 'delegation', sort_key: '0001:0000000001:0000',
    source_refs: [], created_at: now, status: 'complete', stable: true,
    child_session_id: sessionId, agent_id: 'system:implement', title: 'Open child session',
    summary: 'Nested work', tool_call_count: 0, max_tool_calls: 10 };
}

for (const mode of ['open', 'refresh', 'back', 'denied'] as const) {
test(`third-level delegate ownership: ${mode}`, async ({ page }) => {
  await installTaskCockpitFixture(page, { dashboardConversation: true });
  await page.route('**/api/auth/me', (route) => route.fulfill({
    json: { email: 'admin@cognis-e2e.localdev.me', role: 'user', name: 'Owner' },
  }));
  await page.setViewportSize({ width: 1600, height: 1000 });
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/api/ws', (ws) => {
    socket = ws;
    ws.send(JSON.stringify({ type: 'authenticated' }));
  });
  await page.route('**/api/v1/conversations/conv-managed-architect?**', (route) => route.fulfill({
    json: { conversation_id: managedId, agent_id: 'laforge', title: 'Architect session', status: 'active' },
  }));
  await page.route('**/api/v1/chat/v2/**/snapshot**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const nested = path.includes(`/sessions/${nestedId}/`);
    const managed = path.includes(`/conversations/${managedId}/`);
    const scope = nested
      ? { key: `session:${nestedId}`, kind: 'session', session_id: nestedId, conversation_id: managedId }
      : { key: `conversation:${managed ? managedId : mainId}`, kind: 'conversation', conversation_id: managed ? managedId : mainId };
    return route.fulfill({ json: {
      schema_version: 2, projection_version: 'nested-scope', scope,
      conversation: { conversation_id: scope.conversation_id, agent_id: managed ? 'laforge' : 'agent-stage39', status: 'active' },
      timeline: { items: nested ? [] : [delegation(managed ? nestedId : 'managed-architect')], has_more_before: false, before_cursor: null },
      state: { state_version: 1, snapshot_generated_at: now, capabilities: [], active_turn: {}, pending: {}, active_session: {} },
      queue: { messages: [], queued_count: 0 },
      runtime: { runtime_epoch: 'nested', runtime_revision: 1, generated_at: now, has_active_turn: false, volatile_items: [] },
      cursor: `cursor:${scope.key}`, server_time: now,
    } });
  });
  let identityRequests = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/api/v1/sessions/${nestedId}`, async (route) => {
    identityRequests += 1;
    await gate;
    if (mode === 'denied') return route.fulfill({ status: 403, json: { detail: 'Session access denied' } });
    return route.fulfill({ json: {
      session_id: nestedId, conversation_id: managedId, parent_session_id: 'managed-architect',
      agent_id: 'system:implement', delegation_mode: 'delegate', delegation_task: 'Nested implementation',
      status: 'active', intaris_session_id: nestedId,
    } });
  });
  let nestedOverviewRequests = 0;
  await page.route('**/api/v1/chat/v2/**/activity-overview**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const nested = path.includes(`/sessions/${nestedId}/`);
    const managed = path.includes(`/conversations/${managedId}/`);
    if (nested) {
      nestedOverviewRequests += 1;
      expect(identityRequests).toBeGreaterThan(0);
    }
    const scope = nested
      ? { key: `session:${nestedId}`, kind: 'session', session_id: nestedId, conversation_id: managedId }
      : { key: `conversation:${managed ? managedId : mainId}`, kind: 'conversation', conversation_id: managed ? managedId : mainId };
    return route.fulfill({ json: {
      schema_version: 2, projection_version: 'nested-scope', scope, summary,
      workstreams: nodes, recent: {}, work_revision: 1, overview_revision: 'nested:1', server_time: now,
    } });
  });
  await page.goto(`/chat/${mainId}`);
  const details = page.getByRole('button', { name: 'Toggle session details', exact: true });
  await expect(details).toBeVisible();
  if (await details.getAttribute('aria-expanded') !== 'true') await details.click();
  await page.getByRole('button', { name: 'Expand Main agent', exact: true }).click();
  await expect(page.getByRole('button', { name: 'View session Architect session', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'View session Architect session', exact: true }).click();
  await expect(page.getByTestId('child-chat-view')).toBeVisible();
  await page.getByTestId('child-chat-view').getByRole('button', { name: /View session/i }).click();
  await expect.poll(() => identityRequests).toBe(1);
  expect(nestedOverviewRequests).toBe(0);
  if (mode === 'refresh') {
    socket!.send(JSON.stringify({
      type: 'sidebar_conversation_upsert', revision: '2',
      conversation: { conversation_id: mainId, agent_id: 'agent-stage39',
        title: 'Updated main conversation', status: 'active', context: { type: 'web' },
        has_active_turn: false, has_unread: false },
    }));
    await expect(page.getByRole('link', { name: /Updated main conversation/ })).toBeVisible();
  } else if (mode === 'back') {
    await page.getByRole('tabpanel', { name: 'Overview' }).getByRole('button', { name: 'Back', exact: true }).click();
    await expect(page.getByTestId('child-chat-view')).toHaveCount(0);
  }
  release();
  if (mode === 'back' || mode === 'denied') {
    if (mode === 'denied') await expect(page.getByText('Unable to open session', { exact: true })).toBeVisible();
    else await expect(page.getByTestId('conversation-sidebar-metadata')).toBeVisible();
    await expect(page.getByText('Activity overview response scope does not match the request', { exact: true })).toHaveCount(0);
    expect(nestedOverviewRequests).toBe(0);
    return;
  }
  await expect(page.getByTestId('child-sidebar-metadata')).toContainText('system:implement');
  await expect(page.getByTestId('child-sidebar-metadata')).toContainText('Nested implementation');
  await expect.poll(() => nestedOverviewRequests).toBeGreaterThan(0);
  await expect(page.getByText('Activity overview response scope does not match the request', { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('conversation-sidebar-metadata')).toHaveCount(0);
});
}
