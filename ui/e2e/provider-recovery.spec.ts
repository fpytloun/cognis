import { expect, test, type Page } from '@playwright/test';
import type { TimelineItem } from '../src/lib/chat-v2/types';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';

async function recoveryChat(page: Page, waiting: boolean, multiple: boolean) {
  page.on('pageerror', error => console.error(error.stack));
  page.on('console', message => {
    if (message.type() === 'error') console.error(message.text());
  });
  const id = 'provider-recovery';
  const sid = 'recovery-session';
  const model = 'claude-sonnet-4';
  const now = new Date().toISOString();
  const conversation = {
    conversation_id: id, agent_id: 'recovery-agent', title: 'Provider recovery',
    status: 'active', active_session_id: sid, agent_profile_id: 'developer',
    context: { type: 'web', platform_data: {}, memory_labels: {} },
    created_at: now, updated_at: now
  };
  const agent = {
    agent_id: conversation.agent_id, name: 'LaForge', display_name: 'LaForge',
    agent_type: 'primary', status: 'active', default_agent_profile_id: 'developer',
    agent_profiles: { developer: { profile_id: 'developer' } },
    capabilities: { memory_backend: 'none', guardrails_backend: 'none' },
    llm_config: { model, provider_id: 'anthropic' }
  };
  const runtime = {
    runtime_epoch: 'recovery-runtime', runtime_revision: 1, generated_at: now,
    has_active_turn: waiting, volatile_items: [],
    authority: {
      direct_request_id: 'recovery-request', turn_id: 'recovery-turn', fencing_token: 3,
      lifecycle: waiting ? 'recoverable' : 'terminal'
    },
    active_turn: waiting ? {
      turn_id: 'recovery-turn', session_id: sid, status: 'waiting',
      retry_reason: 'rate_limit', retry_at: new Date(Date.now() + 3600000).toISOString(),
      provider_id: 'anthropic', model
    } : null
  };
  const items: TimelineItem[] = [{
    id: 'user:recovery', kind: 'message', role: 'user', turn_id: 'recovery-turn',
    sort_key: '0000:000000000000000:000000:00:000000000',
    source_refs: [], stable: true, attachments: [], partial: false,
    message_id: 'user:recovery', content: 'Continue the task', created_at: now,
  }, {
    id: 'assistant:progress', kind: 'message', role: 'assistant', turn_id: 'recovery-turn',
    sort_key: '0000:000000000000000:000001:00:000000000',
    source_refs: [], stable: true, attachments: [], partial: false,
    message_id: 'assistant:progress', content: 'Checking the task.', created_at: now,
  }, {
    id: 'system:model-error', kind: 'message', role: 'system', turn_id: 'recovery-turn',
    sort_key: '0000:000000000000001:000000:00:000000000',
    source_refs: [], stable: true, message_id: 'system:model-error',
    attachments: [], status: 'complete', partial: false,
    content: 'The model is temporarily unavailable.', notice_kind: 'model_error',
    notice_scope: 'failed_turn', reason_class: waiting ? 'rate_limit' : 'server_error',
    provider_id: 'anthropic', model, recoverable: true, created_at: now
  }, ...(!waiting ? [{
    id: 'error:recovery-turn', kind: 'error', turn_id: 'recovery-turn',
    sort_key: '0000:000000000000002:000000:00:000000000',
    source_refs: [], stable: true, level: 'error', title: 'Turn failed',
    message: 'The model stopped producing output before this turn finished.',
    error_detail: 'Provider message: Inference limit state unavailable',
    error_code: 'step_failed', recoverable: true
  } as TimelineItem] : [])];
  const snapshot = {
    schema_version: 2, projection_version: 'provider-recovery-v1', conversation,
    timeline: { items, has_more_before: false, before_cursor: null },
    state: { state_version: 1, snapshot_generated_at: now, capabilities: [], active_turn: {}, pending: {}, active_session: {} },
    queue: { messages: [], queued_count: 0 }, runtime, cursor: 'recovery-cursor', server_time: now
  };
  const commands: string[] = [];
  let retries = 0;
  let sendFrame: (() => void) | null = null;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) {
      await route.continue();
      return;
    }
    let body: unknown = [];
    if (path === '/api/auth/me') body = { email: 'acceptance@cognis.local', name: 'Acceptance', role: 'admin' };
    else if (path === '/api/health') body = { status: 'ok', services: {}, llm_available: true };
    else if (path === '/api/bootstrap-status') body = { has_users: true, needs_bootstrap: false };
    else if (path === '/api/v1/agents') body = [agent];
    else if (path === `/api/v1/agents/${agent.agent_id}`) body = agent;
    else if (path.endsWith('/sidebar')) body = {
      agents: [agent], agent_direct_chats: [], context_types: ['web'],
      conversations: { items: [conversation], next_cursor: null, has_more: false },
      background_work: { items: [], truncated: false }
    };
    else if (path === '/api/v1/conversations') body = { items: [conversation], next_cursor: null, has_more: false };
    else if (path === `/api/v1/conversations/${id}`) body = conversation;
    else if (path.endsWith('/snapshot') || path.endsWith('/snapshot/cache-only')) body = snapshot;
    else if (path.endsWith('/sync')) body = {
      schema_version: 2, projection_version: snapshot.projection_version,
      conversation_id: id, cursor_before: snapshot.cursor, cursor_after: snapshot.cursor,
      ops: items.map(item => ({ op: 'upsert_item', item })), runtime,
      reset_required: false, has_more: false, server_time: now
    };
    else if (path.endsWith('/retry') && route.request().method() === 'POST') {
      retries += 1;
      runtime.has_active_turn = true;
      runtime.active_turn = { turn_id: 'retry-turn', session_id: sid, status: 'running',
        retry_reason: 'manual_retry', retry_at: now, provider_id: 'anthropic', model };
      runtime.authority.turn_id = 'retry-turn';
      runtime.authority.lifecycle = 'active';
      runtime.runtime_revision += 1;
      body = { schema_version: 2, conversation_id: id, turn_id: 'recovery-turn',
        client_txn_id: route.request().postDataJSON().client_txn_id,
        status: 'accepted', runtime, server_time: now };
    }
    else if (path.endsWith('/retry-provider-options')) body = {
      turn_id: 'recovery-turn', provider_id: 'anthropic', model,
      options: [
        { provider_id: 'anthropic-work', display_name: 'Work', model },
        ...(multiple ? [{ provider_id: 'anthropic-backup', display_name: 'Backup', model }] : [])
      ]
    };
    else if (path.includes('/commands/')) {
      const content = route.request().postDataJSON().content as string;
      commands.push(content);
      body = {
        schema_version: 2, conversation_id: id, session_id: sid,
        client_txn_id: path.split('/').slice(-1)[0], result_type: 'system_message', text: 'Model switched.',
        data: { runtime_selection: { revision: 1, model, provider_id: content.split(' ')[1].split('/')[0] } },
        server_time: now
      };
    } else if (path.endsWith('/sessions')) body = [{ session_id: sid, agent_id: agent.agent_id, status: 'active', created_at: now }];
    else if (path.endsWith('/work')) body = { items: [], categories: [], page_info: { has_more: false } };
    else if (path.includes('/queue')) body = [];
    else if (path.includes('/activity-overview')) body = { workstreams: [], has_more: false };
    else if (path.includes('/llm/providers')) body = [{ provider_id: 'anthropic', enabled: true }];
    await route.fulfill({ json: body });
  });
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    sendFrame = () => ws.send(JSON.stringify({
      type: 'chat_v2_frame', schema_version: 2, projection_version: snapshot.projection_version,
      scope: { key: `conversation:${id}`, kind: 'conversation', conversation_id: id },
      conversation_id: id, cursor_before: snapshot.cursor, cursor_after: snapshot.cursor,
      ops: items.map(item => ({ op: 'upsert_item', item })), runtime, server_time: now,
    }));
  });
  return {
    commands, runtime, items, retries: () => retries,
    publish: () => { runtime.runtime_revision += 1; sendFrame?.(); },
  };
}

