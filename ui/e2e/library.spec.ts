import { expect, test } from '@playwright/test';
import { installLibraryFixture } from './library-fixture';

test('Library supports list pagination, lazy nested folders, and reader deep links', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await installLibraryFixture(page);
  await page.goto('/library');
  await expect(page.getByRole('heading', { name: 'Library', exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('resource-row')).toHaveCount(4);
  await page.getByRole('button', { name: 'Load more resources' }).click();
  await expect(page.getByTestId('resource-row')).toHaveCount(7);
  const laterRow = page.getByRole('link', { name: /Research notes/ });
  await laterRow.click();
  await expect(page.getByTestId('library-resource-modal')).toBeVisible();
  await page.route('**/api/v1/audio/jobs/source?**', route => route.fulfill({
    json: { supported: true, can_generate: true, can_cancel: true, detail: null, jobs: [] }
  }));
  await page.getByRole('button', { name: 'Listen to this document' }).click();
  await expect(page.getByRole('dialog', { name: 'Document audio' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Generate narration' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Generate podcast' })).toBeEnabled();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Document audio' })).toBeHidden();
  await expect(page.getByTestId('library-resource-modal')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('library-resource-modal')).toHaveCount(0);
  await expect(page.getByTestId('resource-row')).toHaveCount(7);
  await expect(laterRow).toBeFocused();
  await page.screenshot({ path: test.info().outputPath('library-desktop-list.png'), fullPage: true });
  await page.getByRole('combobox', { name: 'View', exact: true }).selectOption('project_conversation');
  await page.getByRole('button', { name: 'Cognis 3', exact: true }).click();
  await page.getByRole('button', { name: 'Release planning 2', exact: true }).click();
  await expect(page.getByRole('link', { name: /Release readiness review/ })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('library-desktop-tree.png'), fullPage: true });
  await page.getByRole('link', { name: /Release readiness review/ }).click();
  await expect(page.getByRole('heading', { name: 'Release readiness', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Details', exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('library-desktop-reader.png'), fullPage: true });
  await page.goBack();
  await expect(page.getByRole('combobox', { name: 'View', exact: true })).toHaveValue('project_conversation');
  await expect(page.getByRole('link', { name: /Release readiness review/ })).toBeVisible();
  expect(errors).toEqual([]);
});

test('Files distinguish user uploads and support source filters on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installLibraryFixture(page);
  await page.goto('/library?lib_kind=artifact');
  await expect(page.getByText('User upload', { exact: true })).toHaveCount(2);
  await expect(page.getByText('Agent output', { exact: true })).toHaveCount(2);
  await page.screenshot({ path: test.info().outputPath('library-mobile-files.png'), fullPage: true });
  await page.getByRole('button', { name: 'Filters', exact: true }).click();
  await page.getByRole('combobox', { name: 'File source', exact: true }).selectOption('user');
  await expect(page.getByTestId('resource-row')).toHaveCount(2);
  await expect(page.getByText('Agent output', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('library-mobile-upload-filter.png'), fullPage: true });
  await page.getByRole('link', { name: /Library requirements/ }).click();
  await page.route('**/api/v1/audio/jobs/source?**', route => route.fulfill({
    json: { supported: true, can_generate: true, can_cancel: true, detail: null, jobs: [] }
  }));
  await page.getByRole('button', { name: 'Listen to this document' }).click();
  await expect(page.getByRole('button', { name: 'Generate narration' })).toBeEnabled();
  const audioBounds = await page.getByRole('dialog', { name: 'Document audio' }).boundingBox();
  expect(audioBounds!.x).toBeGreaterThanOrEqual(0);
  expect(audioBounds!.x + audioBounds!.width).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('main', { name: 'Document preview' }).getByText(/Preserve revision history/)).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: test.info().outputPath('library-mobile-file-reader.png'), fullPage: true });
});

test('Search, no results, direct reader refresh and invalid references', async ({ page }) => {
  await installLibraryFixture(page);
  await page.goto('/library');
  await page.getByRole('searchbox').fill('no matching document');
  await expect(page.getByText('No matching resources')).toBeVisible();
  await page.getByRole('searchbox').fill('security');
  await expect(page.getByTestId('resource-row')).toHaveCount(1);
  await page.getByRole('link', { name: /Content access/ }).click();
  await expect(page).toHaveURL(/lib_item=dlv_access_review/);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Content access and sharing security review', exact: true }).first()).toBeVisible();
  await page.goto('/library/artifact/art_missing');
  await expect(page.getByRole('heading', { name: 'Resource unavailable' })).toBeVisible();
});

test('Grouping and filters survive navigation; modal dismissal preserves the browser', async ({ page }) => {
  await installLibraryFixture(page);
  await page.goto('/library');
  await page.getByRole('combobox', { name: 'View', exact: true }).selectOption('conversation');
  await page.getByRole('button', { name: 'Filters', exact: true }).click();
  await page.getByRole('combobox', { name: 'Ownership', exact: true }).selectOption('mine');
  await page.goto('/tasks');
  await page.goto('/library');
  await expect(page.getByRole('combobox', { name: 'View', exact: true })).toHaveValue('conversation');
  await page.getByRole('button', { name: 'Filters (1)', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Ownership', exact: true })).toHaveValue('mine');
  await page.getByRole('button', { name: 'Release planning 2', exact: true }).click();
  await page.getByRole('link', { name: /Release readiness review/ }).click();
  await expect(page.getByTestId('library-resource-modal')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/library');
  await page.getByRole('button', { name: 'Close resource', exact: true }).click();
  await expect(page.getByTestId('library-resource-modal')).toHaveCount(0);
  await expect(page.getByRole('link', { name: /Release readiness review/ })).toBeVisible();
  await page.goto('/library?lib_group=none');
  await expect(page.getByRole('combobox', { name: 'View', exact: true })).toHaveValue('none');
});
