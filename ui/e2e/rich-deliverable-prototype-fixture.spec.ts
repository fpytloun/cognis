import { expect, test } from '@playwright/test';

for (const colorScheme of ['light', 'dark'] as const) {
  test(`renders the production-shaped rich deliverable prototype in ${colorScheme} mode`, async ({ page }) => {
    await page.emulateMedia({ colorScheme });
    await page.goto('/rich-deliverable-prototype-fixture');

    const fixture = page.getByTestId('rich-deliverable-prototype-fixture');
    await expect(fixture).toBeVisible();
    await expect(fixture.locator('h1')).toHaveCount(1);
    await expect(fixture.locator('.rich-actions button')).toHaveCount(2);
    await expect(fixture.locator('.rich-card-visual.has-media')).toBeVisible();
    await expect(fixture.locator('.rich-card-visual img')).toHaveJSProperty('complete', true);
    await expect(fixture.locator('.rich-dashboard-card')).toHaveCount(4);
    await expect(fixture.locator('[data-rich-block-type="day_agenda"]')).toBeVisible();
    await expect(fixture.locator('.rich-card-action')).toHaveCount(1);
    await expect(fixture.locator('[data-rich-block-type="accordion"] details')).toHaveCount(3);
    await expect(fixture.locator('.rich-chart-card')).toBeVisible();
    await expect(fixture.locator('[data-rich-block-type="source_list"]')).toBeVisible();
    await expect(fixture.locator('.rich-block-list').first().evaluate((element) =>
      getComputedStyle(element).getPropertyValue('--rich-text').trim()
    )).resolves.toBe(colorScheme === 'light' ? '#172033' : '#f8fafc');

    const disclosure = fixture.locator('[data-rich-block-type="accordion"] details').first();
    await disclosure.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(disclosure).toHaveAttribute('open', '');
  });
}

test('keeps the prototype single-column at the 390px review width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/rich-deliverable-prototype-fixture');

  const columns = page.locator('.rich-deliverable.pulse .rich-columns');
  await expect(columns).toBeVisible();
  await expect(columns.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(' ').length)).resolves.toBe(1);
  await expect(page.locator('.rich-chart-canvas')).toBeVisible();
  await expect(page.locator('[data-testid="rich-deliverable-prototype-fixture"]').evaluate((element) =>
    element.scrollWidth <= document.documentElement.clientWidth
  )).resolves.toBe(true);

  const visualCard = page.locator('.rich-card-visual');
  await expect(visualCard).toHaveClass(/has-media/);
  await expect(visualCard.locator('img')).toHaveJSProperty('complete', true);
  await expect(visualCard.evaluate((element) => element.scrollWidth <= document.documentElement.clientWidth)).resolves.toBe(true);
  await expect(visualCard.getByText('Nejcennější okno končí v 09:00')).toBeVisible();
});

test('uses the resolved standalone theme instead of the OS preference', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/rich-deliverable-prototype-fixture');
  await page.evaluate(() => {
    document.documentElement.dataset.resolvedTheme = 'dark';
  });

  await expect(page.locator('.rich-block-list').first().evaluate((element) =>
    getComputedStyle(element).getPropertyValue('--rich-text').trim()
  )).resolves.toBe('#f8fafc');
});
