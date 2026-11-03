import { expect, test, type Page } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';
import type { MessageTimelineItem, ToolCallTimelineItem, TimelineItem, RuntimeOverlaySnapshot } from '../src/lib/chat-v2/types';

const assistant: MessageTimelineItem = {
  id: 'motion-assistant', message_id: 'motion-assistant', kind: 'message', role: 'assistant',
  content: 'Hello world', sort_key: '2', source_refs: [], stable: false,
  partial: true, status: 'running', attachments: [], turn_id: 'motion-turn',
};
const tool: ToolCallTimelineItem = {
  id: 'motion-tool', call_id: 'motion-call', kind: 'tool_call', tool_name: 'bash',
  sort_key: '3', source_refs: [], stable: false, status: 'running', arguments: { command: 'git status' },
  result_preview: null, is_error: false, duration_ms: null, attachments: [], file_diffs: [],
  streamed_output: null, truncated: false, has_full_output: false,
  evaluation: null, assistant_phase_index: 0, turn_cycle_index: 0, turn_id: 'motion-turn',
};
async function update(page: Page, items: TimelineItem[]) {
  await page.evaluate(async items => {
    const runtime: RuntimeOverlaySnapshot = {
      runtime_epoch: 'motion-preview', runtime_revision: 1, generated_at: '2026-10-08T08:00:00Z',
      has_active_turn: items.some(item => item.status === 'running' || item.status === 'pending'),
      active_turn: { turn_id: 'motion-turn', session_id: 'motion-session', status: 'running' },
      cycle_states: items.some(item => item.kind === 'tool_call') ? [{
        turn_id: 'motion-turn', turn_cycle_index: 0, lifecycle_status: 'open', has_tool_activity: true,
      }] : [], volatile_items: [],
    };
    (window as typeof window & { streamingFixture: (items: TimelineItem[], runtime: RuntimeOverlaySnapshot) => void })
      .streamingFixture(items, runtime);
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, items);
}

test.beforeEach(async ({ page }) => {
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/streaming.html`);
  await page.waitForFunction(() => 'streamingFixture' in window);
});

test('selected message and word motion only animates new arrivals', async ({ page }) => {
  await update(page, [{ ...assistant, id: 'history', message_id: 'history', status: 'complete', stable: true, partial: false }]);
  await expect(page.locator('.chat-markdown')).toHaveText('Hello world');
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
  await update(page, [assistant]);
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(1);
  await expect(page.locator('.stream-reveal')).toHaveCount(2);
  expect(await page.locator('.stream-reveal').first().evaluate(node => getComputedStyle(node).animationDuration)).toBe('0.32s');
  await page.waitForTimeout(350);
  await update(page, [{ ...assistant, content: 'Hello world more words' }]);
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
  expect(await page.locator('.stream-reveal').allTextContents()).toEqual(['more ', 'words']);
  await update(page, [{ ...assistant, text_streaming: false, content: 'Hello world more words' }]);
  await update(page, [{ ...assistant, content: 'Hello world more words after pause' }]);
  expect(await page.locator('.stream-reveal').allTextContents()).toEqual(['after ', 'pause']);
  await update(page, [{ ...assistant, content: 'Hello world more words after pause\n\n```\nliteral_code\n```' }]);
  await expect(page.locator('code .stream-reveal')).toHaveCount(0);
});

test('group sweep, disclosure and real status handoff use the selected timings', async ({ page }) => {
  await update(page, [tool]);
  const header = page.locator('[data-kind="tool_group"] > button');
  const label = header.locator('.running-label');
  await expect(label).toBeVisible();
  expect(await label.evaluate(node => getComputedStyle(node).animationDuration)).toBe('1.6s');
  expect(await header.evaluate(node => getComputedStyle(node).transitionDuration)).toBe('0.24s, 0.24s, 0.24s');
  await header.click();
  await expect.poll(() => page.evaluate(() =>
    document.getAnimations().some(a => a.effect?.getTiming().duration === 180)
  )).toBe(true);
  await expect(header).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('[data-kind="tool_call"]')).toBeVisible();
  await page.waitForTimeout(250);
  await header.click();
  await expect(header).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('[data-kind="tool_call"]')).toHaveCount(0);
  await update(page, [{ ...tool, status: 'complete', stable: true, result_preview: 'Done' }]);
  await expect(header).toHaveAttribute('data-motion-state', 'complete');
  await expect(label).toHaveCount(0);
  await page.waitForTimeout(260);
  expect(await header.evaluate(node => getComputedStyle(node).color)).toBe('rgb(221, 214, 254)');
});

test('tool-category colors survive completion and failure while details keep status', async ({ page }) => {
  for (const [toolName, color] of [
    ['bash', 'rgb(221, 214, 254)'],
    ['delegate', 'rgb(253, 230, 138)'],
    ['read', 'rgb(186, 230, 253)'],
    ['memory_add', 'rgb(245, 208, 254)'],
  ]) {
    const item = { ...tool, tool_name: toolName };
    await update(page, [item]);
    const header = page.locator('[data-kind="tool_group"] > button');
    await expect(header).toHaveCSS('color', color);
    for (const status of ['complete', 'failed', 'denied'] as const) {
      await update(page, [{ ...item, status, stable: true, is_error: status !== 'complete' }]);
      await expect(header).toHaveCSS('color', color);
      await expect(header).toHaveAttribute('data-motion-state', status);
      if (status === 'failed' && toolName === 'bash') await expect(header).toContainText('1 failed');
    }
  }
});

test('user delivery badges remain but sent messages have no success dot', async ({ page }) => {
  const user: MessageTimelineItem = {
    ...assistant, id: 'motion-user', message_id: 'motion-user', role: 'user',
    content: 'User message', status: 'pending', partial: false,
  };
  await update(page, [user]);
  const message = page.locator('[data-kind="message"]');
  await expect(message).toContainText('sending');
  await update(page, [{ ...user, status: 'failed' }]);
  await expect(message).toContainText('failed');
  await update(page, [{
    ...user, status: 'complete', stable: true,
  }]);
  await expect(message).toContainText('User message');
  await expect(message.locator('.chat-delivery-dot')).toHaveCount(0);
  await expect(message.locator('[title="Sent"]')).toHaveCount(0);
});

test('reduced motion keeps content immediate and disables decorative movement', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await update(page, [assistant, tool]);
  await expect(page.locator('.chat-markdown')).toHaveText('Hello world');
  await expect(page.locator('.stream-reveal')).toHaveCount(0);
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
  const header = page.locator('[data-kind="tool_group"] > button');
  expect(await header.locator('.running-label').evaluate(node => getComputedStyle(node).animationName)).toBe('none');
  await header.click();
  await expect(page.locator('[data-kind="tool_call"]')).toBeVisible();
  expect(await page.evaluate(() => document.getAnimations().some(a => a.effect?.getTiming().duration === 180))).toBe(false);
});

test('historical partial replies never enter again when paginated or restored', async ({ page }) => {
  const current = { ...assistant, stable: true, partial: false, status: 'complete' as const };
  await update(page, [current]);
  const older = {
    ...assistant, id: 'old-partial', message_id: 'old-partial', sort_key: '1',
    content: 'Older stopped response', stable: true, status: 'cancelled' as const, partial: true,
  };
  await update(page, [older, current]);
  await expect(page.locator('.chat-markdown').first()).toHaveText('Older stopped response');
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
  await update(page, []);
  await update(page, [older, current]);
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
});

test('rapid disclosure reversal restores focusability and still unmounts on close', async ({ page }) => {
  await update(page, [tool]);
  const group = page.locator('[data-kind="tool_group"]');
  const header = group.locator(':scope > button');
  await header.click();
  await page.waitForTimeout(240);
  await header.click();
  await expect(group.locator(':scope > div')).toHaveAttribute('inert', '');
  await header.click();
  await expect(header).toHaveAttribute('aria-expanded', 'true');
  await expect(group.locator(':scope > div')).not.toHaveAttribute('inert', '');
  await page.waitForTimeout(240);
  await header.click();
  await expect(group.locator(':scope > div')).toHaveCount(0);
});

test('grouped live arrival enters once without replaying after fold and reopen', async ({ page }) => {
  await page.evaluate(() => {
    (window as typeof window & { streamingGroups: (enabled: boolean) => void }).streamingGroups(true);
  });
  await update(page, [tool]);
  await update(page, [{ ...assistant, assistant_phase_index: 0, turn_cycle_index: 0 }, tool]);
  await expect(page.locator('[data-kind="activity_segment_assistant"] .chat-markdown')).toHaveText('Hello world');
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(1);
  await page.waitForTimeout(300);
  const header = page.locator('[data-kind="activity_segment"] > button');
  await header.click();
  await page.waitForTimeout(250);
  await header.click();
  await expect(page.locator('[data-kind="activity_segment_assistant"] .chat-markdown')).toHaveText('Hello world');
  expect(await page.evaluate(() => document.getAnimations().filter(a => a.effect?.getTiming().duration === 280).length)).toBe(0);
});
