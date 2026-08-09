import { describe, expect, it } from 'vitest';

import { isRenderableChatV2Item, toRenderItem, toRenderItems } from './render-adapter';
import type {
  AssistantDeliverableTimelineItem,
  AuthChallengeTimelineItem,
  CompactionTimelineItem,
  MessageTimelineItem,
  ThinkingTimelineItem,
  TimelineItem,
  ToolCallTimelineItem,
  UserInteractionTimelineItem
} from './types';

function baseRefs(seq = 1) {
  return [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'assistant_message' }];
}

function message(overrides: Partial<MessageTimelineItem> = {}): MessageTimelineItem {
  return {
    id: 'message:1',
    kind: 'message',
    sort_key: '0000:000000000000001:000000:02:000000000',
    source_refs: baseRefs(),
    stable: true,
    role: 'assistant',
    content: 'hello **world**',
    message_id: 'msg-1',
    attachments: [],
    partial: false,
    ...overrides
  };
}

function thinking(overrides: Partial<ThinkingTimelineItem> = {}): ThinkingTimelineItem {
  return {
    id: 'thinking:msg-1:block-1',
    kind: 'thinking',
    sort_key: '0000:000000000000001:000000:01:000000000',
    source_refs: baseRefs(),
    stable: true,
    message_id: 'msg-1',
    blocks: [{ id: 'block-1', title: 'Plan', content: 'thinking text', status: 'complete' }],
    active_title: null,
    ...overrides
  };
}

function toolCall(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool:call-1',
    kind: 'tool_call',
    sort_key: '0000:000000000000001:000000:03:000000000',
    source_refs: baseRefs(),
    stable: true,
    call_id: 'call-1',
    tool_name: 'read',
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: false,
    has_full_output: false,
    ...overrides
  };
}

function assistantDeliverable(
  overrides: Partial<AssistantDeliverableTimelineItem> = {}
): AssistantDeliverableTimelineItem {
  return {
    id: 'assistant-deliverable:dlv-rich',
    kind: 'assistant_deliverable',
    sort_key: '0000:000000000000004:000000:03:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 4, event_type: 'assistant_deliverable' }],
    stable: true,
    deliverable_id: 'dlv-rich',
    format: 'rich',
    title: 'Rich report',
    render_metadata: { presentation: 'rich' },
    export_metadata: null,
    ...overrides
  };
}

function compaction(overrides: Partial<CompactionTimelineItem> = {}): CompactionTimelineItem {
  return {
    id: 'compaction:sess-old',
    kind: 'compaction',
    sort_key: '0000:000000000000004:000000:10:000000000',
    source_refs: baseRefs(4),
    stable: true,
    status: 'compacted',
    session_id: 'sess-new',
    previous_session_id: 'sess-old',
    summary_preview: 'Compacted summary',
    summary: 'Compacted summary details',
    method: 'rotation',
    turns_compacted: 5,
    hard_pressure_exceeded: true,
    used_timeout_fallback: false,
    ...overrides
  };
}

function userInteraction(
  overrides: Partial<UserInteractionTimelineItem> = {}
): UserInteractionTimelineItem {
  return {
    id: 'user-interaction:notif-question',
    kind: 'user_interaction',
    sort_key: '0000:000000000000004:000000:06:000000000',
    source_refs: baseRefs(4),
    stable: true,
    interaction_id: 'notif-question',
    interaction_type: 'step_question',
    origin_call_id: 'call-question',
    title: 'You answered questions',
    answers: [{ question: 'Target?', answer: 'Staging' }],
    status: 'complete',
    ...overrides
  };
}

