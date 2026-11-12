import { expect, test } from '@playwright/test';
import type { ScopedFixtureController } from '../src/lib/chat-v2/scoped-fixture';
import type { MessageTimelineItem } from '../src/lib/chat-v2/types';

test.use({
  serviceWorkers: 'block',
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

for (const mobile of [false, true]) {
  test(`managed history separates private, participant and assistant messages (mobile=${mobile})`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1360, height: 900 });
    await page.goto('/scoped-chat-v2-fixture');
    const shell = page.getByTestId('scoped-timeline-shell');
    await expect(shell).toBeVisible();
    const messages: MessageTimelineItem[] = [
      { id: 'private-instruction', role: 'user', content: 'Private managing instruction', message_metadata: { private_controller_instruction: true } },
      { id: 'participant-reply', role: 'user', content: 'Reply from Sofia', message_metadata: { channel: 'web', sender: 'Sofia Novak', untrusted: true } },
      { id: 'assistant-reply', role: 'assistant', content: 'Public assistant response' },
    ].map((message, index): MessageTimelineItem => ({
      ...message, message_id: message.id, kind: 'message', role: message.role as 'user' | 'assistant',
      status: 'complete', partial: false, stable: true, attachments: [],
      sort_key: `0900:00000000000114${index}:000030:02:000000000`,
      source_refs: [{ store: 'intaris', session_id: 'fixture-parent', seq: 1140 + index, event_type: message.role === 'user' ? 'user_message' : 'assistant_message' }],
    }));
    await page.evaluate(items => {
      const controller = (window as typeof window & { __scopedFixtureController?: ScopedFixtureController }).__scopedFixtureController;
      if (!controller) throw new Error('Missing scoped fixture controller');
      const cursor = document.querySelector('[data-testid="scoped-timeline-shell"] [data-scope-key]')?.getAttribute('data-cursor');
      if (!cursor) throw new Error('Missing canonical cursor');
      controller.realtime.emitFrame({
        type: 'chat_v2_frame', schema_version: 2, projection_version: 'fixture-projection-v1',
        scope: { key: 'conversation:fixture-conversation', kind: 'conversation', conversation_id: 'fixture-conversation' },
        conversation_id: 'fixture-conversation', cursor_before: cursor, cursor_after: `${cursor}:provenance`,
        ops: items.map(item => ({ op: 'upsert_item' as const, item })), runtime: null,
        server_time: '2026-10-28T20:00:00Z',
      });
    }, messages);
    const participant = shell.locator('[data-message-origin="participant"]');
    const instruction = shell.locator('[data-message-origin="private-instruction"]');
    await expect(participant).toContainText('Sofia Novak');
    await expect(participant).toContainText('Reply from Sofia');
    await expect(instruction).toContainText('Private instruction · Not shared');
    await expect(shell.getByText('Public assistant response', { exact: true })).toBeVisible();
    const colors = await Promise.all([participant, instruction].map(node =>
      node.evaluate(element => getComputedStyle(element).backgroundColor)
    ));
    expect(colors[0]).not.toBe(colors[1]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: test.info().outputPath(`managed-message-authorship-${mobile}.png`) });
  });
}
