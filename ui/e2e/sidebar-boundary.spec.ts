import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { productionCss } from './helpers/production-css';

const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
const chat = readFileSync('src/routes/(app)/chat/[conversationId]/+page.svelte', 'utf8');
const rail = layout.match(/<aside\s+class=\{`([^`]+)`\}/)![1];
const timeline = chat.match(/class="([^"]*px-2\.5 pb-1\.5 pt-0[^"]*)"/)![1];

for (const height of [400, 800, 1100]) {
  for (const expanded of [false, true]) {
    test(`rail stays inside ${height}px viewport, expanded=${expanded}`, async ({ page }) => {
      const css = await productionCss();
      const classes = rail.replace(/\$\{sidebarExpanded[^}]+\}/, expanded ? 'w-64 p-4' : 'w-14 p-2');
      await page.setViewportSize({ width: 1200, height });
      await page.setContent(`<style>${css}</style>
        <div class="app-viewport-frame fixed inset-x-0 overflow-hidden">
          <div class="flex h-full app-shell-safe-block">
            <aside class="${classes}">
              <nav class="min-h-0 flex-1 overflow-y-auto"><div style="height:1500px">Navigation</div></nav>
              <footer class="shrink-0" style="height:100px">Footer</footer>
            </aside>
            <main class="flex flex-1 min-h-0 flex-col"><header style="height:60px" class="shrink-0 border-b">Header</header>
              <div id="timeline" class="${timeline}"><div style="height:1500px">Messages</div></div>
            </main>
          </div>
        </div>`);
      const box = (await page.locator('aside').boundingBox())!;
      expect(box.y + box.height).toBeLessThan(height - 5);
      const footer = (await page.locator('footer').boundingBox())!;
      expect(footer.y + footer.height).toBeLessThanOrEqual(box.y + box.height);
      await page.locator('nav').evaluate((node) => { node.scrollTop = 1000; });
      expect(await page.locator('nav').evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
      await expect(page.locator('#timeline')).toHaveCSS('padding-top', '0px');
      const header = (await page.locator('header').boundingBox())!;
      expect((await page.locator('#timeline').boundingBox())!.y).toBe(header.y + header.height);
    });
  }
}
