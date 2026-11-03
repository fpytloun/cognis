import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block' });

test('conversation opens the on-demand new-chat dialog with working bindings and cancellation', async ({ page }) => {
  await installTaskCockpitFixture(page);
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/chat/conv-task-chat');
  const open = page.getByRole('button', { name: 'New conversation', exact: true });
  await open.click();
  const dialog = page.getByRole('dialog', { name: 'New chat', exact: true });
  await expect(dialog.getByLabel('Primary agent')).toBeVisible();
  await dialog.getByLabel('Primary agent').selectOption('agent-stage39');
  await expect(dialog.getByLabel('Primary agent')).toHaveValue('agent-stage39');
  await expect(dialog.getByRole('button', { name: 'Create conversation', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await open.click();
  await expect(dialog.getByLabel('Primary agent')).toBeVisible();
});
