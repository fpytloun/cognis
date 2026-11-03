import { expect, test, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import { expectAttentionTone } from './attention-tone';

for (const width of [1440, 390]) {
  test(`navigation attention counts and dots at ${width}px`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    let socket: WebSocketRoute | undefined;
    await page.routeWebSocket('**/api/ws', (ws) => {
      socket = ws;
      ws.send(JSON.stringify({ type: 'authenticated' }));
      ws.onMessage((message) => {
        const payload = JSON.parse(String(message));
        if (payload.type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
      });
    });
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' } }));
    await page.route('**/api/health', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/health/providers', (route) => route.fulfill({ json: {} }));
    await page.route('**/api/v1/inbox**', (route) => route.fulfill({ json: { items: [], unread_count: 2, next_cursor: null } }));
    let tasks = 6;
    await page.route('**/api/v1/dashboard/navigation-summary', (route) => route.fulfill({ json: {
      chat_groups: [
        { count: 4, is_active: false, has_unread: true, session_observed: false, pending_notification_types: [], active_session_status: null, active_session_completion_reason: null },
        { count: 1, is_active: false, has_unread: true, session_observed: false, pending_notification_types: ['escalation'], active_session_status: null, active_session_completion_reason: null },
      ],
      dashboard: { total: 3, critical: 0, warning: 3, info: 0, truncated: false },
      schedules: 2, tasks,
    } }));
    await page.goto('/');
    await expect.poll(() => errors).toEqual([]);
    if (width < 1024) await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
    else {
      await expect(page.getByRole('button', { name: /(?:Expand|Collapse) sidebar/ })).toBeVisible();
      const expand = page.getByRole('button', { name: 'Expand sidebar' });
      if (await expand.isVisible()) await expand.click();
    }
    const nav = page.locator('nav').filter({ has: page.locator('a[href="/schedules"]') }).filter({ visible: true });
    await expect(nav.locator('a[href="/chat"] [data-testid="navigation-attention-count"]')).toHaveText('5');
    await expectAttentionTone(nav.locator('a[href="/chat"] [data-testid="navigation-attention-count"]'), 'danger');
    await expect(nav.locator('a[href="/"] [data-testid="navigation-attention-count"]')).toHaveText('5');
    await expect(nav.locator('a[href="/schedules"] [data-testid="navigation-attention-count"]')).toHaveText('2');
    await expect(nav.locator('a[href="/tasks"] [data-testid="navigation-attention-count"]')).toHaveText('6');
    await expect(nav.locator('a[href="/agents"] [data-testid="navigation-attention-count"]')).toHaveCount(0);
    await nav.locator('a[href="/agents"]').click();
    await expect(page).toHaveURL(/\/agents$/);
    if (width < 1024) await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
    tasks = 7;
    await expect.poll(() => Boolean(socket)).toBe(true);
    socket!.send(JSON.stringify({ type: 'scope_invalidated', reason: 'executor_state_changed' }));
    await expect(nav.locator('a[href="/tasks"] [data-testid="navigation-attention-count"]')).toHaveText('7');
    if (width >= 1024) {
      await page.getByRole('button', { name: 'Collapse sidebar' }).click();
      await expectAttentionTone(nav.locator('a[href="/chat"] [data-testid="navigation-attention-dot"]'), 'danger', 'dot');
      await expect(nav.getByTestId('navigation-attention-count')).toHaveCount(0);
      tasks = 0;
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await expect(nav.locator('a[href="/tasks"] [data-testid="navigation-attention-dot"]')).toHaveCount(0);
      await page.getByRole('button', { name: 'Expand sidebar' }).click();
      await expect(nav.locator('a[href="/tasks"] [data-testid="navigation-attention-count"]')).toHaveCount(0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: test.info().outputPath('navigation-attention.png') });
  });
}
