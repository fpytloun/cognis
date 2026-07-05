/**
 * Client-store timeline invariant assertions.
 *
 * These invariants are checked against the ChatTimeline store state after
 * replaying each event from a golden event stream.  They verify that the
 * client store handles the backend event sequence correctly.
 *
 * Used by:
 * - chat-timeline.golden.test.ts (vitest replay of golden event streams)
 * - chat-timeline.test.ts (unit tests for specific scenarios)
 */

import type { TimelineItem, MessageTimelineItem, ThinkingTimelineItem, ToolCallTimelineItem } from '$lib/chat';

export interface InvariantViolation {
  invariant: string;
  message: string;
  itemId?: string;
  eventIndex?: number;
}

/**
 * INV-NO-HANG: at and after message_complete, no item for the turn has streaming:true
 * or tool_call status:started.
 *
 * Checks the snapshot AT message_complete (after _finalizeStreamingForTurn runs
 * synchronously) and all subsequent snapshots.  This catches:
 * - Assistant messages left streaming:true (hanging spinner)
 * - Thinking blocks left streaming:true (hanging thinking indicator)
 * - Tool call items left in 'started' status (stuck tool card)
 */
export function checkNoHang(
  items: TimelineItem[],
  turnId: string | null,
  messageId: string | null,
  eventIndex: number,
): InvariantViolation[] {
  const violations: InvariantViolation[] = [];
  for (const item of items) {
    if (item.kind === 'message' && item.role === 'assistant' && item.streaming === true) {
      const msg = item as MessageTimelineItem;
      if (turnId && msg.turnId !== turnId) continue;
      if (messageId && msg.messageId !== messageId) continue;
      violations.push({
        invariant: 'INV-NO-HANG',
        message: `Assistant message still streaming after message_complete: id=${item.id}`,
        itemId: item.id,
        eventIndex,
      });
    }
    if (item.kind === 'thinking' && item.streaming === true) {
      const think = item as ThinkingTimelineItem;
      if (turnId && think.turnId !== turnId) continue;
      if (messageId && think.messageId !== messageId) continue;
      violations.push({
        invariant: 'INV-NO-HANG',
        message: `Thinking item still streaming after message_complete: id=${item.id}`,
        itemId: item.id,
        eventIndex,
      });
    }
    if (item.kind === 'tool_call') {
      const tool = item as ToolCallTimelineItem;
      // Only check tool_calls that belong to this turn
      const toolTurnId = (tool as unknown as { turnId?: string }).turnId;
      if (turnId && toolTurnId && toolTurnId !== turnId) continue;
      if (tool.status === 'started') {
        violations.push({
          invariant: 'INV-NO-HANG',
          message: `Tool call still in 'started' status after message_complete: id=${item.id} tool=${tool.toolName}`,
          itemId: item.id,
          eventIndex,
        });
      }
    }
  }
  return violations;
}

/**
 * INV-NO-DUP: no two items share an id.
 */
export function checkNoDup(
  items: TimelineItem[],
  eventIndex: number,
): InvariantViolation[] {
  const seen = new Set<string>();
  const violations: InvariantViolation[] = [];
  for (const item of items) {
    if (seen.has(item.id)) {
      violations.push({
        invariant: 'INV-NO-DUP',
        message: `Duplicate item id: ${item.id}`,
        itemId: item.id,
        eventIndex,
      });
    }
    seen.add(item.id);
  }
  return violations;
}

/**
 * INV-MONOTONIC-PRESENCE: an item, once in the store, never disappears then reappears.
 *
 * Checks the client store state (not raw event stream) — after replaying events
 * through ChatTimeline, the set of ids in the store should only grow or stay
 * the same (items are removed only via explicit remove_ids, not by being absent
 * from a partial patch).
 */
