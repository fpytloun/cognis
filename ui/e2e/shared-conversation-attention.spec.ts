import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block', bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1' });

for (const width of [390, 1280]) {
  test(`shared conversations keep personal unread but never Waiting (${width})`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const fixture = await installTaskCockpitFixture(page, { dashboardConversation: true });
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'stage39@example.com', role: 'user', name: 'Reader' },
    }));
    await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/v1/skills', (route) => route.fulfill({ json: [] }));
    await page.route('**/api/v1/provider-monitor', (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({ json: [] });
    });
    await page.route('**/api/v1/dashboard/navigation-summary', (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({ json: {
        chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
        schedules: 0, tasks: 1,
      } });
    });
    await page.route(/\/api\/v1\/schedules(?:\?|$)/, (route) => route.fulfill({ json: [] }));
    await page.route(/\/api\/v1\/inbox(?:\?|$)/, (route) => route.fulfill({
      json: { items: [], next_cursor: null, has_more: false, unread_count: 0 },
    }));
    await page.route('**/api/v1/user-preferences', (route) => route.fulfill({
      json: { display: { theme: 'dark', language: 'en', conversation_sidebar_sections: true } },
    }));
    await page.routeWebSocket('**/api/ws', (socket) => {
      socket.send(JSON.stringify({ type: 'authenticated' }));
      socket.onMessage((message) => {
        if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
      });
    });
    let lastRead: string | null = null;
    let reads = 0;
    const shared = {
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', title: 'Shared project discussion',
      context: { type: 'web', ref: null, platform_data: {} }, status: 'active', project_id: 'proj_cognis',
      project_name: 'Cognis', visibility: 'project', user_email: 'author@example.com',
      is_readonly_for_caller: true, has_active_turn: false, has_unread: true,
      pending_notification_types: ['step_question'],
      created_at: '2026-01-01T12:00:00Z', updated_at: '2026-01-02T12:00:00Z',
      last_message_at: '2026-01-02T12:00:00Z', last_read_at: null,
    };
    const active = {
      ...shared, conversation_id: 'conv-shared-active', title: 'Shared work in progress',
      has_active_turn: true,
    };
    const waiting = {
      ...shared, conversation_id: 'conv-owned-waiting', title: 'My approval request',
      user_email: 'stage39@example.com', is_readonly_for_caller: false,
    };
    const directAgent = {
      agent_id: 'riker', name: 'Riker', display_name: 'Riker',
      status: 'active', agent_type: 'primary',
    };
    const direct = {
      ...waiting, conversation_id: 'conv-direct-riker', agent_id: 'riker',
      title: 'Riker direct chat', has_active_turn: true,
      pending_notification_types: ['managed_conversation_completed'],
      context: { type: 'web', ref: null, platform_data: { kind: 'agent_direct' } },
    };
    const unseen = {
      ...shared, conversation_id: 'conv-shared-unseen', title: 'Another shared discussion',
      pending_notification_types: [],
    };
    const source = () => ({ ...shared, last_read_at: lastRead, has_unread: lastRead === null });
    const items = () => [source(), active, waiting, unseen];
    const pageResponse = () => ({ items: items(), has_more: false, next_cursor: null });
    const snapshotFor = (conversation: ReturnType<typeof source>) => ({
      schema_version: 2, projection_version: 'shared-attention',
      scope: {
        key: `conversation:${conversation.conversation_id}`, kind: 'conversation',
        conversation_id: conversation.conversation_id,
      },
      conversation, timeline: { items: [], has_more_before: false, before_cursor: null },
      runtime: {
        active_session_id: null, active_turn_id: null, active_turn_state: null,
        has_active_turn: false, volatile_items: [],
      },
      cursor: 'shared-attention', server_time: '2026-01-02T12:00:00Z',
    });
    const settlePaint = () => page.evaluate(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => undefined)));
    });

    await page.route('**/api/v1/conversations/sidebar**', (route) => route.fulfill({
      json: {
        sidebar_revision: 'shared-attention', projection_mode: 'snapshot',
        synced_at: '2026-01-02T12:00:00Z', context_types: ['web'],
        conversations: pageResponse(),
        agent_direct_chats: [{ agent: directAgent, conversation: direct }],
        active_conversation_ids: [active.conversation_id],
        agents: [{ agent_id: 'agent-stage39', name: 'LaForge', display_name: 'LaForge', status: 'active' }, directAgent],
      },
    }));
    await page.route('**/api/v1/conversations', (route) => route.fulfill({ json: pageResponse() }));
    await page.route('**/api/v1/conversations?**', (route) => route.fulfill({ json: pageResponse() }));
    await page.route('**/api/v1/conversations/active**', (route) => route.fulfill({
      json: { items: [active], has_more: false, next_cursor: null },
    }));
    await page.route('**/api/v1/conversations/waiting**', (route) => route.fulfill({
      json: {
        conversations: { items: [waiting, source(), active], has_more: false, cursor: null },
        active_conversation_ids: [active.conversation_id], activity_complete: true,
      },
    }));
    await page.route('**/api/v1/conversations/conv-task-chat', (route) => route.fulfill({ json: source() }));
    await page.route('**/api/v1/conversations/conv-task-chat?**', (route) => route.fulfill({ json: source() }));
    await page.route('**/api/v1/conversations/conv-task-chat/opened', (route) => route.fulfill({ json: source() }));
    await page.route('**/api/v1/conversations/conv-task-chat/read', (route) => {
      reads += 1;
      lastRead = new Date().toISOString();
      return route.fulfill({ json: { ok: true } });
    });
    await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/snapshot', (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({ json: snapshotFor(source()) });
    });
    await page.route('**/api/v1/chat/v2/conversations/conv-owned-waiting/snapshot', (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({ json: snapshotFor(waiting) });
    });

    await page.goto('/');
    const recent = page.getByTestId('dashboard-conversations-recent');
    const dashboardSource = recent.getByText(shared.title, { exact: true });
    await expect(dashboardSource).toBeVisible();
    await expect(page.getByTestId('dashboard-conversations-waiting')).not.toContainText(shared.title);
    await expect(page.getByTestId('dashboard-conversations-waiting')).toContainText(waiting.title);
    await expect(page.getByTestId('dashboard-conversations-active')).toContainText(active.title);
    await expect(recent.getByTestId('dashboard-conversation-row-conv-task-chat').getByTestId('activity-avatar-unread')).toBeVisible();
    await settlePaint();
    await page.getByTestId('dashboard-conversations-section').screenshot({
      path: test.info().outputPath(`shared-attention-dashboard-${width}.png`),
    });

    await page.goto('/chat/conv-task-chat');
    await expect(page.getByTestId('shared-conversation-banner')).toBeVisible();
    await expect.poll(() => reads).toBeGreaterThan(0);
    if (width < 1024) {
      await page.getByRole('button', { name: 'Open conversations', exact: true }).click();
    } else {
      const showSidebar = page.getByRole('button', { name: 'Show conversations', exact: true });
      if (await showSidebar.isVisible()) await showSidebar.click();
    }
    await expect(page.getByTestId('sidebar-conversation-row-conv-task-chat')).toBeInViewport();
    await expect(page.getByTestId('sidebar-conversations-history')).toContainText(shared.title);
    await expect(page.getByTestId('sidebar-conversations-waiting')).not.toContainText(shared.title);
    await expect(page.getByTestId('sidebar-conversations-waiting')).toContainText(waiting.title);
    await expect(page.getByTestId('sidebar-conversations-active')).toContainText(active.title);
    const directChat = page.getByRole('link', { name: 'Open direct chat with Riker', exact: true });
    await expect(directChat).toBeVisible();
    await expect(directChat.getByTestId('activity-avatar-orbit')).toBeVisible();
    await expect(directChat.getByTestId('activity-avatar-attention')).toHaveCount(0);
    await settlePaint();
    await page.screenshot({ path: test.info().outputPath(`shared-attention-chat-${width}.png`) });

    await page.goto('/');
    await expect(page.getByTestId('dashboard-conversations-recent')).toContainText(shared.title);
    await expect(page.getByTestId('dashboard-conversation-row-conv-task-chat').getByTestId('activity-avatar-unread')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversation-row-conv-shared-unseen').getByTestId('activity-avatar-unread')).toHaveCount(1);
    await expect(page.getByTestId('dashboard-conversations-active').getByTestId('activity-avatar-orbit')).toHaveCount(1);
    expect(fixture.unmockedRequests()).toEqual([]);
  });
}
