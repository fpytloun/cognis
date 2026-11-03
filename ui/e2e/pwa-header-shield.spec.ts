import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { DEFAULT_THEME_FAMILY } from '../src/lib/theme/catalog';
import { productionCss } from './helpers/production-css';
const chat = readFileSync('src/routes/(app)/chat/[conversationId]/+page.svelte', 'utf8');
const child = readFileSync('src/lib/components/chat-v2/SessionLogHeader.svelte', 'utf8');
const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
const headerClasses = [chat, child, layout].map((text) => {
  const match = text.match(/class="([^"]*app-keyboard-stable-header[^"]*)"/);
  if (!match) throw new Error('Missing production header classes');
  return match[1];
});

// This verifies CSS layout/paint, not Apple's installed-PWA system glass.
for (const width of [390, 844, 1023]) {
  for (const [index, classes] of headerClasses.entries()) {
    test(`managed viewport ${width}px header ${index}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 850 });
      const css = await productionCss();
      await page.setContent(`
        <style>${css}</style>
        <style>:root { --app-safe-area-top: 59px; }</style>
        <div class="app-shell-viewport app-viewport-frame fixed inset-x-0 overflow-hidden">
          <div class="app-chat-mobile-safe-top flex h-full flex-col overflow-hidden">
            <header id="header" class="${classes}">
              <button style="height:44px">Header controls</button>
            </header>
            <div id="timeline" class="relative flex-1 overflow-y-auto">
              <div style="height:2000px;background:red">Scrolling content</div>
            </div>
          </div>
        </div>`);
      await page.evaluate((family) => {
        document.documentElement.dataset.themeFamily = family;
        document.documentElement.dataset.resolvedTheme = 'dark';
        document.documentElement.dataset.standalonePwa = 'true';
      }, DEFAULT_THEME_FAMILY);
      const header = page.locator('#header');
      await expect(header).toHaveCSS('background-color', 'rgb(7, 17, 26)');
      await expect(page.locator('html')).toHaveCSS('background-color', 'rgb(7, 17, 26)');
      expect(await header.evaluate((element) => parseFloat(getComputedStyle(element).paddingTop))).toBeLessThan(20);
      await expect(page.locator('.app-chat-mobile-safe-top')).toHaveCSS('padding-top', '0px');
      await page.evaluate(() => {
        document.documentElement.dataset.keyboard = 'open';
        document.documentElement.style.setProperty('--app-visual-viewport-offset-top', '120px');
        document.querySelector('#timeline')!.scrollTop = 200;
      });
      await expect(header).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 120)');
      await expect(header).toHaveCSS('background-color', 'rgb(7, 17, 26)');
      const shield = await header.evaluate((element) => {
        const style = getComputedStyle(element, '::before');
        return { top: style.top, image: style.backgroundImage, color: style.backgroundColor };
      });
      expect(shield.image).toBe('none');
      expect(await header.evaluate((element) => getComputedStyle(element, '::before').content)).toBe('none');
      await page.evaluate(() => { document.documentElement.dataset.keyboard = 'closed'; });
      await expect(header).toHaveCSS('transform', 'none');
      await expect(header).toHaveCSS('background-color', 'rgb(7, 17, 26)');
      await page.evaluate(() => { document.documentElement.dataset.standalonePwa = 'false'; });
      await expect(page.locator('.app-chat-mobile-safe-top')).toHaveCSS('padding-top', '0px');
    });
  }
}