export function checkMonotonicPresence(
  snapshots: Array<{ ids: Set<string>; eventIndex: number }>,
): InvariantViolation[] {
  const everSeen = new Set<string>();
  const violations: InvariantViolation[] = [];

  for (const { ids, eventIndex } of snapshots) {
    // In the client store, items should never disappear unless explicitly removed.
    // Check that no previously-seen id has vanished from the store.
    for (const id of everSeen) {
      if (!ids.has(id)) {
        violations.push({
          invariant: 'INV-MONOTONIC-PRESENCE',
          message: `Item disappeared from store without explicit removal: ${id}`,
          itemId: id,
          eventIndex,
        });
      }
    }

    // Update tracking
    for (const id of ids) {
      everSeen.add(id);
    }
  }

  return violations;
}

/**
 * INV-STABLE-ORDERKEY: orderKey for a given id never increases.
 */
export function checkStableOrderKey(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number }>,
): InvariantViolation[] {
  const bestKey = new Map<string, string>();
  const violations: InvariantViolation[] = [];

  for (const { items, eventIndex } of snapshots) {
    for (const item of items) {
      const key = item.orderKey;
      if (!key) continue;
      const existing = bestKey.get(item.id);
      if (existing !== undefined && key > existing) {
        violations.push({
          invariant: 'INV-STABLE-ORDERKEY',
          message: `orderKey increased for ${item.id}: ${existing} -> ${key}`,
          itemId: item.id,
          eventIndex,
        });
      }
      bestKey.set(item.id, existing ? (key < existing ? key : existing) : key);
    }
  }

  return violations;
}

/**
 * INV-FIELD-PRESERVE: tool_call arguments survive follow-up patches.
 */
export function checkFieldPreserve(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number }>,
): InvariantViolation[] {
  const knownArgs = new Map<string, Record<string, unknown>>();
  const violations: InvariantViolation[] = [];

  for (const { items, eventIndex } of snapshots) {
    for (const item of items) {
      if (item.kind !== 'tool_call') continue;
      const tool = item as ToolCallTimelineItem;

      if (tool.arguments !== undefined && tool.arguments !== null) {
        knownArgs.set(item.id, tool.arguments);
      } else if (knownArgs.has(item.id)) {
        // Arguments were present before but are now undefined/null
        violations.push({
          invariant: 'INV-FIELD-PRESERVE',
          message: `Tool call arguments lost for ${item.id} (had: ${JSON.stringify(knownArgs.get(item.id))})`,
          itemId: item.id,
          eventIndex,
        });
      }
    }
  }

  return violations;
}

/**
 * INV-FINAL-PRESENCE: every assistant message and tool_call that was present
 * during streaming must still be present in the final store state after
 * message_complete.
 *
 * This catches the "message disappears after streaming" bug: an item that was
 * visible during streaming gets removed from the store after the turn completes
 * (e.g., due to a replaceAll call from a history reload that doesn't include
 * the item yet, or a _reconcileMap that deletes runtime-only items).
 *
 * Only checks items that were present BEFORE message_complete (streaming-phase
 * items).  Items added after message_complete (e.g., from history reload) are
 * not checked.
 */
export function checkFinalPresence(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number; eventType: string }>,
  messageCompleteIndex: number | null,
  turnId: string | null,
  messageId: string | null,
): InvariantViolation[] {
  if (messageCompleteIndex === null) return [];

  // Collect all assistant message and tool_call ids that appeared before message_complete
  const streamingIds = new Set<string>();
  for (const snapshot of snapshots) {
    if (snapshot.eventIndex >= messageCompleteIndex) break;
    for (const item of snapshot.items) {
      if (item.kind === 'message' && item.role === 'assistant') {
        const msg = item as MessageTimelineItem;
        // Only track items belonging to this turn
        if (turnId && msg.turnId !== turnId) continue;
        if (messageId && msg.messageId !== messageId) continue;
        streamingIds.add(item.id);
      }
      if (item.kind === 'tool_call') {
        const tool = item as ToolCallTimelineItem;
        const toolTurnId = (tool as unknown as { turnId?: string }).turnId;
        if (turnId && toolTurnId && toolTurnId !== turnId) continue;
        streamingIds.add(item.id);
      }
    }
  }

  if (streamingIds.size === 0) return [];

  // Find the final snapshot (at or after message_complete)
  const finalSnapshot = snapshots.find((s) => s.eventIndex >= messageCompleteIndex);
  if (!finalSnapshot) return [];

  const finalIds = new Set(finalSnapshot.items.map((i) => i.id));
  const violations: InvariantViolation[] = [];

  for (const id of streamingIds) {
    if (!finalIds.has(id)) {
      violations.push({
        invariant: 'INV-FINAL-PRESENCE',
        message: `Item present during streaming disappeared after message_complete: id=${id}`,
        itemId: id,
        eventIndex: finalSnapshot.eventIndex,
      });
    }
  }

  return violations;
}