describe('render-adapter', () => {
  it('renders canonical user interaction answers as a user-side row model', () => {
    const rendered = toRenderItem(userInteraction());

    expect(rendered).toMatchObject({
      id: 'user-interaction:notif-question',
      kind: 'user_interaction',
      originCallId: 'call-question',
      title: 'You answered questions',
      answers: [{ question: 'Target?', answer: 'Staging' }]
    });
  });

  describe('message', () => {
    it('renders markdown and preserves identity/order/attachments', () => {
      const rendered = toRenderItem(message({ attachments: [{ artifact_id: 'a1' }] as never })) as Extract<
        ReturnType<typeof toRenderItem>,
        { kind: 'message' }
      >;
      expect(rendered?.kind).toBe('message');
      expect(rendered?.id).toBe('message:1');
      expect(rendered?.orderKey).toBe('0000:000000000000001:000000:02:000000000');
      expect(rendered?.html).toContain('<strong>world</strong>');
      expect(rendered?.attachments).toHaveLength(1);
    });

    it('is streaming only when unstable and running/partial', () => {
      const stable = toRenderItem(message({ stable: true, status: 'running' }));
      expect(stable?.kind === 'message' && stable.streaming).toBe(false);

      const running = toRenderItem(message({ stable: false, status: 'running' }));
      expect(running?.kind === 'message' && running.streaming).toBe(true);

      const partial = toRenderItem(message({ stable: false, partial: true, status: 'pending' }));
      expect(partial?.kind === 'message' && partial.streaming).toBe(true);
    });

    it('does not reuse markdown output for the same canonical id', () => {
      const first = toRenderItem(message({ id: 'shared-id', content: '**conversation**', stable: false, status: 'running' }));
      const second = toRenderItem(message({ id: 'shared-id', content: '**task step**', stable: false, status: 'running' }));

      expect(first?.kind === 'message' && first.html).toContain('<strong>conversation</strong>');
      expect(second?.kind === 'message' && second.html).toContain('<strong>task step</strong>');
      expect(second?.kind === 'message' && second.html).not.toContain('conversation');
    });

    it('passes through chat mode metadata', () => {
      const rendered = toRenderItem(message({ chat_mode: 'plan', chat_mode_source: 'directive' }));
      expect(rendered?.kind === 'message' && rendered.chatMode).toBe('plan');
      expect(rendered?.kind === 'message' && rendered.chatModeSource).toBe('directive');
    });

    it('renders system-role messages as dim system timeline text', () => {
      const rendered = toRenderItem(
        message({
          id: 'system:notice-1',
          role: 'system',
          content: 'visible system notice',
          notice_id: 'notice-1',
          notice_kind: 'managed_takeover',
          notice_scope: 'conversation',
          retry_reason: 'controller_restart',
          retry_source_turn_id: 'turn-source',
          follow_up_conversation_id: 'conv-follow',
          follow_up_session_id: 'sess-follow',
          created_at: '2026-01-01T00:00:00Z'
        })
      );
      expect(rendered).toMatchObject({
        id: 'system:notice-1',
        kind: 'system_message',
        text: 'visible system notice',
        noticeId: 'notice-1',
        noticeKind: 'managed_takeover',
        noticeScope: 'conversation',
        retryReason: 'controller_restart',
        retrySourceTurnId: 'turn-source',
        followUpConversationId: 'conv-follow',
        followUpSessionId: 'sess-follow',
        timestamp: '2026-01-01T00:00:00Z',
        orderKey: '0000:000000000000001:000000:02:000000000'
      });
    });

    it('renders model-error system notices as unboxed system timeline text', () => {
      const rendered = toRenderItem(
        message({
          id: 'system:model_error:turn-1',
          role: 'system',
          content:
            'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
          notice_id: 'model_error:turn-1',
          notice_kind: 'model_error',
          notice_scope: 'failed_turn',
          reason_class: 'rate_limit',
          provider_id: 'anthropic-lumilens',
          model: 'claude-fable-5',
          retry_after_seconds: 23,
          provider_retry_after_seconds: 23,
          retry_at: '2026-07-09T13:28:00+00:00',
          attempt: 1,
          max_attempts: 3,
          recoverable: true,
          created_at: '2026-01-01T00:00:00Z'
        })
      );

      expect(rendered?.kind).toBe('system_message');
      expect(rendered).toMatchObject({
        id: 'system:model_error:turn-1',
        text:
          'A model error occurred while generating the response. Your tool results have been saved. Please try sending your message again.',
        noticeId: 'model_error:turn-1',
        noticeKind: 'model_error',
        noticeScope: 'failed_turn',
        reasonClass: 'rate_limit',
        providerId: 'anthropic-lumilens',
        model: 'claude-fable-5',
        retryAfterSeconds: 23,
        providerRetryAfterSeconds: 23,
        retryAt: '2026-07-09T13:28:00+00:00',
        attempt: 1,
        maxAttempts: 3,
        recoverable: true
      });
    });
  });

  describe('thinking', () => {
    it('maps a single canonical thinking item to one block (no merge)', () => {
      const rendered = toRenderItem(thinking()) as Extract<ReturnType<typeof toRenderItem>, { kind: 'thinking' }>;
      expect(rendered?.kind).toBe('thinking');
      expect(rendered?.blocks).toHaveLength(1);
      expect(rendered?.blocks[0].block_id).toBe('block-1');
      expect(rendered?.blocks[0].html).toContain('thinking text');
      expect(rendered?.streaming).toBe(false);
    });

    it('keeps distinct thinking items separate (1:1 rendering)', () => {
      const items: TimelineItem[] = [
        thinking({ id: 'thinking:msg-1:block-1', blocks: [{ id: 'block-1', content: 'a', status: 'complete' }] }),
        thinking({ id: 'thinking:msg-1:block-2', blocks: [{ id: 'block-2', content: 'b', status: 'complete' }] })
      ];
      const rendered = toRenderItems(items);
      expect(rendered).toHaveLength(2);
      expect(rendered[0].id).toBe('thinking:msg-1:block-1');
      expect(rendered[1].id).toBe('thinking:msg-1:block-2');
    });

    it('is streaming while a block is running', () => {
      const rendered = toRenderItem(
        thinking({ status: 'running', blocks: [{ id: 'b', content: 'x', status: 'running' }] })
      );
      expect(rendered?.kind === 'thinking' && rendered.streaming).toBe(true);
    });

    it('passes through block timing (started/completed/duration)', () => {
      const rendered = toRenderItem(
        thinking({
          blocks: [
            {
              id: 'block-1',
              content: 'x',
              status: 'complete',
              started_at: '2026-01-01T00:00:00Z',
              completed_at: '2026-01-01T00:00:02Z',
              duration_ms: 2000
            }
          ]
        })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'thinking' }>;
      expect(rendered?.blocks[0].startedAt).toBe('2026-01-01T00:00:00Z');
      expect(rendered?.blocks[0].completedAt).toBe('2026-01-01T00:00:02Z');
      expect(rendered?.blocks[0].durationMs).toBe(2000);
    });
  });

  describe('tool_call', () => {
    it('passes through arguments preview, result, evaluation and attachments', () => {
      const rendered = toRenderItem(
        toolCall({
          arguments_preview: '{"path":"/tmp/x"}',
          result_preview: 'ok',
          evaluation: { decision: 'allow' },
          attachments: [{ artifact_id: 'img-1', mime_type: 'image/png' }] as never
        })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'tool_call' }>;
      expect(rendered?.kind).toBe('tool_call');
      expect(rendered?.callId).toBe('call-1');
      expect(rendered?.arguments).toEqual({ preview: '{"path":"/tmp/x"}' });
      expect(rendered?.result).toBe('ok');
      expect(rendered?.evaluation).toEqual({ decision: 'allow' });
      expect(rendered?.attachments).toHaveLength(1);
    });

    it('normalizes complete status to completed', () => {
      const rendered = toRenderItem(toolCall({ status: 'complete' }));
      expect(rendered?.kind === 'tool_call' && rendered.status).toBe('completed');
    });

    it('prefers structured arguments over the preview wrapper', () => {
      const rendered = toRenderItem(
        toolCall({ arguments: { path: '/tmp/x' }, arguments_preview: "{'path': '/tmp/x'}" })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'tool_call' }>;
      expect(rendered?.arguments).toEqual({ path: '/tmp/x' });
    });

    it('falls back to the preview wrapper only without structured arguments', () => {
      const rendered = toRenderItem(
        toolCall({ arguments: null, arguments_preview: 'raw preview' })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'tool_call' }>;
      expect(rendered?.arguments).toEqual({ preview: 'raw preview' });
    });

    it('passes through apply_patch progress fields', () => {
      const rendered = toRenderItem(
        toolCall({
          tool_name: 'apply_patch',
          status: 'running',
          progress_phase: 'preparing_input',
          progress_input_chars: 120,
          progress_input_lines: 8,
          progress_complete: false
        })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'tool_call' }>;
      expect(rendered?.progressPhase).toBe('preparing_input');
      expect(rendered?.progressInputChars).toBe(120);
      expect(rendered?.progressInputLines).toBe(8);
      expect(rendered?.progressComplete).toBe(false);
    });

    it('folds delegation details onto a delegate tool call', () => {
      const rendered = toRenderItem(
        toolCall({
          tool_name: 'delegate',
          status: 'running',
          delegation: {
            child_session_id: 'sess-child',
            status: 'running',
            title: 'Investigate X',
            used_agent_id: 'laforge',
            started_at: '2026-01-01T00:00:00+00:00',
            duration_ms: 1234,
            tool_call_count: 3,
            max_tool_calls: 20,
            last_tool: 'grep',
            result_content: '### Summary\nDone',
            result_truncated: false,
            todos: [{ content: 'do a', status: 'in_progress' }]
          }
        })
      ) as Extract<ReturnType<typeof toRenderItem>, { kind: 'tool_call' }>;
      expect(rendered?.delegation?.title).toBe('Investigate X');
      expect(rendered?.delegation?.usedAgentId).toBe('laforge');
      expect(rendered?.delegation?.startedAt).toBe('2026-01-01T00:00:00+00:00');
      expect(rendered?.delegation?.durationMs).toBe(1234);
      expect(rendered?.delegation?.toolCallCount).toBe(3);
      expect(rendered?.delegation?.maxToolCalls).toBe(20);
      expect(rendered?.delegation?.lastTool).toBe('grep');
      expect(rendered?.delegation?.resultContent).toBe('### Summary\nDone');
      expect(rendered?.delegation?.resultTruncated).toBe(false);
      expect(rendered?.delegation?.todos).toHaveLength(1);
    });

    it('safely normalizes malformed delegation and evaluation payloads', () => {
      const rendered = toRenderItem(
        toolCall({
          evaluation: { decision: 42, reasoning: ['invalid'] } as never,
          delegation: {
            child_session_id: 42,
            status: ['running'],
            duration_ms: 'slow',
            result_truncated: 'false',
            result_anchors: ['invalid'],
            tool_call_count: Number.NaN
          } as never
        })
      );

      expect(rendered?.kind).toBe('tool_call');
      expect(rendered?.kind === 'tool_call' && rendered.evaluation).toBeUndefined();
      expect(rendered?.kind === 'tool_call' && rendered.delegation).toMatchObject({
        childSessionId: null,
        status: null,
        durationMs: null,
        resultTruncated: null,
        toolCallCount: null
      });
      expect(rendered?.kind === 'tool_call' && rendered.delegation?.resultAnchors).toBeUndefined();
    });
  });

  describe('assistant_deliverable', () => {
    it('is renderable and maps to a first-class deliverable item', () => {
      const item = assistantDeliverable();

      expect(isRenderableChatV2Item(item)).toBe(true);
      expect(toRenderItem(item)).toMatchObject({
        id: 'assistant-deliverable:dlv-rich',
        kind: 'assistant_deliverable',
        deliverableId: 'dlv-rich',
        format: 'rich',
        title: 'Rich report',
        orderKey: '0000:000000000000004:000000:03:000000000',
        sourceRefs: ['intaris:sess-1:4']
      });
    });
  });

  describe('compaction', () => {
    it('maps Chat v2 compaction items to the compaction card shape', () => {
      const rendered = toRenderItem(
        compaction({
          previous_usage_percentage: 86.1,
          effective_usage_percentage: 72.5,
          trigger: 'pressure',
          reason: 'context_pressure',
          created_at: '2026-01-01T00:00:00Z'
        })
      );

      expect(rendered).toMatchObject({
        id: 'compaction:sess-old',
        kind: 'compaction',
        status: 'compacted',
        sessionId: 'sess-new',
        previousSessionId: 'sess-old',
        summaryPreview: 'Compacted summary',
        summary: 'Compacted summary details',
        method: 'rotation',
        turnsCompacted: 5,
        trigger: 'pressure',
        reason: 'context_pressure',
        previousUsagePercentage: 86.1,
        effectiveUsagePercentage: 72.5,
        hardPressureExceeded: true,
        usedTimeoutFallback: false,
        timestamp: '2026-01-01T00:00:00Z',
        orderKey: '0000:000000000000004:000000:10:000000000'
      });
    });
  });

  describe('non-row kinds', () => {
    it('drops todo_state items (no visible row)', () => {
      const item: TimelineItem = {
        id: 'todo:1',
        kind: 'todo_state',
        sort_key: '0000:000000000000001:000000:09:000000000',
        source_refs: baseRefs(),
        stable: true,
        todos: []
      };
      expect(toRenderItem(item)).toBeNull();
      expect(toRenderItems([item])).toHaveLength(0);
    });

    it('classifies non-row kinds via isRenderableChatV2Item', () => {
      const todo: TimelineItem = {
        id: 'todo:1',
        kind: 'todo_state',
        sort_key: '0000:000000000000001:000000:09:000000000',
        source_refs: baseRefs(),
        stable: true,
        todos: []
      };
      expect(isRenderableChatV2Item(todo)).toBe(false);
      expect(isRenderableChatV2Item(message())).toBe(true);
      expect(isRenderableChatV2Item(toolCall())).toBe(true);
    });

    it('renders auth_challenge as a warning notice (never a tool card)', () => {
      const item: AuthChallengeTimelineItem = {
        id: 'auth:1',
        kind: 'auth_challenge',
        sort_key: '0000:000000000000001:000000:08:000000000',
        source_refs: baseRefs(),
        stable: true,
        challenge_id: 'c1',
        challenge_kind: 'mfa',
        label: 'Verify',
        message: 'Enter code',
        metadata: {},
        required_fields: [],
        status: 'waiting'
      };
      const rendered = toRenderItem(item);
      expect(rendered?.kind).toBe('notice');
      expect(rendered?.kind === 'notice' && rendered.tone).toBe('warning');
    });
  });
});
