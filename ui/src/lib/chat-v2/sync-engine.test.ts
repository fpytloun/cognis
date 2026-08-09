import { describe, expect, it } from 'vitest';

import {
  __chatV2SyncEngineTestHooks,
  addLocalSystemMessage,
  addOptimisticUserMessage,
  applyBackfill,
  applyRealtimeFrame,
  applySendResponse,
  applySnapshot,
  applySyncResponse,
  maybeApplyRuntime,
  visibleTimelineItems
} from './sync-engine';
import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  ChatSyncResponse,
  RuntimeOverlaySnapshot,
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

function runtime(revision: number, overrides: Partial<RuntimeOverlaySnapshot> = {}): RuntimeOverlaySnapshot {
  return {
    runtime_epoch: 'epoch-1',
    runtime_revision: revision,
    generated_at: `2026-01-01T00:00:0${revision}Z`,
    has_active_turn: false,
    active_turn: null,
    volatile_items: [],
    ...overrides
  };
}

function snapshot(overrides: Partial<ChatSnapshot> = {}): ChatSnapshot {
  return {
    schema_version: 2,
    projection_version: 'chat-v2-test',
    conversation: {
      conversation_id: 'conv-1',
      agent_id: 'agent-1',
      status: 'active'
    },
    timeline: {
      items: [message()],
      has_more_before: false,
      before_cursor: null
    },
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
    runtime: runtime(1),
    cursor: 'cursor-1',
    server_time: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function syncResponse(overrides: Partial<ChatSyncResponse> = {}): ChatSyncResponse {
  return {
    schema_version: 2,
    projection_version: 'chat-v2-test',
    conversation_id: 'conv-1',
    cursor_before: 'cursor-1',
    cursor_after: 'cursor-2',
    ops: [],
    runtime: null,
    reset_required: false,
    reset_reason: null,
    has_more: false,
    server_time: '2026-01-01T00:00:01Z',
    ...overrides
  };
}

describe('Chat v2 sync engine', () => {
  it('loads a snapshot into ready state', () => {
    const state = applySnapshot(snapshot());

    expect(state.syncStatus).toBe('ready');
    expect(state.cursor).toBe('cursor-1');
    expect(state.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('settles the optimistic sending state as soon as admission is acknowledged', () => {
    const optimistic = addOptimisticUserMessage(
      applySnapshot(snapshot({ timeline: { items: [], has_more_before: false } })),
      {
        content: 'hello',
        clientMessageId: 'client-1',
        createdAt: '2026-01-01T00:00:00Z',
      },
    );

    const accepted = applySendResponse(optimistic, {
      status: 'accepted',
      client_txn_id: 'txn-1',
      client_message_id: 'client-1',
      conversation_id: 'conv-1',
      message_id: null,
      queue_id: null,
      cursor: null,
      server_time: '2026-01-01T00:00:01Z',
    });
    const acceptedItem = accepted.localItems[0];
    expect(acceptedItem?.status).toBe('complete');
    expect(acceptedItem?.updated_at).toBe('2026-01-01T00:00:01Z');

    const duplicate = applySendResponse(optimistic, {
      status: 'duplicate',
      client_txn_id: 'txn-1',
      client_message_id: 'client-1',
      conversation_id: 'conv-1',
      message_id: null,
      queue_id: null,
      cursor: null,
      server_time: '2026-01-01T00:00:01Z',
    });
    expect(duplicate.localItems[0]?.status).toBe('complete');

    const queued = applySendResponse(optimistic, {
      status: 'queued',
      client_txn_id: 'txn-1',
      client_message_id: 'client-1',
      conversation_id: 'conv-1',
      message_id: null,
      queue_id: 'queue-1',
      cursor: null,
      server_time: '2026-01-01T00:00:01Z',
    });
    expect(queued.localItems[0]?.status).toBe('waiting');

    const unrelated = addOptimisticUserMessage(optimistic, {
      content: 'another message',
      clientMessageId: 'client-2',
      createdAt: '2026-01-01T00:00:00Z',
    });
    const acknowledgedOne = applySendResponse(unrelated, {
      status: 'accepted',
      client_txn_id: 'txn-1',
      client_message_id: 'client-1',
      conversation_id: 'conv-1',
      message_id: null,
      queue_id: null,
      cursor: null,
      server_time: '2026-01-01T00:00:01Z',
    });
    expect(acknowledgedOne.localItems.map((item) => item.status)).toEqual(['complete', 'pending']);
  });

  it('persists opaque backfill cursors and disables older loading at the terminal page', () => {
    const state = applySnapshot(snapshot({
      timeline: { items: [message()], has_more_before: true, before_cursor: 'opaque-page-1' }
    }));
    expect(state.hasMoreBefore).toBe(true);
    expect(state.beforeCursor).toBe('opaque-page-1');

    const next = applyBackfill(state, {
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      items: [],
      has_more_before: false,
      before_cursor: null,
      server_time: '2026-01-01T00:00:02Z'
    });
    expect(next.hasMoreBefore).toBe(false);
    expect(next.beforeCursor).toBe(null);
  });

  it('applies sync ops only when cursor matches', () => {
    const state = applySnapshot(snapshot());
    const item = message({
      id: 'message:2',
      sort_key: '0000:000000000000002:000000:02:000000000',
      message_id: 'msg-2',
      content: 'second'
    });

    const result = applySyncResponse(
      state,
      syncResponse({
        ops: [{ op: 'upsert_item', item }]
      })
    );

    expect(result.outcome).toBe('applied');
    expect(result.state.cursor).toBe('cursor-2');
    expect(result.state.timelineItems.map((timelineItem) => timelineItem.id)).toEqual([
      'message:1',
      'message:2'
    ]);
  });

  it('keeps the canonical timeline identity stable for runtime-only frames', () => {
    const state = applySnapshot(snapshot({
      timeline: {
        items: [
          message(),
          message({
            id: 'message:3',
            sort_key: '0000:000000000000003:000000:02:000000000',
            message_id: 'msg-3',
            content: 'third'
          })
        ],
        has_more_before: false,
        before_cursor: null
      }
    }));
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [
          message({
            id: 'message:runtime',
            sort_key: '9998:999999999999999:000000:02:000000000',
            stable: false,
            status: 'running',
            message_id: 'turn-1',
            content: 'streaming',
            partial: true
          })
        ]
      }),
      server_time: '2026-01-01T00:00:02Z'
    };

    __chatV2SyncEngineTestHooks.resetCounters();
    const result = applyRealtimeFrame(state, frame);

    expect(result.outcome).toBe('applied');
    expect(result.state.timelineItems).toBe(state.timelineItems);
    expect(__chatV2SyncEngineTestHooks.counters().reconcileLocalItemsCalls).toBe(0);

    const visible = visibleTimelineItems(result.state);
    expect(visible.map((item) => item.id)).toEqual(['message:1', 'message:3', 'message:runtime']);
    expect(visible[0]).toBe(state.timelineItems[0]);
    expect(visible[1]).toBe(state.timelineItems[1]);
    expect(__chatV2SyncEngineTestHooks.counters().reconcileLocalItemsCalls).toBe(0);
  });

  it('reconciles local items once during a canonical transition and not in visible derives', () => {
    const optimistic = addOptimisticUserMessage(applySnapshot(snapshot({ timeline: { items: [], has_more_before: false } })), {
      content: 'queued',
      clientMessageId: 'cmsg-1',
      createdAt: '2026-01-01T00:00:00Z'
    });
    const canonicalEcho = message({
      id: 'user:cmsg-1',
      sort_key: '0000:000000000000001:000000:00:000000000',
      role: 'user',
      content: 'queued',
      message_id: 'server-msg-1',
      client_message_id: 'cmsg-1',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 1, event_type: 'user_message' }]
    });

    __chatV2SyncEngineTestHooks.resetCounters();
    const result = applySyncResponse(
      optimistic,
      syncResponse({
        cursor_before: 'cursor-1',
        cursor_after: 'cursor-2',
        ops: [{ op: 'upsert_item', item: canonicalEcho }]
      })
    );

    expect(result.outcome).toBe('applied');
    expect(result.state.localItems).toEqual([]);
    expect(__chatV2SyncEngineTestHooks.counters().reconcileLocalItemsCalls).toBe(1);

    expect(visibleTimelineItems(result.state).map((item) => item.id)).toEqual(['user:cmsg-1']);
    expect(visibleTimelineItems(result.state).map((item) => item.id)).toEqual(['user:cmsg-1']);
    expect(__chatV2SyncEngineTestHooks.counters().reconcileLocalItemsCalls).toBe(1);
  });

  it('does not downgrade open tool cycle state from partial sync metadata', () => {
    const state = applySnapshot(snapshot({
      timeline: {
        items: [message()],
        cycle_states: [{
          turn_id: 'turn-1',
          turn_cycle_index: 0,
          lifecycle_status: 'open',
          has_tool_activity: true
        }],
        has_more_before: false,
        before_cursor: null
      }
    }));

    const result = applySyncResponse(
      state,
      syncResponse({
        cycle_states: [{
          turn_id: 'turn-1',
          turn_cycle_index: 0,
          lifecycle_status: 'complete',
          has_tool_activity: false
        }]
      })
    );

    expect(result.state.cycleStates).toEqual([{
      turn_id: 'turn-1',
      turn_cycle_index: 0,
      lifecycle_status: 'open',
      has_tool_activity: true
    }]);
  });

  it('drops stale cycle state when the accepted runtime active turn changes', () => {
    const state = applySnapshot(snapshot({
      runtime: runtime(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        cycle_states: [{
          turn_id: 'turn-1',
          turn_cycle_index: 0,
          lifecycle_status: 'open',
          has_tool_activity: true
        }]
      }),
      timeline: {
        items: [message()],
        cycle_states: [{
          turn_id: 'turn-1',
          turn_cycle_index: 0,
          lifecycle_status: 'open',
          has_tool_activity: true
        }],
        has_more_before: false,
        before_cursor: null
      }
    }));

    const result = applySyncResponse(
      state,
      syncResponse({
        runtime: runtime(2, {
          has_active_turn: true,
          active_turn: { turn_id: 'turn-2', session_id: 'sess-1', status: 'running' },
          cycle_states: [{
            turn_id: 'turn-2',
            turn_cycle_index: 0,
            lifecycle_status: 'open',
            has_tool_activity: false
          }]
        })
      })
    );

    expect(result.state.cycleStates).toEqual([{
      turn_id: 'turn-2',
      turn_cycle_index: 0,
      lifecycle_status: 'open',
      has_tool_activity: false
    }]);
  });

  it('does not merge message items across roles when ids collide', () => {
    const state = applySnapshot(
      snapshot({
        timeline: {
          items: [
            message({
              id: 'message:collision',
              role: 'assistant',
              content: 'assistant text that must not leak',
              message_id: 'turn-1'
            })
          ],
          has_more_before: false,
          before_cursor: null
        }
      })
    );
    const queuedUser = message({
      id: 'message:collision',
      role: 'user',
      content: 'queued user text',
      message_id: 'client-message-1',
      client_message_id: 'client-message-1',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 2, event_type: 'user_message' }]
    });

    const result = applySyncResponse(
      state,
      syncResponse({
        ops: [{ op: 'upsert_item', item: queuedUser }]
      })
    );

    expect(result.state.timelineItems).toHaveLength(1);
    expect(result.state.timelineItems[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'queued user text',
      client_message_id: 'client-message-1'
    });
  });

  it('merges incremental tool result upserts with existing tool call fields', () => {
    const toolCall = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      stable: true,
      status: 'running',
      call_id: 'call-1',
      tool_name: 'bash',
      display_name: null,
      arguments_preview: '{"command":"true"}',
      result_preview: null,
      streamed_output: null,
      is_error: false,
      duration_ms: null,
      attachments: [],
      file_diffs: [],
      output_size: null,
      truncated: false,
      has_full_output: false,
      recovery_call_id: null,
      tool_output_artifact_id: null,
      evaluation: { decision: 'approve' }
    } as TimelineItem;
    const toolResult = {
      ...toolCall,
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 3, event_type: 'tool_result' }],
      status: 'complete',
      tool_name: 'tool',
      arguments_preview: null,
      result_preview: 'done',
      evaluation: null
    } as TimelineItem;
    const state = applySnapshot(snapshot({ timeline: { items: [toolCall], has_more_before: false } }));

    const result = applySyncResponse(
      state,
      syncResponse({ ops: [{ op: 'upsert_item', item: toolResult }] })
    );

    const merged = result.state.timelineItems[0];
    expect(merged.kind).toBe('tool_call');
    if (merged.kind !== 'tool_call') throw new Error('expected tool_call');
    expect(merged.tool_name).toBe('bash');
    expect(merged.arguments_preview).toBe('{"command":"true"}');
    expect(merged.result_preview).toBe('done');
    expect(merged.evaluation).toEqual({ decision: 'approve' });
  });

  it('merges live delegation progress/result onto the existing delegate tool card', () => {
    const toolCall = {
      id: 'tool:call-delegate',
      kind: 'tool_call',
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      stable: true,
      status: 'running',
      call_id: 'call-delegate',
      tool_name: 'delegate',
      display_name: null,
      arguments: { task: 'Inspect implementation' },
      arguments_preview: null,
      result_preview: null,
      streamed_output: null,
      is_error: false,
      duration_ms: null,
      attachments: [],
      file_diffs: [],
      output_size: null,
      truncated: false,
      has_full_output: false,
      recovery_call_id: null,
      tool_output_artifact_id: null,
      evaluation: null,
      delegation: {
        child_session_id: 'sess-child',
        status: 'running',
        title: 'Inspect implementation',
        started_at: '2026-01-01T00:00:00+00:00',
        todos: [{ content: 'old todo', status: 'in_progress' }],
        tool_call_count: 1,
        last_tool: 'grep'
      }
    } as TimelineItem;
    const state = applySnapshot(snapshot({
      timeline: { items: [toolCall], has_more_before: false }
    }));

    const nextRuntime = runtime(2, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [
        {
          ...toolCall,
          stable: false,
          status: 'complete',
          delegation: {
            child_session_id: 'sess-child',
            status: 'completed',
            duration_ms: 1234,
            todos: [],
            result_summary: 'Done',
            result_content: '### Summary\nDone'
          }
        } as TimelineItem
      ]
    });

    const merged = visibleTimelineItems({ ...state, runtime: nextRuntime })[0];
    expect(merged.kind).toBe('tool_call');
    if (merged.kind !== 'tool_call') throw new Error('expected tool_call');
    expect(merged.arguments).toEqual({ task: 'Inspect implementation' });
    expect(merged.delegation?.tool_call_count).toBe(1);
    expect(merged.delegation?.last_tool).toBe('grep');
    expect(merged.delegation?.started_at).toBe('2026-01-01T00:00:00+00:00');
    expect(merged.delegation?.duration_ms).toBe(1234);
    expect(merged.delegation?.todos).toEqual([]);
    expect(merged.delegation?.status).toBe('completed');
    expect(merged.delegation?.result_content).toBe('### Summary\nDone');
  });

  it('merges backfilled tool calls with newer tool results', () => {
    const newerResult = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '0000:000000000000003:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 3, event_type: 'tool_result' }],
      stable: true,
      status: 'complete',
      call_id: 'call-1',
      tool_name: 'tool',
      display_name: null,
      arguments_preview: null,
      result_preview: 'done',
      streamed_output: null,
      is_error: true,
      duration_ms: 123,
      attachments: [],
      file_diffs: [],
      output_size: 456,
      truncated: true,
      has_full_output: true,
      recovery_call_id: null,
      tool_output_artifact_id: 'artifact-1',
      evaluation: null
    } as TimelineItem;
    const olderCall = {
      ...newerResult,
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      status: 'running',
      tool_name: 'bash',
      arguments_preview: '{"command":"true"}',
      result_preview: null,
      evaluation: { decision: 'approve' }
    } as TimelineItem;
    const state = applySnapshot(snapshot({ timeline: { items: [newerResult], has_more_before: true } }));

    const next = applyBackfill(state, {
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      items: [olderCall],
      has_more_before: false,
      before_cursor: null,
      server_time: '2026-01-01T00:00:02Z'
    });

    const merged = next.timelineItems[0];
    expect(merged.kind).toBe('tool_call');
    if (merged.kind !== 'tool_call') throw new Error('expected tool_call');
    expect(merged.status).toBe('complete');
    expect(merged.tool_name).toBe('bash');
    expect(merged.arguments_preview).toBe('{"command":"true"}');
    expect(merged.result_preview).toBe('done');
    expect(merged.evaluation).toEqual({ decision: 'approve' });
    expect(merged.is_error).toBe(true);
    expect(merged.duration_ms).toBe(123);
    expect(merged.output_size).toBe(456);
    expect(merged.truncated).toBe(true);
    expect(merged.has_full_output).toBe(true);
    expect(merged.tool_output_artifact_id).toBe('artifact-1');
  });

  it('never drops or reorders existing newer items when older history is prepended', () => {
    // Regression guard: backfill is a pure prepend/upsert. The newer items the
    // user was reading (and any live streaming tail) must survive untouched and
    // stay sorted after the prepended older page.
    const olderPage = [0, 1, 2].map((seq) => message({
      id: `message:old-${seq}`,
      sort_key: `0000:00000000000000${seq}:000000:02:000000000`,
      message_id: `old-${seq}`,
      content: `older ${seq}`
    }));
    const newer = [7, 8].map((seq) => message({
      id: `message:new-${seq}`,
      sort_key: `0000:00000000000000${seq}:000000:02:000000000`,
      message_id: `new-${seq}`,
      content: `newer ${seq}`
    }));
    // A live streaming assistant in the runtime band (9998) — the active tail.
    const liveTail = {
      ...message({
        id: 'message:live:phase:0',
        message_id: 'live',
        content: 'streaming…',
        stable: false,
        partial: true,
        status: 'running'
      }),
      sort_key: '9998:999999999999999:000000:02:000000000'
    } as TimelineItem;

    const state = applySnapshot(snapshot({
      timeline: { items: [...newer, liveTail], has_more_before: true },
      runtime: runtime(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [liveTail]
      })
    }));
    const beforeIds = state.timelineItems.map((item) => item.id);

    const next = applyBackfill(state, {
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      items: olderPage,
      has_more_before: false,
      before_cursor: null,
      server_time: '2026-01-01T00:00:02Z'
    });

    const afterIds = next.timelineItems.map((item) => item.id);
    // Every pre-existing item is still present (no drops).
    for (const id of beforeIds) {
      expect(afterIds).toContain(id);
    }
    // Older page is prepended; the newer items keep their relative order and
    // the live tail remains last.
    expect(afterIds).toEqual([
      'message:old-0', 'message:old-1', 'message:old-2',
      'message:new-7', 'message:new-8',
      'message:live:phase:0'
    ]);
  });

  it('ignores duplicate realtime frames where cursor_after is already local', () => {
    const state = applySnapshot(snapshot({ cursor: 'cursor-2' }));
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [{ op: 'remove_item', id: 'message:1' }],
      runtime: null,
      server_time: '2026-01-01T00:00:02Z'
    };

    const result = applyRealtimeFrame(state, frame);

    expect(result.outcome).toBe('duplicate');
    expect(result.state.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('marks state gapped on cursor mismatch', () => {
    const state = applySnapshot(snapshot());

    const result = applySyncResponse(
      state,
      syncResponse({
        cursor_before: 'missing-cursor',
        cursor_after: 'cursor-3'
      })
    );

    expect(result.outcome).toBe('cursor_mismatch');
    expect(result.state.syncStatus).toBe('gapped');
  });

  it('rejects a late canonical-recovery sync response for A that resolves after A->B->A switching advanced state past its basis cursor', () => {
    // Regression scenario for the agent-direct cached-restore path: a
    // recoverChatV2Canonical('A') sync request is in flight (basis
    // cursor-1) when the user switches to B and back to A. The page-level
    // route guard alone (conversationId === route) cannot distinguish this
    // late response from a fresh one, because both target the same
    // conversationId once the user is back on A. Correctness instead relies
    // on this cursor_before check: by the time the stale response resolves,
    // either (a) nothing else advanced the state past cursor-1 -- in which
    // case cursor_before matches and the response is legitimately applied,
    // exactly as if no switch had happened -- or (b) a coalesced reissue (or
    // a live frame) already advanced state to cursor-2 first, in which case
    // the mismatch below is what protects the restored view.
    const state = applySnapshot(snapshot({ cursor: 'cursor-2' }));

    const staleResponse = syncResponse({
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2b'
    });

    const result = applySyncResponse(state, staleResponse);

    expect(result.outcome).toBe('cursor_mismatch');
    expect(result.state.cursor).toBe('cursor-2');
  });

  it('does not mark gapped for stale cursor-preserving runtime-only frames', () => {
    const state = applySnapshot(snapshot({ cursor: 'cursor-3', runtime: runtime(1) }));
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: true, volatile_items: [] }),
      server_time: '2026-01-01T00:00:02Z'
    };

    const result = applyRealtimeFrame(state, frame);

    expect(result.outcome).toBe('applied');
    expect(result.state.syncStatus).toBe('ready');
    expect(result.state.cursor).toBe('cursor-3');
    expect(result.state.runtime?.runtime_revision).toBe(2);
  });

  it('accepts server reset responses before cursor reconciliation', () => {
    const state = applySnapshot(snapshot({ cursor: 'cursor-3', runtime: runtime(1) }));

    const result = applySyncResponse(
      state,
      syncResponse({
        cursor_before: 'cursor-1',
        cursor_after: 'cursor-1',
        reset_required: true,
        reset_reason: 'server_restart_lost_runtime',
        ops: [],
        runtime: runtime(2)
      })
    );

    expect(result.outcome).toBe('reset_required');
    expect(result.state.syncStatus).toBe('gapped');
    expect(result.state.cursor).toBe('cursor-3');
  });

  it('ignores older cursor-preserving runtime-only frames without recovery', () => {
    const state = applySnapshot(snapshot({ cursor: 'cursor-3', runtime: runtime(3) }));
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2),
      server_time: '2026-01-01T00:00:02Z'
    };

    const result = applyRealtimeFrame(state, frame);

    expect(result.outcome).toBe('duplicate');
    expect(result.state.syncStatus).toBe('ready');
    expect(result.state.runtime?.runtime_revision).toBe(3);
  });

  it('marks state gapped on explicit reset', () => {
    const state = applySnapshot(snapshot());

    const result = applySyncResponse(
      state,
      syncResponse({
        reset_required: true,
        reset_reason: 'range_too_large'
      })
    );

    expect(result.outcome).toBe('reset_required');
    expect(result.resetReason).toBe('range_too_large');
    expect(result.state.syncStatus).toBe('gapped');
  });

  it('applies only newer runtime overlays for the same active turn', () => {
    const current = runtime(3, {
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-1',
        session_id: 'sess-1',
        status: 'running'
      }
    });
    const olderSameTurn = runtime(2, {
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-1',
        session_id: 'sess-1',
        status: 'running'
      }
    });
    const newerSameTurn = runtime(4, {
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-1',
        session_id: 'sess-1',
        status: 'running'
      }
    });

    expect(maybeApplyRuntime(current, olderSameTurn)).toBe(current);
    expect(maybeApplyRuntime(current, newerSameTurn)?.runtime_revision).toBe(4);
  });

  it('merges context-usage-only runtime frames without dropping volatile items', () => {
    const liveMessage = message({
      id: 'message:live',
      stable: false,
      status: 'running',
      partial: true,
      content: 'streaming',
      turn_id: 'turn-1'
    });
    const current = runtime(2, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [liveMessage]
    });
    const incoming = runtime(3, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [],
      context_usage: {
        prompt_tokens: 42_000,
        max_context_tokens: 128_000,
        percentage: 32.8,
        model: 'test-model',
        reasoning_effort: null,
        projection_policy: { phase: 'within_turn', pressure_mode: 'normal' }
      }
    });

    const merged = maybeApplyRuntime(current, incoming);

    expect(merged?.context_usage?.prompt_tokens).toBe(42_000);
    expect(merged?.volatile_items.map((item) => item.id)).toEqual(['message:live']);
  });

  it('replaces runtime when a newer active turn arrives even with a lower revision', () => {
    // Revision counter regressed (e.g. restart) but the new turn is generated
    // later, so it must win despite the lower revision.
    const current = runtime(7, {
      generated_at: '2026-01-01T00:00:05Z',
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-old',
        session_id: 'sess-1',
        status: 'running'
      },
      volatile_items: [
        message({
          id: 'message:old-live',
          stable: false,
          content: 'old answer',
          turn_id: 'turn-old'
        })
      ]
    });
    const incoming = runtime(1, {
      generated_at: '2026-01-01T00:00:09Z',
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-new',
        session_id: 'sess-1',
        status: 'running'
      },
      volatile_items: []
    });

    expect(maybeApplyRuntime(current, incoming)).toBe(incoming);
  });

  it('clears active runtime when a same-or-newer inactive runtime arrives with a lower revision', () => {
    const current = runtime(7, {
      generated_at: '2026-01-01T00:00:05Z',
      has_active_turn: true,
      active_turn: {
        turn_id: 'turn-old',
        session_id: 'sess-1',
        status: 'running'
      },
      volatile_items: [message({ id: 'message:old-live', stable: false, content: 'old answer' })]
    });
    const inactive = runtime(1, {
      generated_at: '2026-01-01T00:00:06Z',
      has_active_turn: false,
      active_turn: null,
      volatile_items: []
    });

    expect(maybeApplyRuntime(current, inactive)).toBe(inactive);
  });

  it('does NOT let a stale inactive runtime clobber a newer active turn', () => {
    // A delayed settle/sync for turn-old arrives AFTER turn-new started
    // streaming. It must not clear the newer active overlay (which would then
    // freeze turn-new via carry + completed-guard).
    const currentActive = runtime(2, {
      generated_at: '2026-01-01T00:00:10Z',
      has_active_turn: true,
      active_turn: { turn_id: 'turn-new', session_id: 'sess-1', status: 'running' },
      volatile_items: [
        message({ id: 'message:new-live', stable: false, content: 'new answer', turn_id: 'turn-new' })
      ]
    });
    const staleInactive = runtime(9, {
      generated_at: '2026-01-01T00:00:03Z',
      has_active_turn: false,
      active_turn: null,
      volatile_items: []
    });

    expect(maybeApplyRuntime(currentActive, staleInactive)).toBe(currentActive);
  });

  it('does NOT let a stale different-turn active runtime clobber a newer active turn', () => {
    const currentActive = runtime(2, {
      generated_at: '2026-01-01T00:00:10Z',
      has_active_turn: true,
      active_turn: { turn_id: 'turn-new', session_id: 'sess-1', status: 'running' }
    });
    const staleOtherTurn = runtime(9, {
      generated_at: '2026-01-01T00:00:03Z',
      has_active_turn: true,
      active_turn: { turn_id: 'turn-old', session_id: 'sess-1', status: 'running' }
    });

    expect(maybeApplyRuntime(currentActive, staleOtherTurn)).toBe(currentActive);
  });

  it('replaces runtime wholesale on an epoch change (process restart / replica)', () => {
    const current = runtime(50, {
      runtime_epoch: 'epoch-old',
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' }
    });
    const restarted = runtime(1, {
      runtime_epoch: 'epoch-new',
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' }
    });

    expect(maybeApplyRuntime(current, restarted)).toBe(restarted);
  });

  it('does not terminalize a newer active turn when a stale inactive snapshot is rejected', () => {
    // Active turn-new is streaming.
    const active = applySnapshot(
      snapshot({
        timeline: { items: [], has_more_before: false, before_cursor: null },
        runtime: runtime(2, {
          generated_at: '2026-01-01T00:00:10Z',
          has_active_turn: true,
          active_turn: { turn_id: 'turn-new', session_id: 'sess-1', status: 'running' },
          volatile_items: [
            message({ id: 'message:new-live', stable: false, status: 'running', content: 'new', turn_id: 'turn-new' })
          ]
        })
      })
    );
    expect(active.runtime?.has_active_turn).toBe(true);

    // A stale inactive snapshot (delayed settle for a prior turn) arrives.
    const afterStale = applySnapshot(
      snapshot({
        timeline: { items: [], has_more_before: false, before_cursor: null },
        runtime: runtime(9, {
          generated_at: '2026-01-01T00:00:03Z',
          has_active_turn: false,
          active_turn: null,
          volatile_items: []
        })
      }),
      active
    );

    // The stale inactive runtime must be rejected...
    expect(afterStale.runtime?.has_active_turn).toBe(true);
    // ...and it must NOT have terminalized the live item into localItems.
    expect(afterStale.localItems.map((item) => item.id)).not.toContain('message:new-live');
    const live = visibleTimelineItems(afterStale).find((item) => item.id === 'message:new-live');
    expect(live?.status).toBe('running');
  });

  it('treats fresh snapshots as authoritative over older local runtime overlays', () => {
    const oldLive = message({
      id: 'message:old-live',
      stable: false,
      content: 'old answer',
      turn_id: 'turn-old'
    });
    const previous = applySnapshot(
      snapshot({
        runtime: runtime(7, {
          generated_at: '2026-01-01T00:00:05Z',
          has_active_turn: true,
          active_turn: { turn_id: 'turn-old', session_id: 'sess-1', status: 'running' },
          volatile_items: [oldLive]
        })
      })
    );
    const fresh = applySnapshot(
      snapshot({
        runtime: runtime(1, {
          generated_at: '2026-01-01T00:00:09Z',
          has_active_turn: true,
          active_turn: { turn_id: 'turn-new', session_id: 'sess-1', status: 'running' },
          volatile_items: []
        })
      }),
      previous
    );

    expect(fresh.runtime?.active_turn?.turn_id).toBe('turn-new');
    expect(visibleTimelineItems(fresh).map((item) => item.id)).not.toContain('message:old-live');
  });

  it('merges volatile runtime items into visible timeline without replacing canonical state', () => {
    const state = applySnapshot(snapshot());
    const volatile = message({
      id: 'message:volatile',
      stable: false,
      sort_key: '0000:000000000000002:000000:02:000000000',
      message_id: 'msg-live',
      content: 'live'
    });
    const next = {
      ...state,
      runtime: runtime(2, { has_active_turn: true, volatile_items: [volatile] })
    };

    expect(visibleTimelineItems(next).map((item) => item.id)).toEqual(['message:1', 'message:volatile']);
    expect(next.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('merges per-block thinking runtime and canonical items 1:1 without duplication', () => {
    // Runtime overlay now emits one item PER block, keyed identically to the
    // canonical projector. During the live turn the overlay carries the blocks;
    // when canonical /sync delivers the same per-block ids they must merge, not
    // duplicate. This is the multi-block regression the single-card projection
    // caused (extra blocks arrived as unmatched canonical ids → duplicates that
    // only healed on page refresh).
    const thinkingBlock = (blockId: string, seq: number, complete: boolean): TimelineItem =>
      ({
        id: `thinking:msg-1:phase:0:${blockId}`,
        kind: 'thinking',
        sort_key: `9998:999999999999999:000000:01:00000000${seq}`,
        source_refs: [],
        stable: false,
        status: complete ? 'complete' : 'running',
        message_id: 'msg-1',
        turn_id: 'turn-1',
        blocks: [{ id: blockId, content: `block ${blockId}`, status: complete ? 'complete' : 'running' }],
        active_title: null
      }) as TimelineItem;

    // Live: two per-block runtime thinking items in the overlay.
    const live = {
      ...applySnapshot(snapshot({ timeline: { items: [], has_more_before: false, before_cursor: null } })),
      runtime: runtime(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [thinkingBlock('blk_1', 0, true), thinkingBlock('blk_2', 1, false)]
      })
    };
    expect(visibleTimelineItems(live).map((item) => item.id)).toEqual([
      'thinking:msg-1:phase:0:blk_1',
      'thinking:msg-1:phase:0:blk_2'
    ]);

    // Canonical /sync delivers the same two per-block ids with real seq keys
    // and a settled (inactive) runtime. No duplicates; canonical keys win.
    const canonicalBlock = (blockId: string, seq: number): TimelineItem =>
      ({
        id: `thinking:msg-1:phase:0:${blockId}`,
        kind: 'thinking',
        sort_key: `0000:00000000000000${seq}:000000:01:000000000`,
        source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'assistant_thinking' }],
        stable: true,
        status: 'complete',
        message_id: 'msg-1',
        turn_id: 'turn-1',
        blocks: [{ id: blockId, content: `block ${blockId}`, status: 'complete' }],
        active_title: null
      }) as TimelineItem;

    const settled = applySnapshot(
      snapshot({
        timeline: {
          items: [canonicalBlock('blk_1', 1), canonicalBlock('blk_2', 2)],
          has_more_before: false,
          before_cursor: null
        },
        runtime: runtime(3, { generated_at: '2026-01-01T00:00:09Z', has_active_turn: false })
      }),
      live
    );

    const ids = visibleTimelineItems(settled).map((item) => item.id);
    expect(ids).toEqual(['thinking:msg-1:phase:0:blk_1', 'thinking:msg-1:phase:0:blk_2']);
    // No duplicates.
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('keeps optimistic user messages in chat v2 state until canonical echo arrives', () => {
    const state = addOptimisticUserMessage(applySnapshot(snapshot({ timeline: { items: [], has_more_before: false } })), {
      content: 'hello from user',
      clientMessageId: 'cmsg-1'
    });

    expect(visibleTimelineItems(state)).toMatchObject([
      { kind: 'message', role: 'user', content: 'hello from user', client_message_id: 'cmsg-1' }
    ]);

    const refreshed = applySnapshot(
      snapshot({ timeline: { items: [], has_more_before: false } }),
      state
    );

    expect(visibleTimelineItems(refreshed).map((item) => item.id)).toEqual(['local-user:cmsg-1']);

    const canonical = message({
      id: 'user:cmsg-1',
      role: 'user',
      content: 'hello from user',
      message_id: 'user-msg-1',
      client_message_id: 'cmsg-1',
      sort_key: '0000:000000000000001:000000:00:000000000'
    });
    const echoed = applySnapshot(
      snapshot({ timeline: { items: [canonical], has_more_before: false } }),
      refreshed
    );

    expect(visibleTimelineItems(echoed).map((item) => item.id)).toEqual(['user:cmsg-1']);
  });

  it('keeps local slash command system messages visible across snapshot refreshes', () => {
    const state = addLocalSystemMessage(applySnapshot(snapshot({ timeline: { items: [], has_more_before: false } })), {
      id: 'local-command:profile',
      content: 'Agent profile switched to: fast'
    });

    expect(visibleTimelineItems(state)).toMatchObject([
      { id: 'local-command:profile', kind: 'message', role: 'system', content: 'Agent profile switched to: fast' }
    ]);

    const refreshed = applySnapshot(
      snapshot({ timeline: { items: [], has_more_before: false } }),
      state
    );

    expect(visibleTimelineItems(refreshed).map((item) => item.id)).toEqual(['local-command:profile']);
  });

  it('merges runtime tool updates without dropping canonical arguments or evaluation', () => {
    const canonicalTool = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      stable: true,
      status: 'running',
      call_id: 'call-1',
      tool_name: 'read',
      arguments: { file_path: '/tmp/x.py', offset: 780, limit: 55 },
      arguments_preview: null,
      evaluation: { decision: 'approve', risk: 'low' },
      attachments: [],
      file_diffs: [],
      is_error: false,
      truncated: false,
      has_full_output: false
    } as TimelineItem;
    const volatileTool = {
      ...canonicalTool,
      stable: false,
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'tool_call' }],
      result_preview: 'output',
      // The runtime overlay carries no structured arguments; it must NOT
      // clobber the canonical dict (Bug A: read/grep showed {"preview": ...}).
      arguments: null,
      arguments_preview: null,
      evaluation: null
    } as TimelineItem;
    const state = {
      ...applySnapshot(snapshot({ timeline: { items: [canonicalTool], has_more_before: false } })),
      runtime: runtime(2, { has_active_turn: true, volatile_items: [volatileTool] })
    };

    expect(visibleTimelineItems(state)[0]).toMatchObject({
      kind: 'tool_call',
      arguments: { file_path: '/tmp/x.py', offset: 780, limit: 55 },
      result_preview: 'output',
      evaluation: { decision: 'approve', risk: 'low' }
    });
  });

  it('preserves apply_patch progress across a progress-less canonical merge', () => {
    // The runtime overlay carries live apply_patch progress; a later
    // progress-less canonical/settle merge must not null it out (Bug B).
    const runtimeTool = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '9998:999999999999999:000000:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'tool_call' }],
      stable: false,
      status: 'running',
      call_id: 'call-1',
      tool_name: 'apply_patch',
      arguments: null,
      arguments_preview: null,
      progress_phase: 'preparing_input',
      progress_input_chars: 1234,
      progress_input_lines: 42,
      progress_complete: false,
      attachments: [],
      file_diffs: [],
      is_error: false,
      truncated: false,
      has_full_output: false
    } as TimelineItem;
    const canonicalTool = {
      ...runtimeTool,
      stable: true,
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      progress_phase: null,
      progress_input_chars: null,
      progress_input_lines: null,
      progress_complete: null
    } as TimelineItem;
    const state = {
      ...applySnapshot(snapshot({ timeline: { items: [canonicalTool], has_more_before: false } })),
      runtime: runtime(2, { has_active_turn: true, volatile_items: [runtimeTool] })
    };

    expect(visibleTimelineItems(state)[0]).toMatchObject({
      kind: 'tool_call',
      progress_phase: 'preparing_input',
      progress_input_chars: 1234,
      progress_input_lines: 42
    });
  });

  it('keeps multi-phase assistant segments as distinct items (no collapse)', () => {
    // A multi-phase turn persists one assistant_message per phase, all sharing
    // message_id = turn_id but with phase-aware ids. Each must survive as its
    // own item — a phase-1 runtime item must NOT overwrite a phase-0 canonical
    // item (regression guard for "mid-turn assistant messages lost").
    const phase0 = message({
      id: 'message:turn-1:phase:0',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      assistant_phase_index: 0,
      content: 'first segment',
      sort_key: '0000:000000000000002:000000:02:000000000'
    } as Partial<TimelineItem>);
    const phase1Canonical = message({
      id: 'message:turn-1:phase:1',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      assistant_phase_index: 1,
      content: 'final segment',
      sort_key: '0000:000000000000005:000001:02:000000000'
    } as Partial<TimelineItem>);
    const phase1Runtime = {
      ...phase1Canonical,
      stable: false,
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_message' }],
      sort_key: '9998:999999999999999:000001:02:000000000'
    } as TimelineItem;

    const state = {
      ...applySnapshot(
        snapshot({ timeline: { items: [phase0, phase1Canonical], has_more_before: false } })
      ),
      runtime: runtime(2, { has_active_turn: true, volatile_items: [phase1Runtime] })
    };

    const visible = visibleTimelineItems(state);
    const messages = visible.filter((item) => item.kind === 'message');
    expect(messages.map((item) => item.id)).toEqual([
      'message:turn-1:phase:0',
      'message:turn-1:phase:1'
    ]);
    expect(messages.map((item) => (item.kind === 'message' ? item.content : ''))).toEqual([
      'first segment',
      'final segment'
    ]);
  });

  it('retains active thinking as complete when a tool-boundary runtime frame arrives', () => {
    const thinking = {
      id: 'thinking:msg-1:phase:0:block-1',
      kind: 'thinking',
      sort_key: '9998:999999999999999:000000:01:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'thinking' }],
      stable: false,
      status: 'running',
      message_id: 'msg-1',
      turn_id: 'turn-1',
      blocks: [{ id: 'block-1', title: 'Thinking', content: 'work', status: 'running' }]
    } as TimelineItem;
    const tool = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '9998:999999999999999:000000:03:000000001',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'tool_call' }],
      stable: false,
      status: 'running',
      call_id: 'call-1',
      tool_name: 'bash',
      attachments: [],
      file_diffs: [],
      is_error: false,
      truncated: false,
      has_full_output: false
    } as TimelineItem;
    const base = applySnapshot(snapshot({ runtime: runtime(1, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [thinking]
    }) }));

    const result = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [tool]
      }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(visibleTimelineItems(result.state).map((item) => item.id)).toEqual([
      'message:1',
      'thinking:msg-1:phase:0:block-1',
      'tool:call-1'
    ]);
    expect(visibleTimelineItems(result.state)[1]).toMatchObject({
      kind: 'thinking',
      status: 'complete',
      blocks: [expect.objectContaining({ status: 'complete' })]
    });
  });

  it('ignores volatile runtime items after the active turn has settled', () => {
    const state = applySnapshot(snapshot());
    const volatile = message({
      id: 'message:volatile',
      stable: false,
      sort_key: '0000:000000000000002:000000:02:000000000',
      message_id: 'msg-live',
      content: 'live'
    });
    const next = {
      ...state,
      runtime: runtime(2, { has_active_turn: false, volatile_items: [volatile] })
    };

    expect(visibleTimelineItems(next).map((item) => item.id)).toEqual(['message:1']);
  });

  it('carries prior volatile items as finalized locals when runtime settles before canonical sync', () => {
    const thinking = {
      id: 'thinking:msg-1:phase:0:block-1',
      kind: 'thinking',
      sort_key: '9998:999999999999999:000000:01:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'thinking' }],
      stable: false,
      status: 'running',
      message_id: 'msg-1',
      turn_id: 'turn-1',
      blocks: [{ id: 'block-1', title: 'Thinking', content: 'work', status: 'running' }]
    } as TimelineItem;
    const base = applySnapshot(snapshot({ runtime: runtime(1, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [thinking]
    }) }));

    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: false, volatile_items: [] }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(visibleTimelineItems(settled.state).map((item) => item.id)).toEqual([
      'message:1',
      'thinking:msg-1:phase:0:block-1'
    ]);
    expect(visibleTimelineItems(settled.state)[1]).toMatchObject({
      kind: 'thinking',
      status: 'complete',
      blocks: [expect.objectContaining({ status: 'complete' })]
    });
  });

  it('drops an incomplete progress-only apply_patch card when the turn settles', () => {
    const patchProgress = {
      id: 'tool:incomplete-patch',
      kind: 'tool_call',
      sort_key: '9998:999999999999999:000000:03:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'tool_call' }],
      stable: false,
      status: 'running',
      call_id: 'incomplete-patch',
      tool_name: 'apply_patch',
      arguments: null,
      arguments_preview: null,
      result_preview: null,
      streamed_output: null,
      attachments: [],
      file_diffs: [],
      is_error: false,
      truncated: false,
      has_full_output: false,
      progress_phase: 'preparing_input',
      progress_input_chars: 120,
      progress_input_lines: 4,
      progress_complete: false,
      turn_id: 'turn-1'
    } as TimelineItem;
    const base = applySnapshot(snapshot({ runtime: runtime(1, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [patchProgress]
    }) }));

    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: false, volatile_items: [] }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(visibleTimelineItems(settled.state).map((item) => item.id)).toEqual(['message:1']);
  });

  it('drops a transient Intaris recovery system notice when the turn settles', () => {
    const recoveryNotice = {
      id: 'system:intaris-recovery:turn-1:intaris_append',
      kind: 'message',
      sort_key: '9998:999999999999999:000000:09:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'system_message' }],
      stable: false,
      status: 'complete',
      role: 'system',
      content: 'Paused while Intaris recovers.',
      message_id: 'intaris-recovery:turn-1:intaris_append',
      notice_id: 'intaris-recovery:turn-1:intaris_append',
      notice_kind: 'intaris_recovery',
      notice_scope: 'transient_retry',
      turn_id: 'turn-1',
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = applySnapshot(snapshot({ runtime: runtime(1, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [recoveryNotice]
    }) }));

    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: false, volatile_items: [recoveryNotice] }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(visibleTimelineItems(settled.state).map((item) => item.id)).toEqual(['message:1']);
  });

  it('carries the streamed final message on a CURSOR-SKEWED settle frame', () => {
    // Regression: once the client cursor advances past the subscribe-time
    // server cursor (after the first REST sync), every WS runtime frame arrives
    // with cursor_before === cursor_after !== state.cursor. The settle frame on
    // this skewed path used to skip carrySettledRuntimeItems, so the just-
    // streamed final assistant message (a runtime-only volatile item) dropped
    // out of the visible timeline and stayed gone until reload.
    const streamedFinal = {
      id: 'message:msg-final:phase:1',
      kind: 'message',
      sort_key: '9998:999999999999999:000001:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_message' }],
      stable: false,
      role: 'assistant',
      content: 'Here is the final answer.',
      message_id: 'msg-final',
      turn_id: 'turn-1',
      turn_cycle_index: 1,
      attachments: [],
      partial: true,
      status: 'running'
    } as TimelineItem;
    // Advance the client cursor so the frame is cursor-skewed relative to it.
    const base = {
      ...applySnapshot(snapshot({ runtime: runtime(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [streamedFinal]
      }) })),
      cursor: 'cursor-advanced'
    };

    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: false, volatile_items: [] }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(settled.outcome).toBe('applied');
    const visible = visibleTimelineItems(settled.state);
    expect(visible.map((item) => item.id)).toContain('message:msg-final:phase:1');
    expect(visible.find((item) => item.id === 'message:msg-final:phase:1')).toMatchObject({
      status: 'complete',
      partial: false
    });
  });

  it('preserves the streamed turn_cycle_index when the completion frame carries null', () => {
    // Regression: the completion frame shares the streamed item id. The server
    // now sends turn_cycle_index=null when the final cycle is unknown (instead
    // of coercing to 0). The client merge is `incoming ?? existing`, so the
    // correct streamed cycle (1) must survive rather than being clobbered to a
    // colliding value that would fold the settled answer into a tool group.
    const streamed = {
      id: 'message:msg-final:phase:1',
      kind: 'message',
      sort_key: '9998:999999999999999:000001:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_stream' }],
      stable: false,
      role: 'assistant',
      content: 'Streaming…',
      message_id: 'msg-final',
      turn_id: 'turn-1',
      turn_cycle_index: 1,
      attachments: [],
      partial: true,
      status: 'running'
    } as TimelineItem;
    const base = applySnapshot(snapshot({ runtime: runtime(1, {
      has_active_turn: true,
      active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
      volatile_items: [streamed]
    }) }));

    // The completion frame carries the SAME item id, updated content, and a
    // null cycle (final cycle unknown server-side). The turn is still active
    // (continuation pending) so the runtime overlay merges the item rather than
    // replacing wholesale — exercising the `incoming ?? existing` merge.
    const completion = {
      ...streamed,
      content: 'Streaming… done.',
      turn_cycle_index: null,
      partial: false,
      status: 'complete'
    } as TimelineItem;

    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [completion]
      }),
      server_time: '2026-01-01T00:00:02Z'
    });

    expect(settled.outcome).toBe('applied');
    const visible = visibleTimelineItems(settled.state);
    const final = visible.find((item) => item.id === 'message:msg-final:phase:1');
    expect(final).toMatchObject({ content: 'Streaming… done.', turn_cycle_index: 1 });
  });

  it('terminalizes stale stable running items after the active turn has settled', () => {
    const runningThinking = {
      id: 'thinking:running',
      kind: 'thinking',
      sort_key: '0000:000000000000002:000000:01:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'thinking_delta' }],
      stable: true,
      message_id: 'msg-1',
      status: 'running',
      blocks: [{ id: 'block-1', title: 'Thinking', content: 'still live', status: 'running' }]
    } as TimelineItem;
    const state = {
      ...applySnapshot(snapshot({ timeline: { items: [runningThinking], has_more_before: false } })),
      runtime: runtime(2, { has_active_turn: false })
    };

    expect(visibleTimelineItems(state)[0]).toMatchObject({
      kind: 'thinking',
      status: 'complete',
      blocks: [expect.objectContaining({ status: 'complete' })]
    });
  });

  it('clears the partial flag on a settled assistant message', () => {
    // A `partial` assistant message is treated as "live" by the activity-fold
    // gate, which then requires backend cycle-state confirmation to fold. At
    // settle the turn's cycle states are dropped, so a retained `partial` flag
    // deadlocks folding until a full canonical sync replaces the item. Settle
    // must clear it.
    const partialAssistant = message({
      id: 'message:partial',
      sort_key: '0000:000000000000002:000000:02:000000000',
      message_id: 'msg-partial',
      status: 'running',
      partial: true
    });
    const state = {
      ...applySnapshot(snapshot({ timeline: { items: [partialAssistant], has_more_before: false } })),
      runtime: runtime(2, { has_active_turn: false })
    };

    const settled = visibleTimelineItems(state)[0];
    expect(settled).toMatchObject({ kind: 'message', status: 'complete', partial: false });
  });

  it('preserves backfilled older history across a snapshot replace', () => {
    // The user loaded older pages via backfill, then a view refresh fetched a
    // fresh snapshot (which only contains the latest page). The older items
    // must survive — dropping them collapses the content above the viewport
    // and lands the restored scroll position on different content.
    const older = message({
      id: 'message:0',
      sort_key: '0000:000000000000000:000000:02:000000000',
      message_id: 'msg-0',
      content: 'older backfilled message'
    });
    const latest = message();
    const previous = applyBackfill(
      applySnapshot(snapshot({ timeline: { items: [latest], has_more_before: true, before_cursor: 'cursor-0' } })),
      {
        schema_version: 2,
        server_time: '2026-01-01T00:00:01Z',
        conversation_id: 'conv-1',
        projection_version: 'chat-v2-test',
        items: [older],
        has_more_before: false,
        before_cursor: null
      }
    );
    expect(previous.timelineItems.map((item) => item.id)).toEqual(['message:0', 'message:1']);

    const refreshed = applySnapshot(
      snapshot({ timeline: { items: [latest], has_more_before: true, before_cursor: 'cursor-0' } }),
      previous
    );

    expect(refreshed.timelineItems.map((item) => item.id)).toEqual(['message:0', 'message:1']);
  });

  it('does not preserve older history when the snapshot covers the full timeline', () => {
    const older = message({
      id: 'message:0',
      sort_key: '0000:000000000000000:000000:02:000000000',
      message_id: 'msg-0',
      content: 'stray older item'
    });
    const previous = {
      ...applySnapshot(snapshot()),
      timelineItems: [older, message()]
    };

    // has_more_before=false: the snapshot is the complete history — anything
    // older in local state is stale and must be dropped.
    const refreshed = applySnapshot(snapshot(), previous);
    expect(refreshed.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('does not preserve older history across a projection version change', () => {
    const older = message({
      id: 'message:0',
      sort_key: '0000:000000000000000:000000:02:000000000',
      message_id: 'msg-0'
    });
    const previous = {
      ...applySnapshot(snapshot({ timeline: { items: [message()], has_more_before: true, before_cursor: 'cursor-0' } })),
      timelineItems: [older, message()]
    };

    const refreshed = applySnapshot(
      snapshot({
        projection_version: 'chat-v2-next',
        timeline: { items: [message()], has_more_before: true, before_cursor: 'cursor-0' }
      }),
      previous
    );
    expect(refreshed.timelineItems.map((item) => item.id)).toEqual(['message:1']);
  });

  it('carries prior turn volatile items when a DIFFERENT active turn replaces the overlay', () => {
    // Queued message: turn N+1 starts streaming before turn N's finalized
    // reply is canonically confirmed. The reply must not blink out.
    const turn1Reply = {
      id: 'message:turn-1:phase:0',
      kind: 'message',
      sort_key: '9998:999999999999999:000000:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_complete' }],
      stable: false,
      status: 'complete',
      role: 'assistant',
      content: 'first reply',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = applySnapshot(
      snapshot({
        runtime: runtime(1, {
          has_active_turn: true,
          active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
          volatile_items: [turn1Reply]
        })
      })
    );

    const nextTurn = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-2', session_id: 'sess-1', status: 'running' },
        volatile_items: [
          {
            id: 'message:turn-2:phase:0',
            kind: 'message',
            sort_key: '9998:999999999999999:000000:02:000000000',
            source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_stream' }],
            stable: false,
            status: 'running',
            role: 'assistant',
            content: 'second…',
            message_id: 'turn-2',
            turn_id: 'turn-2',
            attachments: [],
            partial: true
          } as TimelineItem
        ]
      }),
      server_time: '2026-01-01T00:00:02Z'
    });

    const ids = visibleTimelineItems(nextTurn.state).map((item) => item.id);
    expect(ids).toContain('message:turn-1:phase:0');
    expect(ids).toContain('message:turn-2:phase:0');
    // Prior-turn reply is carried below canonical items but ABOVE the new
    // turn's streaming items (9996 band < 9998 band).
    expect(ids.indexOf('message:turn-1:phase:0')).toBeLessThan(ids.indexOf('message:turn-2:phase:0'));
    const carried = visibleTimelineItems(nextTurn.state).find(
      (item) => item.id === 'message:turn-1:phase:0'
    );
    expect(carried).toMatchObject({ status: 'complete' });
  });

  it('orders queued optimistic user messages after carried prior-turn replies', () => {
    const turn1Reply = {
      id: 'message:turn-1:phase:0',
      kind: 'message',
      sort_key: '9998:999999999999999:000000:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_complete' }],
      stable: false,
      status: 'complete',
      role: 'assistant',
      content: 'first reply',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = applySnapshot(
      snapshot({
        runtime: runtime(1, {
          has_active_turn: true,
          active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
          volatile_items: [turn1Reply]
        })
      })
    );
    const settled = applyRealtimeFrame(base, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { has_active_turn: false, volatile_items: [] }),
      server_time: '2026-01-01T00:00:02Z'
    });

    const withOptimistic = addOptimisticUserMessage(settled.state, {
      content: 'follow-up question',
      clientMessageId: 'cmsg_follow_up'
    });

    const ids = visibleTimelineItems(withOptimistic).map((item) => item.id);
    expect(ids.indexOf('message:turn-1:phase:0')).toBeLessThan(
      ids.indexOf('local-user:cmsg_follow_up')
    );
  });

  it('orders idle compaction between carried prior-turn output and the optimistic user message', () => {
    const carriedReply = {
      id: 'message:turn-1:phase:0',
      kind: 'message',
      sort_key: '9996:999999999999999:000000:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-old', seq: 0, event_type: 'assistant_complete' }],
      stable: false,
      status: 'complete',
      role: 'assistant',
      content: 'previous reply',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = {
      ...applySnapshot(snapshot()),
      localItems: [carriedReply]
    };
    const withOptimistic = addOptimisticUserMessage(base, {
      content: 'next question',
      clientMessageId: 'cmsg-next'
    });
    const withCompaction = applyRealtimeFrame(withOptimistic, {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'chat-v2-test',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-2', session_id: 'sess-old', status: 'running' },
        volatile_items: [
          {
            id: 'compaction:sess-old',
            kind: 'compaction',
            sort_key: '9997:999999999999999:000000:10:000000000',
            source_refs: [
              {
                store: 'runtime',
                session_id: 'sess-old',
                seq: 0,
                event_type: 'session_compaction_started'
              }
            ],
            stable: false,
            status: 'running',
            session_id: 'sess-old',
            summary_preview: 'Compacting conversation history…',
            method: 'pending',
            turns_compacted: 0,
            hard_pressure_exceeded: false,
            used_timeout_fallback: false
          } as TimelineItem
        ]
      }),
      server_time: '2026-01-01T00:00:02Z'
    });

    const ids = visibleTimelineItems(withCompaction.state).map((item) => item.id);
    expect(ids.indexOf('message:turn-1:phase:0')).toBeLessThan(
      ids.indexOf('compaction:sess-old')
    );
    expect(ids.indexOf('compaction:sess-old')).toBeLessThan(
      ids.indexOf('local-user:cmsg-next')
    );
  });

  it('mints monotonic local sort keys after reconciliation evicts confirmed items', () => {
    const base = applySnapshot(snapshot());
    const withFirst = addOptimisticUserMessage(base, {
      content: 'first',
      clientMessageId: 'cmsg_1'
    });
    const firstKey = withFirst.localItems.find((item) => item.id === 'local-user:cmsg_1')?.sort_key;

    // Canonical echo confirms the first message → local item evicted.
    const confirmed = {
      ...withFirst,
      timelineItems: [
        ...withFirst.timelineItems,
        message({
          id: 'user:cmsg_1',
          role: 'user',
          message_id: 'cmsg_1',
          client_message_id: 'cmsg_1',
          sort_key: '0000:000000000000002:000000:00:000000000'
        })
      ],
      localItems: withFirst.localItems.filter((item) => item.id !== 'local-user:cmsg_1')
    };

    const withSecond = addOptimisticUserMessage(confirmed, {
      content: 'second',
      clientMessageId: 'cmsg_2'
    });
    const secondKey = withSecond.localItems.find((item) => item.id === 'local-user:cmsg_2')?.sort_key;
    // A length-based counter would reuse the first key here.
    expect(secondKey).toBeDefined();
    expect(firstKey).toBeDefined();
    // Keys from two live optimistic messages must differ and be ordered.
    const withBoth = addOptimisticUserMessage(withFirst, {
      content: 'second',
      clientMessageId: 'cmsg_2'
    });
    const keys = withBoth.localItems.map((item) => item.sort_key);
    expect(new Set(keys).size).toBe(keys.length);
    expect([...keys].sort()).toEqual(keys);
  });

  it('evicts stale phase-guess assistant duplicates once canonical passes the phase', () => {
    // A runtime item minted with a guessed phase (0) never id-matches the
    // canonical item persisted at the real phase (1) — it must be evicted
    // when the canonical stream reaches an equal-or-later phase.
    const guessed = {
      id: 'message:turn-1:phase:0',
      kind: 'message',
      sort_key: '9996:999999999999999:000000:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_complete' }],
      stable: false,
      status: 'complete',
      role: 'assistant',
      content: 'the reply',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = {
      ...applySnapshot(snapshot()),
      localItems: [guessed]
    };

    const synced = applySyncResponse(base, syncResponse({
      ops: [
        {
          op: 'upsert_item',
          item: {
            id: 'message:turn-1:phase:1',
            kind: 'message',
            sort_key: '0000:000000000000005:000001:02:000000000',
            source_refs: [
              { store: 'intaris', session_id: 'sess-1', seq: 5, event_type: 'assistant_message' }
            ],
            stable: true,
            status: 'complete',
            role: 'assistant',
            content: 'the reply',
            message_id: 'turn-1',
            turn_id: 'turn-1',
            assistant_phase_index: 1,
            attachments: [],
            partial: false
          } as TimelineItem
        }
      ]
    }));

    const ids = visibleTimelineItems(synced.state).map((item) => item.id);
    expect(ids).toContain('message:turn-1:phase:1');
    expect(ids).not.toContain('message:turn-1:phase:0');
  });

  it('keeps carried later-phase assistant items until canonical catches up', () => {
    const carriedPhase2 = {
      id: 'message:turn-1:phase:2',
      kind: 'message',
      sort_key: '9996:999999999999999:000002:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_complete' }],
      stable: false,
      status: 'complete',
      role: 'assistant',
      content: 'final segment',
      message_id: 'turn-1',
      turn_id: 'turn-1',
      assistant_phase_index: 2,
      attachments: [],
      partial: false
    } as TimelineItem;
    const base = {
      ...applySnapshot(snapshot()),
      localItems: [carriedPhase2]
    };

    const synced = applySyncResponse(base, syncResponse({
      ops: [
        {
          op: 'upsert_item',
          item: {
            id: 'message:turn-1:phase:0',
            kind: 'message',
            sort_key: '0000:000000000000004:000000:02:000000000',
            source_refs: [
              { store: 'intaris', session_id: 'sess-1', seq: 4, event_type: 'assistant_message' }
            ],
            stable: true,
            status: 'complete',
            role: 'assistant',
            content: 'first segment',
            message_id: 'turn-1',
            turn_id: 'turn-1',
            assistant_phase_index: 0,
            attachments: [],
            partial: false
          } as TimelineItem
        }
      ]
    }));

    const ids = visibleTimelineItems(synced.state).map((item) => item.id);
    expect(ids).toContain('message:turn-1:phase:0');
    expect(ids).toContain('message:turn-1:phase:2');
  });

  it('terminalizes stale running canonical items from OTHER turns while a turn is active', () => {
    const staleTool = {
      id: 'tool:call-stale',
      kind: 'tool_call',
      sort_key: '0000:000000000000002:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 2, event_type: 'tool_call' }],
      stable: true,
      status: 'running',
      call_id: 'call-stale',
      tool_name: 'bash',
      turn_id: 'turn-crashed',
      is_error: false,
      truncated: false,
      has_full_output: false,
      attachments: [],
      file_diffs: []
    } as TimelineItem;
    const state = {
      ...applySnapshot(snapshot({ timeline: { items: [staleTool], has_more_before: false } })),
      runtime: runtime(3, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-live', session_id: 'sess-1', status: 'running' },
        volatile_items: []
      })
    };

    const visible = visibleTimelineItems(state);
    expect(visible.find((item) => item.id === 'tool:call-stale')).toMatchObject({
      status: 'complete'
    });
  });

  it('does not regress newer local item state when a stale snapshot arrives mid-stream', () => {
    const newerTool = {
      id: 'tool:call-1',
      kind: 'tool_call',
      sort_key: '0000:000000000000003:000000:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'sess-1', seq: 3, event_type: 'tool_result' }],
      stable: true,
      status: 'complete',
      call_id: 'call-1',
      tool_name: 'bash',
      arguments: { command: 'ls' },
      result_preview: 'file.txt',
      is_error: false,
      truncated: false,
      has_full_output: false,
      attachments: [],
      file_diffs: []
    } as TimelineItem;
    const previous = {
      ...applySnapshot(snapshot({ timeline: { items: [message(), newerTool], has_more_before: false } }))
    };

    // Stale snapshot fetched before the tool completed: same item, older state.
    const staleSnapshotTool = {
      ...newerTool,
      status: 'running',
      result_preview: null,
      arguments: null
    } as TimelineItem;
    const refreshed = applySnapshot(
      snapshot({ timeline: { items: [message(), staleSnapshotTool], has_more_before: false } }),
      previous
    );

    const tool = refreshed.timelineItems.find((item) => item.id === 'tool:call-1');
    expect(tool).toMatchObject({
      status: 'complete',
      arguments: { command: 'ls' },
      result_preview: 'file.txt'
    });
  });
});