/**
 * INV-RECONNECT-NO-HANG: after a conversation_runtime_snapshot with
 * has_active_turn:false is applied, no streaming:true or status:started
 * items remain in the store.
 *
 * This invariant catches the reconnect re-injection bug: if the server
 * sends a snapshot with has_active_turn:false but the snapshot's
 * timeline_items still contain streaming:true items (stale active_thinking),
 * the store must finalize them rather than re-injecting them.
 *
 * Checks every snapshot that immediately follows a
 * conversation_runtime_snapshot event with has_active_turn:false.
 */
export function checkReconnectNoHang(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number; eventType: string }>,
  events: Array<{ type: string; has_active_turn?: boolean }>,
): InvariantViolation[] {
  const violations: InvariantViolation[] = [];

  for (let i = 0; i < events.length; i++) {
    const event = events[i]!;
    if (event.type !== 'conversation_runtime_snapshot') continue;
    // has_active_turn defaults to false when absent (no active turn)
    const hasActiveTurn = event.has_active_turn ?? false;
    if (hasActiveTurn) continue;

    // Find the snapshot taken after this event
    const snapshot = snapshots.find((s) => s.eventIndex >= i);
    if (!snapshot) continue;

    for (const item of snapshot.items) {
      if (item.kind === 'message' && item.role === 'assistant' && item.streaming === true) {
        violations.push({
          invariant: 'INV-RECONNECT-NO-HANG',
          message: `Assistant message still streaming after has_active_turn:false runtime snapshot: id=${item.id}`,
          itemId: item.id,
          eventIndex: snapshot.eventIndex,
        });
      }
      if (item.kind === 'thinking' && item.streaming === true) {
        violations.push({
          invariant: 'INV-RECONNECT-NO-HANG',
          message: `Thinking item still streaming after has_active_turn:false runtime snapshot: id=${item.id}`,
          itemId: item.id,
          eventIndex: snapshot.eventIndex,
        });
      }
      if (item.kind === 'tool_call') {
        const tool = item as ToolCallTimelineItem;
        if (tool.status === 'started' || tool.status === 'running') {
          violations.push({
            invariant: 'INV-RECONNECT-NO-HANG',
            message: `Tool call still in '${tool.status}' after has_active_turn:false runtime snapshot: id=${item.id}`,
            itemId: item.id,
            eventIndex: snapshot.eventIndex,
          });
        }
      }
    }
  }

  return violations;
}

/**
 * INV-REFRESH-NO-DROP: a refresh (replaceAll) must not drop an item that was
 * present and unconfirmed-live immediately before the refresh.
 *
 * The golden replay routes a synthetic `conversation_view_refresh` event
 * through `ChatTimeline.replaceAll`. This invariant asserts that any assistant
 * message or tool_call present in the snapshot taken just BEFORE the refresh is
 * still present in the snapshot taken just AFTER it — catching the
 * "message disappears after refresh" symptom.
 *
 * Persisted items the server legitimately removes are not flagged: the refresh
 * projection in the golden is built from the live state minus only the
 * just-finalized message, so a drop here is always a regression.
 */
export function checkRefreshNoDrop(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number; eventType: string }>,
): InvariantViolation[] {
  const violations: InvariantViolation[] = [];

  for (let i = 0; i < snapshots.length; i++) {
    const snap = snapshots[i]!;
    if (snap.eventType !== 'conversation_view_refresh') continue;
    if (i === 0) continue;
    const before = snapshots[i - 1]!;
    const afterIds = new Set(snap.items.map((it) => it.id));

    for (const item of before.items) {
      const isAssistant = item.kind === 'message' && item.role === 'assistant';
      const isTool = item.kind === 'tool_call';
      if (!isAssistant && !isTool) continue;
      if (!afterIds.has(item.id)) {
        violations.push({
          invariant: 'INV-REFRESH-NO-DROP',
          message: `Item present before refresh disappeared after replaceAll: id=${item.id}`,
          itemId: item.id,
          eventIndex: snap.eventIndex,
        });
      }
    }
  }

  return violations;
}

