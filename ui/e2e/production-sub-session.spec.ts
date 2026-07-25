import { expect, test } from '@playwright/test';
import { login, openOrCreateConversation } from './helpers';

const NOW = '2026-01-01T00:00:00Z';

function message(id: string, index: number, content: string, running = false) {
  return {
    id,
    kind: 'message',
    sort_key: `0001:${String(index).padStart(10, '0')}:0000:${id}`,
    source_refs: [{ store: 'intaris', session_id: 'sess_child', seq: index, event_type: 'message' }],
    created_at: NOW,
    status: running ? 'running' : 'complete',
    stable: !running,
    role: 'assistant',
    content,
    message_id: id,
    attachments: [],
    partial: running,
  };
}

function snapshot(scope: Record<string, unknown>, items: unknown[], active = false) {
  return {
    schema_version: 2,
    projection_version: 'production-shell-e2e',
    scope,
    conversation: { conversation_id: String(scope.conversation_id ?? 'parent'), agent_id: 'e2e-test-agent', title: 'Production shell fixture', status: 'active' },
    timeline: { items, has_more_before: false, before_cursor: null },
    state: { state_version: 1, snapshot_generated_at: NOW, capabilities: [], active_turn: {}, pending: {}, active_session: {} },
    queue: { messages: [], queued_count: 0 },
    runtime: { runtime_epoch: 'e2e', runtime_revision: 1, generated_at: NOW, has_active_turn: active, volatile_items: [] },
    cursor: `cursor:${scope.key}`,
    server_time: NOW,
  };
}

