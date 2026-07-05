import { describe, expect, it } from 'vitest';

import {
  selectActiveTurnId,
  selectHasActiveTurn,
  selectNeedsRecovery,
  selectQueuedCount,
  selectRenderItems
} from './selectors';
import { emptyChatV2State, type ChatV2ClientState } from './sync-engine';
import type { RuntimeOverlaySnapshot, TimelineItem } from './types';

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

const sampleItems: TimelineItem[] = [
  {
    id: 'message:1',
    kind: 'message',
    sort_key: '0000:000000000000001:000000:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 1, event_type: 'user_message' }],
    stable: true,
    role: 'user',
    content: 'hi',
    message_id: 'm1',
    attachments: [],
    partial: false
  },
  {
    id: 'todo:1',
    kind: 'todo_state',
    sort_key: '0000:000000000000001:000000:09:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 1, event_type: 'todo_state' }],
    stable: true,
    todos: []
  }
];

describe('selectRenderItems', () => {
  it('converts visible items to legacy render shape, dropping non-row kinds', () => {
    const rendered = selectRenderItems(sampleItems);
    expect(rendered).toHaveLength(1);
    expect(rendered[0].kind).toBe('message');
    expect(rendered[0].id).toBe('message:1');
  });
});

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
