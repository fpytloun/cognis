/**
 * Chat v2 store invariant suite.
 *
 * Replays realistic frame sequences (streaming frames, completion frames,
 * settle frames, canonical syncs, snapshot refreshes) through the SAME pure
 * functions the production ChatV2Store uses, and asserts the timeline
 * invariants after every step:
 *
 * - INV-NO-HANG: after the turn settles, no visible item is still
 *   running/pending for that turn.
 * - INV-NO-DUP: no two visible items share the same logical identity
 *   (assistant (message_id, phase), tool call_id, thinking block ids).
 * - INV-STABLE-ORDER: once two items are both visible, their relative order
 *   never flips in later steps.
 * - INV-FINAL-PRESENCE: streamed assistant content survives to the final
 *   state (the "message disappears" bug class).
 * - INV-REFRESH-NO-DROP: a canonical sync/snapshot does not evict an
 *   unconfirmed live item that was visible just before it.
 * - INV-NO-REMOUNT-ON-FOLD: the first same-cycle tool frame does not re-home
 *   a live streaming assistant message before backend cycle metadata confirms
 *   tool activity.
 *
 * This suite covers the canonical production path.
 */
import { describe, expect, it } from 'vitest';

import {
  addLocalSystemMessage,
  addOptimisticUserMessage,
  applyRealtimeFrame,
  applySnapshot,
  applySyncResponse,
  emptyChatV2State,
  visibleTimelineItems,
  type ChatV2ClientState
} from './sync-engine';
import { prepareTimelineRows } from './tool-groups';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  ChatSyncResponse,
  RuntimeOverlaySnapshot,
  TimelineItem
} from './types';

const CONV = 'conv-inv';
const PV = 'chat-v2-inv-test';

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

let generatedAtCounter = 0;

function nextGeneratedAt(): string {
  generatedAtCounter += 1;
  return `2026-01-01T00:00:${String(Math.min(generatedAtCounter, 59)).padStart(2, '0')}.${String(
    generatedAtCounter
  ).padStart(6, '0')}Z`;
}

function overlay(
  revision: number,
  overrides: Partial<RuntimeOverlaySnapshot> = {}
): RuntimeOverlaySnapshot {
  return {
    runtime_epoch: 'epoch-inv',
    runtime_revision: revision,
    generated_at: nextGeneratedAt(),
    has_active_turn: false,
    active_turn: null,
    volatile_items: [],
    ...overrides
  };
}

function frame(cursor: string, runtime: RuntimeOverlaySnapshot): ChatRealtimeFrame {
  return {
    type: 'chat_v2_frame',
    schema_version: 2,
    projection_version: PV,
    conversation_id: CONV,
    cursor_before: cursor,
    cursor_after: cursor,
    ops: [],
    runtime,
    server_time: nextGeneratedAt()
  };
}

function syncWith(
  cursorBefore: string,
  cursorAfter: string,
  items: TimelineItem[],
  runtime: RuntimeOverlaySnapshot | null = null
): ChatSyncResponse {
  return {
    schema_version: 2,
    projection_version: PV,
    conversation_id: CONV,
    cursor_before: cursorBefore,
    cursor_after: cursorAfter,
    ops: items.map((item) => ({ op: 'upsert_item', item })),
    runtime,
    reset_required: false,
    reset_reason: null,
    has_more: false,
    server_time: nextGeneratedAt()
  };
}

function snapshot(
  items: TimelineItem[],
  runtime: RuntimeOverlaySnapshot,
  cursor: string
): ChatSnapshot {
  return {
    schema_version: 2,
    projection_version: PV,
    conversation: { conversation_id: CONV, agent_id: 'agent-1', status: 'active' },
    timeline: { items, has_more_before: false, before_cursor: null },
    state: {
      state_version: 1,
      snapshot_generated_at: nextGeneratedAt(),
      capabilities: [],
      active_turn: {},
      pending: {},
      active_session: {},
      task: null
    },
    queue: { messages: [], queued_count: 0 },
    runtime,
    cursor,
    server_time: nextGeneratedAt()
  };
}

