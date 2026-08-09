import type {
  CancelTurnV2Response,
  ChatRealtimeFrame,
  ChatResetReason,
  ChatSnapshot,
  ChatSyncResponse,
  ConversationStateView,
  ConversationSummary,
  QueueMutationResponse,
  QueueState,
  RuntimeOverlaySnapshot,
  SendMessageV2Response,
  TimelineBackfillResponse,
  TimelineItem,
  TimelineItemStatus,
  TurnCycleState
} from './types';
import type { AttachmentRef } from '$lib/types/api';
import { conversationTimelineScope, type TimelineScope } from './types';

export type ChatV2SyncOutcome =
  | 'applied'
  | 'duplicate'
  | 'cursor_mismatch'
  | 'reset_required';

export interface ChatV2SyncResult {
  outcome: ChatV2SyncOutcome;
  state: ChatV2ClientState;
  resetReason?: ChatResetReason;
}

export interface ChatV2ClientState {
  scopeKey: string | null;
  scope: import('./types').TimelineScope | null;
  conversationId: string | null;
  projectionVersion: string | null;
  cursor: string | null;
  hasMoreBefore: boolean;
  beforeCursor: string | null;
  conversation: ConversationSummary | null;
  timelineItems: TimelineItem[];
  state: ConversationStateView | null;
  queue: QueueState | null;
  runtime: RuntimeOverlaySnapshot | null;
  cycleStates: TurnCycleState[];
  localItems: TimelineItem[];
  syncStatus: 'empty' | 'ready' | 'gapped';
  lastError: string | null;
}

function responseScope(scope: TimelineScope | undefined, conversationId: string): TimelineScope {
  return scope ?? conversationTimelineScope(conversationId);
}

interface ChatV2DerivedState {
  timelineById: Map<string, TimelineItem>;
  visibleItems?: TimelineItem[];
}

const derivedStateByState = new WeakMap<ChatV2ClientState, ChatV2DerivedState>();

const testCounters = {
  reconcileLocalItemsCalls: 0
};

export const __chatV2SyncEngineTestHooks = {
  resetCounters(): void {
    testCounters.reconcileLocalItemsCalls = 0;
  },
  counters(): { reconcileLocalItemsCalls: number } {
    return { ...testCounters };
  }
};

export function emptyChatV2State(): ChatV2ClientState {
  return cacheClientState({
    scopeKey: null,
    scope: null,
    conversationId: null,
    projectionVersion: null,
    cursor: null,
    hasMoreBefore: false,
    beforeCursor: null,
    conversation: null,
    timelineItems: [],
    state: null,
    queue: null,
    runtime: null,
    cycleStates: [],
    localItems: [],
    syncStatus: 'empty',
    lastError: null
  }, new Map());
}

function cacheClientState(
  state: ChatV2ClientState,
  timelineById: Map<string, TimelineItem> = buildTimelineById(state.timelineItems)
): ChatV2ClientState {
  derivedStateByState.set(state, { timelineById });
  return state;
}

function getDerivedState(state: ChatV2ClientState): ChatV2DerivedState {
  let derived = derivedStateByState.get(state);
  if (!derived) {
    derived = { timelineById: buildTimelineById(state.timelineItems) };
    derivedStateByState.set(state, derived);
  }
  return derived;
}

function getTimelineById(state: ChatV2ClientState): Map<string, TimelineItem> {
  return getDerivedState(state).timelineById;
}

function buildTimelineById(items: readonly TimelineItem[]): Map<string, TimelineItem> {
  return new Map(items.map((item) => [item.id, item]));
}

function cycleStateKey(state: TurnCycleState): string {
  return `${state.turn_id}:${state.turn_cycle_index}`;
}

function combineCycleState(previous: TurnCycleState, incoming: TurnCycleState): TurnCycleState {
  return {
    ...incoming,
    lifecycle_status: previous.lifecycle_status === 'open' || incoming.lifecycle_status === 'open'
      ? 'open'
      : 'complete',
    has_tool_activity: previous.has_tool_activity || incoming.has_tool_activity
  };
}

function mergeCycleStates(...stateSets: Array<readonly TurnCycleState[] | null | undefined>): TurnCycleState[] {
  const byKey = new Map<string, TurnCycleState>();
  for (const stateSet of stateSets) {
    if (!stateSet) continue;
    for (const state of stateSet) {
      const key = cycleStateKey(state);
      const previous = byKey.get(key);
      byKey.set(key, previous ? combineCycleState(previous, state) : state);
    }
  }
  return Array.from(byKey.values()).sort((a, b) => {
    const turnCompare = a.turn_id.localeCompare(b.turn_id);
    if (turnCompare !== 0) return turnCompare;
    return a.turn_cycle_index - b.turn_cycle_index;
  });
}

function dropCycleStatesForTurn(states: readonly TurnCycleState[], turnId: string | null | undefined): TurnCycleState[] {
  if (!turnId) return [...states];
  return states.filter((state) => state.turn_id !== turnId);
}

function mergeResponseCycleStates(
  state: ChatV2ClientState,
  responseStates: readonly TurnCycleState[] | null | undefined,
  runtime: RuntimeOverlaySnapshot | null
): TurnCycleState[] {
  const previousActiveTurnId = state.runtime?.active_turn?.turn_id ?? null;
  const runtimeActiveTurnId = runtime?.active_turn?.turn_id ?? null;
  const shouldDropPreviousActiveTurn = runtime
    && previousActiveTurnId
    && (!runtime.has_active_turn || runtimeActiveTurnId !== previousActiveTurnId);
  const baseStates = shouldDropPreviousActiveTurn
    ? dropCycleStatesForTurn(state.cycleStates ?? [], previousActiveTurnId)
    : (state.cycleStates ?? []);
  return mergeCycleStates(baseStates, responseStates, runtime?.cycle_states ?? []);
}

