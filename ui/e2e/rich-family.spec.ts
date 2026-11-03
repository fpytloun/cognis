import { expect, test } from '@playwright/test';

for (const family of ['porcelain', 'classic', 'gruvbox', 'paper']) {
  for (const mode of ['light', 'dark'] as const) {
    test(`${family} ${mode} uses production scoped roles with opposite app mode`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: mode });
      await page.addInitScript(mode => localStorage.setItem('cognis-rich-viewer-theme', mode), mode);
      await page.goto('/rich-deliverable-fixture?scenario=design-document-legacy&surface=embedded&capture=viewer');
      const viewer = page.getByTestId('rich-deliverable');
      await expect(viewer).toHaveAttribute('data-viewer-theme', mode);
      await page.evaluate(({ family, mode }) => {
        document.documentElement.dataset.themeFamily = family;
        document.documentElement.dataset.resolvedTheme = mode === 'light' ? 'dark' : 'light';
      }, { family, mode });
      await expect(viewer).toHaveAttribute('data-theme-family', family);
      await expect(viewer).toHaveAttribute('data-resolved-theme', mode === 'light' ? 'dark' : 'light');
      const code = viewer.locator('.rich-markdown pre code').first();
      await code.scrollIntoViewIfNeeded();
      await expect(code.locator('[class*="hljs-"]').first()).toBeAttached({ timeout: 30_000 });
      const result = await viewer.evaluate(node => {
        // Compare against an independently mounted instance of the palette hook.
        const reference = document.createElement('div');
        reference.className = 'theme-palette';
        reference.dataset.themeFamily = node.getAttribute('data-theme-family')!;
        reference.dataset.resolvedTheme = node.getAttribute('data-resolved-theme')!;
        document.body.append(reference);
        const actual = getComputedStyle(node);
        const expected = getComputedStyle(reference);
        const surface = expected.getPropertyValue('--color-surface').trim();
        const luminance = (value: string) => {
          const channels = value.match(/[\d.]+/g)!.slice(0, 3).map(Number);
          return channels.map(v => {
            const c = v / 255;
            return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
          }).reduce((sum, c, i) => sum + c * [.2126, .7152, .0722][i], 0);
        };
        const contrast = (a: string, b: string) => {
          const x = luminance(a), y = luminance(b);
          return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
        };
        const code = node.querySelector('.rich-markdown pre code')!;
        const background = getComputedStyle(code.parentElement!).backgroundColor;
        const contrasts = [code, ...code.querySelectorAll('[class*="hljs-"]')]
          .map(el => ({ selector: el.className, color: getComputedStyle(el).color,
            background, ratio: contrast(getComputedStyle(el).color, background) }));
        const data = {
          surface: actual.getPropertyValue('--rich-surface').trim(),
          expected: `rgb(${surface})`,
          bodyContrast: contrast(actual.getPropertyValue('--rich-text'), `rgb(${surface})`),
          contrasts,
        };
        reference.remove();
        return data;
      });
      expect(result.surface).toBe(result.expected);
      expect(result.bodyContrast).toBeGreaterThanOrEqual(4.5);
      for (const contrast of result.contrasts) expect(contrast.ratio, JSON.stringify(contrast)).toBeGreaterThanOrEqual(4.5);
    });
  }
}

test('embedded projects family into opposite-mode reader and portals; standalone stays independent', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.addInitScript(() => localStorage.setItem('cognis-rich-viewer-theme', 'light'));
  await page.goto('/rich-deliverable-fixture?scenario=every-block-reference&surface=embedded&capture=viewer');
  const viewer = page.getByTestId('rich-deliverable');
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light', { timeout: 30_000 });
  // Fixture-driven root changes exercise the observer boundary, not account persistence.
  await page.evaluate(() => {
    document.documentElement.dataset.resolvedTheme = 'dark';
    document.documentElement.dataset.themeFamily = 'gruvbox';
  });
  await expect(viewer).toHaveAttribute('data-theme-family', 'gruvbox');
  await expect(viewer).toHaveAttribute('data-resolved-theme', 'dark');
  const surface = () => viewer.evaluate(node => getComputedStyle(node).getPropertyValue('--rich-surface').trim());
  const gruvboxSurface = await surface();
  expect(gruvboxSurface).not.toBe('#f6f8fb');
  const diagram = viewer.locator('.rich-mermaid svg').first();
  await expect(diagram).toBeAttached();
  const before = await diagram.evaluate(node => node.outerHTML);
  const chart = viewer.locator('[data-chart-ready="true"] canvas').first();
  await expect(chart).toBeAttached();
  const pixels = await chart.evaluate((node: HTMLCanvasElement) => node.toDataURL());
  await page.evaluate(() => document.documentElement.dataset.themeFamily = 'paper');
  await expect.poll(surface).not.toBe(gruvboxSurface);
  await expect.poll(() => diagram.evaluate(node => node.outerHTML)).not.toBe(before);
  await expect.poll(() => chart.evaluate((node: HTMLCanvasElement) => node.toDataURL())).not.toBe(pixels);
  const stableDiagram = await diagram.elementHandle();
  await page.waitForTimeout(500);
  expect(await stableDiagram!.evaluate(node => node.isConnected)).toBe(true);
  await viewer.getByRole('button', { name: 'Open full view' }).click();
  const full = page.getByTestId('rich-deliverable-full-view');
  await expect(full).toHaveAttribute('data-theme-family', 'paper');
  await expect(full).toHaveAttribute('data-resolved-theme', 'dark');
  expect(await full.evaluate(node => getComputedStyle(node).getPropertyValue('--rich-surface').trim())).toBe(await surface());
  await page.setViewportSize({ width: 390, height: 844 });
  await full.getByRole('button', { name: 'Open table of contents' }).click();
  await expect(page.locator('.rich-toc-drawer-root')).toHaveAttribute('data-theme-family', 'paper');
  expect(await page.locator('.rich-toc-drawer-root').evaluate(node => getComputedStyle(node).getPropertyValue('--rich-surface').trim())).toBe(await surface());
  await page.keyboard.press('Escape');
  await full.locator('button[aria-label^="Enlarge image:"]').first().click();
  const image = page.locator('dialog.image-viewer');
  await expect(image).toHaveAttribute('data-theme-family', 'paper');
  await expect(image).toHaveAttribute('data-resolved-theme', 'dark');
  expect(await image.evaluate(node => getComputedStyle(node).getPropertyValue('--color-foreground').trim()))
    .toBe(await full.evaluate(node => getComputedStyle(node).getPropertyValue('--color-foreground').trim()));
  expect(await image.locator('img').evaluate(node => getComputedStyle(node).filter)).toBe('none');
  await page.keyboard.press('Escape');
  await page.goto('/rich-deliverable-fixture?scenario=every-block-reference&surface=standalone&capture=viewer');
  await expect(viewer).not.toHaveClass(/theme-palette/);
  await expect(viewer).not.toHaveAttribute('data-theme-family');
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
});
