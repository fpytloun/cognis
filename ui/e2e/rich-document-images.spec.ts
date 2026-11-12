import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { expect, test } from '@playwright/test';

const build = resolve('standalone-build');
const entry = JSON.parse(readFileSync(resolve(build, '.vite/manifest.json'), 'utf8'))['src/standalone.ts'];
const fixture = {
  title: 'Station operations via SCC and AWS SSM — Design proposal',
  content: '',
  instanceId: '',
  payload: { blocks: [
    { type: 'markdown', content: 'Draft design proposal.\n\n' + Array.from({ length: 11 }, (_, i) =>
      `## ${i + 1}. Section\n\n${'Design details and acceptance evidence. '.repeat(15)}`
    ).join('\n\n') },
    { type: 'figure', src: '/authorized-diagram.svg', alt: 'Station architecture', title: 'Architecture' },
  ] },
};

for (const width of [390, 1440]) {
  test(`document navigation and image viewer at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/authorized-diagram.svg', route => route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="900"><rect width="1800" height="900" fill="white"/><text x="40" y="60">Station architecture</text></svg>',
    }));
    await page.route('**/standalone-assets/assets/*', route => {
      const file = basename(new URL(route.request().url()).pathname);
      return route.fulfill({ body: readFileSync(resolve(build, 'assets', file)), contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.woff2') ? 'font/woff2' : 'text/javascript' });
    });
    await page.route('**/document-images', route => route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${entry.css.map((file: string) => `<link rel="stylesheet" href="/standalone-assets/${file}">`).join('')}</head><body><div id="cognis-deliverable-root"></div><template id="cognis-deliverable-payload">${JSON.stringify(fixture)}</template><script type="module" src="/standalone-assets/${entry.file}"></script></body></html>`,
    }));
    await page.goto('/document-images');
    await expect(page.locator('.rich-toolbar h1')).toHaveCount(0);
    await expect(page.locator('.rich-body > .document-title')).toHaveText(fixture.title);
    expect(await page.locator('.document-title').evaluate(node => parseFloat(getComputedStyle(node).fontSize))).toBeLessThanOrEqual(44);
    const chrome = page.getByTestId('rich-viewer-chrome');
    const theme = chrome.locator('.theme-control');
    await expect(theme).toHaveCount(1);
    for (const mode of ['light', 'dark', 'system']) {
      await theme.click();
      await expect(theme).toHaveAttribute('data-theme-mode', mode);
      await expect(theme.locator('svg')).toHaveAttribute('data-viewer-icon', mode);
      expect(await page.evaluate(() => localStorage.getItem('cognis-rich-viewer-theme'))).toBe(mode);
    }
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(page.getByTestId('rich-deliverable')).toHaveAttribute('data-viewer-theme', 'dark');
    if (width === 1440) {
      await page.locator('aside.rich-toc').getByRole('button', { name: 'Close table of contents' }).click();
    }
    const openToc = page.getByRole('button', { name: 'Open table of contents' });
    const tocBox = await openToc.boundingBox();
    const titleBox = await chrome.locator('.viewer-title').boundingBox();
    const themeBox = await theme.boundingBox();
    expect(tocBox!.width).toBe(themeBox!.width);
    expect(tocBox!.height).toBe(themeBox!.height);
    if (width === 1440) {
      expect(tocBox!.x).toBeLessThan(titleBox!.x);
      await expect(openToc.locator('svg')).toHaveAttribute('data-viewer-icon', 'expand');
      await page.setViewportSize({ width: 390, height: 900 });
      await expect(openToc.locator('svg')).toHaveAttribute('data-viewer-icon', 'contents');
      await openToc.click();
      await expect(page.getByRole('dialog', { name: 'Table of contents' })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(openToc).toBeFocused();
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(openToc.locator('svg')).toHaveAttribute('data-viewer-icon', 'expand');
    } else {
      expect(tocBox!.x).toBeGreaterThan(titleBox!.x);
      await expect(openToc.locator('svg')).toHaveAttribute('data-viewer-icon', 'contents');
      await openToc.click();
      await expect(page.getByRole('dialog', { name: 'Table of contents' })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(openToc).toBeFocused();
    }
    if (await openToc.isVisible()) await openToc.click();
    await page.getByRole('button', { name: '11. Section', exact: true }).click();
    const section = page.getByRole('heading', { name: '11. Section', exact: true });
    await expect(section).toBeFocused();
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
    const trigger = page.getByRole('button', { name: 'Enlarge image: Station architecture' });
    await trigger.focus();
    await page.keyboard.press('Enter');
    const dialog = page.getByRole('dialog', { name: 'Architecture' });
    await expect(dialog).toBeVisible();
    const originalSrc = await dialog.locator('img').getAttribute('src');
    await page.emulateMedia({ colorScheme: 'light' });
    await expect(dialog).toHaveAttribute('data-viewer-theme', 'light');
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(dialog).toHaveAttribute('data-viewer-theme', 'dark');
    await expect(dialog.locator('img')).toHaveAttribute('src', originalSrc!);
    expect(await dialog.locator('img').evaluate(node => getComputedStyle(node).filter)).toBe('none');
    await expect(dialog.locator('header')).toHaveCount(0);
    await expect(dialog.locator('.image-controls button')).toHaveCount(2);
    expect(await dialog.evaluate(node => getComputedStyle(node).borderTopWidth)).toBe('0px');
    await expect(dialog.locator('img')).toHaveAttribute('src', await trigger.locator('img').getAttribute('src') as string);
    const fittedImage = await dialog.locator('img').boundingBox();
    expect(fittedImage!.width).toBeLessThan(width);
    await dialog.getByRole('button', { name: 'Zoom in' }).click();
    await expect(dialog.locator('img')).toHaveClass(/zoomed/);
    await expect(dialog.getByRole('button', { name: 'Zoom out' })).toHaveAttribute('aria-pressed', 'true');
    expect((await dialog.locator('img').boundingBox())!.width).toBe(Math.max(1800, fittedImage!.width * 2));
    await dialog.getByRole('button', { name: 'Zoom out' }).click();
    await expect(dialog.locator('img')).not.toHaveClass(/zoomed/);
    await dialog.getByRole('button', { name: 'Zoom image', exact: true }).click();
    await expect(dialog.locator('img')).toHaveClass(/zoomed/);
    await dialog.getByRole('button', { name: 'Zoom out' }).click();
    for (let i = 0; i < 5; i++) await page.keyboard.press('Tab');
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await page.getByRole('button', { name: 'Close image preview' }).click();
    await expect(trigger).toBeFocused();
    await trigger.click();
    await dialog.click({ position: { x: 4, y: 100 } });
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  });
}

