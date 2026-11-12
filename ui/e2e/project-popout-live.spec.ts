import { expect, test } from '@playwright/test';
import { login } from './helpers';

test('project pop-out loads a focused window through real APIs', async ({ page, context }, testInfo) => {
  await login(page);
  const response = await page.request.post('/api/v1/projects', {
    data: { name: 'Project window acceptance', description: '' },
  });
  expect(response.ok()).toBeTruthy();
  const project = await response.json();
  try {
    await page.goto(`/projects/${project.project_id}?tab=conversations`);
    const open = page.getByRole('button', { name: 'Open project in separate window', exact: true });
    await expect(open).toBeVisible();
    const popupPromise = context.waitForEvent('page');
    await open.click();
    const popup = await popupPromise;
    await expect(popup.getByRole('heading', { name: project.name, exact: true })).toBeVisible();
    await expect(popup).toHaveURL(url => url.searchParams.get('window') === '1'
      && url.searchParams.get('tab') === 'conversations');
    await expect(popup.getByRole('link', { name: '← All projects', exact: true })).toHaveCount(0);
    await expect(popup.locator('aside')).toHaveCount(0);
    await expect(popup.getByText('No description', { exact: true })).toHaveCount(0);
    expect(await popup.evaluate(() => window.opener === null)).toBe(true);
    for (const width of [1440, 390]) {
      await popup.setViewportSize({ width, height: 900 });
      await expect(popup.getByRole('button', { name: 'New chat in project', exact: true })).toBeVisible();
      await expect(popup.getByText('Loading', { exact: true })).toHaveCount(0);
      await popup.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await popup.screenshot({ path: testInfo.outputPath(`project-window-${width}.png`), fullPage: true });
    }
    await popup.getByRole('tab', { name: /^Tasks\b/ }).click();
    await expect(popup).toHaveURL(url => url.searchParams.get('window') === '1' && url.searchParams.get('tab') === 'tasks');
    await popup.reload();
    await expect(popup.getByRole('heading', { name: project.name, exact: true })).toBeVisible();
    await expect(popup.locator('aside')).toHaveCount(0);
    await popup.close();
  } finally {
    const deleted = await page.request.delete(`/api/v1/projects/${project.project_id}`);
    expect(deleted.ok()).toBeTruthy();
  }
});
