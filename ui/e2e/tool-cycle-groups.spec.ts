import { expect, test } from '@playwright/test';

for (const separate of [false, true]) {
  test(`task Logs and chat merge consecutive exploration cycles (separate=${separate})`, async ({ page }, info) => {
    const errors: string[] = [];
    const unexpected: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/') || url.origin !== 'http://127.0.0.1:4193') {
        unexpected.push(url.pathname);
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(`/groups.html${separate ? '?separate' : ''}`);
    for (const surface of ['task_step', 'conversation']) {
      const timeline = page.getByTestId(surface);
      const explore = timeline.locator('[data-tool-group-kind="explore"]');
      await expect(explore).toHaveCount(1);
      await expect(explore).toContainText('5 tools');
      await expect(timeline.locator('[data-tool-group-kind="command"]')).toHaveCount(1);
      await expect(timeline.locator('[data-tool-group-kind="edit"]')).toHaveCount(1);
      await expect(timeline.locator('[data-tool-group-kind]')).toHaveCount(3);
      await explore.getByRole('button').first().click();
      await expect(explore.locator('article')).toHaveCount(5);
      await explore.locator('article').first().getByRole('button').first().click();
      await expect(timeline.getByText('Synthetic tool result', { exact: true }).first()).toBeVisible();
      await explore.getByRole('button').first().click();
      await expect(explore.locator('article').first()).not.toBeVisible();
    }
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    expect(unexpected).toEqual([]);
    await page.screenshot({ path: info.outputPath('consecutive-tool-cycles.png'), fullPage: true });
  });
}
