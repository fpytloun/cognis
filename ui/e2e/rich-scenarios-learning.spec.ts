import { expect, test } from '@playwright/test';

const scenarios = [
  {
    id: 'solar-infographic',
    heading: 'A field guide to our planetary neighborhood',
    imageAlt: 'Illustrated Solar System with the Sun and the eight named planets on nested orbits.',
  },
  {
    id: 'educational-cheatsheet',
    heading: 'The exposure triangle',
    imageAlt: 'Triangular diagram linking aperture, shutter speed, and ISO to exposure.',
  },
  {
    id: 'book-spoilers',
    heading: 'Frankenstein: a reader’s guide',
    imageAlt: 'Moonlit alpine laboratory and a solitary figure in a cover illustration for Frankenstein.',
  },
] as const;

async function expectNoHorizontalOverflow(page: import('@playwright/test').Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test.describe('learning rich scenarios', () => {
  for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
    test(`renders media and hierarchy without overflow at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ reducedMotion: 'reduce' });

      for (const scenario of scenarios) {
        await page.goto(`/rich-deliverable-fixture?scenario=${scenario.id}&width=${viewport.width}`, {
          waitUntil: 'domcontentloaded',
        });
        await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', scenario.id);
        await expect(page.getByRole('heading', { name: scenario.heading })).toBeVisible();
        const image = page.getByRole('img', { name: scenario.imageAlt });
        await expect(image).toBeVisible();
        await expect(image).toHaveJSProperty('complete', true);
        expect(await image.evaluate((node: HTMLImageElement) => node.naturalWidth)).toBeGreaterThan(0);
        await expectNoHorizontalOverflow(page);
      }
    });
  }

  test('keeps spoilers collapsed until keyboard reveal', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/rich-deliverable-fixture?scenario=book-spoilers&width=390', {
      waitUntil: 'domcontentloaded',
    });

    const disclosure = page.locator('[data-rich-block-type="accordion"]');
    const plot = disclosure.getByText('Full plot summary — spoilers');
    const plotSummary = disclosure.locator('summary').filter({ hasText: 'Full plot summary — spoilers' });
    const endingText = page.getByText('Victor dies while pursuing the creature in the Arctic.');
    await expect(plot).toBeVisible();
    await expect(endingText).toBeHidden();

    await plotSummary.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByText('Victor creates a living being, recoils from it, and abandons it.')).toBeVisible();
    await expectNoHorizontalOverflow(page);

    const ending = disclosure.getByText('Ending explained — spoilers');
    const endingSummary = disclosure.locator('summary').filter({ hasText: 'Ending explained — spoilers' });
    await endingSummary.focus();
    await page.keyboard.press('Space');
    await expect(endingText).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test('retains representative subordinate hierarchy and spoiler warning', async ({ page }) => {
    await page.goto('/rich-deliverable-fixture?scenario=solar-infographic', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: 'Eight worlds, radically different years' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Compact glossary' })).toBeVisible();
    await expect(page.getByText('Solar System Exploration: Planets', { exact: true })).toBeVisible();

    await page.goto('/rich-deliverable-fixture?scenario=educational-cheatsheet', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: 'Quick settings' })).toBeVisible();
    await expect(page.getByText('Common mistakes', { exact: true })).toBeVisible();
    await expect(page.getByText('Source list', { exact: true })).toBeVisible();

    await page.goto('/rich-deliverable-fixture?scenario=book-spoilers', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: 'Discussion questions' })).toBeVisible();
    await expect(page.getByText('Spoiler warning', { exact: true })).toBeVisible();
    await expect(page.getByText(/next two disclosures reveal the full plot and the ending/i)).toBeVisible();
  });
});
