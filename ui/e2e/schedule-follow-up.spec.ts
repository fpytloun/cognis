import { expect, test } from '@playwright/test';

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  for (const mode of ['auto', 'default'] as const) {
  test(`schedule ${mode} follow-up instructions remain discoverable and persist at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    page.on('pageerror', (error) => { throw error; });
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis-e2e.localdev.me', name: 'Owner', role: 'admin', is_active: true },
    }));
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/v1/agents') {
        await route.fulfill({ json: { items: [
          { agent_id: 'riker', name: 'Riker', display_name: 'Riker', agent_type: 'primary', status: 'active' },
          { agent_id: 'laforge', name: 'LaForge', display_name: 'LaForge', agent_type: 'primary', status: 'active' },
        ], next_cursor: null, has_more: false } });
      } else if (path === '/api/v1/workflows') {
        await route.fulfill({ json: { items: [], next_cursor: null, has_more: false } });
      } else {
        await route.fulfill({ json: [] });
      }
    });
    let schedule = {
      schedule_id: 'schedule-follow-up', name: 'Project report', description: 'Summarize changes',
      schedule_type: 'interval', interval_seconds: 3600, cron_expr: null, one_shot_at: null,
      timezone: 'UTC', workflow_id: 'system:general-task', enabled: true,
      agent_id: 'laforge', agent_profile_id: null, priority: 0, max_concurrent_runs: 1,
      delete_after_run: false, created_at: null, next_run_at: null, last_run_at: null,
      delivery_mode: 'preferred_channel', delivery_target: null, completion_mode_family: 'direct',
      on_finish_instructions: null as string | null, allow_silent_completion: false,
      interaction_mode_override: null, escalation_timeout_seconds: null, labels: [],
      inputs: {}, session_policy: null, task_retries_enabled: false, recent_tasks: [],
    };
    const mutations: Record<string, unknown>[] = [];
    await page.route('**/api/v1/schedules/schedule-follow-up', async (route) => {
      if (route.request().method() === 'PUT') {
        const update = route.request().postDataJSON();
        mutations.push(update);
        schedule = { ...schedule, ...update };
      }
      await route.fulfill({ json: schedule });
    });
    await page.goto('/schedules/schedule-follow-up');
    await page.getByRole('button', { name: 'Edit schedule' }).click();
    const instructions = page.getByRole('textbox', { name: 'Finish instructions (optional)' });
    await expect(instructions).toBeVisible();
    await expect(instructions).toBeDisabled();
    await expect(page.getByText(/Select Agent for follow-up instructions/)).toBeVisible();
    const behavior = page.getByLabel('Completion notification behavior');
    expect(await behavior.locator('option').allTextContents()).toEqual([
      expect.stringMatching(/^Default — Direct on success, Agent otherwise/),
      expect.stringMatching(/^Agent — Agent handles the outcome/),
      expect.stringMatching(/^Direct — Result only/),
    ]);
    await behavior.selectOption(mode);
    await expect(instructions).toBeEnabled();
    if (mode === 'auto') {
      await expect(page.getByText(/uses these instructions only for non-success outcomes/)).toBeVisible();
    }
    await instructions.fill('Report useful changes and verify the outcome.');
    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => mutations.length).toBe(1);
    expect(mutations[0]).toMatchObject({
      completion_mode_family: mode,
      on_finish_instructions: 'Report useful changes and verify the outcome.',
    });
    await page.reload();
    await expect(instructions).toHaveValue('Report useful changes and verify the outcome.');
    await expect(instructions).toBeEnabled();
    await expect(behavior).toHaveValue(mode);
    await expect(behavior.locator('option:checked')).toContainText(mode === 'auto' ? 'Default' : 'Agent');
    await instructions.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath('schedule-follow-up.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
  }
}
