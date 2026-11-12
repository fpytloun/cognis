import { test, expect } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

const files = new Map<string, { body: string | Buffer; contentType: string }>();
test.beforeAll(async () => {
  const { build } = await import('vite');
  const result = await build({ configFile: 'vite.standalone.config.ts', mode: 'offline', logLevel: 'error',
    build: { write: false, manifest: false, minify: false, rollupOptions: { input: 'e2e/fixtures/annotation-grid.ts' } } });
  const outputs = Array.isArray(result) ? result.flatMap(value => value.output) : 'output' in result ? result.output : [];
  for (const output of outputs) files.set(`/${output.fileName}`, {
    body: output.type === 'chunk' ? output.code : typeof output.source === 'string' ? output.source : Buffer.from(output.source),
    contentType: output.fileName.endsWith('.css') ? 'text/css' : 'text/javascript',
  });
});

for (const width of [960, 1280]) for (const mode of ['unbound', 'view', 'comment']) {
  test(`unequal-content peers stretch at ${width}px with annotations ${mode}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1200 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.route('https://annotation-grid.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const asset = files.get(path);
      if (asset) return route.fulfill(asset);
      if (path !== '/') return route.abort();
      const styles = [...files.keys()].filter(file => file.endsWith('.css')).map(file => `<link rel="stylesheet" href="${file}">`).join('');
      const script = [...files.keys()].find(file => file.endsWith('.js'))!;
      return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head>${styles}</head><body><main id="fixture"></main><script src="${script}"></script></body></html>` });
    });
    await page.goto(`https://annotation-grid.test/?mode=${mode}`);
    const short = page.locator('[data-feedback-rich-block="short-card"]');
    const long = page.locator('[data-feedback-rich-block="long-card"]');
    await expect(short.locator(':scope > .rich-card')).toBeVisible();
    const measurements = await page.evaluate(() => {
      const read = (id: string) => {
        const anchor = document.querySelector<HTMLElement>(`[data-feedback-rich-block="${id}"]`)!;
        const card = anchor.querySelector<HTMLElement>(':scope > .rich-card')!;
        const body = card.querySelector<HTMLElement>(':scope > .rich-card-body')!;
        const content = body.querySelector<HTMLElement>(':scope > .rich-markdown')!;
        return { anchor: anchor.getBoundingClientRect().toJSON(), card: card.getBoundingClientRect().toJSON(),
          content: content.getBoundingClientRect().toJSON(), display: getComputedStyle(anchor).display,
          direction: getComputedStyle(anchor).flexDirection, cardGrow: getComputedStyle(card).flexGrow,
          body: body.getBoundingClientRect().toJSON(), id: anchor.id, span: anchor.style.gridColumn,
          directCard: anchor.firstElementChild === card,
          controls: Array.from(anchor.children).filter(child => child !== card).map(child => child.getBoundingClientRect().toJSON()) };
      };
      return { short: read('short-card'), long: read('long-card') };
    });
    const measurementsPath = info.outputPath('peer-measurements.json');
    await writeFile(measurementsPath, JSON.stringify(measurements, null, 2));
    await info.attach('peer-measurements', { path: measurementsPath, contentType: 'application/json' });
    expect(measurements.long.content.height - measurements.short.content.height).toBeGreaterThan(80);
    expect(Math.abs(measurements.short.anchor.top - measurements.long.anchor.top)).toBeLessThan(1);
    expect(Math.abs(measurements.short.anchor.height - measurements.long.anchor.height)).toBeLessThan(1);
    expect(Math.abs(measurements.short.card.height - measurements.long.card.height)).toBeLessThan(1);
    for (const peer of Object.values(measurements)) {
      expect(peer.directCard).toBe(true);
      expect(peer.display).toBe('flex');
      expect(peer.direction).toBe('column');
      expect(peer.cardGrow).toBe('1');
      expect(peer.card.bottom).toBeLessThanOrEqual(peer.anchor.bottom + 1);
      expect(peer.body.bottom).toBeLessThanOrEqual(peer.card.bottom + 1);
      expect(peer.content.bottom).toBeLessThanOrEqual(peer.card.bottom + 1);
      for (const controls of peer.controls) {
        expect(controls.top).toBeGreaterThanOrEqual(peer.card.bottom - 1);
        expect(controls.bottom).toBeLessThanOrEqual(peer.anchor.bottom + 1);
      }
      expect(peer.id).not.toBe('');
      expect(peer.span).toBe('span 1');
    }
    await page.screenshot({ path: info.outputPath('equal-peer-heights.png'), fullPage: true });
    if (mode !== 'unbound') {
      await page.getByTestId('locate-short').click();
      await expect(page.getByTestId('located-status')).toHaveText('exact');
      await expect(short).toBeFocused();
    }
    if (mode === 'comment') {
      await short.locator('summary').click();
      const contentField = await short.getByRole('option', { name: /^content(?: \(|$)/ }).getAttribute('value');
      expect(contentField).not.toBeNull();
      await short.getByLabel('Source field').selectOption(contentField!);
      await short.getByRole('button', { name: 'Load canonical text for selection' }).click();
      const input = short.getByRole('textbox', { name: 'Canonical source text' });
      await expect(input).toHaveValue('Short card body.');
      await input.evaluate((node: HTMLTextAreaElement) => {
        node.setSelectionRange(0, 5); node.dispatchEvent(new Event('select', { bubbles: true }));
      });
      await short.getByRole('button', { name: 'Comment on selected text', exact: true }).click();
      await expect(page.getByTestId('selected-field')).toContainText('"exact":"Short"');
      await expect(page.getByTestId('selected-field')).toContainText('"field_path":["content"]');
    }
  });
}