function userItem(id: string, seq: number, clientMessageId: string, content: string): TimelineItem {
  return {
    id,
    kind: 'message',
    sort_key: `0000:${String(seq).padStart(15, '0')}:000000:00:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'user_message' }],
    stable: true,
    status: 'complete',
    role: 'user',
    content,
    message_id: clientMessageId,
    client_message_id: clientMessageId,
    attachments: [],
    partial: false
  } as TimelineItem;
}

function assistantStreamItem(
  turnId: string,
  phase: number,
  content: string,
  overrides: Partial<TimelineItem> = {}
): TimelineItem {
  return {
    id: `message:${turnId}:phase:${phase}`,
    kind: 'message',
    sort_key: `9998:999999999999999:${String(phase).padStart(6, '0')}:02:000000000`,
    source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_stream' }],
    stable: false,
    status: 'running',
    role: 'assistant',
    content,
    message_id: turnId,
    turn_id: turnId,
    assistant_phase_index: phase,
    attachments: [],
    partial: true,
    ...overrides
  } as TimelineItem;
}

function assistantCompletionItem(turnId: string, phase: number, content: string): TimelineItem {
  return assistantStreamItem(turnId, phase, content, {
    status: 'complete',
    partial: false,
    source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'assistant_complete' }]
  });
}

function assistantCanonicalItem(
  turnId: string,
  phase: number,
  seq: number,
  content: string
): TimelineItem {
  return {
    id: `message:${turnId}:phase:${phase}`,
    kind: 'message',
    sort_key: `0000:${String(seq).padStart(15, '0')}:${String(phase).padStart(6, '0')}:02:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'assistant_message' }],
    stable: true,
    status: 'complete',
    role: 'assistant',
    content,
    message_id: turnId,
    turn_id: turnId,
    assistant_phase_index: phase,
    attachments: [],
    partial: false
  } as TimelineItem;
}

function thinkingRuntimeItem(turnId: string, phase: number, blockId: string, content: string, complete = false): TimelineItem {
  return {
    id: `thinking:${turnId}:phase:${phase}:${blockId}`,
    kind: 'thinking',
    sort_key: `9998:999999999999999:${String(phase).padStart(6, '0')}:01:000000000`,
    source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'thinking' }],
    stable: false,
    status: complete ? 'complete' : 'running',
    message_id: turnId,
    turn_id: turnId,
    assistant_phase_index: phase,
    blocks: [
      { id: blockId, title: 'Thinking', content, status: complete ? 'complete' : 'running' }
    ]
  } as TimelineItem;
}

function thinkingCanonicalItem(turnId: string, phase: number, blockId: string, seq: number, content: string): TimelineItem {
  return {
    id: `thinking:${turnId}:phase:${phase}:${blockId}`,
    kind: 'thinking',
    sort_key: `0000:${String(seq).padStart(15, '0')}:${String(phase).padStart(6, '0')}:01:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'assistant_thinking' }],
    stable: true,
    status: 'complete',
    message_id: turnId,
    turn_id: turnId,
    assistant_phase_index: phase,
    blocks: [{ id: blockId, title: 'Thinking', content, status: 'complete' }]
  } as TimelineItem;
}

function toolRuntimeItem(
  turnId: string,
  phase: number,
  callId: string,
  status: 'running' | 'complete' = 'running'
): TimelineItem {
  return {
    id: `tool:${callId}`,
    kind: 'tool_call',
    sort_key: `9998:999999999999999:${String(phase).padStart(6, '0')}:03:000000000`,
    source_refs: [{ store: 'runtime', session_id: 'sess-1', seq: 0, event_type: 'tool_call' }],
    stable: false,
    status,
    call_id: callId,
    tool_name: 'bash',
    turn_id: turnId,
    assistant_phase_index: phase,
    turn_cycle_index: 0,
    arguments: { command: 'ls' },
    is_error: false,
    truncated: false,
    has_full_output: false,
    attachments: [],
    file_diffs: []
  } as TimelineItem;
}

function toolCanonicalItem(turnId: string, phase: number, callId: string, seq: number): TimelineItem {
  return {
    ...toolRuntimeItem(turnId, phase, callId, 'complete'),
    sort_key: `0000:${String(seq).padStart(15, '0')}:${String(phase).padStart(6, '0')}:03:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'tool_call' }],
    stable: true,
    result_preview: 'ok'
  } as TimelineItem;
}

