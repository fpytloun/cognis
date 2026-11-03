import { expect, test } from '@playwright/test';
import { syncHealthWorkflow } from '../src/lib/testing/workflow-plan-fixture';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

for (const width of [1440, 390]) {
  test(`scheduled workflow explains actions and alternative outcomes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installTaskCockpitFixture(page);
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' },
    }));
    await page.route('**/api/health', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/health/providers', (route) => route.fulfill({ json: {} }));
    const workflow = syncHealthWorkflow();
    // Exercise expansion and wrapping, not only short demo strings.
    workflow.steps[2].complete!.content += `\n${'Verify synthetic catalog health before attempting recovery. '.repeat(35)}`;
    workflow.steps[1].condition!.if = `{{ steps.probe.outputs.${'health_result_'.repeat(12)} == true }}`;
    const schedule = {
      schedule_id: 'schedule-sync-health',
      name: 'Sample catalog health',
      description: 'Check the synthetic catalog health.',
      schedule_type: 'interval',
      interval_seconds: 300,
      timezone: 'UTC',
      agent_id: 'agent-stage39',
      workflow_id: workflow.workflow_id,
      task_template: { title: 'Sample catalog health', description: 'Check the synthetic catalog health.' },
      enabled: true,
      next_fire_at: '2099-01-01T08:00:00Z',
      consecutive_errors: 0,
      last_run_status: null,
      human_schedule: 'Every five minutes',
    };
    await page.route('**/api/v1/schedules**', (route) => route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith(schedule.schedule_id)
        ? schedule : [schedule],
    }));
    await page.route(`**/api/v1/workflows/${workflow.workflow_id}`, (route) => route.fulfill({ json: workflow }));
    await page.goto('/');
    await page.getByTestId(`dashboard-schedule-row-${schedule.schedule_id}`).click();
    await page.getByTestId('dashboard-schedule-modal-tab-steps').click();
    const plan = page.getByTestId('dashboard-schedule-planned-steps');
    await expect(plan).toContainText('Notify only when unhealthy');
    await expect(plan).toContainText('Synthetic catalog health probe');
    await expect(plan).toContainText('Side effects are permitted; this is not a read-only guarantee.');
    await expect(plan).toContainText('not every step runs');
    await expect(page.getByTestId('planned-step-route')).toContainText('If true: healthy');
    await expect(page.getByTestId('planned-step-route')).toContainText('If false: unhealthy');
    await expect(page.getByTestId('planned-step-healthy')).toContainText('Finish silently');
    const unhealthy = page.getByTestId('planned-step-unhealthy');
    await expect(unhealthy).toContainText('preferred channel');
    const summary = unhealthy.locator('summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    await expect(unhealthy.locator('details')).toHaveAttribute('open', '');
    await expect(unhealthy.getByText(/Ukázkový katalog vyžaduje kontrolu/)).toBeVisible();
    await expect.poll(() => plan.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.getByTestId('planned-step-probe').scrollIntoViewIfNeeded();
    await page.screenshot({ path: test.info().outputPath(`workflow-plan-${width}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}
