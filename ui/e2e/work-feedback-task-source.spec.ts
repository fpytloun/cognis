import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from '@playwright/test';
import { injectScenario, login, openOrCreateConversation } from './helpers';

test('produces a real completed task result for feedback revision', async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  await login(page);
  await openOrCreateConversation(page);
  const conversationId = new URL(page.url()).pathname.split('/').at(-1);
  const workflowResponse = await page.request.get('/api/v1/workflows');
  expect(workflowResponse.ok()).toBeTruthy();
  const workflows = await workflowResponse.json();
  const workflow = workflows.items.find((item: { workflow_id: string }) =>
    item.workflow_id.endsWith('general-task')
  );
  expect(workflow, 'The seeded general-task workflow must exist').toBeTruthy();
  await injectScenario('work-feedback-task-result');
  const response = await page.request.post('/api/v1/tasks', {
    data: {
      agent_id: 'e2e-test-agent',
      title: 'Weekly infrastructure review — feedback acceptance',
      description: 'scenario:work-feedback-task-result',
      expected_output: 'Synthetic weekly review with explicit human-review follow-ups.',
      workflow_id: workflow.workflow_id,
      delivery_mode: 'specific_conversation',
      delivery_target: conversationId,
      interaction_mode_override: 'none',
      status: 'queued'
    }
  });
  expect(response.ok(), `Task creation: HTTP ${response.status()}`).toBeTruthy();
  const created = await response.json();
  const taskId = created.task_id;
  let detail: {
    status: string;
    step_runs: { step_run_id: string; deliverable_id: string | null }[];
  } | undefined;
  await expect.poll(async () => {
    const current = await page.request.get(`/api/v1/tasks/${taskId}`);
    expect(current.ok()).toBeTruthy();
    detail = await current.json();
    return ['completed', 'failed', 'cancelled'].includes(detail?.status ?? '');
  }, { timeout: 90_000, intervals: [500, 1000, 2000] }).toBe(true);
  expect(detail?.status, 'The task must complete, not merely reach a terminal state').toBe('completed');
  const step = detail?.step_runs.find((item) => item.deliverable_id);
  expect(step?.deliverable_id).toBeTruthy();
  const sources = {
    task_id: taskId,
    step_run_id: step?.step_run_id,
    deliverable_id: step?.deliverable_id,
    conversation_id: conversationId,
    synthetic: true
  };
  const directory = resolve(testInfo.config.rootDir, '../review-artifacts/work-feedback');
  await mkdir(directory, { recursive: true });
  await writeFile(resolve(directory, 'task-source.json'), JSON.stringify(sources, null, 2));
  await testInfo.attach('task-source', {
    body: JSON.stringify(sources, null, 2),
    contentType: 'application/json'
  });
});