export function applySnapshot(snapshot: ChatSnapshot, previous?: ChatV2ClientState): ChatV2ClientState {
  const conversationId = snapshot.scope?.conversation_id ?? snapshot.conversation?.conversation_id ?? '';
  const scope = responseScope(snapshot.scope, conversationId);
  const timelineItems = mergeBackfilledHistory(
    mergeSnapshotWithExisting(sortTimelineItems(snapshot.timeline.items), snapshot, previous),
    snapshot,
    previous
  );
  // Route the snapshot runtime through the same acceptance rules as live
  // frames. maybeApplyRuntime now handles epoch change, turn change, and
  // settle as authoritative, while still guarding against a stale snapshot
  // regressing a strictly-newer live overlay for the same epoch+turn.
  const previousRuntime = previous?.runtime ?? null;
  const runtime = maybeApplyRuntime(previousRuntime, snapshot.runtime);
  // Carry against the ACCEPTED runtime so a stale snapshot that was rejected
  // does not terminalize a still-active overlay's items.
  const localItems = reconcileLocalItems(
    carrySettledRuntimeItems(previous?.localItems ?? [], previousRuntime, runtime),
    timelineItems
  );
  const previousActiveTurnId = previous?.runtime?.active_turn?.turn_id ?? null;
  const runtimeActiveTurnId = runtime?.active_turn?.turn_id ?? null;
  const shouldDropPreviousActiveTurn = runtime
    && previousActiveTurnId
    && (!runtime.has_active_turn || runtimeActiveTurnId !== previousActiveTurnId);
  const previousCycleStates = shouldDropPreviousActiveTurn
    ? dropCycleStatesForTurn(previous?.cycleStates ?? [], previous?.runtime?.active_turn?.turn_id ?? null)
    : (previous?.cycleStates ?? []);
  return cacheClientState({
    scopeKey: scope.key,
    scope,
    conversationId: scope.conversation_id ?? snapshot.conversation?.conversation_id ?? null,
    projectionVersion: snapshot.projection_version,
    cursor: snapshot.cursor,
    hasMoreBefore: snapshot.timeline.has_more_before && Boolean(snapshot.timeline.before_cursor),
    beforeCursor: snapshot.timeline.before_cursor ?? null,
    conversation: snapshot.conversation,
    timelineItems,
    state: snapshot.state,
    queue: snapshot.queue,
    runtime,
    cycleStates: mergeCycleStates(previousCycleStates, snapshot.timeline.cycle_states ?? [], runtime?.cycle_states ?? []),
    localItems,
    syncStatus: 'ready',
    lastError: null
  });
}

/**
 * Preserve already-backfilled older history across a snapshot replace.
 *
 * A snapshot contains only the latest timeline page. Blindly replacing the
 * item set with it dropped every older item the user had loaded via
 * "load older" — the content above their viewport vanished and the scroll
 * position landed on entirely different content (a major source of scroll
 * jumps on refresh). The snapshot stays authoritative for its own range and
 * beyond; items strictly OLDER than the snapshot's first item are kept when
 * the server confirms more history exists before the page
 * (`has_more_before`) and the conversation/projection identity is unchanged.
 */
function mergeBackfilledHistory(
  snapshotItems: TimelineItem[],
  snapshot: ChatSnapshot,
  previous?: ChatV2ClientState
): TimelineItem[] {
  if (!previous || previous.timelineItems.length === 0) return snapshotItems;
  if (previous.scopeKey !== responseScope(snapshot.scope, snapshot.scope?.conversation_id ?? snapshot.conversation?.conversation_id ?? '').key) return snapshotItems;
  if (previous.projectionVersion !== snapshot.projection_version) return snapshotItems;
  if (snapshotItems.length === 0 || !snapshot.timeline.has_more_before) return snapshotItems;
  const earliest = snapshotItems[0];
  const olderItems = previous.timelineItems.filter(
    (item) =>
      (compareCodepoints(item.sort_key, earliest.sort_key) ||
        compareCodepoints(item.id, earliest.id)) < 0
  );
  if (olderItems.length === 0) return snapshotItems;
  return sortTimelineItems([...olderItems, ...snapshotItems]);
}

/**
 * Merge a fresh snapshot page with items already known to the client.
 *
 * A snapshot fetched while WS frames were still being applied (post-send
 * recovery racing the live stream) can carry item states OLDER than what the
 * client already rendered — blindly replacing regresses tool statuses and
 * drops fields mid-stream until the next recovery. Merging per item keeps the
 * newer state (mergeTimelineItem protects terminal statuses and preserved
 * fields) while the snapshot stays authoritative for item MEMBERSHIP in its
 * own range: items the snapshot no longer contains are dropped, not kept.
 */
function mergeSnapshotWithExisting(
  snapshotItems: TimelineItem[],
  snapshot: ChatSnapshot,
  previous?: ChatV2ClientState
): TimelineItem[] {
  if (!previous || previous.timelineItems.length === 0) return snapshotItems;
  if (previous.scopeKey !== responseScope(snapshot.scope, snapshot.scope?.conversation_id ?? snapshot.conversation?.conversation_id ?? '').key) return snapshotItems;
  if (previous.projectionVersion !== snapshot.projection_version) return snapshotItems;
  const previousById = getTimelineById(previous);
  return snapshotItems.map((item) => {
    const existing = previousById.get(item.id);
    return existing ? mergeTimelineItem(existing, item) : item;
  });
}

export function applyBackfill(state: ChatV2ClientState, response: TimelineBackfillResponse): ChatV2ClientState {
  if (state.scopeKey !== responseScope(response.scope, response.conversation_id).key) {
    return markGapped(state, 'lineage_changed', 'Backfill conversation does not match local state');
  }
  if (state.projectionVersion !== response.projection_version) {
    return markGapped(state, 'projection_version_changed', 'Backfill projection version does not match local state');
  }
  const byId = new Map(getTimelineById(state));
  const timelineItems = [...state.timelineItems];
  for (const item of response.items) {
    const existing = byId.get(item.id);
    const nextItem = existing ? mergeTimelineItem(existing, item) : item;
    upsertSortedTimelineItem(timelineItems, byId, nextItem);
  }
  return cacheClientState({
    ...state,
    timelineItems,
    hasMoreBefore: response.has_more_before && Boolean(response.before_cursor),
    beforeCursor: response.before_cursor ?? null,
    cycleStates: mergeCycleStates(state.cycleStates ?? [], response.cycle_states ?? []),
    localItems: reconcileLocalItems(state.localItems, timelineItems),
    lastError: null
  }, byId);
}

