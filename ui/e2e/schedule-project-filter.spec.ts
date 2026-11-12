import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  // Static preview lacks controller-provided CSP hashes; API data is synthetic.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

for (const width of [1440, 390]) {
  test(`schedule project selection persists and can be cleared at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await installTaskCockpitFixture(page);
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin', is_active: true },
    }));
    await page.route('**/api/v1/projects', (route) => route.fulfill({ json: [
      { project_id: 'project-a', name: 'Project A', status: 'active' },
      { project_id: 'project-b', name: 'Project B', status: 'active' },
    ] }));
    const requests: (string | null)[] = [];
    await page.route('**/api/v1/schedules**', (route) => {
      const project = new URL(route.request().url()).searchParams.get('project_id');
      requests.push(project);
      const schedules = [
        {
          schedule_id: 'schedule-a', name: 'Project A review', project_id: 'project-a',
          schedule_type: 'interval', interval_seconds: 3600, agent_id: 'agent-stage39',
          enabled: true, is_expired: false, completion_mode_family: 'auto',
          consecutive_errors: 0, human_schedule: 'Every hour',
        },
        {
          schedule_id: 'schedule-b', name: 'Project B review', project_id: 'project-b',
          schedule_type: 'interval', interval_seconds: 3600, agent_id: 'agent-stage39',
          enabled: true, is_expired: false, completion_mode_family: 'auto',
          consecutive_errors: 0, human_schedule: 'Every hour',
        },
      ];
      return route.fulfill({ json: schedules.filter((schedule) => !project || schedule.project_id === project) });
    });
    await page.goto('/schedules');
    await expect(page.getByRole('heading', { name: 'Project A review' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Project B review' })).toBeVisible();
    const filter = page.getByRole('combobox', { name: 'Project filter' });
    await filter.selectOption('project-a');
    await expect(filter).toHaveValue('project-a');
    await expect(page).toHaveURL(/\/schedules\?project_id=project-a$/);
    await expect(page.getByRole('heading', { name: 'Project B review' })).toHaveCount(0);
    await expect.poll(() => requests.at(-1)).toBe('project-a');
    await page.reload();
    await expect(filter).toHaveValue('project-a');
    await expect(page.getByRole('heading', { name: 'Project A review' })).toBeVisible();
    await page.goto('/schedules?project_id=project-b');
    await expect(filter).toHaveValue('project-b');
    await expect(page.getByRole('heading', { name: 'Project B review' })).toBeVisible();
    await page.goBack();
    await expect(filter).toHaveValue('project-a');
    await expect(page.getByRole('heading', { name: 'Project A review' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Project B review' })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('schedule-project-selected.png'), fullPage: true });
    await filter.selectOption('');
    await expect(page).toHaveURL(/\/schedules$/);
    await expect(page.getByRole('heading', { name: 'Project B review' })).toBeVisible();
    await expect.poll(() => requests.at(-1)).toBe(null);
    expect(errors).toEqual([]);
  });
}