function systemNoticeItem(noticeId: string, seq: number, content: string): TimelineItem {
  return {
    id: `system:${noticeId}`,
    kind: 'message',
    sort_key: `0000:${String(seq).padStart(15, '0')}:000000:09:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'sess-1', seq, event_type: 'lifecycle' }],
    stable: true,
    status: 'complete',
    role: 'system',
    content,
    message_id: noticeId,
    notice_id: noticeId,
    notice_kind: 'command_result',
    notice_scope: 'session',
    attachments: [],
    partial: false
  } as TimelineItem;
}

// ---------------------------------------------------------------------------
// Invariant checkers
// ---------------------------------------------------------------------------

interface StepRecord {
  label: string;
  items: TimelineItem[];
}

function logicalIdentity(item: TimelineItem): string | null {
  if (item.kind === 'message' && item.role === 'assistant' && item.message_id) {
    const phase = typeof item.assistant_phase_index === 'number' ? item.assistant_phase_index : 0;
    return `assistant|${item.message_id}|${phase}`;
  }
  if (item.kind === 'message' && item.role === 'user' && item.client_message_id) {
    return `user|${item.client_message_id}`;
  }
  if (item.kind === 'tool_call') {
    return `tool|${item.call_id || item.id}`;
  }
  return null;
}

function checkNoDuplicates(record: StepRecord): void {
  const seen = new Map<string, string>();
  for (const item of record.items) {
    const identity = logicalIdentity(item);
    if (!identity) continue;
    const existing = seen.get(identity);
    expect(
      existing,
      `INV-NO-DUP violated at "${record.label}": ${identity} present as both ${existing} and ${item.id}`
    ).toBeUndefined();
    seen.set(identity, item.id);
  }
}

function checkNoHang(record: StepRecord, settledTurnIds: Set<string>): void {
  for (const item of record.items) {
    const turnId = (item as { turn_id?: string | null }).turn_id;
    if (!turnId || !settledTurnIds.has(turnId)) continue;
    expect(
      item.status === 'running' || item.status === 'pending',
      `INV-NO-HANG violated at "${record.label}": ${item.id} still ${item.status} after turn ${turnId} settled`
    ).toBe(false);
  }
}

function checkStableRelativeOrder(history: StepRecord[]): void {
  // For every pair of item ids visible together in consecutive steps, the
  // relative order must not flip.
  for (let step = 1; step < history.length; step += 1) {
    const previous = history[step - 1];
    const current = history[step];
    const previousIndex = new Map(previous.items.map((item, index) => [item.id, index]));
    const currentIndex = new Map(current.items.map((item, index) => [item.id, index]));
    for (const [idA, prevA] of previousIndex) {
      const curA = currentIndex.get(idA);
      if (curA === undefined) continue;
      for (const [idB, prevB] of previousIndex) {
        if (idA >= idB) continue;
        const curB = currentIndex.get(idB);
        if (curB === undefined) continue;
        const before = Math.sign(prevA - prevB);
        const after = Math.sign(curA - curB);
        expect(
          before === after,
          `INV-STABLE-ORDER violated between "${previous.label}" and "${current.label}": ` +
            `${idA} and ${idB} swapped relative order`
        ).toBe(true);
      }
    }
  }
}

function checkRefreshNoDrop(before: StepRecord, after: StepRecord): void {
  // Live unconfirmed items present before a refresh must survive it, either
  // under the same id or as a canonically confirmed item with the same
  // logical identity.
  const afterIds = new Set(after.items.map((item) => item.id));
  const afterIdentities = new Set(
    after.items.map((item) => logicalIdentity(item)).filter((value): value is string => !!value)
  );
  for (const item of before.items) {
    const identity = logicalIdentity(item);
    const survives = afterIds.has(item.id) || (identity !== null && afterIdentities.has(identity));
    expect(
      survives,
      `INV-REFRESH-NO-DROP violated at "${after.label}": ${item.id} vanished across the refresh`
    ).toBe(true);
  }
}

function assistantRendererKeys(state: ChatV2ClientState): string[] {
  return prepareTimelineRows(visibleTimelineItems(state), DEFAULT_USER_PREFERENCES, state.cycleStates)
    .flatMap((row) => {
      if (row.kind === 'item') {
        const item = row.item;
        return item.kind === 'message' && item.role === 'assistant'
          ? [`message:assistant:${item.id}`]
          : [];
      }
      if (row.kind === 'activity_segment') {
        return row.entries.flatMap((entry) =>
          entry.kind === 'assistant' ? [`message:assistant:${entry.item.id}`] : []
        );
      }
      return [];
    });
}

function timelineRowKeys(state: ChatV2ClientState): string[] {
  return prepareTimelineRows(visibleTimelineItems(state), DEFAULT_USER_PREFERENCES, state.cycleStates)
    .map((row) => {
      if (row.kind === 'item') {
        const item = row.item;
        return item.kind === 'message' ? `${item.kind}:${item.role}:${item.id}` : `${item.kind}:${item.id}`;
      }
      return row.id;
    });
}

function checkNoDuplicateTimelineRowKeys(label: string, state: ChatV2ClientState): void {
  const seen = new Set<string>();
  for (const key of timelineRowKeys(state)) {
    expect(
      seen.has(key),
      `INV-NO-ROW-DUP violated at "${label}": duplicate timeline row key ${key}`
    ).toBe(false);
    seen.add(key);
  }
}

class InvariantScenario {
  state: ChatV2ClientState;
  history: StepRecord[] = [];
  settledTurnIds = new Set<string>();

  constructor(initial: ChatV2ClientState) {
    this.state = initial;
    this.record('initial');
  }

  record(label: string): StepRecord {
    const record = { label, items: visibleTimelineItems(this.state) };
    checkNoDuplicates(record);
    checkNoHang(record, this.settledTurnIds);
    checkNoDuplicateTimelineRowKeys(label, this.state);
    this.history.push(record);
    return record;
  }

  step(label: string, next: ChatV2ClientState): void {
    this.state = next;
    this.record(label);
  }

  settleTurn(turnId: string): void {
    this.settledTurnIds.add(turnId);
  }

  finish(): void {
    checkStableRelativeOrder(this.history);
  }

  lastRecord(): StepRecord {
    return this.history[this.history.length - 1];
  }
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

describe('Chat v2 invariant scenarios', () => {
  it('single-phase stream: stream -> completion -> settle -> canonical sync', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'hello')], overlay(0), 'cur-0'))
    );

    // Streaming frames
    for (const [revision, text] of [
      [1, 'Hel'],
      [2, 'Hello th'],
      [3, 'Hello there!']
    ] as const) {
      const result = applyRealtimeFrame(
        scenario.state,
        frame(
          'cur-0',
          overlay(revision, {
            has_active_turn: true,
            active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
            volatile_items: [assistantStreamItem('turn-1', 0, text)]
          })
        )
      );
      expect(result.outcome).toBe('applied');
      scenario.step(`stream r${revision}`, result.state);
    }

    // Completion frame (still active until settle)
    const completion = applyRealtimeFrame(
      scenario.state,
      frame(
        'cur-0',
        overlay(4, {
          has_active_turn: true,
          active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
          volatile_items: [assistantCompletionItem('turn-1', 0, 'Hello there!')]
        })
      )
    );
    scenario.step('completion', completion.state);

    // Settle
    const settle = applyRealtimeFrame(scenario.state, frame('cur-0', overlay(5)));
    scenario.settleTurn('turn-1');
    scenario.step('settle', settle.state);
    const beforeSync = scenario.lastRecord();

    // Canonical sync
    const sync = applySyncResponse(
      scenario.state,
      syncWith('cur-0', 'cur-1', [assistantCanonicalItem('turn-1', 0, 2, 'Hello there!')])
    );
    scenario.step('canonical sync', sync.state);
    checkRefreshNoDrop(beforeSync, scenario.lastRecord());

    // INV-FINAL-PRESENCE: the streamed reply is present in the final state.
    const finalItems = scenario.lastRecord().items;
    const reply = finalItems.find((item) => item.id === 'message:turn-1:phase:0');
    expect(reply).toBeDefined();
    expect(reply?.kind === 'message' ? reply.content : '').toBe('Hello there!');
    expect(reply?.status).toBe('complete');
    scenario.finish();
  });

  it('multiphase turn: thinking -> tool -> text with per-tool phases', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'inspect the repo')], overlay(0), 'cur-0'))
    );
    const activeTurn = { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' as const };

    const thinking = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [thinkingRuntimeItem('turn-1', 0, 'blk-1', 'planning…')]
      }))
    );
    scenario.step('thinking', thinking.state);

    // Tool boundary frame: thinking completes, tool starts at phase 0.
    const toolStart = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(2, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [
          thinkingRuntimeItem('turn-1', 0, 'blk-1', 'planning done', true),
          toolRuntimeItem('turn-1', 0, 'call-1', 'running')
        ]
      }))
    );
    scenario.step('tool start', toolStart.state);

    // Tool completes; assistant text streams at phase 1.
    const textStream = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(3, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [
          toolRuntimeItem('turn-1', 0, 'call-1', 'complete'),
          assistantStreamItem('turn-1', 1, 'Found the answer')
        ]
      }))
    );
    scenario.step('text stream', textStream.state);

    const settle = applyRealtimeFrame(scenario.state, frame('cur-0', overlay(4)));
    scenario.settleTurn('turn-1');
    scenario.step('settle', settle.state);
    const beforeSync = scenario.lastRecord();

    const sync = applySyncResponse(
      scenario.state,
      syncWith('cur-0', 'cur-1', [
        thinkingCanonicalItem('turn-1', 0, 'blk-1', 2, 'planning done'),
        toolCanonicalItem('turn-1', 0, 'call-1', 3),
        assistantCanonicalItem('turn-1', 1, 4, 'Found the answer')
      ])
    );
    scenario.step('canonical sync', sync.state);
    checkRefreshNoDrop(beforeSync, scenario.lastRecord());

    const finalIds = scenario.lastRecord().items.map((item) => item.id);
    expect(finalIds.indexOf('thinking:turn-1:phase:0:blk-1')).toBeLessThan(
      finalIds.indexOf('tool:call-1')
    );
    expect(finalIds.indexOf('tool:call-1')).toBeLessThan(
      finalIds.indexOf('message:turn-1:phase:1')
    );
    scenario.finish();
  });

  it('INV-NO-REMOUNT-ON-FOLD: mid-stream tool arrival does not re-home the assistant row', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'inspect the repo')], overlay(0), 'cur-0'))
    );
    const activeTurn = { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' as const };

    const assistantOnly = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [
          assistantStreamItem('turn-1', 0, 'I will inspect this.', { turn_cycle_index: 0 })
        ]
      }))
    );
    scenario.step('assistant stream before tool', assistantOnly.state);
    expect(timelineRowKeys(scenario.state)).toEqual(['message:user:user:c1', 'message:assistant:message:turn-1:phase:0']);
    expect(assistantRendererKeys(scenario.state)).toEqual(['message:assistant:message:turn-1:phase:0']);

    const toolArrivedBeforeCycleConfirmation = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(2, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [
          assistantStreamItem('turn-1', 0, 'I will inspect this.', { turn_cycle_index: 0 }),
          toolRuntimeItem('turn-1', 0, 'call-1', 'running')
        ],
        cycle_states: [
          { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'open', has_tool_activity: false }
        ]
      }))
    );
    scenario.step('tool arrived before cycle confirmation', toolArrivedBeforeCycleConfirmation.state);
    expect(
      timelineRowKeys(scenario.state),
      'INV-NO-REMOUNT-ON-FOLD: assistant row must keep its standalone key when the first tool frame arrives'
    ).toEqual([
      'message:user:user:c1',
      'message:assistant:message:turn-1:phase:0',
      'tool-group:turn-1:tool:call-1'
    ]);
    expect(assistantRendererKeys(scenario.state)).toEqual(['message:assistant:message:turn-1:phase:0']);

    const confirmedToolActivity = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(3, {
        has_active_turn: true,
        active_turn: activeTurn,
        volatile_items: [
          assistantStreamItem('turn-1', 0, 'I will inspect this.', { turn_cycle_index: 0 }),
          toolRuntimeItem('turn-1', 0, 'call-1', 'running')
        ],
        cycle_states: [
          { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'open', has_tool_activity: true }
        ]
      }))
    );
    scenario.step('confirmed tool activity fold', confirmedToolActivity.state);
    expect(timelineRowKeys(scenario.state)).toEqual([
      'message:user:user:c1',
      'activity-segment:turn-1:0:t:call-1'
    ]);
    expect(assistantRendererKeys(scenario.state)).toEqual(['message:assistant:message:turn-1:phase:0']);
    scenario.finish();
  });

  it('queued message cross-turn: turn 2 starts before turn 1 is confirmed', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'first question')], overlay(0), 'cur-0'))
    );

    // Turn 1 streams and completes (still active while draining).
    const stream1 = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [assistantCompletionItem('turn-1', 0, 'first answer')]
      }))
    );
    scenario.step('turn1 completion', stream1.state);

    // User queues a second message.
    scenario.step(
      'optimistic user message',
      addOptimisticUserMessage(scenario.state, {
        content: 'second question',
        clientMessageId: 'cmsg-2'
      })
    );

    // Turn 2's ACTIVE overlay replaces turn 1's (queued message drains
    // immediately) — turn 1's reply must not blink out.
    const stream2 = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(2, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-2', session_id: 'sess-1', status: 'running' },
        volatile_items: [assistantStreamItem('turn-2', 0, 'second ans…')]
      }))
    );
    scenario.settleTurn('turn-1');
    scenario.step('turn2 stream (cross-turn)', stream2.state);

    const crossTurnIds = scenario.lastRecord().items.map((item) => item.id);
    expect(crossTurnIds).toContain('message:turn-1:phase:0');
    expect(crossTurnIds).toContain('local-user:cmsg-2');
    expect(crossTurnIds).toContain('message:turn-2:phase:0');
    // Order: turn1 reply -> queued user msg -> turn2 stream.
    expect(crossTurnIds.indexOf('message:turn-1:phase:0')).toBeLessThan(
      crossTurnIds.indexOf('local-user:cmsg-2')
    );
    expect(crossTurnIds.indexOf('local-user:cmsg-2')).toBeLessThan(
      crossTurnIds.indexOf('message:turn-2:phase:0')
    );

    // Canonical sync confirms turn 1's reply and the queued user message.
    const beforeSync = scenario.lastRecord();
    const sync = applySyncResponse(
      scenario.state,
      syncWith('cur-0', 'cur-1', [
        assistantCanonicalItem('turn-1', 0, 2, 'first answer'),
        userItem('user:cmsg-2', 3, 'cmsg-2', 'second question')
      ])
    );
    scenario.step('canonical sync turn1', sync.state);
    checkRefreshNoDrop(beforeSync, scenario.lastRecord());

    const ids = scenario.lastRecord().items.map((item) => item.id);
    expect(ids).toContain('message:turn-1:phase:0');
    expect(ids).toContain('user:cmsg-2');
    expect(ids).not.toContain('local-user:cmsg-2');
    scenario.finish();
  });

  it('reconnect: a settle overlay after reconnect finalizes stale streaming items', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'question')], overlay(0), 'cur-0'))
    );

    const streaming = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [
          thinkingRuntimeItem('turn-1', 0, 'blk-1', 'thinking…'),
          assistantStreamItem('turn-1', 0, 'partial ans')
        ]
      }))
    );
    scenario.step('streaming', streaming.state);

    // Reconnect: snapshot reports NO active turn (turn finished while away).
    // The finished turn's thinking and text are both persisted.
    const refreshed = applySnapshot(
      snapshot(
        [
          userItem('user:c1', 1, 'c1', 'question'),
          thinkingCanonicalItem('turn-1', 0, 'blk-1', 2, 'thinking done'),
          assistantCanonicalItem('turn-1', 0, 3, 'partial answer, finished')
        ],
        overlay(2, { has_active_turn: false }),
        'cur-2'
      ),
      scenario.state
    );
    scenario.settleTurn('turn-1');
    scenario.step('reconnect snapshot', refreshed);

    // INV-RECONNECT-NO-HANG: nothing streams after the inactive snapshot.
    for (const item of scenario.lastRecord().items) {
      expect(item.status === 'running' || item.status === 'pending').toBe(false);
    }
    const reply = scenario.lastRecord().items.find((item) => item.id === 'message:turn-1:phase:0');
    expect(reply?.kind === 'message' ? reply.content : '').toBe('partial answer, finished');
    scenario.finish();
  });

  it('INV-NO-PERMANENT-TAIL: command system notice is canonical-id anchored mid-turn', () => {
    const scenario = new InvariantScenario(
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'use fast profile')], overlay(0), 'cur-0'))
    );

    const active = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-1', session_id: 'sess-1', status: 'running' },
        volatile_items: [assistantStreamItem('turn-1', 0, 'working…')]
      }))
    );
    scenario.step('mid-turn stream', active.state);

    const noticeId = 'command:profile:abc123';
    scenario.step(
      'profile switch notice',
      addLocalSystemMessage(scenario.state, {
        id: `system:${noticeId}`,
        noticeId,
        content: 'Agent profile switched to: fast'
      })
    );

    const localNotice = scenario.lastRecord().items.find((item) => item.id === `system:${noticeId}`);
    expect(localNotice).toBeDefined();
    expect(localNotice?.sort_key.startsWith('9998:999999999999999:')).toBe(false);
    expect((localNotice?.sort_key ?? '') > '0000:000000000000001:000000:00:000000000').toBe(true);

    const sync = applySyncResponse(
      scenario.state,
      syncWith('cur-0', 'cur-1', [systemNoticeItem(noticeId, 2, 'Agent profile switched to: fast')])
    );
    scenario.step('canonical notice sync', sync.state);

    const matching = scenario.lastRecord().items.filter((item) => item.id === `system:${noticeId}`);
    expect(matching).toHaveLength(1);
    expect(matching[0].stable).toBe(true);
    expect(scenario.state.localItems.some((item) => item.id === `system:${noticeId}`)).toBe(false);
    scenario.finish();
  });

  it('empty state bootstrap preserves invariants', () => {
    const scenario = new InvariantScenario(emptyChatV2State());
    scenario.step(
      'first snapshot',
      applySnapshot(snapshot([userItem('user:c1', 1, 'c1', 'hi')], overlay(0), 'cur-0'))
    );
    scenario.finish();
  });

  it('INV-RETRY-NO-DUP: retrying a failed turn does not duplicate the user message', () => {
    const scenario = new InvariantScenario(
      applySnapshot(
        snapshot(
          [
            userItem('user:c1', 1, 'c1', 'retry me'),
            {
              ...assistantCanonicalItem('turn-1', 0, 2, 'The model failed before answering.'),
              status: 'failed',
              partial: false
            } as TimelineItem
          ],
          overlay(0),
          'cur-0'
        )
      )
    );

    const assertSingleUserMessage = (label: string) => {
      const userMessages = visibleTimelineItems(scenario.state).filter(
        (item) => item.kind === 'message' && item.role === 'user' && item.content === 'retry me'
      );
      expect(userMessages, `INV-RETRY-NO-DUP violated at "${label}"`).toHaveLength(1);
      expect((userMessages[0] as { client_message_id?: string } | undefined)?.client_message_id).toBe('c1');
    };

    assertSingleUserMessage('failed baseline');

    const retryRuntime = applyRealtimeFrame(
      scenario.state,
      frame('cur-0', overlay(1, {
        has_active_turn: true,
        active_turn: { turn_id: 'turn-2', session_id: 'sess-1', status: 'running' },
        volatile_items: [assistantStreamItem('turn-2', 0, 'Retry answer')]
      }))
    );
    scenario.step('retry runtime without optimistic user', retryRuntime.state);
    assertSingleUserMessage('retry runtime without optimistic user');

    const retrySettle = applyRealtimeFrame(scenario.state, frame('cur-0', overlay(2)));
    scenario.settleTurn('turn-2');
    scenario.step('retry settle', retrySettle.state);
    assertSingleUserMessage('retry settle');

    const retrySync = applySyncResponse(
      scenario.state,
      syncWith('cur-0', 'cur-1', [
        systemNoticeItem('retry:turn-1:turn-2', 3, 'Retrying turn…'),
        assistantCanonicalItem('turn-2', 0, 4, 'Retry answer')
      ])
    );
    scenario.step('retry canonical sync', retrySync.state);
    assertSingleUserMessage('retry canonical sync');

    scenario.finish();
  });
});
