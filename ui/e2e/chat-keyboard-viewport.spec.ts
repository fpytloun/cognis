import { expect, test } from '@playwright/test';
import { login, openOrCreateConversation } from './helpers';

test.use({ serviceWorkers: 'block', hasTouch: true, viewport: { width: 834, height: 1194 } });

test('tablet composer follows keyboard viewport changes and restores after dismissal', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'standalone', { value: true });
    Object.defineProperty(navigator, 'platform', { value: 'MacIntel' });
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5 });
    const viewport = new EventTarget();
    Object.assign(viewport, { height: innerHeight, width: innerWidth, offsetTop: 0, offsetLeft: 0, scale: 1 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  });
  await login(page);
  await openOrCreateConversation(page);
  await expect(page.getByText('Send the first message to start this conversation.', { exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-standalone-pwa', 'true');
  const composer = page.locator('textarea').first();
  await composer.tap();
  // Desktop browser engines do not open an iPad software keyboard. Inject only
  // its viewport events; application rendering and APIs remain real.
  for (const [height, offsetTop] of [[850, 0], [720, 0], [720, 100], [850, 0]]) {
    await page.evaluate(({ height, offsetTop }) => {
      Object.assign(window.visualViewport!, { height, offsetTop });
      window.visualViewport!.dispatchEvent(new Event('resize'));
    }, { height, offsetTop });
    await expect(page.locator('html')).toHaveAttribute('data-keyboard', 'open');
    await expect.poll(async () => {
      const rect = await composer.boundingBox();
      return rect ? rect.y + rect.height : Infinity;
    }).toBeLessThanOrEqual(height + offsetTop);
    await page.screenshot({ path: test.info().outputPath(`keyboard-${height}-${offsetTop}.png`) });
  }
  await page.evaluate(() => {
    Object.assign(window.visualViewport!, { height: innerHeight, offsetTop: 0 });
    window.visualViewport!.dispatchEvent(new Event('resize'));
  });
  await expect(page.locator('html')).toHaveAttribute('data-keyboard', 'closed');
  await expect(composer).toBeVisible();
});