export function applySyncResponse(state: ChatV2ClientState, response: ChatSyncResponse): ChatV2SyncResult {
  if (state.scopeKey !== responseScope(response.scope, response.conversation_id).key) {
    return { outcome: 'cursor_mismatch', state: markGapped(state, 'lineage_changed', 'Sync scope does not match local state') };
  }
  return applySyncLike(state, response);
}

export function applyRealtimeFrame(state: ChatV2ClientState, frame: ChatRealtimeFrame): ChatV2SyncResult {
  if (state.scopeKey !== responseScope(frame.scope, frame.conversation_id).key) {
    return { outcome: 'cursor_mismatch', state: markGapped(state, 'lineage_changed', 'Realtime scope does not match local state') };
  }
  return applySyncLike(state, frame);
}

export function applySendResponse(state: ChatV2ClientState, response: SendMessageV2Response): ChatV2ClientState {
  if (state.conversationId !== response.conversation_id) {
    return markGapped(state, 'lineage_changed', 'Send response conversation does not match local state');
  }
  const acknowledgedStatus: TimelineItemStatus =
    response.status === 'queued' ? 'waiting' : 'complete';
  const localItems = state.localItems.map((item) =>
    item.kind === 'message'
    && item.role === 'user'
    && item.client_message_id === response.client_message_id
      ? {
          ...item,
          status: acknowledgedStatus,
          updated_at: response.server_time,
        }
      : item
  );
  return cacheClientState({
    ...state,
    cursor: response.cursor ?? state.cursor,
    localItems,
    lastError: null
  }, getTimelineById(state));
}

export function addOptimisticUserMessage(
  state: ChatV2ClientState,
  input: {
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    createdAt?: string;
  }
): ChatV2ClientState {
  if (state.localItems.some((item) => item.kind === 'message' && item.client_message_id === input.clientMessageId)) {
    return state;
  }
  const createdAt = input.createdAt ?? new Date().toISOString();
  const item: TimelineItem = {
    id: `local-user:${input.clientMessageId}`,
    kind: 'message',
    sort_key: nextLocalSortKey(state),
    source_refs: [],
    created_at: createdAt,
    updated_at: createdAt,
    stable: false,
    status: 'pending',
    role: 'user',
    content: input.content,
    message_id: input.clientMessageId,
    client_message_id: input.clientMessageId,
    attachments: input.attachments ?? [],
    partial: false
  };
  return cacheClientState({
    ...state,
    localItems: reconcileLocalItems([...state.localItems, item], state.timelineItems)
  }, getTimelineById(state));
}

export function markOptimisticUserMessageFailed(
  state: ChatV2ClientState,
  clientMessageId: string,
): ChatV2ClientState {
  const localItems = state.localItems.map((item) => (
    item.kind === 'message' && item.client_message_id === clientMessageId
      ? { ...item, status: 'failed' as const, stable: true, updated_at: new Date().toISOString() }
      : item
  ));
  return cacheClientState({ ...state, localItems }, getTimelineById(state));
}

export function addLocalSystemMessage(
  state: ChatV2ClientState,
  input: {
    id: string;
    content: string;
    noticeId?: string | null;
    createdAt?: string;
  }
): ChatV2ClientState {
  if (state.localItems.some((item) => item.id === input.id)) return state;
  const createdAt = input.createdAt ?? new Date().toISOString();
  const item: TimelineItem = {
    id: input.id,
    kind: 'message',
    sort_key: nextLocalSystemSortKey(state),
    source_refs: [],
    created_at: createdAt,
    updated_at: createdAt,
    stable: false,
    status: 'complete',
    role: 'system',
    content: input.content,
    message_id: input.id,
    notice_id: input.noticeId ?? (input.id.startsWith('system:') ? input.id.slice('system:'.length) : undefined),
    attachments: [],
    partial: false
  };
  return cacheClientState({
    ...state,
    localItems: reconcileLocalItems([...state.localItems, item], state.timelineItems)
  }, getTimelineById(state));
}

export function applyCancelResponse(state: ChatV2ClientState, response: CancelTurnV2Response): ChatV2ClientState {
  if (state.conversationId !== response.conversation_id) {
    return markGapped(state, 'lineage_changed', 'Cancel response conversation does not match local state');
  }
  const previousRuntime = state.runtime;
  const runtime = maybeApplyRuntime(previousRuntime, response.runtime ?? null);
  // Carry settled runtime items: a cancel response can flip the overlay to
  // inactive, which would otherwise drop the in-flight volatile items (already
  // streamed assistant text / tool cards) until the next canonical sync.
  const carriedLocalItems = carrySettledRuntimeItems(state.localItems, previousRuntime, runtime);
  const localItems = carriedLocalItems === state.localItems
    ? state.localItems
    : reconcileLocalItems(carriedLocalItems, state.timelineItems);
  return cacheClientState({
    ...state,
    runtime,
    cycleStates: mergeResponseCycleStates(state, [], runtime),
    localItems,
    lastError: null
  }, getTimelineById(state));
}

export function applyQueueMutationResponse(
  state: ChatV2ClientState,
  response: QueueMutationResponse
): ChatV2ClientState {
  if (state.conversationId !== response.conversation_id) {
    return markGapped(state, 'lineage_changed', 'Queue response conversation does not match local state');
  }
  const previousRuntime = state.runtime;
  const runtime = maybeApplyRuntime(previousRuntime, response.runtime ?? null);
  // Carry settled runtime items (this path also advances the cursor, which is
  // what later skews WS runtime frames): an inactive overlay here must not drop
  // the in-flight volatile items before canonical confirmation.
  const carriedLocalItems = carrySettledRuntimeItems(state.localItems, previousRuntime, runtime);
  const localItems = carriedLocalItems === state.localItems
    ? state.localItems
    : reconcileLocalItems(carriedLocalItems, state.timelineItems);
  return cacheClientState({
    ...state,
    queue: response.queue,
    cursor: response.cursor ?? state.cursor,
    runtime,
    cycleStates: mergeResponseCycleStates(state, [], runtime),
    localItems,
    lastError: null
  }, getTimelineById(state));
}

