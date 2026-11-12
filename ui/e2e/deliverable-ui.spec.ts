import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  page.on('pageerror', error => console.error('Fixture runtime error:', error));
  page.on('console', message => {
    if (message.type() === 'error') console.error('Fixture console error:', message.text());
  });
});

for (const width of [390, 1280]) {
  test(`recap keeps its header and footer fixed at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/__deliverable-fixture?recap');
    const recap = page.getByRole('region', { name: 'Conversation recap', exact: true });
    const content = page.getByRole('region', { name: 'Recap content', exact: true });
    await expect(content).toContainText('Section 20');
    const geometry = () => recap.evaluate(element => {
      const header = element.querySelector('header')!.getBoundingClientRect();
      const footer = element.querySelector('footer')!.getBoundingClientRect();
      const body = element.querySelector<HTMLElement>('[aria-label="Recap content"]')!;
      const bounds = body.getBoundingClientRect();
      return {
        header: [header.top, header.bottom], footer: [footer.top, footer.bottom],
        body: [bounds.top, bounds.bottom], scrollTop: body.scrollTop,
        scrollable: body.scrollHeight > body.clientHeight,
        height: element.getBoundingClientRect().height,
      };
    });
    const before = await geometry();
    expect(before.scrollable).toBe(true);
    expect(before.height).toBeLessThanOrEqual(300);
    await content.hover();
    await page.mouse.wheel(0, 800);
    await expect.poll(async () => (await geometry()).scrollTop).toBeGreaterThan(0);
    const after = await geometry();
    expect(after.header).toEqual(before.header);
    expect(after.footer).toEqual(before.footer);
    expect(after.body[0]).toBeGreaterThanOrEqual(after.header[1]);
    expect(after.body[1]).toBeLessThanOrEqual(after.footer[0]);
    await page.screenshot({ path: testInfo.outputPath(`4d8aba045-ui-working-tree-recap-${width}.png`) });
  });
}

test('reference controls open one standard fullscreen document and restore focus', async ({ page }, testInfo) => {
  await page.goto('/__deliverable-fixture');
  const reference = page.getByTestId('reference-actions');
  const opener = reference.getByRole('button', { name: 'Open fullscreen' });
  await expect(opener).toBeVisible();
  await expect(page.getByTestId('rich-deliverable-body')).toHaveCount(0);
  await opener.click();
  const full = page.getByTestId('rich-deliverable-full-view');
  await expect(full).toBeVisible();
  await expect(full.getByRole('heading', { name: 'Section 1', exact: true })).toBeVisible();
  await expect(page.getByText('Loading deliverable…', { exact: true })).toHaveCount(0);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.screenshot({ path: testInfo.outputPath('4d8aba045-ui-working-tree-document-fullscreen.png') });
  await page.keyboard.press('Escape');
  await expect(full).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('published palette keeps device appearance', async ({ page }, testInfo) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/__deliverable-fixture?standalone');
  const document = page.getByTestId('rich-deliverable');
  await expect(document).toHaveAttribute('data-theme-family', 'gruvbox');
  await expect(document).toHaveAttribute('data-viewer-theme', 'dark');
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(document).toHaveAttribute('data-viewer-theme', 'light');
  await page.screenshot({ path: testInfo.outputPath('4d8aba045-ui-working-tree-published-gruvbox-light.png') });
});

test('many recap documents remain accessible without collapsing the prose', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/__deliverable-fixture?many&recap');
  const content = page.getByRole('region', { name: 'Recap content' });
  await expect(content).toContainText('Section 20');
  expect((await content.boundingBox())!.height).toBeGreaterThan(100);
  const document = page.getByRole('button', { name: 'Delivery review 30' });
  expect((await document.boundingBox())!.width).toBeGreaterThan(100);
  await document.scrollIntoViewIfNeeded();
  await expect(document).toBeInViewport();
  const last = page.getByRole('button', { name: '1 artifact ↗' });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  expect((await content.boundingBox())!.height).toBeGreaterThan(100);
});

test('comment-enabled markdown honors the public palette instead of the app root', async ({ page }, testInfo) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/__deliverable-fixture?comment');
  const document = page.getByTestId('rich-deliverable');
  await expect(document.getByRole('heading', { name: 'Section 1', exact: true })).toBeVisible();
  await page.evaluate(() => {
    window.document.documentElement.dataset.themeFamily = 'classic';
    window.document.documentElement.dataset.resolvedTheme = 'light';
  });
  await expect(document).toHaveAttribute('data-theme-family', 'gruvbox');
  await expect(document).toHaveAttribute('data-viewer-theme', 'dark');
  const colors = await document.evaluate(element => ({
    document: getComputedStyle(element).getPropertyValue('--color-canvas').trim(),
    app: getComputedStyle(window.document.documentElement).getPropertyValue('--color-canvas').trim(),
  }));
  expect(colors.document).not.toBe(colors.app);
  await page.screenshot({ path: testInfo.outputPath('24c3ba193-public-theme-working-tree.png') });
});
