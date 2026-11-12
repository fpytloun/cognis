import { expect, test } from '@playwright/test';
import { installLibraryFixture } from './library-fixture';

test('deliverable desktop menu opens an isolated revision window', async ({ page, context }) => {
  await installLibraryFixture(page);
  await page.goto('/library/deliverable/dlv_release_review');
  await expect(page.getByRole('heading', { name: 'Release readiness', exact: true })).toBeVisible();
  await page.getByLabel('More document actions').click();
  const action = page.getByRole('button', { name: 'Open in new window' });
  await expect(action).toBeVisible();
  const bounds = await action.boundingBox();
  const viewport = page.viewportSize()!;
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
  await page.screenshot({ path: test.info().outputPath('desktop-menu.png') });
  const navigation = context.waitForEvent('request', {
    predicate: request => request.isNavigationRequest() && new URL(request.url()).searchParams.get('window') === '1',
  });
  const popup = context.waitForEvent('page');
  await action.click();
  expect(new URL((await navigation).url()).pathname).toBe('/library/deliverable/dlv_release_review');
  await (await popup).close();

  // Use the same page's API fixture to exercise the destination reader; the
  // popup's initial navigation above separately verifies the actual window action.
  await page.goto('/library/deliverable/dlv_release_review?window=1');
  await expect(page.getByRole('heading', { name: 'Release readiness', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '← Library', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('sidebar-logo-link')).toHaveCount(0);
  await expect(page.getByText('Loading document…', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('isolated-reader.png') });
});

test('deliverable menu hides popout on phones', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installLibraryFixture(page);
  await page.goto('/library/deliverable/dlv_release_review');
  await expect(page.getByRole('heading', { name: 'Release readiness', exact: true })).toBeVisible();
  await page.getByLabel('More document actions').click();
  await expect(page.getByRole('button', { name: 'Copy document', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open in new window' })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('phone-menu.png') });
});
