import { expect, test } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';
import type { ChatSnapshot, ChatRealtimeFrame, RuntimeOverlaySnapshot, TimelineItem, MessageTimelineItem } from '../src/lib/chat-v2/types';

test.use({ serviceWorkers: 'block', viewport: { width: 390, height: 844 } });

const message: MessageTimelineItem = {
  id: 'stream-message', message_id: 'stream-message', kind: 'message', role: 'assistant',
  content: '![', sort_key: '2', source_refs: [], stable: false, partial: true,
  status: 'running', turn_id: 'stream-turn', attachments: [],
};
const runtime: RuntimeOverlaySnapshot = {
  runtime_epoch: 'stream-epoch', runtime_revision: 1, generated_at: '2026-10-07T18:00:00Z',
  has_active_turn: true, active_turn: { turn_id: 'stream-turn', session_id: 'stream-session', status: 'running' },
  volatile_items: [],
};

test('streams Markdown and fades only new text; keeps group progress through thinking', async ({ page }) => {
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/streaming.html`);
  await page.waitForFunction(() => 'streamingFixture' in window);
  const update = (items: TimelineItem[], overlay: RuntimeOverlaySnapshot | null = runtime) =>
    page.evaluate(({ items, overlay }) => {
      (window as typeof window & { streamingFixture: (items: TimelineItem[], runtime: RuntimeOverlaySnapshot | null) => void })
        .streamingFixture(items, overlay);
    }, { items, overlay });

  const shell = page.getByTestId('streaming-fixture');
  await update([message]);
  await expect(shell).not.toContainText('![');
  await update([{ ...message, content: 'Hello' }]);
  await expect(shell.locator('.chat-markdown')).toHaveText('Hello');
  await update([{ ...message, content: 'Hello **new words**' }]);
  await expect(shell.locator('strong')).toHaveText('new words');
  await expect(shell.locator('strong .stream-reveal')).toHaveText(['new ', 'words']);
  await expect(shell.locator('.stream-reveal').first()).toHaveCSS('animation-duration', '0.32s');
  await expect(shell.locator('p')).toHaveText('Hello new words');
  expect(await shell.locator('p').evaluate((node) => getComputedStyle(node, '::after').content))
    .toBe('none');
  await update([{ ...message, content: 'Done.', partial: false, stable: true, status: 'complete' }], null);
  await expect(shell.locator('.stream-reveal')).toHaveCount(0);

  const tool: TimelineItem = {
    id: 'tool:stream-call', kind: 'tool_call', call_id: 'stream-call', tool_name: 'web_search',
    turn_id: 'stream-turn', sort_key: '1', source_refs: [], stable: true, status: 'complete',
    attachments: [], file_diffs: [], is_error: false, truncated: false, has_full_output: false,
  };
  await update([tool]);
  await expect(shell.locator('.running-label')).toHaveCount(1);
  await expect(shell.locator('.running-label')).toHaveCSS('animation-duration', '1.6s');
  const sweep = await shell.locator('.running-label').evaluate((node) => getComputedStyle(node).backgroundImage);
  expect(sweep).toMatch(/\/ 0\.4|rgba\([^)]*, 0\.4\)/);
  await update([tool, {
    id: 'thinking', kind: 'thinking', sort_key: '2', source_refs: [], stable: false,
    status: 'running', turn_id: 'stream-turn', blocks: [],
  }]);
  await expect(shell.locator('.running-label')).toHaveCount(1);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(shell.locator('.running-label')).toHaveCSS('animation-name', 'none');
  await update([tool], { ...runtime, has_active_turn: false, active_turn: null });
  await expect(shell.locator('.running-label')).toHaveCount(0);
});

test('shows every incoming prefix, hides delimiters, and pauses only the typing indicator', async ({ page }) => {
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/streaming.html`);
  await page.waitForFunction(() => 'streamingFixture' in window);
  const update = (item: MessageTimelineItem) => page.evaluate(({ item, runtime }) => {
    (window as typeof window & {
      streamingFixture: (items: TimelineItem[], overlay: RuntimeOverlaySnapshot | null) => void
    }).streamingFixture([item], runtime);
  }, { item, runtime });
  const content = page.getByTestId('streaming-fixture').locator('.chat-markdown');
  for (const prefix of ['**Yes', '**Yes—that', '**Yes—that is a', '**Yes—that is a useful case']) {
    await update({ ...message, content: prefix, text_streaming: true });
    await expect(content).toHaveText(prefix.slice(2));
    await expect(content.locator('strong')).toHaveText(prefix.slice(2));
    await page.waitForTimeout(100);
    await expect(content).not.toContainText('**');
  }
  const typing = page.getByTestId('streaming-fixture').getByText('Streaming', { exact: true });
  await expect(typing).toHaveCount(1);
  await update({ ...message, content: '**Yes—that is a useful case', text_streaming: false });
  await expect(typing).toHaveCount(0);
  await expect(content).toHaveText('Yes—that is a useful case');
  await expect(content.locator('strong')).toHaveText('Yes—that is a useful case');
  await update({ ...message, content: '**Yes—that is a useful case**. More text.', text_streaming: true });
  await expect(typing).toHaveCount(1);
  await expect(content).toHaveText('Yes—that is a useful case. More text.');
  await update({ ...message, content: '**Yes—that is a useful case**. More text.', stable: true, partial: false, status: 'complete' });
  await expect(typing).toHaveCount(0);
  await expect(content.locator('strong')).toHaveText('Yes—that is a useful case');
});

