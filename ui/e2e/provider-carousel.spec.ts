import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ hasTouch: true });

for (const width of [390, 1440]) {
  test(`provider card paging and paired health at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' } }));
    await page.route('**/api/health', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/health/providers', (route) => route.fulfill({ json: {} }));
    await page.route('**/api/v1/schedules**', (route) => route.fulfill({ json: [] }));
    const stamp = new Date().toISOString();
    const make = (id: string, name: string, used: number | null) => ({
      provider_id: id, display_name: name,
      monitoring: { enabled: true, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
      health_status: id === 'ollama' ? 'unknown' : 'healthy', health_detail: 'Provider reachable',
      checked_at: stamp, health_checked_at: stamp, next_refresh_at: new Date(Date.now() + 900000).toISOString(),
      error: null, alert_severity: null, stale: false,
      usage: used === null ? null : {
        ok: true, provider_id: id, source: id === 'codex' ? 'chatgpt_codex_usage' : 'anthropic_subscription_usage',
        fetched_at: stamp,
        plan_type: id === 'codex' ? 'pro' : 'common',
        primary: { used_percent: id === 'codex' ? used : 0, window_duration_mins: id === 'codex' ? 10080 : 300,
          resets_at: new Date(Date.now() + (id === 'codex' ? 172800000 : 18000000)).toISOString() },
        secondary: id === 'codex' ? null : { used_percent: used, window_duration_mins: 10080, resets_at: new Date(Date.now() + 172800000).toISOString() },
        additional_rate_limits: id === 'codex' ? [] : [{ limit_name: 'Sonnet', primary: { used_percent: used + 1, resets_at: new Date(Date.now() + 172800000).toISOString() } }]
      }
    });
    const providers = [make('anthropic', 'Anthropic', 36), make('anthropic-training', 'Anthropic (Training)', 8), make('codex', 'Codex', 42), make('litellm', 'LiteLLM', null), make('ollama', 'Ollama (Demo workstation)', null)];
    await page.route('**/api/v1/provider-monitor**', (route) => {
      const id = new URL(route.request().url()).pathname.split('/')[4];
      return route.fulfill({ json: id ? providers.find((provider) => provider.provider_id === id) : providers });
    });
    await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
      generated_at: stamp, summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, issues: []
    } }));
    await page.goto('/');
    const strip = page.getByTestId('dashboard-health-strip');
    await expect(strip).toBeVisible({ timeout: 30000 });
    const cards = strip.getByTestId('provider-card');
    await expect(cards).toHaveCount(4);
    await expect(cards.nth(0).getByText('5-hour reset', { exact: true })).toBeAttached();
    await expect(cards.nth(0).getByText('Weekly reset', { exact: true })).toBeAttached();
    await expect(cards.nth(0).getByText('Sonnet reset', { exact: true })).toBeAttached();
    await expect(cards.nth(2).getByText('Plan pro', { exact: true })).toBeAttached();
    await expect(strip.getByText('Plan common', { exact: true })).toHaveCount(0);
    await expect(strip.getByText(/Refreshed/)).toHaveCount(0);
    await expect(cards.nth(2).getByRole('button', { name: 'Refresh Codex' })).toHaveAttribute('title', /Refreshed/);
    const firstCard = cards.nth(0);
    const cardBox = (await firstCard.boundingBox())!;
    const refreshBox = (await firstCard.getByRole('button', { name: 'Refresh Anthropic', exact: true }).boundingBox())!;
    const rootFontSize = await page.locator('html').evaluate(node => getComputedStyle(node).fontSize);
    await test.info().attach('provider-control-geometry.json', {
      contentType: 'application/json',
      body: JSON.stringify({
        card: cardBox, refresh: refreshBox,
        rootFontSize,
        usage: await firstCard.getByTestId('provider-usage-bars').locator('div, p, span').evaluateAll(nodes =>
          nodes.map(node => ({ className: node.className, rect: node.getBoundingClientRect().toJSON() }))),
      }),
    });
    expect((await strip.boundingBox())!.height).toBeLessThan(135);
    for (const name of ['5-hour', 'Weekly', 'Sonnet']) {
      const label = firstCard.getByText(name, { exact: true });
      await expect(label).toBeVisible();
      expect(await label.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    }
    // Preserve the existing two-rem control at the application's actual root size.
    expect(refreshBox.width).toBeGreaterThanOrEqual(2 * parseFloat(rootFontSize));
    expect(refreshBox.height).toBeGreaterThanOrEqual(2 * parseFloat(rootFontSize));
    expect(refreshBox.y).toBeGreaterThanOrEqual(cardBox.y - 1);
    expect(refreshBox.x + refreshBox.width).toBeLessThanOrEqual(cardBox.x + cardBox.width + 1);
    for (const name of ['5-hour', 'Weekly', 'Sonnet']) {
      const labelBox = (await firstCard.getByText(name, { exact: true }).boundingBox())!;
      const intersects = labelBox.x < refreshBox.x + refreshBox.width - 1
        && labelBox.x + labelBox.width > refreshBox.x + 1
        && labelBox.y < refreshBox.y + refreshBox.height - 1
        && labelBox.y + labelBox.height > refreshBox.y + 1;
      expect(intersects, `${name} overlaps the refresh control`).toBe(false);
    }
    await expect(cards.nth(2).getByText('LiteLLM', { exact: true })).toBeAttached();
    await expect(cards.nth(2).getByRole('progressbar')).toHaveCount(1);
    await expect(cards.nth(3).getByText('Ollama (Demo workstation)', { exact: true })).toBeAttached();
    await expect(cards.nth(3).getByRole('progressbar')).toHaveCount(0);
    await expect(page.getByTestId('control-center-summary')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Cognis', exact: true })).toHaveCount(0);
    await expect(page.getByTestId('running-task-count')).toBeVisible();
    await expect(page.getByTestId('active-conversation-count')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    if (width < 640) {
      await expect(cards.nth(0)).toBeInViewport({ ratio: 0.95 });
      await expect(cards.nth(1)).not.toBeInViewport();
      await page.screenshot({ path: test.info().outputPath('mobile-usage-card.png') });
      await strip.getByRole('button', { name: 'Previous provider card' }).click();
      await expect(cards.nth(3)).toBeInViewport({ ratio: 0.95 });
      await page.screenshot({ path: test.info().outputPath('mobile-paired-health-card.png') });
      await strip.getByRole('button', { name: 'Next provider card' }).click();
      await expect(cards.nth(0)).toBeInViewport({ ratio: 0.95 });
      const box = (await page.getByTestId('provider-card-scroller').boundingBox())!;
      const cdp = await page.context().newCDPSession(page);
      const y = box.y + box.height / 2;
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + box.width - 15, y }] });
      for (let step = 1; step <= 8; step++) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: box.x + box.width - 15 - (box.width - 30) * step / 8, y }] });
        await page.waitForTimeout(30);
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await expect(cards.nth(1)).toBeInViewport({ ratio: 0.95 });
      await cdp.detach();
    } else {
      await expect(strip.getByRole('button', { name: 'Next provider card' })).not.toBeVisible();
      await expect(cards.nth(3)).toBeInViewport({ ratio: 0.95 });
      await page.screenshot({ path: test.info().outputPath('desktop-paired-provider-cards.png') });
    }
  });
}