test('real /chat shell keeps parent fixed while the nested scoped viewport owns navigation', async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  let parentConversationId = '';
  const parentItems = [
    message('parent-message', 1, 'Parent state marker'),
    {
      id: 'delegation-child',
      kind: 'delegation',
      sort_key: '0001:0000000002:0000:delegation-child',
      source_refs: [{ store: 'intaris', session_id: 'parent-session', seq: 2, event_type: 'delegation' }],
      created_at: NOW,
      status: 'complete',
      stable: true,
      child_session_id: 'sess_child',
      agent_id: 'e2e-test-agent',
      title: 'Open nested production session',
      summary: 'Long nested session',
      result_summary: null,
      result_anchors: null,
      todos: null,
      tool_call_count: 2,
      max_tool_calls: 10,
      last_tool: 'web_search',
    },
  ];
  const childItems = Array.from({ length: 45 }, (_, index) =>
    message(`child-${index}`, index + 1, index === 44
      ? `${'# Long active answer\n\n'}${'Detailed nested assistant output. '.repeat(180)}`
      : `Nested production session row ${index + 1}`)
  );
  childItems[44] = message('child-active', 45, `${'# Long active answer\n\n'}${'Detailed nested assistant output. '.repeat(180)}`, true);

  await page.route('**/api/v1/chat/v2/conversations/*/snapshot', async (route) => {
    const match = route.request().url().match(/conversations\/([^/]+)\/snapshot/);
    parentConversationId = match?.[1] ?? parentConversationId;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(snapshot({
      key: `conversation:${parentConversationId}`,
      kind: 'conversation',
      conversation_id: parentConversationId,
    }, parentItems)) });
  });
  await page.route('**/api/v1/chat/v2/sessions/sess_child/snapshot', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(snapshot({
      key: 'session:sess_child',
      kind: 'session',
      session_id: 'sess_child',
      conversation_id: parentConversationId,
    }, childItems, true)),
  }));
  await page.route('**/api/v1/sessions/sess_child/intaris', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      session_id: 'sess_child',
      intaris_session_id: 'sess_child',
      title: 'Nested production session',
      intention: 'Inspect nested scope',
      summary: 'Shared session details',
      status: 'active',
      total_calls: 9,
      approved_count: 7,
      denied_count: 1,
      escalated_count: 1,
      context_usage: {
        prompt_tokens: 2400,
        max_context_tokens: 16000,
        percentage: 15,
        model: 'scope-model',
        reasoning_effort: 'medium',
        agent_profile_id: 'scope-profile',
        provider_id: 'scope-provider',
        effective_prompt_budget: 12000,
      },
      last_generation: {
        is_local: true,
        provider_id: 'scope-provider',
        provider_name: 'Scope Provider',
        runtime: 'ollama',
        location: 'executor',
        executor_id: 'scope-executor',
        executor_name: 'Scope Executor',
        model: 'scope-model',
        digest: null,
        quantization: 'Q4',
        configured_context_tokens: 16000,
        prompt_tokens: 2400,
        completion_tokens: 300,
        prompt_tokens_per_second: 70,
        generation_tokens_per_second: 30,
        time_to_first_token_seconds: 0.2,
        load_duration_seconds: 0.1,
        total_duration_seconds: 3,
        processor: 'GPU',
        gpu_residency: 'full',
        measured_at: NOW,
      },
    }),
  }));
  await page.route('**/api/v1/chat/v2/**/sync**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ schema_version: 2, projection_version: 'production-shell-e2e', scope: {}, cursor_before: '', cursor_after: '', ops: [], reset_required: false, has_more: false, server_time: NOW }),
  }));

  await login(page);
  await openOrCreateConversation(page);
  const parentViewport = page.getByTestId('timeline-viewport');
  const parentTop = await parentViewport.evaluate((node) => (node as HTMLElement).scrollTop);
  await page.getByRole('button', { name: /View session/i }).click();

  const overlay = page.locator('aside').filter({ hasText: 'Sub-session' });
  const scoped = overlay.getByTestId('scoped-timeline-viewport');
  await expect(scoped).toBeVisible();
  await overlay.getByRole('button', { name: 'Toggle session details' }).click();
  const details = overlay.getByTestId('session-details-content');
  await expect(details).toContainText('Shared session details');
  await expect(details).toContainText('scope-model · Scope Provider');
  await expect(details).toContainText('scope-profile');
  await expect(details).toContainText('2,400 / 16,000 tokens');
  await expect(details).toContainText('Scope Executor');
  await overlay.getByRole('button', { name: 'Toggle session details' }).click();
  await expect.poll(() => scoped.evaluate((node) => {
    const element = node as HTMLElement;
    return Math.round(element.scrollHeight - element.scrollTop - element.clientHeight);
  })).toBeLessThanOrEqual(2);
  await expect(overlay.getByRole('navigation', { name: 'Live tail controls' })).toBeVisible();
  await scoped.hover();
  await page.mouse.wheel(0, -900);
  await expect(overlay.getByText('Live follow paused')).toBeVisible();
  await expect(overlay.getByRole('navigation', { name: 'Message navigation' })).toBeVisible();
  expect(await parentViewport.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(parentTop);

  await overlay.getByRole('button', { name: 'Resume live follow' }).click();
  await expect.poll(() => scoped.evaluate((node) => {
    const element = node as HTMLElement;
    return Math.round(element.scrollHeight - element.scrollTop - element.clientHeight);
  })).toBeLessThanOrEqual(2);
  const activeRow = overlay.locator('[data-message-id="child-active"]');
  await scoped.hover();
  await page.mouse.wheel(0, -500);
  await expect(overlay.getByRole('button', { name: /active message/i })).toBeVisible();
  await overlay.getByRole('button', { name: /active message/i }).click();
  await expect.poll(async () => Math.abs(
    await scoped.evaluate((node) => node.getBoundingClientRect().top) -
    await activeRow.evaluate((node) => node.getBoundingClientRect().top)
  )).toBeLessThanOrEqual(8);

  const screenshotPath = testInfo.outputPath('production-sub-session-timeline.png');
  await overlay.screenshot({ path: screenshotPath, animations: 'disabled' });
  await testInfo.attach('production-sub-session-timeline', { path: screenshotPath, contentType: 'image/png' });
  await overlay.getByRole('button', { name: 'Back to conversation' }).click();
  await expect(overlay).toHaveCount(0);
  expect(await parentViewport.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(parentTop);
  await expect(page.getByText('Parent state marker')).toBeVisible();
});