test.use({ serviceWorkers: 'block' });
for (const width of [390, 1440]) {
  test(`switches from yellow rate-limit wait at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const { commands } = await recoveryChat(page, true, false);
    await page.goto('/chat/provider-recovery');
    const bar = page.locator('div').filter({ has: page.getByText(/Waiting to retry at/, { exact: false }) }).filter({ has: page.getByRole('button', { name: 'Switch to Work' }) }).last();
    await expect(bar.getByRole('button', { name: 'Switch to Work' })).toBeVisible();
    await page.screenshot({ path: `/tmp/provider-recovery-yellow-${width}.png` });
    await bar.getByRole('button', { name: 'Switch to Work' }).click();
    await expect.poll(() => commands).toEqual(['/model anthropic-work/claude-sonnet-4']);
  });
  test(`offers same-model dropdown in current red failure at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const { commands } = await recoveryChat(page, false, true);
    await page.goto('/chat/provider-recovery');
    const select = page.getByRole('combobox', { name: 'Switch provider for the same model' });
    await expect(select).toBeVisible();
    await expect(page.getByText('Model request failed', { exact: true })).toHaveCount(1);
    await expect(page.getByText('Turn failed', { exact: true })).toHaveCount(0);
    await page.screenshot({ path: `/tmp/provider-recovery-red-${width}.png` });
    await select.focus();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await expect.poll(() => commands.length).toBe(1);
    expect(commands[0]).toMatch(/^\/model anthropic-(work|backup)\/claude-sonnet-4$/);
  });
}

