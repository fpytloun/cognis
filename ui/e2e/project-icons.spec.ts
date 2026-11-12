import { test } from '@playwright/test';
import { expect, login } from './helpers';

test('project icons search, save and reload as glyphs on desktop and phone', async ({ page }, testInfo) => {
  await login(page);
  const response = await page.request.post('/api/v1/projects', {
    data: { name: 'Icon library acceptance', color: '#38bdf8' },
  });
  expect(response.ok()).toBe(true);
  const project = await response.json();
  try {
    await page.goto(`/projects/${project.project_id}?tab=settings`);
    const picker = page.getByRole('group', { name: 'Project icon', exact: true });
    await expect(picker.getByRole('button', { name: 'Use Cognis icon' })).toBeVisible();
    await expect(picker.getByRole('button', { name: /^Use .+ icon$/ })).toHaveCount(60);
    await picker.getByRole('button', { name: 'Show more icons' }).click();
    await expect(picker.getByRole('button', { name: /^Use .+ icon$/ })).toHaveCount(120);
    await picker.getByRole('searchbox').fill('coffee');
    await picker.getByRole('button', { name: 'Use Coffee icon', exact: true }).click();
    await expect(picker.getByRole('button', { name: 'Use Coffee icon', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(picker.getByRole('img', { name: 'Selected project icon' }).locator('svg.lucide-coffee')).toBeVisible();
    const saved = page.waitForResponse((res) => res.request().method() === 'PATCH'
      && new URL(res.url()).pathname === `/api/v1/projects/${project.project_id}`);
    await page.getByRole('button', { name: 'Save project', exact: true }).click();
    expect((await saved).ok()).toBe(true);
    await page.reload();
    await expect(picker.getByRole('img', { name: 'Selected project icon' }).locator('svg.lucide-coffee')).toBeVisible();
    await picker.getByRole('searchbox').fill('coffee');
    await expect(picker.getByRole('button', { name: 'Use Coffee icon', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await picker.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath('desktop-coffee.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await picker.scrollIntoViewIfNeeded();
    await expect(picker.getByRole('button', { name: 'Use Coffee icon', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('phone-coffee.png') });
    await picker.getByRole('searchbox').fill('no matching project symbol');
    await expect(picker.getByText(/No icons match/)).toBeVisible();
    await picker.getByRole('button', { name: 'Use default' }).click();
    await expect(picker.getByRole('button', { name: 'Use default' })).toHaveAttribute('aria-pressed', 'true');
  } finally {
    const removed = await page.request.delete(`/api/v1/projects/${project.project_id}`);
    expect(removed.ok()).toBe(true);
  }
});
