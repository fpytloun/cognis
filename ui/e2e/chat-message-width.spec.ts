import { expect, test, type Page } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';

// Real ChatMessage/notice rendering inside the scoped Chat v2 timeline.
async function open(page: Page, family: string, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/scoped.html?layout-cases`);
  const shell = page.getByTestId('scoped-timeline-shell');
  const assistant = shell.locator('article.chat-message-assistant').filter({ hasText: 'reading measure' });
  await expect(assistant).toBeVisible();
  // The fixture has no account; project the family once the app has settled.
  await page.evaluate((value) => { document.documentElement.dataset.themeFamily = value; }, family);
  await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
  return { shell, assistant };
}

const measure = (element: Element) => {
  const probe = document.createElement('span');
  probe.textContent = '0';
  probe.style.cssText = 'position:absolute;visibility:hidden';
  element.appendChild(probe);
  const ch = probe.getBoundingClientRect().width;
  probe.remove();
  return ch;
};

for (const family of ['classic', 'porcelain', 'gruvbox', 'paper']) {
  test(`${family}: assistant prose uses the 70ch measure`, async ({ page }) => {
    const { assistant } = await open(page, family, 1600);
    const paragraph = assistant.locator('.chat-markdown > p').first();
    const width = await paragraph.evaluate((node) => node.getBoundingClientRect().width);
    // The measure is 70ch of the message font size and UI face.
    const ch = await assistant.evaluate(measure);
    // At most the measure; at least close to it (bubbles subtract padding).
    expect(width / ch).toBeGreaterThan(64);
    expect(width / ch).toBeLessThanOrEqual(70.5);
  });
}

test('paper: tables use the full column while prose stays bounded', async ({ page }) => {
  const { assistant } = await open(page, 'paper', 1600);
  const prose = await assistant.locator('.chat-markdown > p').first().boundingBox();
  const table = await assistant.locator('.markdown-table-wrap').boundingBox();
  const article = await assistant.boundingBox();
  expect(table!.width).toBeGreaterThan(prose!.width + 100);
  expect(Math.abs(table!.width - article!.width)).toBeLessThan(40);
});

test('bubble themes: tables stay inside the measured bubble', async ({ page }) => {
  const { assistant } = await open(page, 'classic', 1600);
  const table = await assistant.locator('.markdown-table-wrap').boundingBox();
  const article = await assistant.boundingBox();
  expect(table!.width).toBeLessThanOrEqual(article!.width);
});

for (const width of [1440, 390]) {
  test(`long unbreakable notice does not widen the chat at ${width}px`, async ({ page }) => {
    const { shell } = await open(page, 'classic', width);
    const notice = shell.locator('article').filter({ hasText: 'ERR_PERMISSION_DENIED' }).first();
    await notice.scrollIntoViewIfNeeded();
    await expect(notice).toBeVisible();
    const overflow = await shell.evaluate((root) => {
      const wide = [...root.querySelectorAll<HTMLElement>('*')]
        .filter((node) => node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== 'visible'
          && !node.closest('.markdown-table-wrap, pre'))
        .map((node) => `${node.tagName}.${node.className}`.slice(0, 120));
      return { wide, page: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    expect(overflow.wide).toEqual([]);
    expect(overflow.page).toBeLessThanOrEqual(0);
    const box = await notice.boundingBox();
    const shellBox = await shell.boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(shellBox!.x + shellBox!.width + 1);
  });
}
