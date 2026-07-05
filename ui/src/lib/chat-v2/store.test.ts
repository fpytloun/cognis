import { describe, expect, it } from 'vitest';

import { ChatV2Store } from './store.svelte';
import type { ChatSnapshot, TimelineItem } from './types';

function message(overrides: Partial<TimelineItem> = {}): TimelineItem {
  return {
    id: 'message:1',
    kind: 'message',
    sort_key: '0000:000000000000001:000000:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 1, event_type: 'assistant_message' }],
    stable: true,
    role: 'assistant',
    content: 'hello',
    message_id: 'msg-1',
    attachments: [],
    partial: false,
    ...overrides
  } as TimelineItem;
}

function snapshot(items: TimelineItem[]): ChatSnapshot {
  return {
    schema_version: 2,
    projection_version: 'chat-v2-test',
    conversation: { conversation_id: 'conv-1', agent_id: 'agent-1', status: 'active' },
    timeline: { items, has_more_before: false, before_cursor: null },
    state: {
      state_version: 1,
      snapshot_generated_at: '2026-01-01T00:00:00Z',
      capabilities: [],
      active_turn: {},
      pending: {},
      active_session: {},
      task: null
    },
    queue: { messages: [], queued_count: 0 },
    runtime: {
      runtime_epoch: 'epoch-1',
      runtime_revision: 0,
      generated_at: '2026-01-01T00:00:00Z',
      has_active_turn: false,
      active_turn: null,
      volatile_items: []
    },
    cursor: 'cursor-1',
    server_time: '2026-01-01T00:00:00Z'
  };
}

describe('ChatV2Store serialize/restore (conversation-view cache)', () => {
  it('serializeState returns a plain, non-proxy deep copy that survives structuredClone', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));

    const serialized = store.serializeState();

    // Regression: the store holds a Svelte $state proxy; a naive
    // structuredClone(this._state) threw "Proxy object could not be cloned",
    // breaking conversation switching and every WS frame that saved the view.
    // The serialized snapshot must be a plain object that clones cleanly.
    expect(() => structuredClone(serialized)).not.toThrow();
    expect(serialized.conversationId).toBe('conv-1');
    expect(serialized.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('serialized snapshot is not mutated by later store updates', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message({ content: 'first' })]));
    const serialized = store.serializeState();

    store.replaceFromSnapshot(snapshot([message({ content: 'second' }), message({ id: 'message:2' })]));

    const firstItem = serialized.timelineItems.find((item) => item.id === 'message:1');
    expect(firstItem && firstItem.kind === 'message' ? firstItem.content : null).toBe('first');
    expect(serialized.timelineItems).toHaveLength(1);
  });

  it('restoreState round-trips a serialized snapshot into visibleItems', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const serialized = store.serializeState();

    store.reset();
    expect(store.visibleItems).toHaveLength(0);

    store.restoreState(serialized);
    expect(store.visibleItems.map((item) => item.id)).toEqual(['message:1']);
    // The restored state must be independent of the cached entry.
    expect(() => structuredClone(store.serializeState())).not.toThrow();
  });
});