test('renders cumulative realtime frames before canonical completion on mobile', async ({ page }) => {
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/streaming.html`);
  await page.waitForFunction(() => 'streamingSnapshot' in window);
  const snapshot: ChatSnapshot = {
    schema_version: 2, projection_version: 'streaming-test-v1', cursor: 'streaming-cursor',
    conversation: {
      conversation_id: 'streaming-conversation', agent_id: 'streaming-agent', title: 'Streaming',
      status: 'active', active_session_id: 'stream-session',
    },
    timeline: { items: [], has_more_before: false, before_cursor: null },
    state: { state_version: 1, snapshot_generated_at: '2026-10-08T07:00:00Z', capabilities: [], active_turn: {}, pending: {}, active_session: {} },
    queue: { messages: [], queued_count: 0 }, runtime: {
      ...runtime, runtime_revision: 0,
      authority: {
        direct_request_id: 'stream-request', turn_id: 'stream-turn', fencing_token: 3,
        lifecycle: 'active', source_epoch: 'stream-source', source_revision: 0,
      },
    },
    server_time: '2026-10-08T07:00:00Z',
  };
  await page.evaluate(snapshot => {
    (window as typeof window & { streamingSnapshot: (snapshot: ChatSnapshot) => void }).streamingSnapshot(snapshot);
  }, snapshot);
  const content = page.getByTestId('streaming-fixture').locator('.chat-markdown');
  const prefixes = ['**Yes', '**Yes—that', '**Yes—that is a', '**Yes—that is a useful case'];
  for (let index = 0; index < prefixes.length; index++) {
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame', schema_version: 2, projection_version: snapshot.projection_version,
      conversation_id: snapshot.conversation.conversation_id,
      cursor_before: snapshot.cursor, cursor_after: snapshot.cursor, ops: [],
      runtime: {
        ...runtime, runtime_revision: index + 1,
        authority: { ...snapshot.runtime.authority!, source_revision: index + 1 },
        volatile_items: [{ ...message, content: prefixes[index], text_streaming: true }],
      }, server_time: snapshot.server_time,
    };
    await page.evaluate(frame => {
      (window as typeof window & { streamingFrame: (frame: ChatRealtimeFrame) => void }).streamingFrame(frame);
    }, frame);
    await expect(content).toHaveText(prefixes[index].slice(2));
    await expect(content.locator('strong')).toHaveText(prefixes[index].slice(2));
  }
});
