import { expect, test, type Locator, type Page } from '@playwright/test';

const candidate = async (page: Page) => JSON.parse(await page.getByLabel('Exact candidate').textContent() ?? 'null');
async function drag(page: Page, from: Locator, to: Locator) {
  const start = await from.boundingBox(), end = await to.boundingBox();
  if (!start || !end) throw new Error('Range endpoints are not visible');
  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
  await page.mouse.down();
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 8 });
  await page.mouse.up();
}
test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/fixtures/table-range/index.html');
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('button', { name: 'Select column 1' })).toBeVisible();
});
test('direct row, column and cell handles expose only the exact extent before any comment action', async ({ page }) => {
  const cells = page.locator('tbody td');
  await cells.nth(0).hover();
  await drag(page, page.getByRole('button', { name: 'Select rows from authored row 1' }), cells.nth(2));
  await expect.poll(() => candidate(page)).toMatchObject({ table_range: { scope: 'rows', rows: [0, 1], column_start: 0, column_end: 2 } });
  await expect(page.locator('[data-feedback-table-selected]')).toHaveCount(4);
  await expect(page.getByLabel('Selected rect count')).toHaveText('4');
  await drag(page, page.getByRole('button', { name: 'Select column 2' }), page.locator('thead th').nth(0));
  await expect.poll(() => candidate(page)).toMatchObject({ table_range: { scope: 'columns', rows: [0, 1, 2], column_start: 0, column_end: 2 } });
  await page.getByRole('button', { name: 'Clear selection', exact: true }).click();
  await cells.nth(5).hover();
  await drag(page, page.getByRole('button', { name: 'Select cells from authored row 3, column 2' }), cells.nth(0));
  await expect.poll(() => candidate(page)).toMatchObject({ table_range: { scope: 'cells', rows: [0, 1, 2], column_start: 0, column_end: 2 } });
  await expect(page.getByLabel('Selected rect count')).toHaveText('6');
});
test('sorted row neighbors retain authored membership and keyboard Shift extends precisely', async ({ page }) => {
  await page.getByRole('button', { name: 'Sort by Name' }).click();
  await page.locator('tbody td').first().hover();
  const handle = page.getByRole('button', { name: 'Select rows from authored row 2' });
  await handle.focus(); await handle.press('Enter'); await handle.press('Shift+ArrowDown');
  await expect.poll(() => candidate(page)).toMatchObject({ table_range: { rows: [1, 2] } });
  await expect(page.locator('[data-feedback-table-selected]')).toHaveCount(4);
  await page.keyboard.press('Escape');
  await expect(page.getByLabel('Exact candidate')).toHaveText('null');
});
test('native cell word/range selection and ordinary touch swipes never become structural rows', async ({ page }) => {
  const cell = page.locator('tbody td').first();
  const word = await cell.evaluate(node => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let text: Node | null;
    do { text = walker.nextNode(); } while (text && !text.textContent?.startsWith('Zulu'));
    if (!text) throw new Error('Source word is not rendered');
    const range = document.createRange();
    range.setStart(text, 0); range.setEnd(text, 4);
    const rect = range.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  await page.mouse.dblclick(word.x, word.y);
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toBe('Zulu');
  await expect.poll(async () => (await candidate(page))?.range?.exact).toBe('Zulu');
  expect((await candidate(page))?.table_range).toBeUndefined();
  await page.getByRole('button', { name: 'Clear selection', exact: true }).click();
  const bounds = await cell.boundingBox();
  if (!bounds) throw new Error('Cell is not visible');
  const downAllowed = await cell.evaluate((node, bounds) => node.dispatchEvent(new PointerEvent('pointerdown', {
    bubbles: true, cancelable: true, pointerId: 12, pointerType: 'touch', clientX: bounds.x + 25, clientY: bounds.y + 15,
  })), bounds);
  expect(downAllowed).toBe(true);
  await cell.dispatchEvent('pointermove', { pointerId: 12, pointerType: 'touch', clientY: bounds.y - 30 });
  await cell.dispatchEvent('pointerup', { pointerId: 12, pointerType: 'touch' });
  expect((await candidate(page))?.table_range).toBeUndefined();
  await page.mouse.wheel(0, 400);
  await expect.poll(() => page.getByRole('main').evaluate(node => node.scrollTop)).toBeGreaterThan(0);
});
