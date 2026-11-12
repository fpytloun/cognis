import { describe, expect, it, vi } from 'vitest';

import { ChatV2Store } from './store.svelte';
import { refreshCachedTimeline } from '../chat-page';
import { reactiveAttachmentRefs } from './store-test-helpers.svelte';
import { emptyChatV2State } from './sync-engine';
import type { ChatV2ClientState } from './sync-engine';
import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  QueueMutationResponse,
  RuntimeOverlaySnapshot,
  TimelineBackfillResponse,
  TimelineItem
} from './types';

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
  it('keeps copied compaction-tail identities unique across repeated snapshot recovery', () => {
    const store = new ChatV2Store();
    const older = message({ id: 'older', sort_key: '0022:0001' });
    const copied = message({ id: 'copied', sort_key: '0022:0002', content: 'original' });
    store.replaceFromSnapshot(snapshot([older, copied]));
    const rotated = snapshot([
      {
        id: 'compaction:rotation', kind: 'compaction', sort_key: '0023:0001',
        stable: true, source_refs: [], status: 'compacted',
        session_id: 'sess-2', previous_session_id: 'sess-1',
        summary_preview: 'Recap', method: 'summary', turns_compacted: 1,
      },
      message({
        id: copied.id, sort_key: '0023:0002', content: 'canonical copy',
        source_refs: [{ store: 'intaris', session_id: 'sess-2', seq: 2, event_type: 'assistant_message' }],
      }),
    ]);
    rotated.timeline.has_more_before = true;
    rotated.timeline.before_cursor = 'older-page';
    for (let attempt = 0; attempt < 3; attempt += 1) {
      store.replaceFromSnapshot(rotated);
      expect(store.snapshot.timelineItems.map(item => item.id)).toEqual([
        older.id, copied.id, 'compaction:rotation',
      ]);
      expect(store.visibleItems.map(item => item.id)).toEqual([
        older.id, copied.id, 'compaction:rotation',
      ]);
      expect(store.snapshot.timelineItems[1]).toMatchObject({
        content: 'canonical copy', sort_key: copied.sort_key,
        source_refs: [{ store: 'intaris', session_id: 'sess-2', seq: 2, event_type: 'assistant_message' }],
      });
    }
  });

  it('repairs duplicate identities in an old cached view before publishing its idle timeline', () => {
    const original = message({ id: 'copied', sort_key: '0022:0002', content: 'original' });
    const duplicate = message({ id: original.id, content: 'canonical copy', sort_key: '0022:0002' });
    const cached: ChatV2ClientState = {
      ...emptyChatV2State(), conversationId: 'conv-1',
      timelineItems: [original, duplicate],
    };
    const store = new ChatV2Store();
    store.restoreState(cached);
    expect(store.snapshot.timelineItems).toHaveLength(1);
    expect(store.visibleItems).toEqual([expect.objectContaining({ id: original.id, content: 'canonical copy' })]);
    expect(cached.timelineItems).toHaveLength(2);
    store.addOptimisticUser({ content: 'draft', clientMessageId: 'local-admission' });
    const restored = new ChatV2Store();
    restored.restoreState(store.serializeState());
    expect(restored.visibleItems.map(item => item.id)).toEqual([original.id, 'user:local-admission']);
  });

  it('prepares all retained views before one membership projection and unregisters on teardown', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const phases: string[] = [];
    const stop = store.observeMembership((before, after) => {
      expect(store.visibleItems).toBe(before);
      expect(after.map(item => item.id)).toContain('older');
      phases.push('prepare:first');
      return () => { phases.push('commit:first'); expect(store.visibleItems).toBe(after); };
    });
    const stopSecond = store.observeMembership(() => {
      phases.push('prepare:second');
      return () => { phases.push('commit:second'); };
    });
    store.applyBackfill({
      schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'conv-1',
      items: [message({ id: 'older', sort_key: '0000' })],
      has_more_before: false, before_cursor: null, server_time: '',
    });
    expect(phases).toEqual(['prepare:first', 'prepare:second', 'commit:first', 'commit:second']);
    stop();
    stopSecond();
    store.addLocalSystemMessage({ id: 'notice', content: 'local' });
    expect(phases).toHaveLength(4);
  });

  it('preserves runtime, optimistic and loaded history admissions while accepting a canonical base', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const watermark = store.refreshWatermark();
    store.addOptimisticUser({ content: 'local admission', clientMessageId: 'recovery-client' });
    store.applyBackfill({
      schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'conv-1',
      items: [message({ id: 'older', sort_key: '0000' })],
      has_more_before: false, before_cursor: null, server_time: '',
    });
    store.applyRealtime({
      type: 'chat_v2_frame', schema_version: 2, projection_version: 'chat-v2-test',
      conversation_id: 'conv-1', cursor_before: 'cursor-1', cursor_after: 'cursor-1', ops: [],
      runtime: { ...snapshot([]).runtime!, runtime_revision: 10 }, server_time: '',
    });
    const recovered = snapshot([message({ content: 'recovered' })]);
    recovered.timeline.has_more_before = true;
    recovered.timeline.before_cursor = 'before:recovered';
    expect(store.replaceFromSnapshotForRecovery(recovered, watermark)).toBe(true);
    expect(store.visibleItems.map(item => item.id)).toContain('older');
    expect(store.visibleItems).toEqual(expect.arrayContaining([
      expect.objectContaining({ content: 'local admission' }), expect.objectContaining({ content: 'recovered' }),
    ]));
    expect(store.snapshot.runtime?.runtime_revision).toBe(10);
  });

  it('never lets a delayed recovery snapshot overwrite intervening canonical progress', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const watermark = store.refreshWatermark();
    store.applyRealtime({
      type: 'chat_v2_frame', schema_version: 2, projection_version: 'chat-v2-test',
      conversation_id: 'conv-1', cursor_before: 'cursor-1', cursor_after: 'cursor-2',
      ops: [{ op: 'upsert_item', item: message({ content: 'new canonical' }) }], runtime: null, server_time: '',
    });
    expect(store.replaceFromSnapshotForRecovery(snapshot([message({ content: 'stale' })]), watermark)).toBe(false);
    expect(store.visibleItems[0]).toMatchObject({ content: 'new canonical' });
  });

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

  it('accepts the populated canonical bootstrap after a pre-bootstrap reset', () => {
    const store = new ChatV2Store();
    const watermark = store.refreshWatermark();

    // A legacy session_reset can arrive after the cold request captures its
    // watermark but before the populated snapshot resolves.
    store.reset();

    expect(store.replaceFromSnapshotForCanonicalBootstrap(
      snapshot([message()]),
      watermark,
      true,
    )).toBe(true);
    expect(store.snapshot.conversationId).toBe('conv-1');
    expect(store.visibleItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('preserves an optimistic admission created during canonical bootstrap', () => {
    const store = new ChatV2Store();
    const watermark = store.refreshWatermark();
    store.reset();
    store.addOptimisticUser({
      clientMessageId: 'client-during-bootstrap',
      content: 'message sent during bootstrap',
      createdAt: '2025-01-01T00:00:02Z',
    });

    expect(store.replaceFromSnapshotForCanonicalBootstrap(
      snapshot([message()]),
      watermark,
      true,
    )).toBe(true);
    expect(store.snapshot.conversationId).toBe('conv-1');
    expect(store.visibleItems.map((item) => item.id)).toEqual([
      'message:1',
      'user:client-during-bootstrap',
    ]);
  });

  it('keeps watermark protection after canonical bootstrap completes', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([message()]));
    const watermark = store.refreshWatermark();
    store.reset();

    expect(store.replaceFromSnapshotForCanonicalBootstrap(
      snapshot([message({ content: 'stale' })]),
      watermark,
      false,
    )).toBe(false);
    expect(store.visibleItems).toEqual([]);
  });

  it('rejects a delayed snapshot after a queue deletion and falls back to sync', async () => {
    const store = new ChatV2Store();
    const queuedSnapshot = snapshot([message()]);
    queuedSnapshot.queue = {
      messages: [
        {
          queue_id: 'queue-1',
          content: 'queued',
          attachments: [],
          position: 0
        }
      ],
      queued_count: 1
    };
    store.replaceFromSnapshot(queuedSnapshot);
    let resolveProbe: ((value: ChatSnapshot) => void) | undefined;
    const probe = new Promise<ChatSnapshot>((resolve) => {
      resolveProbe = resolve;
    });
    const sync = vi.fn(async () => undefined);
    const refresh = refreshCachedTimeline({
      captureWatermark: () => store.refreshWatermark(),
      probe: () => probe,
      applyIfUnchanged: (candidate, watermark) =>
        store.replaceFromSnapshotIfUnchanged(candidate, watermark),
      snapshot: vi.fn(async () => undefined),
      sync
    });
    const deletion: QueueMutationResponse = {
      conversation_id: 'conv-1',
      client_txn_id: 'txn-1',
      status: 'deleted',
      queue: { messages: [], queued_count: 0 },
      cursor: 'cursor-1',
      server_time: '2026-01-01T00:00:01Z'
    };

    store.applyQueueMutation(deletion);
    resolveProbe?.(queuedSnapshot);
    await expect(refresh).resolves.toBe('sync');

    expect(sync).toHaveBeenCalledTimes(1);
    expect(store.snapshot.queue?.messages).toEqual([]);
  });

  it('rejects a delayed snapshot after backfill, falls back to sync, and preserves pagination state', async () => {
    const store = new ChatV2Store();
    const current = snapshot([message()]);
    current.timeline = { items: current.timeline.items, has_more_before: true, before_cursor: 'before-1' };
    store.replaceFromSnapshot(current);
    let resolveProbe: ((value: ChatSnapshot) => void) | undefined;
    const probe = new Promise<ChatSnapshot>((resolve) => {
      resolveProbe = resolve;
    });
    const sync = vi.fn(async () => undefined);
    const refresh = refreshCachedTimeline({
      captureWatermark: () => store.refreshWatermark(),
      probe: () => probe,
      applyIfUnchanged: (candidate, watermark) =>
        store.replaceFromSnapshotIfUnchanged(candidate, watermark),
      snapshot: vi.fn(async () => undefined),
      sync
    });
    const older = message({
      id: 'message:older',
      sort_key: '0000:000000000000000:000000:02:000000000',
      content: 'older'
    });
    const backfill: TimelineBackfillResponse = {
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      items: [older],
      has_more_before: true,
      before_cursor: 'before-2',
      server_time: '2026-01-01T00:00:01Z'
    };

    store.applyBackfill(backfill);
    resolveProbe?.(snapshot([message({ content: 'stale' })]));
    await expect(refresh).resolves.toBe('sync');

    expect(sync).toHaveBeenCalledTimes(1);
    expect(store.visibleItems.map((item) => item.id)).toEqual(['message:older', 'message:1']);
    expect(store.snapshot.beforeCursor).toBe('before-2');
    expect(store.snapshot.hasMoreBefore).toBe(true);
  });

  it('returns backfill admission and preserves current cursor on projection drift', () => {
    const store = new ChatV2Store();
    const current = snapshot([message()]);
    current.timeline = { items: current.timeline.items, has_more_before: true, before_cursor: 'current-before' };
    store.replaceFromSnapshot(current);
    const admitted = store.applyBackfill({
      schema_version: 2,
      projection_version: 'newer-projection',
      conversation_id: 'conv-1',
      items: [message({ id: 'message:old', content: 'stale older' })],
      has_more_before: false,
      before_cursor: null,
      server_time: '2026-01-01T00:00:02Z'
    });
    expect(admitted).toBe(false);
    expect(store.snapshot.beforeCursor).toBe('current-before');
    expect(store.visibleItems.some((item) => item.id === 'message:old')).toBe(false);
  });

  it('does not advance the mutation revision for rejected duplicate optimistic mutations', () => {
    const store = new ChatV2Store();
    const input = {
      content: 'pending',
      clientMessageId: 'client-1',
      createdAt: '2026-01-01T00:00:00Z'
    };
    store.addOptimisticUser(input);
    const watermark = store.refreshWatermark();

    store.addOptimisticUser(input);

    expect(store.refreshWatermark()).toEqual(watermark);
  });

  it('rejects a snapshot that started before an optimistic admission', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([]));
    const watermark = store.refreshWatermark();

    store.addOptimisticUser({
      content: 'visible immediately',
      clientMessageId: 'client-admission',
      createdAt: '2026-01-01T00:00:01Z',
    });

    expect(store.replaceFromSnapshotIfUnchanged(snapshot([]), watermark)).toBe(false);
    expect(store.visibleItems).toHaveLength(1);
    expect(store.visibleItems[0]?.kind === 'message' ? store.visibleItems[0].content : null)
      .toBe('visible immediately');
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

  it('restores queued local admission placement without a visibility gap', () => {
    const store = new ChatV2Store();
    store.replaceFromSnapshot(snapshot([]));
    store.addOptimisticUser({
      content: 'queued locally',
      clientMessageId: 'client-queue',
      createdAt: '2026-01-01T00:00:01Z',
    });
    store.applySend({
      status: 'queued',
      client_txn_id: 'txn-queue',
      client_message_id: 'client-queue',
      conversation_id: 'conv-1',
      message_id: null,
      queue_id: 'queue-1',
      cursor: null,
      server_time: '2026-01-01T00:00:02Z',
    });
    const serialized = store.serializeState();

    store.reset();
    store.restoreState(serialized);

    expect(store.visibleItems).toEqual([]);
    expect(store.visibleQueue.messages).toEqual([
      expect.objectContaining({
        queue_id: 'queue-1',
        client_message_id: 'client-queue',
        content: 'queued locally',
      }),
    ]);
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
        authority: {
          protocol: 'runtime_authority_v1',
          direct_request_id: 'request-1',
          turn_id: 'turn-1',
          fencing_token: 1,
          lifecycle: 'active',
        },
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
    expect(cached.runtime?.authority).toBeNull();
    expect(cached.runtime?.active_turn).toBeNull();
    expect(cached.runtime?.volatile_items).toEqual([]);
    expect(cached.cycleStates).toEqual([]);
    expect(store.snapshot.runtime?.has_active_turn).toBe(true);
    expect(store.snapshot.runtime?.authority).toEqual(state.runtime?.authority);
    expect(store.snapshot.runtime?.volatile_items).toHaveLength(1);
  });

  it.each(['new-cache', 'previous-cache', 'terminal-cache'] as const)(
    'reconciles server runtime after restoring %s without weakening terminal ordering',
    (cacheKind) => {
      const liveSnapshot = snapshot([message()]);
      const runtime: RuntimeOverlaySnapshot = {
        ...liveSnapshot.runtime,
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        authority: {
          protocol: 'runtime_authority_v1',
          direct_request_id: 'request-1',
          turn_id: 'turn-1',
          fencing_token: 1,
          lifecycle: 'active',
          source_epoch: 'epoch-1',
          source_revision: 1,
        },
      };
      const store = new ChatV2Store();
      store.replaceFromSnapshot({ ...liveSnapshot, runtime });
      const cached = JSON.parse(JSON.stringify(store.serializeState({
        settleRuntimeOverlay: true,
      }))) as ChatV2ClientState;
      if (cacheKind !== 'new-cache') {
        cached.runtime!.authority = {
          ...runtime.authority!,
          lifecycle: cacheKind === 'terminal-cache' ? 'terminal' : 'active',
        };
      }
      const restored = new ChatV2Store();
      restored.restoreState(cached);
      expect(restored.snapshot.runtime?.has_active_turn).toBe(false);
      expect(restored.applyRealtime({
        type: 'chat_v2_frame',
        schema_version: 2,
        projection_version: liveSnapshot.projection_version,
        conversation_id: 'conv-1',
        cursor_before: liveSnapshot.cursor,
        cursor_after: 'cursor-2',
        ops: [],
        runtime: {
          ...runtime,
          runtime_revision: 2,
          authority: { ...runtime.authority!, source_revision: 2 },
        },
        server_time: liveSnapshot.server_time,
      }).outcome).toBe('applied');
      expect(restored.snapshot.runtime?.has_active_turn).toBe(cacheKind !== 'terminal-cache');
      expect(store.snapshot.runtime?.has_active_turn).toBe(true);
    },
  );

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
