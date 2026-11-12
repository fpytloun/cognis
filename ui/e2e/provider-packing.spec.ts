import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  // Static preview has no controller-provided bootstrap CSP hashes.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

for (const width of [390, 768, 1440, 2048]) {
  test(`packs mixed provider reports into one readable scrolling row at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem('cognis.dashboardHealth.selected', 'providers');
      localStorage.setItem('cognis.dashboardHealth.collapsed', 'false');
    });
    const fixture = await installTaskCockpitFixture(page);
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/v1/inbox**', (route) => route.fulfill({ json: {
      items: [], unread_count: 0, next_cursor: null,
    } }));
    await page.route('**/api/v1/dashboard/navigation-summary', (route) => route.fulfill({ json: {
      chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
      schedules: 0, tasks: 0,
    } }));
    await page.route('**/api/v1/schedules**', (route) => route.fulfill({ json: [] }));
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' }
    }));
    await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.routeWebSocket('**/api/ws', (socket) => {
      socket.send(JSON.stringify({ type: 'authenticated' }));
      socket.onMessage((message) => {
        if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
      });
    });
    const now = new Date().toISOString();
    await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
      generated_at: now, summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, issues: [],
    } }));
    const providers = [
      ['anthropic', 3], ['anthropic-team', 3], ['codex', 1], ['elevenlabs', 1],
      ['gemini', 0], ['groq', 0], ['ollama', 0], ['openai', 0], ['openrouter', 0]
    ].map(([id, count]) => ({
      provider_id: id, display_name: id,
      monitoring: { enabled: true, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
      health_status: 'healthy', health_detail: 'Available', checked_at: now, health_checked_at: now,
      next_refresh_at: null, error: null, alert_severity: null, stale: false,
      usage: count ? {
        ok: true, provider_id: id, source: 'subscription', fetched_at: now,
        primary: count === 3 || id === 'codex' ? { used_percent: 25, window_duration_mins: 10080, resets_at: '2026-11-12T12:00:00Z' } : null,
        secondary: count === 3 ? { used_percent: 50, window_duration_mins: 300 } : null,
        additional_rate_limits: count === 3
          ? [{ limit_name: 'Sonnet weekly limit', primary: { used_percent: 30 } }]
          : id === 'elevenlabs' ? [{ limit_name: 'Subscription credits', primary: { used_percent: 15 } }] : []
      } : null
    }));
    await page.route('**/api/v1/provider-monitor**', async (route) => {
      const id = new URL(route.request().url()).pathname.split('/')[4];
      await route.fulfill({ json: id ? providers.find((provider) => provider.provider_id === id) : providers });
    });
    await page.goto('/');
    await expect(page.getByRole('tab', { name: /Providers/ })).toHaveAttribute('aria-selected', 'true');
    const cards = page.getByTestId('provider-card');
    await expect(cards).toHaveCount(5);
    await expect(cards.nth(0).getByRole('progressbar')).toHaveCount(3);
    await expect(cards.nth(1).getByRole('progressbar')).toHaveCount(3);
    await expect(cards.nth(2).getByRole('link', { name: 'gemini', exact: true })).toHaveCount(1);
    await expect(cards.nth(4).getByRole('link')).toHaveCount(3);

    const assertReadable = async (card: typeof cards) => {
      const clipped = await card.locator('[data-testid="provider-usage-bars"] span, [data-testid="provider-usage-bars"] time')
        .evaluateAll((nodes) => nodes.filter((node) => node.scrollWidth > node.clientWidth + 1).map((node) => node.textContent));
      expect(clipped).toEqual([]);
      for (const bar of await card.getByRole('progressbar').all()) {
        const heading = bar.locator('..').locator(':scope > div').first();
        const name = (await heading.locator(':scope > span').first().boundingBox())!;
        const value = (await heading.locator(':scope > span').last().boundingBox())!;
        expect(name.x + name.width).toBeLessThanOrEqual(value.x + 1);
        // Desktop cards keep name, reset and usage on one line so each limit is two lines tall.
        if (width >= 640) expect((await heading.boundingBox())!.height).toBeLessThanOrEqual(20);
      }
    };
    await assertReadable(cards.nth(0));
    for (const refresh of await page.getByRole('button', { name: /^Refresh / }).all()) {
      await expect(refresh).toBeEnabled();
    }
    await page.getByTestId('dashboard-health-strip').screenshot({
      path: test.info().outputPath(`provider-limits-${width}.png`),
    });

    if (width < 640) {
      await page.getByRole('button', { name: 'Next provider card' }).click();
      await expect.poll(async () => {
        const card = await cards.nth(1).boundingBox();
        const scroller = await page.getByTestId('provider-card-scroller').boundingBox();
        return Math.abs(card!.x - scroller!.x);
      }).toBeLessThan(2);
      await assertReadable(cards.nth(1));
      await page.getByRole('button', { name: 'Next provider card' }).click();
      await expect(cards.nth(2).getByRole('button', { name: 'Refresh gemini' })).toBeInViewport();
      await page.getByRole('button', { name: 'Next provider card' }).click();
      await expect(cards.nth(3).getByRole('button', { name: 'Refresh elevenlabs' })).toBeInViewport();
      await assertReadable(cards.nth(3));
      await page.getByTestId('dashboard-health-strip').screenshot({ path: test.info().outputPath('provider-subscription-phone.png') });
      await page.getByRole('button', { name: 'Next provider card' }).click();
      await expect(cards.nth(4).getByRole('button', { name: 'Refresh openrouter' })).toBeInViewport();
    } else {
      const codex = await page.getByTestId('provider-health-codex').boundingBox();
      const gemini = await page.getByTestId('provider-health-gemini').boundingBox();
      expect(gemini!.y).toBeGreaterThanOrEqual(codex!.y + codex!.height);
      expect(gemini!.x).toBe(codex!.x);
      // Cards stay in one horizontally scrolling row instead of wrapping into taller rows.
      const top = (await cards.nth(0).boundingBox())!.y;
      for (const card of await cards.all()) {
        expect((await card.boundingBox())!.y).toBe(top);
        await assertReadable(card);
      }
      const scroller = page.getByTestId('provider-card-scroller');
      const overflow = await scroller.evaluate((node) => node.scrollWidth > node.clientWidth);
      if (overflow) {
        await expect(cards.nth(4)).not.toBeInViewport({ ratio: 0.95 });
        await scroller.evaluate((node) => node.scrollTo({ left: node.scrollWidth, behavior: 'instant' }));
      }
      await expect(cards.nth(4).getByRole('button', { name: 'Refresh openrouter' })).toBeInViewport();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: test.info().outputPath(`provider-packing-${width}.png`) });
    expect(errors).toEqual([]);
    expect(fixture.unmockedRequests()).toEqual([]);
  });
}