function applySyncLike(
  state: ChatV2ClientState,
  response: ChatSyncResponse | ChatRealtimeFrame
): ChatV2SyncResult {
  if (state.conversationId !== response.conversation_id) {
    const next = markGapped(state, 'lineage_changed', 'Frame conversation does not match local state');
    return { outcome: 'cursor_mismatch', state: next, resetReason: 'lineage_changed' };
  }

  if ('reset_required' in response && response.reset_required) {
    const resetReason = response.reset_reason ?? 'cursor_invalid';
    const next = markGapped(state, resetReason, `Server requested reset: ${resetReason}`);
    return { outcome: 'reset_required', state: next, resetReason };
  }

  if (state.projectionVersion !== response.projection_version) {
    const next = markGapped(
      state,
      'projection_version_changed',
      'Frame projection version does not match local state'
    );
    return { outcome: 'cursor_mismatch', state: next, resetReason: 'projection_version_changed' };
  }

  if (
    isRealtimeFrame(response) &&
    response.ops.length === 0 &&
    response.cursor_before === response.cursor_after &&
    response.cursor_before !== state.cursor
  ) {
    const previousRuntime = state.runtime;
    const runtime = maybeApplyRuntime(previousRuntime, response.runtime ?? null);
    if (runtime === state.runtime) {
      return { outcome: 'duplicate', state };
    }
    // CRITICAL: carry settled runtime items here too. This branch handles every
    // WS runtime frame once the client cursor has advanced past the subscribe
    // -time server cursor (which happens after the first REST sync, because the
    // server never updates its per-connection cursor). The settle frame arrives
    // via this path, so without the carry the just-streamed final assistant
    // message — a runtime-only volatile item never present in localItems — drops
    // out of visibleTimelineItems the instant has_active_turn flips false, and
    // stays gone until a canonical sync re-adds it (which can be swallowed).
    const carriedLocalItems = carrySettledRuntimeItems(state.localItems, previousRuntime, runtime);
    const localItems = carriedLocalItems === state.localItems
      ? state.localItems
      : reconcileLocalItems(carriedLocalItems, state.timelineItems);
    return {
      outcome: 'applied',
      state: cacheClientState({
        ...state,
        runtime,
        cycleStates: mergeResponseCycleStates(state, response.cycle_states ?? [], runtime),
        localItems,
        lastError: null
      }, getTimelineById(state))
    };
  }

  if (response.cursor_after === state.cursor && response.cursor_before !== state.cursor) {
    return { outcome: 'duplicate', state };
  }

  if (response.cursor_before !== state.cursor) {
    const next = markGapped(state, 'cursor_invalid', 'Frame cursor does not match local state');
    return { outcome: 'cursor_mismatch', state: next, resetReason: 'cursor_invalid' };
  }

  const resetOp = response.ops.find((op) => op.op === 'reset');
  const resetRequired =
    'reset_required' in response && response.reset_required
      ? response.reset_reason ?? (resetOp?.op === 'reset' ? resetOp.reason : 'cursor_invalid')
      : resetOp?.op === 'reset'
        ? resetOp.reason
        : null;

  if (resetRequired) {
    const next = markGapped(state, resetRequired, `Server requested reset: ${resetRequired}`);
    return { outcome: 'reset_required', state: next, resetReason: resetRequired };
  }

  let next = applyOps(state, response.ops);
  const previousRuntime = next.runtime;
  const runtime = maybeApplyRuntime(previousRuntime, response.runtime ?? null);
  const carriedLocalItems = carrySettledRuntimeItems(next.localItems, previousRuntime, runtime);
  const localItems = carriedLocalItems === next.localItems
    ? next.localItems
    : reconcileLocalItems(carriedLocalItems, next.timelineItems);
  next = cacheClientState({
    ...next,
    cursor: response.cursor_after,
    runtime,
    cycleStates: mergeResponseCycleStates(next, response.cycle_states ?? [], runtime),
    // Carry settled runtime items only when the overlay we ACCEPTED represents
    // a real active->inactive settle. Passing the accepted `runtime` (not the
    // raw incoming frame) ensures a stale inactive frame that maybeApplyRuntime
    // rejected does not terminalize the still-active overlay's items.
    localItems,
    syncStatus: 'ready',
    lastError: null
  }, getTimelineById(next));
  return { outcome: 'applied', state: next };
}

function isRealtimeFrame(response: ChatSyncResponse | ChatRealtimeFrame): response is ChatRealtimeFrame {
  return 'type' in response && response.type === 'chat_v2_frame';
}

function applyOps(state: ChatV2ClientState, ops: ChatSyncResponse['ops']): ChatV2ClientState {
  if (ops.length === 0) return state;

  let conversation = state.conversation;
  let conversationState = state.state;
  let queue = state.queue;
  const previousTimelineById = getTimelineById(state);
  let timelineById = previousTimelineById;
  let timelineItems = state.timelineItems;
  let timelineChanged = false;

  const ensureTimelineMutable = (): void => {
    if (timelineChanged) return;
    timelineChanged = true;
    timelineItems = [...state.timelineItems];
    timelineById = new Map(previousTimelineById);
  };

  for (const op of ops) {
    switch (op.op) {
      case 'upsert_item':
        {
          const existing = timelineById.get(op.item.id);
          const nextItem = existing ? mergeTimelineItem(existing, op.item) : op.item;
          ensureTimelineMutable();
          upsertSortedTimelineItem(timelineItems, timelineById, nextItem);
        }
        break;
      case 'remove_item':
        if (!timelineById.has(op.id)) break;
        ensureTimelineMutable();
        removeSortedTimelineItem(timelineItems, timelineById, op.id);
        break;
      case 'replace_conversation':
        conversation = op.conversation;
        break;
      case 'replace_state':
        conversationState = op.state;
        break;
      case 'replace_queue':
        queue = op.queue;
        break;
      case 'reset':
        break;
    }
  }

  return cacheClientState({
    ...state,
    conversation,
    state: conversationState,
    queue,
    timelineItems,
    localItems: timelineChanged ? reconcileLocalItems(state.localItems, timelineItems) : state.localItems
  }, timelineById);
}

/**
 * Byte-wise (codepoint) string comparison. Sort keys are fixed-format digit
 * strings and ids mix punctuation (`:`/`_`/`-`); `localeCompare` weighs
 * punctuation differently per ICU tailoring, so ordering could differ between
 * browsers/locales and from the server's byte-wise ordering.
 */
