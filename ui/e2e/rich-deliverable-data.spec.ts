import { expect, test } from '@playwright/test';

test.describe('rich deliverable interactive data blocks', () => {
  test.use({ serviceWorkers: 'block' });

  test('supports chart, dashboard, and incident interactions in production preview', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 2200 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /interactive operations dashboard/i }).click();

    const fixture = page.getByTestId('rich-deliverable-fixture');
    await expect(fixture).toHaveAttribute('data-scenario', 'interactive-data-dashboard');
    await expect(page.locator('[data-rich-block-type="dashboard"]')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="chart"]')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="incident_timeline"]')).toBeVisible();
    await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);

    const thirtyDay = page.getByRole('button', { name: '30D' });
    await thirtyDay.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await thirtyDay.click();
    await expect(thirtyDay).toHaveAttribute('aria-pressed', 'true');

    const errors = page.getByRole('button', { name: 'Errors' });
    await errors.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await errors.click();
    await expect(errors).toHaveAttribute('aria-pressed', 'false');

    const details = page.locator('.rich-dashboard-card').filter({ hasText: 'Availability' }).locator('summary');
    await details.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await details.click();
    await expect(page.getByText('API gateway: 99.98%')).toBeVisible();

    const chart = page.locator('[data-rich-block-type="chart"] canvas').first();
    await chart.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await chart.hover({ position: { x: 160, y: 90 } });
    await expect(page.getByTestId('rich-chart-tooltip')).toBeVisible();
    await chart.click({ position: { x: 160, y: 90 } });
    await expect(page.getByTestId('rich-chart-pinned')).toBeVisible();

    const incidentEntry = page.getByText('Traffic shifted');
    await incidentEntry.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await incidentEntry.click();
    await expect(page.getByText('Requests were shifted away from the degraded provider while queue depth drained.')).toBeVisible();
    await expect(page.getByLabel('Publish dashboard interaction QA')).toBeVisible();
  });
});

