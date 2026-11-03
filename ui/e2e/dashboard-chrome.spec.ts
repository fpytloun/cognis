import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

for (const width of [1440, 390]) {
  test(`neutral notification panel and compact branding at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' } }));
    await page.route('**/api/health', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/health/providers', (route) => route.fulfill({ json: {} }));
    await page.route('**/api/v1/schedules**', (route) => route.fulfill({ json: [] }));
    await page.route('**/api/v1/provider-monitor**', (route) => route.fulfill({ json: [{
      provider_id: 'claude', display_name: 'Claude',
      monitoring: { enabled: false, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
      health_status: 'unknown', health_detail: 'Not checked', health_checked_at: null,
      checked_at: null, next_refresh_at: null, usage: null, error: null, alert_severity: null, stale: true,
    }] }));
    await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({ json: {
      generated_at: new Date().toISOString(), summary: { total: 2, warning: 1, critical: 1, info: 0, truncated: false },
      issues: [
        { id: 'runtime-warning', severity: 'warning', kind: 'executor_degraded',
          title: 'Fixture Linux is degraded', detail: 'The executor reported a degraded runtime state.',
          resource: { type: 'executor', id: 'fixture-linux', label: 'Fixture Linux' },
          observed_at: null, action_url: '/settings?tab=executors', action_label: 'View' },
        { id: 'quota-critical', severity: 'critical', kind: 'provider_quota',
          title: 'Codex usage limit reached', detail: 'Weekly usage is exhausted until the next reset.',
          resource: { type: 'llm_provider', id: 'codex', label: 'Codex' },
          observed_at: null, action_url: '/settings?tab=providers', action_label: 'View' }
      ]
    } }));
    await page.goto('/');
    const strip = page.getByTestId('dashboard-issues-strip');
    await expect(strip).toBeVisible({ timeout: 30000 });
    const panel = page.getByTestId('dashboard-issues-needs-attention').locator(':scope > div');
    await expect(panel).toHaveClass(/border-separator/);
    await expect(panel).not.toHaveClass(/(?:bg|border)-(?:warning|danger)(?:[-/\s]|$)/);
    await expect(page.getByTestId('dashboard-issue-runtime-warning')).toHaveClass(/border-warning-border/);
    await expect(page.getByTestId('dashboard-issue-quota-critical')).toHaveClass(/border-danger-border/);
    const warning = page.getByTestId('dashboard-issue-runtime-warning');
    const details = warning.getByRole('button', { name: 'Show details' });
    const view = warning.getByRole('button', { name: 'View', exact: true });
    await expect(details).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(async () => {
      const detailsBox = (await details.boundingBox())!;
      const viewBox = (await view.boundingBox())!;
      return Math.abs(detailsBox.y - viewBox.y);
    }).toBeLessThan(2);
    await expect(page.getByText('Agent workspace', { exact: true })).toHaveCount(0);
    if (width > 1024) {
      const expand = page.getByRole('button', { name: 'Expand sidebar' });
      if (await expand.isVisible()) await expand.click();
      const logo = page.getByTestId('sidebar-logo-link');
      // The SVG has its own accessible title; count only the visible wordmark.
      const wordmark = logo.locator(':scope > span:not(.cognis-logo)');
      await expect(wordmark).toHaveCount(1);
      await expect(wordmark).toHaveText('Cognis');
      await expect.poll(() => logo.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
      await logo.hover();
      const hoverColor = await logo.evaluate(() =>
        `rgba(${getComputedStyle(document.documentElement).getPropertyValue('--color-raised').trim().split(/\s+/).join(', ')}, 0.6)`);
      await expect(logo).toHaveCSS('background-color', hoverColor);
      await logo.screenshot({ path: test.info().outputPath('sidebar-brand-hover.png') });
      await logo.focus();
      await page.keyboard.press('Tab');
      await page.keyboard.press('Shift+Tab');
      await expect(logo).toBeFocused();
      await expect(logo).toHaveCSS('outline-style', 'solid');
      await expect(logo).toHaveCSS('outline-width', '2px');
      await expect(logo).toHaveCSS('outline-offset', '-2px');
      await logo.screenshot({ path: test.info().outputPath('sidebar-brand-focus.png') });
      await page.screenshot({ path: test.info().outputPath('dashboard-neutral-notifications-desktop.png') });
      await page.getByRole('button', { name: 'Collapse sidebar' }).click();
      await expect(wordmark).toHaveCount(0);
      await expect.poll(() => logo.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    } else {
      await page.screenshot({ path: test.info().outputPath('dashboard-neutral-notifications-mobile.png') });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

    const health = page.getByTestId('dashboard-health-strip');
    const notifications = health.getByRole('tab', { name: /Notifications/ });
    const providers = health.getByRole('tab', { name: /Providers/ });
    const expandedHeight = (await health.boundingBox())!.height;
    await notifications.click();
    await expect(strip).toBeHidden();
    await expect(notifications).toBeVisible();
    await expect(providers).toBeVisible();
    expect((await health.boundingBox())!.height).toBeLessThan(expandedHeight);
    await health.screenshot({ path: test.info().outputPath(`dashboard-status-collapsed-${width}.png`) });
    await page.reload();
    await expect(page.getByRole('button', { name: 'Expand dashboard status' })).toBeVisible();
    await expect(strip).toBeHidden();
    await providers.click();
    await expect(page.getByTestId('dashboard-providers')).toBeVisible();
    await providers.click();
    await expect(page.getByTestId('dashboard-providers')).toBeHidden();
    await providers.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('dashboard-providers')).toBeVisible();
    await page.keyboard.press('ArrowLeft');
    await expect(notifications).toBeFocused();
    await expect(strip).toBeVisible();
    await page.getByRole('button', { name: 'Collapse dashboard status' }).click();
    await page.getByRole('button', { name: 'Expand dashboard status' }).click();
    await expect(strip).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}
