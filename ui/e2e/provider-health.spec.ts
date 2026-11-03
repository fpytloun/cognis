import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

for (const width of [390, 1440]) {
  test(`compact provider health and manual refresh at ${width}px`, async ({ page }) => {
    page.on('pageerror', (error) => console.error(`Page error: ${error.message}`));
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' } }));
    await page.route('**/api/health', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/health/providers', (route) => route.fulfill({ json: {} }));
    let refreshes = 0;
    const provider = {
      provider_id: 'claude', display_name: 'Claude subscription with a long descriptive provider name',
      monitoring: { enabled: false, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
      health_status: 'healthy', health_detail: 'Usage endpoint reachable', checked_at: new Date().toISOString(),
      health_checked_at: new Date(Date.now() - 7_200_000).toISOString(), next_refresh_at: new Date(Date.now() - 3_600_000).toISOString(),
      usage: {
        ok: true, provider_id: 'claude', source: 'anthropic_subscription_usage',
        fetched_at: new Date(Date.now() - 7_200_000).toISOString(),
        primary: { used_percent: 12, window_duration_mins: 300, resets_at: '2026-10-02T12:00:00Z' },
        secondary: { used_percent: 70, window_duration_mins: 10080, resets_at: '2026-10-08T12:00:00Z' },
        additional_rate_limits: [{ limit_name: 'Sonnet', primary: { used_percent: 69 } }]
      }, error: null, alert_severity: null, stale: true
    };
    await page.route('**/api/v1/provider-monitor**', async (route) => {
      if (route.request().method() === 'POST') {
        refreshes++;
        provider.health_checked_at = new Date().toISOString();
        provider.stale = false;
        provider.next_refresh_at = new Date(Date.now() + 900_000).toISOString();
        provider.usage.fetched_at = provider.health_checked_at;
      }
      await route.fulfill({ json: new URL(route.request().url()).pathname === '/api/v1/provider-monitor' ? [provider] : provider });
    });
    await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
      generated_at: new Date().toISOString(), summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, issues: []
    } }));
    await page.goto('/');
    const strip = page.getByTestId('dashboard-health-strip');
    await expect(strip).toBeVisible();
    await expect(strip.getByRole('tablist')).toHaveCount(1);
    const tab = strip.getByRole('tab', { name: /^Providers/ });
    if (await tab.getAttribute('aria-selected') !== 'true') await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    const providers = strip.getByTestId('dashboard-providers');
    await expect(providers.getByRole('tablist')).toHaveCount(0);
    await expect(providers.getByText(/auto off/)).toBeVisible();
    await expect(providers.getByRole('progressbar')).toHaveCount(3);
    await expect(providers.getByText('12% used')).toBeVisible();
    const stale = providers.getByText(/Refreshed/);
    await expect(stale).toBeVisible();
    await expect.poll(() => stale.evaluate((element) => {
      const probe = document.createElement('span');
      probe.className = 'text-warning';
      element.append(probe);
      const matches = getComputedStyle(element).color === getComputedStyle(probe).color;
      probe.remove();
      return matches;
    })).toBe(true);
    const box = await providers.boundingBox();
    expect(box!.height).toBeLessThanOrEqual(136);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await strip.getByRole('button', { name: /Refresh Claude/ }).click();
    await expect.poll(() => refreshes).toBe(1);
    await expect(strip.getByText(/Refreshed/)).toHaveCount(0);
    await expect(strip.getByRole('button', { name: /Refresh Claude/ })).toHaveAttribute('title', /Refreshed/);
    await page.screenshot({ path: test.info().outputPath(`provider-health-${width}.png`) });
  });
}