function compareCodepoints(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function compareTimelineItems(a: TimelineItem, b: TimelineItem): number {
  return compareCodepoints(a.sort_key, b.sort_key) || compareCodepoints(a.id, b.id);
}

export function sortTimelineItems(items: TimelineItem[]): TimelineItem[] {
  return [...items].sort(compareTimelineItems);
}

function sortedInsertionIndex(items: readonly TimelineItem[], item: TimelineItem): number {
  let low = 0;
  let high = items.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (compareTimelineItems(items[mid], item) <= 0) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  return low;
}

function removeSortedTimelineItem(
  items: TimelineItem[],
  byId: Map<string, TimelineItem>,
  id: string
): void {
  const index = items.findIndex((item) => item.id === id);
  if (index >= 0) items.splice(index, 1);
  byId.delete(id);
}

function upsertSortedTimelineItem(
  items: TimelineItem[],
  byId: Map<string, TimelineItem>,
  item: TimelineItem
): void {
  if (byId.has(item.id)) {
    removeSortedTimelineItem(items, byId, item.id);
  }
  const insertAt = sortedInsertionIndex(items, item);
  items.splice(insertAt, 0, item);
  byId.set(item.id, item);
}

export function visibleTimelineItems(state: ChatV2ClientState): TimelineItem[] {
  const derived = getDerivedState(state);
  if (derived.visibleItems) return derived.visibleItems;

  const baseItems = runtimeAdjustedCanonicalItems(state);
  const runtimeItems = state.runtime?.has_active_turn ? state.runtime.volatile_items : [];
  if (state.localItems.length === 0 && runtimeItems.length === 0) {
    derived.visibleItems = baseItems;
    return baseItems;
  }

  const visible = [...baseItems];
  const visibleById = baseItems === state.timelineItems
    ? new Map(derived.timelineById)
    : buildTimelineById(baseItems);

  // state.localItems is reconciled at transition sites; the visible derive
  // trusts it and only merges it into the already-sorted canonical array.
  for (const item of state.localItems) {
    upsertVisibleTimelineItem(visible, visibleById, item);
  }
  for (const item of runtimeItems) {
    upsertVisibleTimelineItem(visible, visibleById, item);
  }
  derived.visibleItems = visible;
  return visible;
}

function runtimeAdjustedCanonicalItems(state: ChatV2ClientState): TimelineItem[] {
  if (!state.runtime) return state.timelineItems;
  const activeTurnId = state.runtime.has_active_turn
    ? (state.runtime.active_turn?.turn_id ?? null)
    : null;
  let adjusted: TimelineItem[] | null = null;

  for (let index = 0; index < state.timelineItems.length; index += 1) {
    const item = state.timelineItems[index];
    let nextItem = item;
    if (!state.runtime.has_active_turn) {
      nextItem = terminalizeSettledItem(item);
    } else {
      // A turn is active: canonical items stuck in a running state from a
      // PRIOR turn (e.g. a crashed turn) must not render as active spinners
      // for the whole new turn. Items of the active turn stay untouched.
      const itemTurnId = itemTurnIdOf(item);
      if (itemTurnId && activeTurnId && itemTurnId !== activeTurnId) {
        nextItem = terminalizeSettledItem(item);
      }
    }
    if (nextItem !== item && !adjusted) {
      adjusted = state.timelineItems.slice(0, index);
    }
    adjusted?.push(nextItem);
  }

  return adjusted ?? state.timelineItems;
}

function upsertVisibleTimelineItem(
  items: TimelineItem[],
  byId: Map<string, TimelineItem>,
  item: TimelineItem
): void {
  const existing = byId.get(item.id);
  upsertSortedTimelineItem(items, byId, existing ? mergeTimelineItem(existing, item) : item);
}

function itemTurnIdOf(item: TimelineItem): string | null {
  const turnId = (item as { turn_id?: string | null }).turn_id;
  return typeof turnId === 'string' && turnId ? turnId : null;
}

function mergeTimelineItem(existing: TimelineItem, incoming: TimelineItem): TimelineItem {
  if (existing.kind !== incoming.kind) return incoming;
  const sort_key = existing.sort_key <= incoming.sort_key ? existing.sort_key : incoming.sort_key;
  if (existing.kind === 'tool_call' && incoming.kind === 'tool_call') {
    const existingTerminal = isTerminalStatus(existing.status);
    const incomingTerminal = isTerminalStatus(incoming.status);
    return {
      ...existing,
      ...incoming,
      sort_key,
      status: existingTerminal && !isTerminalStatus(incoming.status) ? existing.status : incoming.status,
      tool_name: incoming.tool_name === 'tool' && existing.tool_name !== 'tool' ? existing.tool_name : incoming.tool_name,
      display_name: incoming.display_name ?? existing.display_name,
      // Structured, named arguments drive the per-tool subtitle and rich body.
      // The runtime overlay item carries no arguments (null); it must NOT
      // clobber the canonical structured dict, otherwise the tool card falls
      // back to the raw `arguments_preview` repr (e.g. {"preview": "{'path'..}"}).
      arguments: incoming.arguments ?? existing.arguments,
      arguments_preview: incoming.arguments_preview ?? existing.arguments_preview,
      result_preview: incoming.result_preview ?? existing.result_preview,
      streamed_output: incoming.streamed_output ?? existing.streamed_output,
      attachments: incoming.attachments.length ? incoming.attachments : existing.attachments,
      file_diffs: incoming.file_diffs.length ? incoming.file_diffs : existing.file_diffs,
      evaluation: incoming.evaluation ?? existing.evaluation,
      is_error: incomingTerminal ? incoming.is_error : existing.is_error,
      duration_ms: incoming.duration_ms ?? existing.duration_ms,
      output_size: incoming.output_size ?? existing.output_size,
      truncated: incoming.truncated || existing.truncated,
      has_full_output: incoming.has_full_output || existing.has_full_output,
      recovery_call_id: incoming.recovery_call_id ?? existing.recovery_call_id,
      tool_output_artifact_id: incoming.tool_output_artifact_id ?? existing.tool_output_artifact_id,
      // apply_patch live progress rides the runtime overlay; a progress-less
      // canonical/settle merge must not null it out mid-stream.
      progress_phase: incoming.progress_phase ?? existing.progress_phase,
      progress_input_chars: incoming.progress_input_chars ?? existing.progress_input_chars,
      progress_input_lines: incoming.progress_input_lines ?? existing.progress_input_lines,
      managed_conversation: incoming.managed_conversation ?? existing.managed_conversation,
      progress_complete: incoming.progress_complete ?? existing.progress_complete,
      delegation: mergeDelegationRuntime(existing.delegation, incoming.delegation),
      created_at: existing.created_at ?? incoming.created_at,
      turn_id: incoming.turn_id ?? existing.turn_id,
      assistant_phase_index: incoming.assistant_phase_index ?? existing.assistant_phase_index,
      turn_cycle_index: incoming.turn_cycle_index ?? existing.turn_cycle_index
    };
  }
  if (existing.kind === 'thinking' && incoming.kind === 'thinking') {
    const blocksById = new Map(existing.blocks.map((block) => [block.id, block]));
    for (const block of incoming.blocks) {
      blocksById.set(block.id, { ...blocksById.get(block.id), ...block });
    }
    return {
      ...existing,
      ...incoming,
      sort_key,
      blocks: [...blocksById.values()],
      created_at: existing.created_at ?? incoming.created_at,
      turn_cycle_index: incoming.turn_cycle_index ?? existing.turn_cycle_index
    };
  }
  if (existing.kind === 'message' && incoming.kind === 'message') {
    if (existing.role !== incoming.role) {
      return incoming;
    }
    if (existing.role === 'assistant' && existing.status === 'complete' && incoming.status === 'running') {
      return existing;
    }
    return {
      ...existing,
      ...incoming,
      sort_key,
      attachments: incoming.attachments.length ? incoming.attachments : existing.attachments,
      created_at: existing.created_at ?? incoming.created_at,
      turn_cycle_index: incoming.turn_cycle_index ?? existing.turn_cycle_index
    };
  }
  return { ...existing, ...incoming, sort_key } as TimelineItem;
}

function terminalizeSettledItem(item: TimelineItem): TimelineItem {
  if (item.kind === 'thinking') {
    const needsTerminalStatus = item.status !== 'complete';
    const needsBlockStatus = item.blocks.some((block) => block.status !== 'complete');
    if (!needsTerminalStatus && !needsBlockStatus) return item;
    return {
      ...item,
      status: 'complete',
      blocks: item.blocks.map((block) => ({ ...block, status: 'complete' }))
    };
  }
  if (
    item.kind === 'message'
    && item.role === 'assistant'
    && (item.status === 'running' || item.partial)
  ) {
    // Clear `partial` on settle. The activity-fold gate treats a `partial`
    // assistant message as "live" and then requires backend cycle-state
    // confirmation to fold it; at settle the turn's cycle states are dropped,
    // so a retained `partial` flag deadlocks folding until a full canonical
    // sync replaces the item. A settled assistant message is never live.
    return { ...item, status: 'complete', partial: false };
  }
  if (item.kind === 'tool_call' && (item.status === 'running' || item.status === 'pending')) {
    return { ...item, status: 'complete' };
  }
  return item;
}

function mergeDelegationRuntime(
  existing: Record<string, unknown> | null | undefined,
  incoming: Record<string, unknown> | null | undefined
): Record<string, unknown> | null | undefined {
  if (!incoming) return existing;
  if (!existing) return incoming;
  const incomingStatus = typeof incoming.status === 'string' ? incoming.status : null;
  const incomingTerminal = ['completed', 'complete', 'failed', 'cancelled', 'canceled'].includes(
    incomingStatus ?? ''
  );
  return {
    ...existing,
    ...incoming,
    child_session_id: incoming.child_session_id ?? existing.child_session_id,
    agent_id: incoming.agent_id ?? existing.agent_id,
    used_agent_id: incoming.used_agent_id ?? existing.used_agent_id,
    title: incoming.title ?? existing.title,
    summary: incoming.summary ?? existing.summary,
    started_at: incoming.started_at ?? existing.started_at,
    duration_ms: incoming.duration_ms ?? existing.duration_ms,
    result_summary: incoming.result_summary ?? existing.result_summary,
    result_content: incoming.result_content ?? existing.result_content,
    result_source: incoming.result_source ?? existing.result_source,
    result_truncated: incoming.result_truncated ?? existing.result_truncated,
    result_anchors: incoming.result_anchors ?? existing.result_anchors,
    todos: Array.isArray(incoming.todos)
      ? incoming.todos.length > 0 || incomingTerminal
        ? incoming.todos
        : existing.todos
      : existing.todos,
    tool_call_count: incoming.tool_call_count ?? existing.tool_call_count,
    max_tool_calls: incoming.max_tool_calls ?? existing.max_tool_calls,
    last_tool: incoming.last_tool ?? existing.last_tool,
    error: incoming.error ?? existing.error
  };
}

export function maybeApplyRuntime(
  current: RuntimeOverlaySnapshot | null,
  incoming: RuntimeOverlaySnapshot | null
): RuntimeOverlaySnapshot | null {
  if (!incoming) return current;
  if (!incoming.has_active_turn) {
    const volatileItems = incoming.volatile_items.filter((item) => !(
      item.kind === 'message'
      && item.role === 'system'
      && item.notice_scope === 'transient_retry'
    ));
    if (volatileItems.length !== incoming.volatile_items.length) {
      incoming = { ...incoming, volatile_items: volatileItems };
    }
  }
  if (!current) return incoming;
  // A new epoch (process restart / different replica / reset) is authoritative
  // and replaces the overlay wholesale, regardless of revision numbers.
  if (incoming.runtime_epoch !== current.runtime_epoch) return incoming;
  // A turn-complete (inactive) overlay can clear stale streaming rows even
  // when the revision counter regressed (e.g. after a restart). But it must
  // NOT clobber a NEWER active turn: a delayed settle/sync for turn N can
  // arrive after turn N+1 has already started streaming. Guard with
  // generated_at (server time, monotonic across revision resets) so a stale
  // inactive overlay never wins over a more recently generated active one.
  if (!incoming.has_active_turn) {
    if (!current.has_active_turn) {
      // Inactive -> inactive: advance on a newer revision (same epoch).
      return incoming.runtime_revision > current.runtime_revision ? incoming : current;
    }
    // Inactive -> active(current): only settle if the inactive overlay is at
    // least as recent as the current active one; otherwise it is stale.
    return isRuntimeAtLeastAsRecent(incoming, current) ? incoming : current;
  }
  // A different active turn is authoritative even with a lower revision (the
  // in-memory revision counter can regress across restarts), but only when it
  // is at least as recently generated as the current overlay — this prevents a
  // late frame from a prior turn from replacing the current turn.
  if (incoming.active_turn?.turn_id !== current.active_turn?.turn_id) {
    return isRuntimeAtLeastAsRecent(incoming, current) ? incoming : current;
  }
  // Same active turn, same epoch: monotonic revision guards against
  // out-of-order duplicate frames.
  if (incoming.runtime_revision > current.runtime_revision) return mergeRuntimeOverlay(current, incoming);
  return current;
}

/**
 * Whether `incoming` was generated at or after `current`, using the overlay's
 * server-assigned `generated_at`. ISO-8601 UTC timestamps compare correctly as
 * strings. Falls back to accepting when a timestamp is missing so we never get
 * stuck rejecting an overlay we cannot compare.
 */
function isRuntimeAtLeastAsRecent(
  incoming: RuntimeOverlaySnapshot,
  current: RuntimeOverlaySnapshot
): boolean {
  if (!incoming.generated_at || !current.generated_at) return true;
  return incoming.generated_at >= current.generated_at;
}

function mergeRuntimeOverlay(
  current: RuntimeOverlaySnapshot,
  incoming: RuntimeOverlaySnapshot
): RuntimeOverlaySnapshot {
  const context_usage = incoming.context_usage ?? current.context_usage;
  if (!incoming.has_active_turn || !current.has_active_turn) return { ...incoming, context_usage };
  if (incoming.active_turn?.turn_id !== current.active_turn?.turn_id) return { ...incoming, context_usage };
  const byId = new Map(current.volatile_items.map((item) => [item.id, item]));
  const incomingIds = new Set(incoming.volatile_items.map((item) => item.id));
  const closesPriorPhase = incoming.volatile_items.some((item) => item.kind === 'tool_call');
  for (const item of incoming.volatile_items) {
    const existing = byId.get(item.id);
    byId.set(item.id, existing ? mergeTimelineItem(existing, item) : item);
  }
  if (closesPriorPhase) {
    for (const [id, item] of byId) {
      if (incomingIds.has(id)) continue;
      if (item.kind === 'message' && item.role === 'assistant' && item.status === 'running') {
        byId.set(id, { ...item, status: 'complete', partial: false });
      }
      if (item.kind === 'thinking' && item.status === 'running') {
        byId.set(id, {
          ...item,
          status: 'complete',
          active_title: null,
          blocks: item.blocks.map((block) => ({ ...block, status: 'complete' }))
        });
      }
    }
  }
  return {
    ...incoming,
    context_usage,
    volatile_items: sortTimelineItems([...byId.values()]),
    cycle_states: mergeCycleStates(current.cycle_states ?? [], incoming.cycle_states ?? [])
  };
}

function reconcileLocalItems(localItems: TimelineItem[], canonicalItems: TimelineItem[]): TimelineItem[] {
  testCounters.reconcileLocalItemsCalls += 1;
  if (localItems.length === 0) return localItems;
  const canonicalIds = new Set(canonicalItems.map((item) => item.id));
  const canonicalClientMessageIds = new Set(
    canonicalItems
      .filter((item) => item.kind === 'message' && item.role === 'user' && item.client_message_id)
      .map((item) => (item.kind === 'message' ? item.client_message_id : null))
      .filter((value): value is string => typeof value === 'string')
  );
  const canonicalSystemIds = new Set(
    canonicalItems
      .filter((item) => item.kind === 'message' && item.role === 'system')
      .map((item) => item.id)
  );
  // Phase-guess eviction inputs: a runtime item minted with a GUESSED phase
  // (message:{mid}:phase:{guess}) never matches the canonical id when the
  // persisted phase differs — it would survive every refresh as a permanent
  // duplicate. Evict a local assistant/thinking leftover once the canonical
  // timeline contains the same logical content at an equal-or-later phase.
  const canonicalAssistantMaxPhase = new Map<string, number>();
  const canonicalThinkingBlockKeys = new Set<string>();
  for (const item of canonicalItems) {
    if (item.kind === 'message' && item.role === 'assistant' && item.message_id) {
      const phase = typeof item.assistant_phase_index === 'number' ? item.assistant_phase_index : 0;
      const prev = canonicalAssistantMaxPhase.get(item.message_id);
      canonicalAssistantMaxPhase.set(
        item.message_id,
        prev === undefined ? phase : Math.max(prev, phase)
      );
    }
    if (item.kind === 'thinking' && item.message_id) {
      for (const block of item.blocks) {
        canonicalThinkingBlockKeys.add(`${item.message_id}|${block.id}`);
      }
    }
  }
  const reconciled = localItems.filter((item) => {
    if (canonicalIds.has(item.id)) return false;
    if (item.kind === 'message' && item.role === 'system' && canonicalSystemIds.has(item.id)) {
      return false;
    }
    if (item.kind === 'message' && item.role === 'assistant' && item.message_id) {
      const maxPhase = canonicalAssistantMaxPhase.get(item.message_id);
      const localPhase =
        typeof item.assistant_phase_index === 'number' ? item.assistant_phase_index : 0;
      // Same (message_id, phase) always shares the same id, so an id-mismatched
      // local at a phase the canonical stream has already passed is a stale
      // phase-guess duplicate.
      if (maxPhase !== undefined && maxPhase >= localPhase) return false;
      return true;
    }
    if (item.kind === 'thinking' && item.message_id) {
      const blocks = item.blocks;
      if (
        blocks.length > 0 &&
        blocks.every((block) => canonicalThinkingBlockKeys.has(`${item.message_id}|${block.id}`))
      ) {
        return false;
      }
      return true;
    }
    if (item.kind !== 'message' || item.role !== 'user') return true;
    if (!item.client_message_id) return true;
    return !canonicalClientMessageIds.has(item.client_message_id);
  });
  return reconciled.length === localItems.length ? localItems : reconciled;
}

/** Sentinel band for carried (settled-but-unconfirmed) prior-turn items. */
const CARRIED_LINEAGE_PREFIX = '9996:';
const ACTIVE_LINEAGE_PREFIX = '9998:';

/**
 * Rekey a carried runtime item below the pre-turn and active-turn bands
 * (9998 → 9996). The backend reserves 9997 for idle-checkpoint compaction,
 * which must render after the previous turn but before the next user message.
 *
 * Carried items belong to a FINISHED turn; the next turn's runtime items and
 * new optimistic user messages live in the 9998 band and must sort AFTER
 * them. Without the rekey, both turns share the 9998 phase space and the
 * relative order degenerates to id comparison (out-of-order queued messages,
 * replies swapping positions). Relative order among the carried items
 * themselves is preserved (the key suffix is untouched), and the item still
 * sorts after every canonical item, so nothing moves visually at carry time.
 */
function carriedSortKey(sortKey: string): string {
  if (sortKey.startsWith(ACTIVE_LINEAGE_PREFIX)) {
    return CARRIED_LINEAGE_PREFIX + sortKey.slice(ACTIVE_LINEAGE_PREFIX.length);
  }
  return sortKey;
}

function carrySettledRuntimeItems(
  localItems: TimelineItem[],
  currentRuntime: RuntimeOverlaySnapshot | null,
  incomingRuntime: RuntimeOverlaySnapshot | null
): TimelineItem[] {
  if (!incomingRuntime) return localItems;
  if (!currentRuntime?.has_active_turn || currentRuntime.volatile_items.length === 0) return localItems;
  // Carry when the accepted overlay no longer represents the current active
  // turn: either the turn settled (inactive) or a DIFFERENT turn's active
  // overlay replaced it wholesale. The active→active transition matters for
  // queued messages: turn N+1 can start streaming before turn N's items are
  // canonically confirmed — dropping them made the just-finished reply blink
  // out until the next canonical sync.
  const replacesCurrentTurn =
    !incomingRuntime.has_active_turn ||
    incomingRuntime.runtime_epoch !== currentRuntime.runtime_epoch ||
    incomingRuntime.active_turn?.turn_id !== currentRuntime.active_turn?.turn_id;
  if (!replacesCurrentTurn) return localItems;
  const byId = new Map<string, TimelineItem>();
  // Rekey EXISTING 9998-band local items (optimistic user messages minted
  // during the finished turn) into the carried band as well, preserving their
  // key suffix. They belong to the pre-transition era: the next turn's 9998
  // items must sort after them, and their relative order against the carried
  // runtime items must not flip.
  for (const item of localItems) {
    byId.set(item.id, { ...item, sort_key: carriedSortKey(item.sort_key) });
  }
  for (const item of currentRuntime.volatile_items) {
    // This notice describes in-flight recovery only. It has no canonical event
    // and must disappear when the active turn settles.
    if (
      item.kind === 'message'
      && item.role === 'system'
      && item.notice_scope === 'transient_retry'
    ) {
      continue;
    }
    // Native apply_patch input creates a progress-only runtime card before the
    // provider emits a complete tool call. If the turn stops during that input,
    // no canonical event can reconcile this empty placeholder.
    if (
      item.kind === 'tool_call'
      && item.tool_name === 'apply_patch'
      && item.progress_phase === 'preparing_input'
      && !item.progress_complete
      && !item.arguments
      && !item.arguments_preview
      && !item.result_preview
      && !item.streamed_output
      && item.file_diffs.length === 0
    ) {
      continue;
    }
    const settled = terminalizeSettledItem(item);
    byId.set(item.id, { ...settled, sort_key: carriedSortKey(settled.sort_key) });
  }
  return sortTimelineItems([...byId.values()]);
}

function nextLocalSortKey(state: ChatV2ClientState): string {
  // Monotonic tail key: derive from the max already-minted local ordinal
  // instead of the list LENGTH — reconciliation evicts confirmed items, so a
  // length-based counter reuses keys and two quickly-sent messages can render
  // in the wrong relative order until their echoes land.
  let maxLocal = 0;
  for (const item of state.localItems) {
    const parts = item.sort_key.split(':');
    if (
      parts.length === 5
      && (parts[0] === '9996' || parts[0] === '9997' || parts[0] === '9998')
    ) {
      const local = Number(parts[4]);
      if (Number.isFinite(local) && local > maxLocal) maxLocal = local;
    }
  }
  // The message must sort AFTER everything the active turn has streamed so
  // far (its reply precedes the next question). Runtime items carry their
  // per-item phase in the key; minting at maxPhase+1 places the optimistic
  // message after them now, and the carry rekey (9998 → 9996, suffix kept)
  // preserves that order when the next turn starts.
  let phase = 0;
  if (state.runtime?.has_active_turn) {
    for (const item of state.runtime.volatile_items) {
      const rawPhase = (item as { assistant_phase_index?: number | null }).assistant_phase_index;
      const itemPhase = typeof rawPhase === 'number' ? rawPhase : 0;
      if (itemPhase + 1 > phase) phase = itemPhase + 1;
    }
  }
  return `9998:999999999999999:${String(phase).padStart(6, '0')}:00:${String(maxLocal + 1).padStart(9, '0')}`;
}

function nextLocalSystemSortKey(state: ChatV2ClientState): string {
  const maxCanonical = state.timelineItems.reduce<string | null>(
    (max, item) => (max === null || compareCodepoints(max, item.sort_key) < 0 ? item.sort_key : max),
    null
  );
  if (!maxCanonical) return nextLocalSortKey(state);
  const parts = maxCanonical.split(':');
  if (parts.length !== 5) return nextLocalSortKey(state);
  const prefix = parts.slice(0, 4).join(':');
  let maxLocal = Number(parts[4]);
  for (const item of state.localItems) {
    const localParts = item.sort_key.split(':');
    if (localParts.length !== 5 || localParts.slice(0, 4).join(':') !== prefix) continue;
    const local = Number(localParts[4]);
    if (Number.isFinite(local) && local > maxLocal) maxLocal = local;
  }
  return `${prefix}:${String(maxLocal + 1).padStart(9, '0')}`;
}

function isTerminalStatus(status: TimelineItem['status'] | undefined | null): boolean {
  return status === 'complete' || status === 'failed' || status === 'cancelled';
}

function markGapped(state: ChatV2ClientState, reason: ChatResetReason, message: string): ChatV2ClientState {
  return cacheClientState({
    ...state,
    syncStatus: 'gapped',
    lastError: `${reason}: ${message}`
  }, getTimelineById(state));
}
