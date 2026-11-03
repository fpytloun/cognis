import { expect, test, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import { expectAttentionTone } from './attention-tone';

test.use({ serviceWorkers: 'block' });

for (const width of [1440, 390]) {
  test(`old unread chats are discoverable independently of recent history at ${width}px`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => { errors.push(error.message); console.error('Waiting browser error:', error.message); });
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    let socket: WebSocketRoute | undefined;
    await page.routeWebSocket('**/api/ws', (ws) => {
      socket = ws;
      ws.send(JSON.stringify({ type: 'authenticated' }));
      ws.onMessage((message) => {
        if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
      });
    });
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' } }));
    await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
      generated_at: '2026-10-14T20:00:00Z', issues: [], summary: { total: 0, warning: 0, critical: 0, info: 0, truncated: false },
    } }));
    const row = (id: string, unread: boolean, context = 'web') => ({
      conversation_id: id, title: id.startsWith('old-') ? `Historical unread ${id}` : `Recent ${id}`,
      user_email: 'owner@cognis.test', agent_id: 'agent-stage39', status: 'active',
      context: { type: context, platform_data: {}, ref: null }, has_unread: unread, has_active_turn: false,
      pending_notification_types: [], attention_actions: [], active_session_status: null,
      last_message_at: id.startsWith('old-') ? '2026-07-01T00:00:00Z' : '2026-10-14T20:00:00Z',
      created_at: '2026-07-01T00:00:00Z', updated_at: '2026-10-14T20:00:00Z',
    });
    const history = Array.from({ length: 50 }, (_, i) => row(`recent-${i}`, false));
    // Old unread topics remain discoverable; hidden channels must never leak
    // through the Dashboard Waiting renderer, including older cached responses.
    let waiting = Array.from({ length: 21 }, (_, i) => row(`old-${i}`, true));
    const hidden = [row('hidden-matrix', true, 'matrix'), row('hidden-managed', true, 'agent_work'), row('hidden-task', true, 'task')];
    await page.route('**/api/v1/conversations?**', (route) => route.fulfill({ json: { items: history, cursor: 'history-tail', has_more: true } }));
    await page.route('**/api/v1/conversations/sidebar?**', (route) => route.fulfill({ json: {
      agents: [], agent_direct_chats: [], conversations: { items: history, cursor: 'history-tail', has_more: true },
      context_types: ['web', 'matrix', 'agent_work'], sidebar_revision: '1',
      background_work: { items: [], active_count: 0, truncated: false, generated_at: '2026-10-14T20:00:00Z' },
    } }));
    const cursors: string[] = [];
    await page.route('**/api/v1/conversations/waiting?**', (route) => {
      const cursor = new URL(route.request().url()).searchParams.get('cursor');
      cursors.push(cursor ?? 'first');
      const start = cursor ? Number(cursor.slice('waiting-'.length)) : 0;
      const end = start + 10;
      const candidates = [...waiting, ...hidden];
      return route.fulfill({ json: {
        conversations: { items: candidates.slice(start, end), cursor: end < candidates.length ? `waiting-${end}` : null, has_more: end < candidates.length },
        active_conversation_ids: [],
      } });
    });
    await page.route('**/api/v1/dashboard/navigation-summary', (route) => route.fulfill({ json: {
      chat_groups: [{ count: waiting.length, has_unread: true, is_active: false, session_observed: false, pending_notification_types: [] }],
      dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, schedules: 0, tasks: 0,
    } }));
    await page.goto('/');
    await expect(page.locator('[data-testid^="dashboard-conversation-waiting-reason-old-"]')).toHaveCount(21);
    await expect(page.getByTestId('dashboard-conversation-waiting-reason-old-20')).toHaveText('Unread messages');
    await expect(page.locator('[data-testid^="dashboard-conversation-waiting-reason-hidden-"]')).toHaveCount(0);
    expect(cursors).toContain('waiting-20');
    if (width < 1024) await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
    else {
      const expand = page.getByRole('button', { name: 'Expand sidebar' });
      if (await expand.isVisible()) await expand.click();
    }
    const badge = page.locator('a[href="/chat"] [data-testid="navigation-attention-count"]').filter({ visible: true });
    await expect(badge).toHaveText('21');
    await expectAttentionTone(badge, 'accent');
    if (width < 1024) await page.getByRole('button', { name: 'Close navigation', exact: true }).click();
    waiting = waiting.filter((item) => item.conversation_id !== 'old-0');
    socket!.send(JSON.stringify({ type: 'conversation_updated', conversation_id: 'old-0', has_unread: false, last_read_at: '2026-10-14T20:01:00Z' }));
    await expect(page.getByTestId('dashboard-conversation-waiting-reason-old-0')).toHaveCount(0);
    await page.goto('/chat/conv-task-chat');
    if (width >= 1024) {
      await expect(page.getByText('Historical unread old-20', { exact: true }).first()).toBeVisible();
      await expect(page.getByText('Historical unread old-0', { exact: true })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    expect(errors).toEqual([]);
  });
}
