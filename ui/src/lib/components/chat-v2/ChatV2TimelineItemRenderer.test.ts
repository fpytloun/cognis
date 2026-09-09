import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ChatV2TimelineItemRenderer from './ChatV2TimelineItemRenderer.svelte';
import type { CompactionTimelineItem, MessageTimelineItem, TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';

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

function compaction(status: CompactionTimelineItem['status']): CompactionTimelineItem {
  return {
    id: 'compaction:sess-old',
    kind: 'compaction',
    sort_key: '0001:0000000003:0000:compaction',
    source_refs: [{ ...sourceRefs[0], event_type: 'compaction_summary' }],
    created_at: '2026-01-01T10:00:02Z',
    status,
    stable: status !== 'running',
    session_id: 'sess-new',
    previous_session_id: 'sess-old',
    summary_preview: status === 'failed' ? 'Provider unavailable' : 'No compaction needed',
    method: 'idle_checkpoint',
    turns_compacted: 0,
    trigger: 'pre_turn_auto',
    reason: status === 'failed' ? 'Provider unavailable' : 'context_pressure',
    effective_usage_percentage: 87
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

  it('keeps the running compaction message compact and puts metadata in details', async () => {
    render(ChatV2TimelineItemRenderer, { item: compaction('running'), scope });

    expect(screen.getByText('Reducing conversation history before the next response.')).toBeTruthy();
    expect(screen.queryByText('Show compaction summary')).toBeNull();
    await fireEvent.click(screen.getByText('Details'));
    expect(screen.getByText('pre_turn_auto')).toBeTruthy();
    expect(screen.getByText('context_pressure')).toBeTruthy();
    expect(screen.getByText('87.0% effective')).toBeTruthy();
  });

  it('keeps compaction metadata available after completion', async () => {
    render(ChatV2TimelineItemRenderer, { item: compaction('compacted'), scope });

    await fireEvent.click(screen.getByText('Details'));
    expect(screen.getByText('pre_turn_auto')).toBeTruthy();
    expect(screen.getByText('87.0% effective')).toBeTruthy();
  });

  it('preserves user messages and gives system messages a compact activity row', () => {
    const user = render(ChatV2TimelineItemRenderer, { item: message('user', '# User Markdown'), scope });
    expect(user.container.querySelector('[data-role="user"] .prose-user h1')?.textContent).toBe('User Markdown');
    expect(user.container.querySelector('[data-role="user"] .prose-user')?.classList).toContain('prose-invert');
    user.unmount();

    const system = render(ChatV2TimelineItemRenderer, { item: message('system', 'System notice'), scope });
    expect(screen.getByText('System notice', { selector: 'p' })).toBeTruthy();
    expect(system.container.querySelector('article')).toBeTruthy();
    expect(screen.queryByText('Details')).toBeNull();
  });

  it('does not offer details when they only repeat normalized notice text', () => {
    const notice = [
      'Agent profile switched to: developer-senior',
      'Cleared /model, /thinking, and /fast overrides. Takes effect on next message.'
    ].join('\n');

    render(ChatV2TimelineItemRenderer, { item: message('system', notice), scope });

    expect(screen.getByText(notice.replace('\n', ' '))).toBeTruthy();
    expect(screen.queryByText('Details')).toBeNull();
  });

  it('labels a follow-up turn boundary without a generic system title', () => {
    const turnInitiated = {
      ...message('system', 'Turn initiated by other: Agent work finished.'),
      notice_kind: 'turn_initiated',
      notice_scope: 'turn',
    };

    render(ChatV2TimelineItemRenderer, { item: turnInitiated, scope });

    expect(screen.getByText('Turn initiated')).toBeTruthy();
    expect(screen.queryByText('System notice')).toBeNull();
  });

  it('keeps the complete technical model error in expandable details', async () => {
    const technicalError =
      'Provider message: peer closed connection without sending complete message body (incomplete chunked read)';
    const systemError = {
      ...message('system', technicalError),
      notice_kind: 'model_error',
      notice_scope: 'failed_turn',
      reason_class: 'transport',
      recoverable: true
    };

    render(ChatV2TimelineItemRenderer, { item: systemError, scope });

    expect(screen.getByText('Model request failed')).toBeTruthy();
    await fireEvent.click(screen.getByText('Details'));
    expect(screen.getByText(technicalError)).toBeTruthy();
  });

  it('hides expired and legacy retry notices without an active expiry', () => {
    const expiredRetry = {
      ...message('system', 'Expired provider retry details'),
      notice_kind: 'model_recovery',
      notice_scope: 'retry',
      retry_at: '2000-01-01T00:00:00Z',
      attempt: 1,
      max_attempts: 3
    };
    const expired = render(ChatV2TimelineItemRenderer, { item: expiredRetry, scope });
    expect(expired.container.querySelector('article')).toBeNull();
    expired.unmount();

    const compatibleRetry = {
      ...expiredRetry,
      id: 'message:retry-without-expiry',
      retry_at: null
    };
    const compatible = render(ChatV2TimelineItemRenderer, { item: compatibleRetry, scope });
    expect(compatible.container.querySelector('article')).toBeNull();
    compatible.unmount();

    const activeRetry = {
      ...expiredRetry,
      id: 'message:active-retry',
      retry_at: '2999-01-01T00:00:00Z'
    };
    const active = render(ChatV2TimelineItemRenderer, { item: activeRetry, scope });
    expect(active.container.querySelector('article')).toBeTruthy();
    expect(screen.getByText('Connection interrupted')).toBeTruthy();
  });

  it('renders a runtime-shaped retry without expiry in the pinned slot', () => {
    const runtimeRetry = {
      ...message('system', 'Current provider retry details'),
      notice_kind: 'model_recovery',
      notice_scope: 'retry',
      retry_at: null,
      stable: false,
    };

    const pinned = render(ChatV2TimelineItemRenderer, {
      item: runtimeRetry,
      scope,
      pinnedTransient: true,
    });

    expect(pinned.container.querySelector('article')).toBeTruthy();
    expect(screen.getByText('Connection interrupted')).toBeTruthy();
  });

  it('does not truncate long notice text', () => {
    const longNotice = `Technical detail ${'x'.repeat(300)}`;
    render(ChatV2TimelineItemRenderer, { item: message('system', longNotice), scope });

    expect(screen.getByText(longNotice)).toBeTruthy();
  });

  it.each([
    ['failed', 'Compaction failed', 'failed'],
    ['skipped', 'Compaction skipped', 'skipped']
  ] satisfies Array<[CompactionTimelineItem['status'], string, string]>)(
    'labels %s compaction without claiming success',
    (status, title, badge) => {
      render(ChatV2TimelineItemRenderer, { item: compaction(status), scope });

      expect(screen.getByText(title)).toBeTruthy();
      expect(screen.getByText(badge)).toBeTruthy();
      expect(screen.queryByText('Session compacted')).toBeNull();
    }
  );

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
