import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { selectViewerTheme } from './viewer-theme';

test.use({ viewport: { width: 1230, height: 1133 }, deviceScaleFactor: 2, contextOptions: { reducedMotion: 'reduce' } });

for (const theme of ['dark', 'light'] as const) {
  test(`reference geometry ${theme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript(theme => localStorage.setItem('cognis-rich-viewer-theme', theme === 'dark' ? 'system' : 'light'), theme);
    await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&surface=standalone&capture=viewer&width=1230');
    const viewer = page.getByTestId('rich-deliverable');
    await expect(viewer).toHaveAttribute('data-viewer-theme', theme);
    await page.evaluate(() => document.fonts.ready);
    await expect(viewer.locator('aside.rich-toc')).toBeVisible();
    const icons = await viewer.locator('[aria-label="Viewer controls"] [data-viewer-icon]').evaluateAll(nodes => nodes.map(n => n.getAttribute('data-viewer-icon')));
    expect(icons).toEqual([theme === 'dark' ? 'system' : 'light', 'copy']);
    await expect(viewer.locator('.download-menu')).toHaveCount(0);
    const geometry = await viewer.evaluate(el => {
      const rect = (selector: string) => {
        const node = el.querySelector(selector)!;
        const r = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return { x: r.x, y: r.y, width: r.width, height: r.height, fontSize: style.fontSize, lineHeight: style.lineHeight, fontFamily: style.fontFamily };
      };
      return { header: rect('.viewer-chrome'), toc: rect('.rich-toc'), body: rect('.rich-body'), h1: rect('.rich-hero h1'), prose: rect('.rich-markdown p'), lede: rect('.rich-lede'), summary: rect('.rich-kv-summary'), section: rect('[data-rich-block-type="section"]'), copy: rect('.copy-control') };
    });
    await writeFile(`/tmp/rich-parity-${theme}-geometry.json`, JSON.stringify(geometry, null, 2));
    await page.screenshot({ path: `/tmp/rich-parity-${theme}-open.png` });
    expect(geometry.header.height).toBe(63);
    expect(geometry.toc.width).toBe(262);
    expect(geometry.body.x).toBe(374);
    expect(geometry.body.width).toBe(816);
    expect(geometry.h1.fontSize).toBe('44px');
    expect(geometry.prose.width).toBe(630);
    expect(geometry.copy.width).toBe(34);
    expect(await viewer.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await viewer.getByRole('button', { name: 'Close table of contents' }).click();
    await expect(viewer.locator('aside.rich-toc')).toHaveCount(0);
    expect(await viewer.locator('.rich-body').evaluate(el => el.clientWidth)).toBeGreaterThan(1000);
    await page.screenshot({ path: `/tmp/rich-parity-${theme}-collapsed.png` });
  });
}

test('full view keeps focus inside after collapsing default sidebar', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&surface=embedded&capture=viewer&width=1230&theme=light');
  const viewer = page.getByTestId('rich-deliverable');
  await expect(viewer.locator('aside.rich-toc')).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: '/tmp/rich-parity-embedded.png' });
  await viewer.getByRole('button', { name: 'Open full view' }).click();
  const full = page.getByTestId('rich-deliverable-full-view');
  await page.evaluate(() => { document.documentElement.dataset.resolvedTheme = 'dark'; });
  await expect(full).toHaveAttribute('data-viewer-theme', 'dark');
  await expect(full.locator('.theme-control')).toHaveCount(0);
  await expect(full.locator('aside.rich-toc')).toBeVisible();
  await page.screenshot({ path: '/tmp/rich-parity-full.png' });
  await full.getByRole('button', { name: 'Close table of contents' }).click();
  await expect(full.getByRole('button', { name: 'Open table of contents' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(viewer.getByRole('button', { name: 'Open full view' })).toBeFocused();
});

test('390px drawer, local overflow, clipboard failure and live system theme', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&surface=standalone&capture=viewer');
  const viewer = page.getByTestId('rich-deliverable');
  await selectViewerTheme(viewer, 'system');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'dark');
  await page.evaluate(() => document.fonts.ready);
  expect(await viewer.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  const trigger = viewer.getByRole('button', { name: 'Open table of contents' });
  await trigger.click();
  const drawer = page.getByRole('dialog', { name: 'Table of contents' });
  await expect(drawer).toBeVisible();
  await page.screenshot({ path: '/tmp/rich-parity-drawer-390.png' });
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) }, configurable: true }));
  await viewer.getByRole('button', { name: 'Copy document' }).click();
  await expect(viewer.getByRole('alert')).toContainText('Could not copy');
});

test('200 percent reflow and reading progress', async ({ page }) => {
  await page.setViewportSize({ width: 615, height: 566 });
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&surface=standalone&capture=viewer');
  const viewer = page.getByTestId('rich-deliverable');
  expect(await viewer.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await viewer.locator('[data-rich-block-type="section"]').last().scrollIntoViewIfNeeded();
  await expect.poll(async () => Number(await viewer.getByRole('progressbar', { name: 'Reading progress' }).getAttribute('aria-valuenow'))).toBeGreaterThan(0);
});
