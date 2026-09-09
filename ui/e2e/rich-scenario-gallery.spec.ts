import { expect, test } from '@playwright/test';

test('loads, updates, and restores scenarios by stable ID', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=incident-report&theme=dark&width=768&surface=embedded');
  const fixture = page.getByTestId('rich-deliverable-fixture');
  await expect(fixture).toHaveAttribute('data-scenario', 'incident-report');
  await expect(fixture).toHaveAttribute('data-theme', 'dark');
  await expect(fixture).toHaveAttribute('data-width', '768');

  await page.locator('[data-scenario-id="product-comparison"]').click();
  await expect(fixture).toHaveAttribute('data-scenario', 'product-comparison');
  await expect(page).toHaveURL(/scenario=product-comparison/);

  await page.goBack();
  await expect(fixture).toHaveAttribute('data-scenario', 'incident-report');
});

test('falls back to the default for an unknown scenario ID', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=missing-scenario');
  await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute(
    'data-scenario',
    'research-answer',
  );
});

test('restores the document theme after leaving the fixture', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?theme=dark');
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.resolvedTheme)).toBe('dark');
  await page.goto('/rich-deliverable-block-fixture?block=hero');
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.resolvedTheme)).toBeUndefined();
});

test('loads every gallery scenario without unsupported blocks', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture');
  const tabs = page.locator('[data-scenario-id]');
  const ids = await tabs.evaluateAll((elements) => elements.map((element) => element.getAttribute('data-scenario-id')));
  for (const id of ids) {
    await page.goto(`/rich-deliverable-fixture?scenario=${encodeURIComponent(id ?? '')}`);
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', id ?? '');
    await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);
  }
});
