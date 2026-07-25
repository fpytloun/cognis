import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ChatV2TimelineItemRenderer from './ChatV2TimelineItemRenderer.svelte';
import type { MessageTimelineItem, TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';

const sourceRefs = [{ store: 'intaris', session_id: 'sess_1', seq: 1, event_type: 'message' }];
const scope: TimelineScope = {
  key: 'conversation:conv_1',
  kind: 'conversation',
  conversation_id: 'conv_1',
};

function message(role: MessageTimelineItem['role'], content: string): MessageTimelineItem {
  return {
    id: `message:${role}`,
    kind: 'message',
    sort_key: `0001:0000000001:0000:message:${role}`,
    source_refs: sourceRefs,
    created_at: '2026-01-01T10:00:00Z',
    status: 'complete',
    stable: true,
    role,
    content,
    message_id: `msg_${role}`,
    attachments: [],
    partial: false,
  };
}

function tool(): ToolCallTimelineItem {
  return {
    id: 'tool:call_1',
    kind: 'tool_call',
    sort_key: '0001:0000000002:0000:tool:call_1',
    source_refs: [{ ...sourceRefs[0], event_type: 'tool_result' }],
    created_at: '2026-01-01T10:00:01Z',
    status: 'complete',
    stable: true,
    call_id: 'call_1',
    tool_name: 'grep',
    arguments: { pattern: 'canonical' },
    result_preview: 'matched',
    streamed_output: 'matched',
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: true,
    has_full_output: true,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ChatV2TimelineItemRenderer shared presentation boundary', () => {
  it.each([
    ['conversation', scope],
    ['session', { key: 'session:sess_1', kind: 'session', session_id: 'sess_1', conversation_id: 'conv_1' }],
    ['task_step', { key: 'task_step:run_1', kind: 'task_step', task_id: 'task_1', step_run_id: 'run_1' }],
  ] satisfies Array<[string, TimelineScope]>)(
    'renders ChatMessage and ToolCallBlock leaves for %s scope',
    async (_name, timelineScope) => {
      const renderedMessage = render(ChatV2TimelineItemRenderer, {
        item: message('assistant', '**Established** presentation'),
        scope: timelineScope,
      });
      expect(renderedMessage.container.querySelector('[data-kind="message"][data-role="assistant"]')).toBeTruthy();
      expect(renderedMessage.container.querySelector('.chat-markdown strong')?.textContent).toBe('Established');
      renderedMessage.unmount();

      const renderedTool = render(ChatV2TimelineItemRenderer, { item: tool(), scope: timelineScope });
      expect(renderedTool.container.querySelector('[data-kind="tool_call"] article')).toBeTruthy();
      await fireEvent.click(screen.getByRole('button', { name: /grep canonical/i }));
      expect(screen.getByText('matched')).toBeTruthy();
      expect(screen.getByText(/Open full output/i)).toBeTruthy();
    },
  );

  it('preserves established user and system message DOM', () => {
    const user = render(ChatV2TimelineItemRenderer, { item: message('user', '# User Markdown'), scope });
    expect(user.container.querySelector('[data-role="user"] .prose-user h1')?.textContent).toBe('User Markdown');
    user.unmount();

    const system = render(ChatV2TimelineItemRenderer, { item: message('system', 'System notice'), scope });
    expect(system.container.querySelector('p.text-center')?.textContent).toBe('System notice');
  });

  it('isolates identical item ids across scopes and tears down cleanly before terminalization', () => {
    const conversation = render(ChatV2TimelineItemRenderer, {
      item: { ...message('assistant', '**conversation stream**'), id: 'shared-id', stable: false, status: 'running' },
      scope,
    });
    const taskStep = render(ChatV2TimelineItemRenderer, {
      item: { ...message('assistant', '**task stream**'), id: 'shared-id', stable: false, status: 'running' },
      scope: { key: 'task_step:run_1', kind: 'task_step', task_id: 'task_1', step_run_id: 'run_1' },
    });

    expect(conversation.container.querySelector('.chat-markdown strong')?.textContent).toBe('conversation stream');
    expect(taskStep.container.querySelector('.chat-markdown strong')?.textContent).toBe('task stream');

    conversation.unmount();
    taskStep.unmount();

    const session = render(ChatV2TimelineItemRenderer, {
      item: { ...message('assistant', '**session stream**'), id: 'shared-id', stable: false, status: 'running' },
      scope: { key: 'session:sess_1', kind: 'session', session_id: 'sess_1', conversation_id: 'conv_1' },
    });
    expect(session.container.querySelector('.chat-markdown strong')?.textContent).toBe('session stream');
    expect(session.container.textContent).not.toContain('conversation stream');
    expect(session.container.textContent).not.toContain('task stream');
  });
});
