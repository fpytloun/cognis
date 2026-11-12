import { expect, test } from '@playwright/test';

test('mobile ongoing work titles open command details and subsessions without overflow', async ({ page }) => {
  await page.route('**/api/v1/conversations/*/background-commands/*', (route) => route.fulfill({
    json: { shell_id: 'shell-preview', executor_id: 'sample-desktop', command: 'npm run dev',
      cwd: '/workspace/ui', pid: 123, status: 'running', runtime_seconds: 70, idle_seconds: 5,
      exit_code: null, output_tail: 'long-output-'.repeat(100), output_chars: 1200, trimmed_chars: 0 },
  }));
  await page.goto('/');
  await page.getByRole('button', { name: /^Preview / }).click();
  await expect(page.getByLabel('Command', { exact: true })).toContainText('npm run dev');
  await expect(page.getByLabel('Latest command output')).toContainText('long-output');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Open source session' }).click();
  await expect(page.getByLabel('Selected session')).toHaveText('sess-root');
  await page.getByRole('button', { name: 'Subsession review', exact: true }).click();
  await expect(page.getByLabel('Selected session')).toHaveText('sess-review');
  await page.getByRole('button', { name: 'Refresh details' }).click();
  await expect(page.getByLabel('Latest command output')).toContainText('long-output');
});
