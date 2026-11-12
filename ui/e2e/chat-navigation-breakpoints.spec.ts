import { expect, test, type Page } from '@playwright/test';
import { login, openOrCreateConversation } from './helpers';

test.use({ serviceWorkers: 'block', hasTouch: true });

async function settleLayout(page: Page) {
  await page.evaluate(async () => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    await Promise.all(document.getAnimations()
      .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
      .map((animation) => animation.finished.catch(() => {})));
  });
}

test('chat navigation remains reachable across phone, tablet, and desktop widths', async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
  await expect(page.getByText('Send the first message to start this conversation.', { exact: true })).toBeVisible();
  const header = page.getByTestId('chat-header');
  const navigation = header.getByRole('button', { name: 'Navigation', exact: true });
  const conversations = header.getByRole('button', { name: 'Open conversations', exact: true });

  for (const width of [834, 390, 639, 640, 768, 1023, 1024, 1194]) {
    await page.setViewportSize({ width, height: 1194 });
    if (width < 1024) {
      await expect(navigation).toBeVisible();
      await expect(conversations).toBeVisible();
      const navBox = (await navigation.boundingBox())!;
      const listBox = (await conversations.boundingBox())!;
      expect(navBox.x + navBox.width).toBeLessThanOrEqual(listBox.x);
      await settleLayout(page);
      await page.screenshot({ path: test.info().outputPath(`chat-${width}.png`) });

      await navigation.tap();
      const drawer = page.getByRole('dialog', { name: 'Navigation menu', exact: true });
      await expect(drawer).toBeVisible();
      await expect(drawer.locator('a[href="/agents"]')).toBeVisible();
      await settleLayout(page);
      const drawerBox = (await drawer.boundingBox())!;
      expect(drawerBox.x).toBeGreaterThanOrEqual(0);
      expect(drawerBox.x + drawerBox.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: test.info().outputPath(`navigation-${width}.png`) });
      await page.getByRole('button', { name: 'Close navigation', exact: true }).tap();
      await expect(drawer).toBeHidden();

      await conversations.tap();
      const list = page.getByRole('dialog', { name: 'Conversation list', exact: true });
      await expect(list).toBeVisible();
      await list.getByRole('button', { name: 'Close conversation list', exact: true }).tap();
      await expect(list).toBeHidden();
    } else {
      await expect(navigation).toBeHidden();
      await expect(conversations).toBeHidden();
      await expect(page.getByRole('button', { name: /(?:Expand|Collapse) sidebar/ })).toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }

  await page.setViewportSize({ width: 834, height: 1194 });
  await page.goto(`${page.url()}?window=1`);
  await expect(page.locator('textarea').first()).toBeVisible();
  await expect(navigation).toHaveCount(0);
  await expect(conversations).toHaveCount(0);
});