/** Kind rank for phase-order checking — must match backend _KIND_RANK. */
const _KIND_RANK: Record<string, number> = {
  'thinking': 1,
  'message:assistant': 2,
  'tool_call': 3,
};

function _itemKindRank(item: TimelineItem): number {
  if (item.kind === 'message') return item.role === 'assistant' ? 2 : -1;
  return _KIND_RANK[item.kind] ?? -1;
}

function _itemPhase(item: TimelineItem): number | null {
  const p = (item as unknown as { assistantPhaseIndex?: unknown }).assistantPhaseIndex;
  return typeof p === 'number' ? p : null;
}

function _itemTurnId(item: TimelineItem): string | null {
  const t = (item as unknown as { turnId?: unknown }).turnId;
  return typeof t === 'string' ? t : null;
}

/**
 * INV-PHASE-ORDER: within a turn, items must be ordered by (phase, kind_rank).
 *
 * A later-phase item must never sort above an earlier-phase item of the same
 * turn. Within the same phase, thinking (rank 1) < assistant (rank 2) < tool (rank 3).
 *
 * This catches the "assistant, thinking, tool" live ordering bug: the completion
 * item got a real Intaris seq (small) while earlier-phase siblings had the
 * sentinel seq (large), making the completion jump above them in the sort.
 *
 * Only checks items that have both a turnId and an assistantPhaseIndex. Skips
 * user messages, notices, system messages, and unattributed items.
 */
export function checkPhaseOrder(
  items: TimelineItem[],
  eventIndex: number,
): InvariantViolation[] {
  const violations: InvariantViolation[] = [];

  // Group items by turnId
  const byTurn = new Map<string, Array<{ item: TimelineItem; phase: number; kindRank: number; renderIndex: number }>>();
  for (let i = 0; i < items.length; i++) {
    const item = items[i]!;
    const turnId = _itemTurnId(item);
    const phase = _itemPhase(item);
    const kindRank = _itemKindRank(item);
    if (!turnId || phase === null || kindRank < 0) continue;
    if (!byTurn.has(turnId)) byTurn.set(turnId, []);
    byTurn.get(turnId)!.push({ item, phase, kindRank, renderIndex: i });
  }

  for (const [, turnItems] of byTurn) {
    // Check each pair: if item A renders before item B (lower renderIndex),
    // then A's (phase, kindRank) must be <= B's (phase, kindRank).
    for (let a = 0; a < turnItems.length; a++) {
      for (let b = a + 1; b < turnItems.length; b++) {
        const ia = turnItems[a]!;
        const ib = turnItems[b]!;
        // ia renders before ib (lower renderIndex). Check ia <= ib in (phase, kindRank).
        const aKey = ia.phase * 100 + ia.kindRank;
        const bKey = ib.phase * 100 + ib.kindRank;
        if (aKey > bKey) {
          violations.push({
            invariant: 'INV-PHASE-ORDER',
            message:
              `Phase order violated: ${ia.item.id} (phase=${ia.phase} kind=${ia.item.kind}) ` +
              `renders before ${ib.item.id} (phase=${ib.phase} kind=${ib.item.kind}) ` +
              `but has higher (phase,kind) rank`,
            itemId: ia.item.id,
            eventIndex,
          });
        }
      }
    }
  }

  return violations;
}

