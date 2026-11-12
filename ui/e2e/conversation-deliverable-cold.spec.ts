import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  // Synthetic preview fixtures only; not deployment CSP acceptance evidence.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
  viewport: { width: 1280, height: 1700 },
});

for (const paged of [false, true]) {
test(`six cold documents ${paged ? 'fill a reduced history window' : 'are ready in the initial chat frame'} without detail requests`, async ({ page }, testInfo) => {
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  const items: Array<Record<string, unknown>> = [{
    id: 'message:cold-introduction', kind: 'message', sort_key: '0000',
    source_refs: [], stable: true, role: 'assistant', partial: false,
    message_id: 'cold-introduction', content: 'Cold conversation introduction.', attachments: [],
  }];
  for (let index = 1; index <= 6; index++) {
    const rich = index % 2 === 0;
    const content = `# Cold report ${index}\n\nReady with the conversation.`;
    const document = {
      deliverable_id: `dlv_cold_report_${index}`, format: rich ? 'rich' : 'markdown',
      content, title: null, rich_payload: rich ? { blocks: [{ type: 'markdown', content }] } : null,
      outputs: {}, render_metadata: {}, export_metadata: {}, validation_warnings: [],
      step_run_id: null, version: 1, attempt_number: 1, target: null,
      status: 'approved', evaluator_feedback: null, created_at: null, updated_at: null,
      lineage_id: `dlv_cold_report_${index}`, revision: 1, latest_revision: 1,
      latest_deliverable_id: `dlv_cold_report_${index}`, revision_count: 1, is_latest: true,
    };
    items.push({
      id: `deliverable-cold-row-${index}`, kind: 'assistant_deliverable',
      sort_key: String(index).padStart(4, '0'), source_refs: [], stable: true,
      deliverable_id: document.deliverable_id, format: document.format, deliverable: document,
    });
  }
  const fixture = await installTaskCockpitFixture(page, paged ? {
    conversationHistoryPages: [
      [...items.slice(-2), { ...items[0], sort_key: '99999999999999999999' }],
      items.slice(3, 5),
      items.slice(1, 3),
    ],
  } : { conversationTimelineItems: items });
  await page.route(/\/api\/v1\/inbox(?:\?.*)?$/, route => route.fulfill({
    json: { items: [], unread_count: 0, next_cursor: null },
  }));
  await page.route('**/api/v1/dashboard/navigation-summary', route => route.fulfill({
    json: {
      chat_groups: [], schedules: 0, tasks: 0,
      dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
    },
  }));
  await page.route(/\/api\/v1\/library\/items\/deliverable\/dlv_cold_report_[1-6]$/, route => {
    const id = route.request().url().split('/').pop()!;
    return route.fulfill({ json: {
      id, kind: 'deliverable', title: null,
      format: Number(id.slice(-1)) % 2 === 0 ? 'rich' : 'markdown',
      owner_email: 'owner@cognis.test', agent_id: 'stage39', agent_name: 'Stage 39 Agent',
      project_id: null, project_name: null, conversation_id: 'conv-task-chat',
      conversation_title: 'Release safety review', task_id: null, task_title: null,
      lineage_id: id, revision: 1, created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z', size_bytes: 0, status: 'approved',
      origin: 'agent', is_readonly: true, is_favorite: false,
    } });
  });
  await page.route('**/api/v1/feedback/conversations/conv-task-chat/staging', route =>
    route.fulfill({ json: {
      conversation_id: 'conv-task-chat', version: 0, items: [], receipts: [], subject_ids: [],
    } }));
  const detailRequests: string[] = [];
  await page.route('**/api/v1/deliverables/**', async route => {
    detailRequests.push(route.request().url());
    await route.abort();
  });
  await page.addInitScript(() => {
    const state = window as typeof window & {
      coldFrame?: { documents: number; introduction: boolean; time: number };
      coldLoadingObserved?: boolean;
    };
    state.coldLoadingObserved = false;
    let scheduled = false;
    new MutationObserver(() => {
      if (document.body?.textContent?.includes('Loading deliverable…')) {
        state.coldLoadingObserved = true;
      }
      if (!scheduled && document.querySelector('.assistant-deliverable-wrapper')) {
        scheduled = true;
        requestAnimationFrame(() => {
          state.coldFrame = {
            documents: document.querySelectorAll('.rich-inline-document').length,
            introduction: document.body.textContent!.includes('Cold conversation introduction.'),
            time: performance.now(),
          };
        });
      }
    }).observe(document, { subtree: true, childList: true });
  });
  await page.goto('/chat/conv-task-chat');
  for (let index = 1; index <= 6; index++) {
    await expect(page.getByRole('heading', { name: `Cold report ${index}`, exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: `Cold report ${index}`, exact: true })).toBeInViewport();
  }
  await expect(page.locator('textarea').first()).toBeEditable();
  const state = await page.evaluate(() => {
    const value = window as typeof window & {
      coldFrame?: { documents: number; introduction: boolean; time: number };
      coldLoadingObserved?: boolean;
    };
    return { frame: value.coldFrame, loadingObserved: value.coldLoadingObserved };
  });
  if (paged) expect(state.frame?.documents).toBeGreaterThanOrEqual(2);
  else expect(state.frame?.documents).toBe(6);
  expect(state.frame?.introduction).toBe(true);
  expect(state.loadingObserved).toBe(false);
  expect(detailRequests).toEqual([]);
  expect(fixture.unmockedRequests()).toEqual([]);
  await testInfo.attach('cold-readiness', {
    body: JSON.stringify(state, null, 2), contentType: 'application/json',
  });
  await page.screenshot({ path: testInfo.outputPath('cold-multiple-documents.png') });
});
}
