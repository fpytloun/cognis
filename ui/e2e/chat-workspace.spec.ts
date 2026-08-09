import { expect, test } from '@playwright/test';

test.describe.configure({ mode: 'serial' });
test.describe('chat workspace inspector', () => {
  test.use({ serviceWorkers: 'block' });

  for (const viewport of [
    { width: 844, height: 390 },
    { width: 1024, height: 768 },
    { width: 1440, height: 900 },
    { width: 1920, height: 1080 },
  ]) {
    test(`contains horizontal overflow at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.goto('/chat-workspace-fixture');
      await expect(page.getByTestId('workspace-fixture')).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      if (viewport.width >= 1024) {
        await expect(page.getByRole('separator', { name: 'Resize conversation inspector' })).toBeVisible();
      } else {
        await expect(page.getByRole('dialog', { name: 'Conversation information' })).toBeVisible();
      }
    });
  }

  test('supports focus mode and narrow file drill-in', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 });
    await page.goto('/chat-workspace-fixture');
    await page.getByRole('button', { name: 'Expand inspector' }).click();
    await expect(page.getByRole('button', { name: 'Exit expanded inspector' })).toBeVisible();
    await page.getByRole('button', { name: 'Exit expanded inspector' }).click();
    await expect(page.getByRole('button', { name: 'Expand inspector' })).toBeFocused();
    await page.getByRole('treeitem', { name: /a-very-long-file-name/ }).click();
    await expect(page.getByRole('button', { name: 'Back to files' })).toBeVisible();
  });
});