test('shows the actual cause and removes the recovery action after retry and reload', async ({ page }) => {
  const fixture = await recoveryChat(page, false, false);
  await page.goto('/chat/provider-recovery');
  const banner = page.getByTestId('recovery-banner');
  await expect(banner).toContainText('Inference limit state unavailable');
  await banner.getByRole('button', { name: 'Retry failed turn' }).click();
  await expect.poll(fixture.retries).toBe(1);
  await expect(banner).toHaveCount(0);
  fixture.runtime.has_active_turn = false;
  fixture.runtime.active_turn = null;
  fixture.runtime.authority.lifecycle = 'terminal';
  fixture.publish();
  await expect(banner).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel('Chat message')).toBeVisible();
  await expect(banner).toHaveCount(0);
});

test('clears controls on live recovery and does not rearm for a late error after a newer message', async ({ page }) => {
  const fixture = await recoveryChat(page, false, false);
  await page.goto('/chat/provider-recovery');
  const banner = page.getByTestId('recovery-banner');
  await expect(banner).toBeVisible();
  fixture.runtime.has_active_turn = true;
  fixture.publish();
  await expect(banner).toHaveCount(0);
  fixture.items.push({
    id: 'user:new', kind: 'message', role: 'user', turn_id: 'new-turn',
    sort_key: '0000:000000000000003:000000:00:000000000',
    source_refs: [], stable: true, attachments: [], partial: false,
    message_id: 'user:new', content: 'New request',
  });
  fixture.runtime.has_active_turn = false;
  fixture.runtime.authority.turn_id = 'new-turn';
  fixture.publish();
  await expect(page.getByText('New request', { exact: true })).toBeVisible();
  fixture.items.push({
    id: 'late:error', kind: 'error', turn_id: 'recovery-turn',
    sort_key: '0000:000000000000004:000000:00:000000000',
    source_refs: [], stable: true, level: 'error', title: 'Old failure',
    error_detail: 'Late old error', recoverable: true,
  });
  fixture.publish();
  await expect(banner).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel('Chat message')).toBeVisible();
  await expect(banner).toHaveCount(0);
});

test('task recovery retries the saved task rather than cloning it and clears the banner', async ({ page }) => {
  await recoveryChat(page, false, false);
  const fixture = await installTaskCockpitFixture(page);
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    json: { email: 'user@test.com', name: 'Tester', role: 'admin', is_active: true },
  }));
  fixture.setStatus('failed');
  let retryCount = 0;
  let rerunCount = 0;
  await page.route(`**/api/v1/tasks/${TASK_ID}/retry`, async route => {
    retryCount += 1;
    fixture.setStatus('running');
    await route.fulfill({ json: { ok: true, task_id: TASK_ID, status: 'running' } });
  });
  await page.route(`**/api/v1/tasks/${TASK_ID}/rerun`, async route => {
    rerunCount += 1;
    await route.fulfill({ status: 409, json: { detail: 'Recovery must not clone the task' } });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  const banner = page.getByTestId('task-stopped');
  await expect(banner).toContainText('Provider connection reset after 3 attempts.');
  await banner.getByRole('button', { name: 'Retry from failed step', exact: true }).click();
  await expect.poll(() => retryCount).toBe(1);
  await expect(banner).toHaveCount(0);
  expect(rerunCount).toBe(0);
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Release safety review' })).toBeVisible();
  await expect(banner).toHaveCount(0);
});
