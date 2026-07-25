import { expect, test } from '@playwright/test';

test.use({ serviceWorkers: 'block' });

test.describe('rich deliverable evidence interactions', () => {
  test('supports citations, evidence expansion, and sortable decision rows', async ({ page }) => {
    await page.goto('/rich-deliverable-evidence-fixture');

    await expect(page.getByTestId('rich-deliverable-evidence-fixture')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="research_answer"]')).toBeVisible();
    await page.getByRole('button', { name: /Citation 1: Renderer-owned interactivity notes/i }).click();
    await expect(page.getByRole('dialog', { name: /Source Renderer-owned interactivity notes/i })).toBeVisible();
    await expect(
      page.getByRole('dialog', { name: /Source Renderer-owned interactivity notes/i })
        .getByText(/Interactions are declarative/)
    ).toBeVisible();

    await page.getByRole('button', { name: /Citation 2: Unsafe source should not link/i }).click();
    const unsafeSource = page.getByRole('dialog', { name: /Source Unsafe source should not link/i });
    await expect(unsafeSource.getByRole('link', { name: 'Open source' })).toHaveCount(0);
    await page.keyboard.press('Escape');

    const fixture = page.getByTestId('rich-deliverable-evidence-fixture');
    await fixture.evaluate((element) => { element.scrollTop = 520; });
    await page.getByText('Evidence snippets').first().click();
    await expect(page.getByText('Payloads remain renderer-neutral while the UI owns interaction state.')).toBeVisible();

    const matrix = page.locator('[data-rich-block-type="decision_matrix"]');
    await fixture.evaluate((element) => { element.scrollTop = 1000; });
    await matrix.getByRole('button', { name: 'Sort by Score' }).click();
    await expect(matrix.locator('tbody tr').first().getByText('Micro-app payloads')).toBeVisible();
    await matrix.getByRole('button', { name: 'Show evidence' }).first().click();
    await expect(page.getByText('Would let payloads define behavior instead of data.')).toBeVisible();
  });

  test('uses mobile-friendly source sheet and matrix cards', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 1200 });
    await page.goto('/rich-deliverable-evidence-fixture');

    await page.getByRole('button', { name: /Citation 1: Renderer-owned interactivity notes/i }).click();
    await expect(page.getByRole('dialog', { name: /Source Renderer-owned interactivity notes/i })).toBeVisible();
    await expect(page.locator('[data-rich-block-type="decision_matrix"] td[data-label="Option"]').first()).toBeVisible();
  });
});
