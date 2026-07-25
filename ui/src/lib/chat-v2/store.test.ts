import { describe, expect, it } from 'vitest';

import { ChatV2Store } from './store.svelte';
import { reactiveAttachmentRefs } from './store-test-helpers.svelte';
import { emptyChatV2State } from './sync-engine';
import type { ChatV2ClientState } from './sync-engine';
import type { ChatRealtimeFrame, ChatSnapshot, TimelineItem } from './types';

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
  it('rejects a snapshot that resolves after a realtime cursor advanced', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const watermark = store.refreshWatermark();
    const frame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [{ op: 'upsert_item', item: message({ id: 'message:live', content: 'live' }) }],
      runtime: null,
      server_time: '2026-01-01T00:00:01Z'
    } as ChatRealtimeFrame;

    expect(store.applyRealtime(frame).outcome).toBe('applied');
    expect(store.replaceFromSnapshotIfUnchanged(snapshot([message({ content: 'stale' })]), watermark)).toBe(false);
    expect(store.snapshot.cursor).toBe('cursor-2');
    expect(store.visibleItems.map((item) => item.id)).toContain('message:live');
  });

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

  it('can serialize a cache-safe state without live runtime overlay', () => {
    const store = new ChatV2Store();
    const state: ChatV2ClientState = {
      ...emptyChatV2State(),
      conversationId: 'conv-1',
      runtime: {
        runtime_epoch: 'epoch-1',
        runtime_revision: 1,
        generated_at: '2026-07-07T00:00:00Z',
        has_active_turn: true,
        active_turn: {
          turn_id: 'turn-1',
          session_id: 'sess-1',
          status: 'running',
          started_at: '2026-07-07T00:00:00Z',
          updated_at: '2026-07-07T00:00:01Z',
        },
        volatile_items: [
          message({
            id: 'volatile-message',
            stable: false,
            partial: true,
            content: 'streaming',
          }),
        ],
        cycle_states: [],
      },
      cycleStates: [
        {
          turn_id: 'turn-1',
          turn_cycle_index: 0,
          lifecycle_status: 'open',
          has_tool_activity: false,
        },
      ],
    };
    store.restoreState(state);

    const preserved = store.serializeState();
    const cached = store.serializeState({ settleRuntimeOverlay: true });

    expect(preserved.runtime?.has_active_turn).toBe(true);
    expect(preserved.runtime?.volatile_items).toHaveLength(1);
    expect(cached.runtime?.has_active_turn).toBe(false);
    expect(cached.runtime?.active_turn).toBeNull();
    expect(cached.runtime?.volatile_items).toEqual([]);
    expect(cached.cycleStates).toEqual([]);
    expect(store.snapshot.runtime?.has_active_turn).toBe(true);
    expect(store.snapshot.runtime?.volatile_items).toHaveLength(1);
  });

  it('serializes and restores reactive-proxy attachments on local optimistic items', () => {
    const store = new ChatV2Store();
    const attachments = reactiveAttachmentRefs();

    store.addOptimisticUser({
      content: 'attached',
      attachments,
      clientMessageId: 'client-with-attachment',
      createdAt: '2026-01-01T00:00:00Z',
    });

    const serialized = store.serializeState();

    expect(() => structuredClone(serialized)).not.toThrow();
    const localMessage = serialized.localItems.find(
      (item) => item.kind === 'message' && item.client_message_id === 'client-with-attachment'
    );
    expect(localMessage && localMessage.kind === 'message' ? localMessage.attachments : []).toEqual([
      expect.objectContaining({
        artifact_id: 'artifact-1',
        filename: 'trace.txt',
      }),
    ]);

    const restored = new ChatV2Store();
    restored.restoreState(serialized);
    expect(() => structuredClone(restored.serializeState())).not.toThrow();
    expect(restored.visibleItems).toHaveLength(1);
  });
});
