import { expect, test } from '@playwright/test';
import { installLibraryFixture, installLibraryRoutes, libraryCatalog } from './library-fixture';

test('task markers are checkbox-only in chat and fullscreen without hiding ordinary nested bullets', async ({ page }) => {
  await page.goto('/chat-deliverable-fixture?tasks');
  async function check(selector: string) {
    const result = await page.locator(selector).evaluateAll(roots => roots.flatMap(root =>
      Array.from(root.querySelectorAll('li')).map(li => ({
        task: !!li.querySelector(':scope > input[type=checkbox], :scope > p:first-child > input[type=checkbox]'),
        marker: getComputedStyle(li, '::marker').content,
        style: getComputedStyle(li).listStyleType,
        display: getComputedStyle(li).display,
      }))
    ));
    expect(result.some(item => item.task)).toBe(true);
    expect(result.some(item => !item.task)).toBe(true);
    for (const item of result) {
      expect(item.display).toBe('list-item');
      if (item.task) expect(item.marker).toBe('""');
      else {
        expect(item.marker).not.toBe('""');
        expect(item.style).not.toBe('none');
      }
    }
  }
  await expect(page.locator('.chat-markdown input[type=checkbox]').first()).toBeVisible();
  await check('.chat-markdown');
  await expect(page.getByTestId('rich-deliverable-inline-document').locator('input[type=checkbox]').first()).toBeVisible();
  await check('[data-testid="rich-deliverable-inline-document"] .rich-markdown');
  await page.getByRole('button', { name: 'Open fullscreen' }).click();
  await expect(page.getByTestId('rich-deliverable-full-view')).toBeVisible();
  await check('.rich-full .rich-markdown');
});

test('fullscreen favorite keeps its star and supports Enter, Space, and click', async ({ page }) => {
  await installLibraryFixture(page);
  await installLibraryRoutes(page, [{ ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review' }]);
  await page.goto('/chat-deliverable-fixture?tasks&favorites');
  await page.getByRole('button', { name: 'Open fullscreen' }).click();
  const full = page.getByTestId('rich-deliverable-full-view');
  const favorite = full.getByRole('button', { name: 'Add to favorites' });
  await expect(favorite).toBeEnabled();
  await expect(favorite).toHaveText('');
  await expect(favorite.locator('svg')).toBeVisible();
  await expect(favorite).toHaveAttribute('title', 'Add to favorites');
  await favorite.focus();
  await page.keyboard.press('Enter');
  const starred = full.getByRole('button', { name: 'Remove from favorites' });
  await expect(starred).toHaveAttribute('aria-pressed', 'true');
  await expect(starred.locator('svg')).toHaveAttribute('fill', 'currentColor');
  await expect(starred).toBeEnabled();
  await starred.focus();
  await page.keyboard.press('Space');
  await expect(favorite).toHaveAttribute('aria-pressed', 'false');
  await expect(favorite.locator('svg')).toHaveAttribute('fill', 'none');
  await favorite.click();
  await expect(starred).toHaveAttribute('aria-pressed', 'true');
});
