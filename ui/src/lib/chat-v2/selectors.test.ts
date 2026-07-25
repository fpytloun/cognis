import { describe, expect, it } from 'vitest';

import {
  selectActiveTurnId,
  selectHasActiveTurn,
  selectNeedsRecovery,
  selectLatestTodoState,
  selectPendingInputItem,
  selectPendingInputToolCall,
  selectQueuedCount
} from './selectors';
import { emptyChatV2State, type ChatV2ClientState } from './sync-engine';
import type { RuntimeOverlaySnapshot, TimelineItem, ToolCallTimelineItem } from './types';

function state(overrides: Partial<ChatV2ClientState> = {}): ChatV2ClientState {
  return { ...emptyChatV2State(), ...overrides };
}

function activeRuntime(overrides: Partial<RuntimeOverlaySnapshot> = {}): RuntimeOverlaySnapshot {
  return {
    runtime_epoch: 'epoch-1',
    runtime_revision: 1,
    generated_at: '2026-01-01T00:00:01Z',
    has_active_turn: true,
    active_turn: {
      turn_id: 'turn-9',
      session_id: 'sess-1',
      status: 'running'
    },
    volatile_items: [],
    ...overrides
  };
}

describe('runtime selectors', () => {
  it('reports active turn flags', () => {
    expect(selectHasActiveTurn(state())).toBe(false);
    expect(selectActiveTurnId(state())).toBeNull();

    const withTurn = state({ runtime: activeRuntime() });
    expect(selectHasActiveTurn(withTurn)).toBe(true);
    expect(selectActiveTurnId(withTurn)).toBe('turn-9');
  });

  it('reports recovery and queued count', () => {
    expect(selectNeedsRecovery(state({ syncStatus: 'gapped' }))).toBe(true);
    expect(selectNeedsRecovery(state({ syncStatus: 'ready' }))).toBe(false);
    expect(selectQueuedCount(state({ queue: { messages: [], queued_count: 3 } }))).toBe(3);
    expect(selectQueuedCount(state())).toBe(0);
  });
});

function tool(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool:call-1',
    kind: 'tool_call',
    sort_key: '0001',
    source_refs: [],
    stable: true,
    status: 'complete',
    call_id: 'call-1',
    tool_name: 'grep',
    arguments: {},
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: false,
    has_full_output: false,
    ...overrides,
  };
}

describe('canonical pending-input selectors', () => {
  it('ignores historical canonical tool calls and selects a waiting request without legacy fields', () => {
    const items: TimelineItem[] = [
      tool({ call_id: 'historical', tool_name: 'grep', result_preview: 'done' }),
      tool({ call_id: 'question', tool_name: 'request_user_input', status: 'waiting', stable: false }),
    ];

    expect(() => selectPendingInputToolCall(items)).not.toThrow();
    expect(selectPendingInputToolCall(items)?.call_id).toBe('question');
  });

  it('selects canonical question and auth rows directly', () => {
    const items: TimelineItem[] = [
      {
        id: 'questions',
        kind: 'question_set',
        sort_key: '0001',
        source_refs: [],
        stable: true,
        status: 'waiting',
        request_id: 'req-1',
        questions: [],
      },
      {
        id: 'auth',
        kind: 'auth_challenge',
        sort_key: '0002',
        source_refs: [],
        stable: true,
        status: 'waiting',
        challenge_id: 'auth-1',
        challenge_kind: 'otp_code',
        label: 'OTP',
        message: 'Enter code',
        metadata: {},
        required_fields: ['code'],
      },
    ];

    expect(selectPendingInputItem(items)?.kind).toBe('auth_challenge');
  });
});

describe('canonical todo selectors', () => {
  it('uses only the newest canonical todo_state despite missing or reordered legacy tool events', () => {
    const items: TimelineItem[] = [
      tool({ call_id: 'legacy-result-arrived-first', tool_name: 'step_todo_write', result_preview: '{"todos":[{"content":"wrong","status":"pending"}]}' }),
      {
        id: 'todo:authoritative',
        kind: 'todo_state',
        sort_key: '0002',
        source_refs: [],
        stable: true,
        status: 'complete',
        todos: [{ content: 'Canonical authority', status: 'in_progress', priority: 'high' }],
      },
      tool({ call_id: 'legacy-call-arrived-last', tool_name: 'step_todo_write', arguments: { todos: [{ content: 'also wrong', status: 'pending' }] } }),
    ];

    expect(selectLatestTodoState(items)).toEqual([
      { content: 'Canonical authority', status: 'in_progress', priority: 'high' },
    ]);
  });
});