/**
 * INV-NO-FOREIGN-SESSION: no item whose sessionId differs from the active
 * session (and is not in the active session's compaction lineage) should
 * survive in the store.
 *
 * This catches the sub-session tool_call leak: WORKFLOW_PROGRESS events from
 * delegated/workflow child sessions are fanned out to the parent conversation
 * with the child's sessionId. The client's turnInProgress bypass previously
 * admitted them into the main timeline. The fix makes the bypass lineage-aware:
 * only compaction predecessors (previous_session_id chain, parent_session_id=null)
 * are allowed; sub-sessions (parent_session_id set) are rejected.
 *
 * @param activeSessionId - the conversation's active session id
 * @param lineageSessionIds - set of session ids in the compaction lineage
 *   (active + predecessors via previous_session_id, stopping at parent_session_id)
 */
export function checkNoForeignSession(
  items: TimelineItem[],
  eventIndex: number,
  activeSessionId: string | null,
  lineageSessionIds: ReadonlySet<string>,
): InvariantViolation[] {
  if (!activeSessionId) return [];
  const violations: InvariantViolation[] = [];
  for (const item of items) {
    if (item.kind !== 'message' && item.kind !== 'tool_call' && item.kind !== 'thinking') continue;
    const sessionId = (item as unknown as { sessionId?: string }).sessionId;
    if (!sessionId) continue;
    if (sessionId === activeSessionId) continue;
    if (lineageSessionIds.has(sessionId)) continue;
    violations.push({
      invariant: 'INV-NO-FOREIGN-SESSION',
      message: `Foreign-session item in main timeline: id=${item.id} sessionId=${sessionId} (active=${activeSessionId})`,
      itemId: item.id,
      eventIndex,
    });
  }
  return violations;
}

/**
 * Run all invariants against a sequence of timeline snapshots.
 * Returns all violations found.
 */
export function checkAllInvariants(
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number; eventType: string }>,
  messageCompleteIndex: number | null,
  turnId: string | null,
  messageId: string | null,
  rawEvents: Array<{ type: string; has_active_turn?: boolean }> = [],
): InvariantViolation[] {
  const violations: InvariantViolation[] = [];

  // Per-snapshot checks
  for (const snapshot of snapshots) {
    violations.push(...checkNoDup(snapshot.items, snapshot.eventIndex));
  }

  // INV-NO-HANG: check the snapshot AT message_complete (after _finalizeStreamingForTurn runs synchronously)
  if (messageCompleteIndex !== null) {
    // Find the snapshot at or immediately after message_complete
    const atComplete = snapshots.find((s) => s.eventIndex >= messageCompleteIndex);
    if (atComplete) {
      violations.push(...checkNoHang(atComplete.items, turnId, messageId, atComplete.eventIndex));
    }
    // Also check all subsequent snapshots (post-completion events must not re-introduce hangs)
    for (const snapshot of snapshots) {
      if (snapshot.eventIndex <= messageCompleteIndex) continue;
      violations.push(...checkNoHang(snapshot.items, turnId, messageId, snapshot.eventIndex));
    }
  }

  // INV-FINAL-PRESENCE: streaming items must survive to the final state
  violations.push(...checkFinalPresence(snapshots, messageCompleteIndex, turnId, messageId));

  // INV-RECONNECT-NO-HANG: after a has_active_turn:false runtime snapshot,
  // no streaming:true or status:started items remain.
  if (rawEvents.length > 0) {
    violations.push(...checkReconnectNoHang(snapshots, rawEvents));
  }

  // INV-REFRESH-NO-DROP: a refresh (replaceAll) must not drop unconfirmed-live
  // items present immediately before it.
  violations.push(...checkRefreshNoDrop(snapshots));

  // Cross-snapshot checks
  const presenceSnapshots = snapshots.map((s) => ({
    ids: new Set(s.items.map((i) => i.id)),
    eventIndex: s.eventIndex,
  }));
  violations.push(...checkMonotonicPresence(presenceSnapshots));
  violations.push(...checkStableOrderKey(snapshots));
  violations.push(...checkFieldPreserve(snapshots));

  // INV-PHASE-ORDER: within a turn, items must be ordered by (phase, kind_rank).
  // Catches the "assistant, thinking, tool" live ordering bug where the
  // completion item's real seq jumped it above sentinel-seq earlier-phase siblings.
  for (const snapshot of snapshots) {
    violations.push(...checkPhaseOrder(snapshot.items, snapshot.eventIndex));
  }

  return violations;
}
