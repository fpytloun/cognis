import { expect, test } from '@playwright/test';

const scenarios = [
  {
    id: 'weekend-schedule',
    heading: 'Keep the weekend light enough to enjoy',
    mediaCount: 1,
  },
  {
    id: 'fridge-comparison',
    heading: 'Buy the quiet model that actually clears the cabinet',
    mediaCount: 3,
  },
] as const;

for (const scenario of scenarios) {
  for (const width of [390, 1440]) {
    test(`${scenario.id} renders without overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`/rich-deliverable-fixture?scenario=${scenario.id}&theme=light&width=${width}&surface=embedded`);

      const fixture = page.getByTestId('rich-deliverable-fixture');
      await expect(fixture).toHaveAttribute('data-scenario', scenario.id);
      await expect(fixture.getByRole('heading', { name: scenario.heading })).toBeVisible();
      await expect(fixture.locator('img')).toHaveCount(scenario.mediaCount);
      await expect(fixture.locator('img').evaluateAll((images) =>
        images.every((image) => image instanceof HTMLImageElement && image.complete && image.naturalWidth > 0),
      )).resolves.toBe(true);
      await expect(fixture.evaluate((element) => element.scrollWidth <= document.documentElement.clientWidth)).resolves.toBe(true);
    });
  }
}

test('fridge comparison preserves product names next to their illustrations in dark mode', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/rich-deliverable-fixture?scenario=fridge-comparison&theme=dark&width=1440&surface=embedded');

  const fixture = page.getByTestId('rich-deliverable-fixture');
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.resolvedTheme)).toBe('dark');
  const richText = await fixture.locator('.rich-block-list').first().evaluate((element) =>
    getComputedStyle(element).getPropertyValue('--rich-text').trim(),
  );
  expect(['rgb(248 250 252)', '#f8fafc']).toContain(richText);

  const nordhavenCard = fixture.getByRole('heading', { name: 'Nordhaven 430 Graphite' })
    .first()
    .locator('xpath=ancestor::article');
  await expect(nordhavenCard).toHaveCount(1);
  await expect(nordhavenCard.locator('img')).toHaveAttribute(
    'alt',
    'Illustration of the graphite Nordhaven 430 refrigerator with two doors and a B energy label',
  );

  const harborCard = fixture.getByRole('heading', { name: 'Harbor 390 Cream' })
    .first()
    .locator('xpath=ancestor::article');
  await expect(harborCard).toHaveCount(1);
  await expect(harborCard.locator('img')).toHaveAttribute(
    'alt',
    'Illustration of the cream Harbor 390 refrigerator with a lower freezer door and a C energy label',
  );

  await expect(fixture.getByText('Physical fit and rejection reasons')).toBeVisible();
  await expect(fixture.getByText('Polar Arc 510 Steel')).toBeVisible();
  await expect(fixture.locator('.rich-card-visual.has-media')).toHaveCount(2);
});
