import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block' });

test('hook autocomplete shows disabled status and inserts stable ID', async ({ page }) => {
  await installTaskCockpitFixture(page);
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' }
  }));
  await page.route('**/api/health**', route => route.fulfill({ json: { status: 'ok', providers: {} } }));
  await page.routeWebSocket('**/api/ws', socket => {
    socket.send(JSON.stringify({ type: 'authenticated' }));
  });
  const conversation = {
    conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', user_email: 'owner@cognis.test',
    context: { type: 'web', ref: null, platform_data: {} }, title: 'Manual hooks',
    status: 'active', is_readonly_for_caller: false, can_continue: false,
    created_at: '2026-10-17T09:00:00Z', updated_at: '2026-10-17T09:00:00Z',
    last_message_at: '2026-10-17T09:00:00Z', has_active_turn: false, visibility: 'private',
  };
  await page.route('**/api/v1/conversations/conv-task-chat', route => route.fulfill({ json: conversation }));
  await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/snapshot', route => route.fulfill({ json: {
    schema_version: 2, projection_version: 'hooks',
    scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' },
    conversation, timeline: { items: [{
      id: 'hook:compact-card', kind: 'notice', level: 'info', sort_key: '0001',
      source_refs: [], stable: true, title: 'Consolidate memories',
      hook: { run_id: 'compact-card', hook_id: 'system:self-improvement',
        trigger: 'conversation_idle', status: 'completed', child_session_id: 'session-compact-card',
        outcome: 'Updated reusable procedures.\n\nFull detailed outcome with **references**.' },
    }], has_more_before: false, before_cursor: null },
    runtime: { active_session_id: null, active_turn_id: null, active_turn_state: null, has_active_turn: false, volatile_items: [] },
    cursor: 'hooks', server_time: '2026-10-17T09:00:00Z',
  } }));
  await page.route('**/slash-command-suggestions?**', route => route.fulfill({ json: { items: [{
    kind: 'parameter', command: '/hook', value: 'system:self-improvement',
    label: 'Learn reusable skills (disabled)', description: 'Disabled — manual only: Preserve useful procedures',
    insert_text: '/hook system:self-improvement', suffix: 'none', badges: [],
  }] } }));
  await page.goto('/chat/conv-task-chat');
  const composer = page.locator('textarea[placeholder*="Message"], textarea[placeholder*="message"]').first();
  await composer.fill('/hoo');
  await expect(page.getByText('List or manually run an assigned hook', { exact: true })).toBeVisible();
  await composer.fill('/hook Learn reusable');
  await page.getByRole('option', { name: /Learn reusable skills \(disabled\).*Disabled — manual only/ }).click();
  await expect(composer).toHaveValue('/hook system:self-improvement');
  const card = page.getByRole('region', { name: 'Background hook' });
  await expect(card.getByText('Idle hook')).toBeVisible();
  await expect(card.getByRole('button', { name: 'Open session' })).toBeVisible();
  await expect(card.locator('.chat-markdown')).toHaveCount(0);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = await card.boundingBox();
    expect(bounds!.width).toBeLessThanOrEqual(640);
    expect(bounds!.height).toBeLessThanOrEqual(64);
    expect(await card.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  }
  await card.getByRole('button', { name: 'Expand' }).click();
  await expect(card.getByRole('button', { name: 'Open session' })).toBeVisible();
  await expect(card.locator('.chat-markdown')).toContainText('Full detailed outcome');
  await card.getByRole('button', { name: 'Collapse' }).click();
  await expect(card.locator('.chat-markdown')).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Open session' })).toBeVisible();
});
