import { expect, test } from '@playwright/test';
import { channelAccounts, channelTypes } from './messaging-channels-fixture';

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`Messaging channels uses a grouped list and modal editor at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.emulateMedia({ colorScheme: viewport.width > 1000 ? 'dark' : 'light' });
    page.on('pageerror', (error) => { throw error; });
    const mutations: Record<string, unknown>[] = [];
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis-e2e.localdev.me', name: 'Owner', role: 'admin', is_active: true },
    }));
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === 'PATCH' && path.startsWith('/api/v1/channels/accounts/')) {
        mutations.push(route.request().postDataJSON());
        await route.fulfill({ json: { ...channelAccounts[0], ...mutations[mutations.length - 1] } });
      } else if (path === '/api/v1/channels/types') {
        await route.fulfill({ json: channelTypes });
      } else if (path === '/api/v1/channels/accounts') {
        await route.fulfill({ json: channelAccounts });
      } else if (path === '/api/v1/agents') {
        await route.fulfill({ json: { items: [
          { agent_id: 'riker', name: 'Riker', display_name: 'Riker', agent_type: 'primary', status: 'active' },
          { agent_id: 'laforge', name: 'LaForge', display_name: 'LaForge', agent_type: 'primary', status: 'active' },
        ], next_cursor: null, has_more: false } });
      } else {
        await route.fulfill({ json: [] });
      }
    });
    await page.goto('/channels');
    await expect(page.getByRole('heading', { name: 'Messaging channels', exact: true })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('channels-list.png'), fullPage: true });
    const apps = page.getByRole('region', { name: 'Cognis channels' });
    await expect(apps.getByRole('button', { name: 'Delete' })).toHaveCount(0);
    await apps.getByRole('button', { name: 'Edit', exact: true }).nth(1).click();
    const dialog = page.getByRole('dialog', { name: 'Edit channel account' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('checkbox', { name: /preferred channel/ })).toBeChecked();
    await expect(dialog.getByRole('checkbox', { name: /preferred channel/ })).toBeDisabled();
    await expect(dialog.getByRole('button', { name: 'Save preference' })).toBeDisabled();
    await expect(dialog.getByRole('combobox')).toHaveCount(0);
    await expect(dialog.getByRole('textbox')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('channel-editor.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await apps.getByRole('button', { name: 'Edit', exact: true }).first().click();
    await dialog.getByRole('checkbox', { name: /preferred channel/ }).uncheck();
    await dialog.getByRole('button', { name: 'Save preference' }).click();
    await expect.poll(() => mutations).toEqual([{ preferred_for_task_delivery: false }]);
    await expect(dialog).toHaveCount(0);
    await page.getByTestId('channels-add-account').click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Select Cognis app' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Select Signal' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