for (const presentation of ['default', 'pulse']) {
  for (const width of [390, 1440]) {
    test.describe(`${presentation} ${width}px`, () => {
      test.use({ isMobile: width === 390, hasTouch: width === 390 });
    test(`inline images retain intrinsic size in ${presentation} at ${width}px`, async ({ page, browserName }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      page.on('pageerror', error => errors.push(error.message));
      const images = [
        { name: 'Small', width: 200, height: 100 },
        { name: 'Large', width: 1800, height: 900 },
        { name: 'Portrait', width: 200, height: 400 },
      ];
      const blocks = images.flatMap(image => [
        { type: 'figure', src: `/inline-${image.name}.svg`, alt: `Figure ${image.name}`, caption: `Figure ${image.name}` },
        { type: 'gallery', items: [{ src: `/inline-${image.name}.svg`, alt: `Gallery ${image.name}`, caption: `Gallery ${image.name}` }] },
        { type: 'card', title: `Card ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Card ${image.name}` } },
        { type: 'card', title: `Linked ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Linked ${image.name}`, link: 'https://cognis.test/image' } },
        { type: 'card', title: `Leading ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Leading ${image.name}`, placement: 'leading' } },
        { type: 'card', title: `Background ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Background ${image.name}`, placement: 'background' } },
        { type: 'card', title: `Background linked ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Background linked ${image.name}`, placement: 'background', link: 'https://cognis.test/image' } },
        { type: 'card', variant: 'visual', title: `Visual ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Visual ${image.name}` } },
        { type: 'hero', title: `Hero ${image.name}`, media: { src: `/inline-${image.name}.svg`, alt: `Hero ${image.name}` } },
      ]);
      const sizedCards = [
        { name: 'Oversized', width: 1000, height: 500, expectedWidth: 200 },
        { name: 'Width cap', width: 120, expectedWidth: 120 },
        { name: 'Height cap', height: 40, expectedWidth: 80 },
        { name: 'Conflicting caps', width: 120, height: 40, expectedWidth: 80 },
      ];
      const payload = {
        title: 'Synthetic image sizing regression',
        content: '',
        instanceId: '',
        payload: {
          metadata: { presentation, publication: false, show_toc: false },
          blocks: [
            ...blocks,
            ...sizedCards.map(({ name, expectedWidth, ...dimensions }) => ({
              type: 'card', title: name, media: { src: '/inline-Small.svg', alt: name, ...dimensions },
            })),
          ],
        },
      };
      await page.route('**/inline-*.svg', route => {
        const image = images.find(image => route.request().url().endsWith(`/inline-${image.name}.svg`))!;
        return route.fulfill({
          contentType: 'image/svg+xml',
          body: `<svg xmlns="http://www.w3.org/2000/svg" width="${image.width}" height="${image.height}"><rect width="100%" height="100%" fill="#e0f2fe"/><rect x="1" y="1" width="${image.width - 2}" height="${image.height - 2}" fill="none" stroke="#0369a1"/><text x="12" y="30" fill="#0c4a6e">${image.name}: ${image.width} × ${image.height}</text></svg>`,
        });
      });
      await page.route('**/standalone-assets/assets/*', route => {
        const file = basename(new URL(route.request().url()).pathname);
        return route.fulfill({ body: readFileSync(resolve(build, 'assets', file)), contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.woff2') ? 'font/woff2' : 'text/javascript' });
      });
      await page.route('**/inline-images', route => route.fulfill({
        contentType: 'text/html; charset=utf-8',
        body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${entry.css.map((file: string) => `<link rel="stylesheet" href="/standalone-assets/${file}">`).join('')}<style>#cognis-deliverable-root { max-width: 1000px; margin: auto; }</style></head><body><div id="cognis-deliverable-root"></div><template id="cognis-deliverable-payload">${JSON.stringify(payload)}</template><script type="module" src="/standalone-assets/${entry.file}"></script></body></html>`,
      }));
      await page.goto('/inline-images');
      await expect(page.getByTestId('rich-deliverable')).toHaveAttribute('data-presentation', presentation);
      for (const image of images) {
        for (const surface of ['Figure', 'Gallery', 'Card', 'Linked', 'Leading', 'Background', 'Background linked', 'Visual', 'Hero']) {
          const element = page.getByAltText(`${surface} ${image.name}`, { exact: true });
          await element.scrollIntoViewIfNeeded();
          await expect.poll(() => element.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth)).toBe(image.width);
          const geometry = await element.evaluate((node: HTMLImageElement) => {
            const box = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return {
              width: box.width - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth),
              height: box.height - parseFloat(style.borderTopWidth) - parseFloat(style.borderBottomWidth),
            };
          });
          expect(geometry.width).toBeGreaterThan(0);
          expect(geometry.width).toBeLessThanOrEqual(image.width);
          expect(geometry.height).toBeLessThanOrEqual(image.height);
          expect(geometry.width / geometry.height).toBeCloseTo(image.width / image.height, 2);
          if (image.name === 'Large') expect(geometry.width).toBeLessThan(image.width);
          if (['Background', 'Background linked', 'Visual', 'Hero'].includes(surface)) {
            const containment = await element.evaluate(node => {
              const image = node.getBoundingClientRect();
              const frame = node.closest('figure')!;
              const owner = frame.closest('.rich-card, .rich-hero')!;
              return [frame, owner].filter(container => container === frame
                || ['hidden', 'clip'].includes(getComputedStyle(container).overflow)).flatMap(container => {
                const bounds = container.getBoundingClientRect();
                return image.left >= bounds.left - 1 && image.top >= bounds.top - 1
                  && image.right <= bounds.right + 1 && image.bottom <= bounds.bottom + 1
                  ? [] : [{ image: image.toJSON(), bounds: bounds.toJSON(), container: container.className }];
              });
            });
            expect(containment, `${surface} ${image.name}`).toEqual([]);
          }
          if (surface === 'Background linked') {
            const link = page.getByRole('link', { name: `${surface} ${image.name}`, exact: true });
            await expect(link).toHaveAttribute('href', 'https://cognis.test/image');
            await link.click({ trial: true });
          }
        }
      }
      for (const card of sizedCards) {
        const image = page.getByAltText(card.name, { exact: true });
        await image.scrollIntoViewIfNeeded();
        const box = await image.boundingBox();
        expect(box!.width).toBeCloseTo(card.expectedWidth, 0);
        expect(box!.height).toBeCloseTo(card.expectedWidth / 2, 0);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      for (const image of images.slice(0, 2)) {
        const trigger = page.getByRole('button', { name: `Enlarge image: Figure ${image.name}`, exact: true });
        await trigger.focus();
        await page.keyboard.press('Enter');
        const dialog = page.getByRole('dialog', { name: `Figure ${image.name}`, exact: true });
        await expect(dialog).toBeVisible();
        const fittedWidth = (await dialog.locator('img').boundingBox())!.width;
        await dialog.getByRole('button', { name: 'Zoom in' }).click();
        expect((await dialog.locator('img').boundingBox())!.width).toBe(Math.max(image.width, fittedWidth * 2));
        await page.keyboard.press('Escape');
        await expect(trigger).toBeFocused();
      }
      await page.getByAltText('Figure Portrait', { exact: true }).scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      if ((browserName === 'chromium' && width === 1440) || (browserName === 'webkit' && width === 390)) {
        await page.screenshot({ path: testInfo.outputPath(`synthetic-${presentation}-${width}.png`) });
      }
      expect(errors).toEqual([]);
    });
    });
  }
}
