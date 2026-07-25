import { expect, test } from '@playwright/test';

test.describe('Session logs drawer parity', () => {
  test.use({ serviceWorkers: 'block' });

  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/notifications**', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '[]',
    }));
    await page.goto('/session-logs-drawer-fixture');
  });

  test('locks the page, shows activity, contains boundary scrolling, and restores focus', async ({ page }) => {
    const trigger = page.getByTestId('open-session-drawer');
    await trigger.focus();
    await trigger.click();
    const drawer = page.getByRole('dialog', { name: 'Session logs' });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId('session-activity-status')).toContainText('Agent is working');
    await expect.poll(() => page.evaluate(() => document.body.style.position)).toBe('fixed');

    const viewport = drawer.getByTestId('scoped-timeline-viewport');
    await viewport.hover();
    const pageY = await page.evaluate(() => window.scrollY);
    await page.mouse.wheel(0, -500);
    expect(await page.evaluate(() => window.scrollY)).toBe(pageY);

    await drawer.getByRole('button', { name: 'Close' }).click();
    await expect(drawer).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => document.body.style.position)).toBe('');
    await expect(trigger).toBeFocused();
  });

  test('replaces working dots with authoritative terminal status without remounting the drawer', async ({ page }) => {
    await page.getByTestId('open-session-drawer').click();
    const drawer = page.getByRole('dialog', { name: 'Session logs' });
    await expect(drawer.getByTestId('session-activity-status')).toContainText('Agent is working');
    await drawer.evaluate((node) => { (node as HTMLElement).dataset.mountMarker = 'preserved'; });

    await page.getByTestId('finish-session-step').evaluate((button) => (button as HTMLButtonElement).click());

    await expect(drawer.getByTestId('session-activity-status')).toHaveText('completed');
    await expect(drawer.getByTestId('session-activity-status').locator('[aria-label="Agent is working…"]')).toHaveCount(0);
    await expect(drawer).toHaveAttribute('data-mount-marker', 'preserved');
  });
});
