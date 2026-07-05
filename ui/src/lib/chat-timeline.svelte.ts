/**
 * Server-authoritative timeline store.
 *
 * Design principles:
 *  - Items are keyed by their stable backend `id` in a SvelteMap.
 *  - Ordering is driven entirely by the server-assigned `orderKey`.
 *  - Incoming `timeline_patch` events are buffered and flushed on
 *    requestAnimationFrame so the renderer sees at most one update per frame
 *    regardless of token rate.
 *  - Low-frequency WS events (user_message, system_message, escalation, etc.)
 *    are routed synchronously through an explicit per-type dispatch
 *    (`applyTimelineMutationEvent`, gated by `_TIMELINE_MUTATING_EVENT_TYPES`)
 *    so they are immediately visible (scroll, notice, etc.).
 *  - Optimistic user messages are the only client-minted items; they are
 *    reconciled by `clientMessageId` when the server echo arrives.
 *
 * Upsert merge semantics
 * ----------------------
 * The backend emits tool-call patches across multiple independent paths
 * (on_tool_call, on_tool_progress, on_tool_result) and each live patch is
 * projected in isolation — so a tool_result patch genuinely omits `arguments`,
 * `streamedOutput`, `evaluation`, etc. that arrived in the earlier on_tool_call
 * patch. Verbatim replace would drop those fields on every follow-up patch,
 * causing the tool card to lose its title/args and flicker.
 *
 * Every upsert goes through `_upsertItem` which calls `mergeTimelinePatchItem`
 * when an item with the same id already exists. That function implements the
 * correct per-kind merge:
 *   - tool_call: arguments/streamedOutput/evaluation/attachments preserved;
 *     terminal-status protected; orderKey = min(existing, patch).
 *   - message: content verbatim from patch (server is authoritative for text).
 *   - delegation/thinking: existing merge helpers.
 * `replaceAll` and `restoreFromArray` bypass merge (full authoritative snapshots).
 *
 * No-clear mutation policy
 * ------------------------
 * `SvelteMap.clear()` fires reactivity that empties the `{#each}`, causing
 * every timeline item to unmount and remount — a full blink on every history
 * refresh, message_complete, escalation, etc. All mutations are therefore
 * surgical (per-id set/delete). `_reconcileMap` diffs the target set against
 * the current map: deletes absent ids, sets changed/new ids. `replaceAll` and
 * `_reconcileMap` never call `clear()`.
 *
 * message_complete handling
 * -------------------------
 * The backend sends two things at turn end:
 *   1. `live.assistant_complete` — a timeline_patch with the stable id
 *      `message:{message_id}:phase:{phase}` and streaming:false (rAF path).
 *   2. `message_complete` — a WS event carrying side-effects (context usage,
 *      queued count, turn-settled) but also a redundant assistant item mutation
 *      via the old array engine, which can produce a second item with a
 *      divergent id → hanging spinner.
 *
 * Fix: `applyEvent` intercepts `message_complete` before routing to
 * `applyWebSocketEvent`. It applies the side-effect-only parts via the page
 * (returned as `false` — no timeline mutation). For the assistant item, it
 * relies on the `live.assistant_complete` patch (already correct, id-keyed).
 * As a safety net, if after flushing pending patches a streaming assistant item
 * with the matching messageId is still present, it is finalized in place via a
 * targeted `_upsertItem` — no array rebuild, no clear, no duplicate.
 */

import { SvelteMap } from 'svelte/reactivity';
import {
  timelineFromProjection,
  sortByOrderKey,
  applyWebSocketEvent,
  mergeTimelinePatchItem,
  isUnpersistedRuntimeOrderKey,
  appendOptimisticUserMessage,
  reconcileOptimisticUserMessageDraftItems,
  removeQueuedUserMessageTimelineItems,
  annotateStepRequestInputWithNotification,
  optimisticallyResolveStepRequestInput,
  optimisticallyCancelStepRequestInput,
  type MessageTimelineItem,
  type ThinkingTimelineItem,
  type ToolCallTimelineItem,
  type TimelineItem,
} from '$lib/chat';
import type {
  AttachmentRef,
  CognisWebSocketEvent,
  QueuedMessage,
  QuestionSetReply,
  TimelineProjectionItem,
} from '$lib/types/api';
import type { OptimisticUserMessageDraft } from '$lib/interactive-drafts';

// ---------------------------------------------------------------------------
// ChatTimeline
// ---------------------------------------------------------------------------

export class ChatTimeline {
  // The canonical store: id → item.  Reactive via SvelteMap.
  private readonly _map = new SvelteMap<string, TimelineItem>();

  // Derived sorted render list.  Svelte recomputes this whenever _map changes.
  readonly list: TimelineItem[] = $derived(sortByOrderKey([...this._map.values()]));

  // rAF batching: queued patch batches waiting for the next animation frame.
  private _pending: Array<{ projected: TimelineProjectionItem[]; removeIds: string[] }> = [];
  private _rafHandle: number | null = null;
  private _runtimeSettled = false;

  // ---------------------------------------------------------------------------
  // Core map operations
  // ---------------------------------------------------------------------------

  /**
   * Replace all items with a fresh projection (history load / full refresh).
   *
   * Uses _reconcileMap (surgical diff) so unchanged items keep their object
   * identity and are not remounted. Pending rAF patches are re-applied on top
   * of the snapshot to preserve streaming state that arrived after the fetch.
   */
  replaceAll(
    projected: TimelineProjectionItem[],
    options: { preserveLive?: boolean; terminalizeSettledRuntime?: boolean } = {},
  ): void {
    // Cancel the scheduled rAF flush — we will apply pending patches manually
    // after the snapshot so they land on top of the authoritative base.
    this._cancelRaf();
    const pendingBatches = this._pending;
    this._pending = [];

    // Symptom 1 fix ("message disappears after refresh"): a refresh fires
    // aggressively (visibilitychange / focus / pageshow / online / reconnect /
    // stale-runtime guards). If a refresh lands in the window after a turn
    // produced an assistant/tool item but before that event is durably
    // queryable from Intaris, the fresh history projection omits the item's id.
    // The default delete pass would then evict the just-finalized message.
    //
    // Guard: preserve any current item that is still streaming, still in a
    // non-terminal tool status, or carries an unpersisted runtime sentinel
    // orderKey — when it is absent from the incoming snapshot. The snapshot
    // remains fully authoritative for every id it DOES contain (those are
    // replaced verbatim). This only protects unconfirmed live items from a
    // transient projection gap; persisted removals still apply normally.
    const preserveIds = options.preserveLive === false ? new Set<string>() : this._unconfirmedLiveIds();

    // Apply the authoritative history snapshot via surgical diff (no clear).
    const shouldTerminalizeProjection = this._runtimeSettled && options.terminalizeSettledRuntime !== false;
    const next = timelineFromProjection(
      shouldTerminalizeProjection ? this._terminalizeProjection(projected) : projected
    );
    this._reconcileMap(next, { merge: false, preserveIds });

    // Re-apply any patches that arrived after the snapshot was fetched.
    // Use _upsertItem so partial patches merge with the snapshot's full item.
    for (const { projected: batchProjected, removeIds } of pendingBatches) {
      for (const id of removeIds) {
        this._map.delete(id);
      }
      const items = timelineFromProjection(batchProjected);
      for (const item of items) {
        this._upsertItem(item);
      }
    }
  }

  /**
   * Restore from a cached array (e.g. conversation view cache restore).
   * Uses _reconcileMap so unchanged items keep their identity.
   */
  restoreFromArray(items: TimelineItem[]): void {
    this._cancelRaf();
    this._pending = [];
    this._reconcileMap(items, { merge: false });
  }

  /**
   * Prepend older paginated items without disturbing live items.
   * Existing items (live streaming, in-flight tools) take precedence.
   */
  prependOlder(projected: TimelineProjectionItem[]): void {
    const older = timelineFromProjection(projected);
    for (const item of older) {
      if (!this._map.has(item.id)) {
        this._map.set(item.id, item);
      }
    }
  }

  /** Remove a set of ids from the map. */
  remove(ids: string[]): void {
    for (const id of ids) {
      this._map.delete(id);
    }
  }

  /** Clear all items and cancel any pending rAF flush. */
  clear(): void {
    this._cancelRaf();
    this._pending = [];
    // clear() is only called on conversation switch / session_reset — full
    // emptying is intentional here, not a blink source.
    this._map.clear();
  }

  // ---------------------------------------------------------------------------
  // rAF-batched ingestion (for high-frequency streaming events)
  // ---------------------------------------------------------------------------

  /**
   * Enqueue a timeline_patch for batched application on the next animation
   * frame.  All patches queued within the same frame are applied together in
   * a single map mutation, producing exactly one Svelte reactive update per
   * frame regardless of token rate.
   */
  enqueuePatch(projected: TimelineProjectionItem[], removeIds: string[] = []): void {
    this._runtimeSettled = false;
    this._pending.push({ projected, removeIds });
    if (this._rafHandle === null) {
      this._rafHandle = requestAnimationFrame(() => this._flush());
    }
  }

  /**
   * Flush all pending patches immediately.
   * Called before synchronous operations that need a consistent map state.
   */
  flushPending(): void {
    this._cancelRaf();
    this._flush();
  }

  private _flush(): void {
    this._rafHandle = null;
    if (this._pending.length === 0) return;

    const batches = this._pending;
    this._pending = [];

    for (const { projected, removeIds } of batches) {
      for (const id of removeIds) {
        this._map.delete(id);
      }
      const items = timelineFromProjection(projected);
      for (const item of items) {
        // Merge with existing item so partial patches (tool_result without
        // arguments, tool_progress without arguments, etc.) preserve fields
        // that the patch omits. mergeTimelinePatchItem handles per-kind merge:
        // tool_call preserves arguments/streamedOutput/evaluation/orderKey-min;
        // message content is taken verbatim from the patch (server-authoritative).
        this._upsertItem(item);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // WebSocket event ingestion
  // ---------------------------------------------------------------------------

  /**
   * Apply a WebSocket event to the timeline.
   *
   * `timeline_patch` events are buffered for rAF-batched application.
   *
   * `message_complete` is intercepted: the assistant item finalization is
   * handled by the `live.assistant_complete` timeline_patch (already in the
   * rAF queue). This method applies a targeted streaming-off fallback in case
   * that patch was dropped, then returns false so the page handles the
   * side-effects (context usage, queued count, turn state) without triggering
   * a full timeline rebuild.
   *
   * Mutating low-frequency events are routed through an explicit per-type
   * dispatch (`applyTimelineMutationEvent`, gated by
   * `_TIMELINE_MUTATING_EVENT_TYPES`) + `_reconcileMap` (surgical diff, no
   * clear). Non-mutating types short-circuit to `false`.
   *
   * `activeSessionId` and `turnInProgress` are used to filter `timeline_patch`
   * items to the active session. During an active turn, session filtering is
   * bypassed so in-flight items from the previous session during compaction
   * are not dropped (the "disappears during streaming, heals on refresh" bug).
   *
   * Returns true if the event produced a timeline change (for scroll/sync
   * decisions in the caller).
   */
  applyEvent(
    event: CognisWebSocketEvent,
    activeSessionId: string | null = null,
    turnInProgress = false,
    activeSessionLineage: ReadonlySet<string> | null = null,
  ): boolean {
    if (event.type === 'timeline_patch') {
      const items = (event.items ?? []).filter((item) =>
        _itemBelongsToSession(item, activeSessionId, turnInProgress, activeSessionLineage),
      );
      const removeIds = event.remove_ids ?? [];
      if (items.length === 0 && removeIds.length === 0) return false;
      this.enqueuePatch(items, removeIds);
      return true;
    }

    // message_complete: flush pending (which includes live.assistant_complete
    // patch setting streaming:false on the primary phase), then finalize any
    // remaining streaming items for the turn as a safety net.
    //
    // The safety net covers:
    //   - Multi-phase assistant turns: the completion patch only carries the
    //     final phase id; earlier phases may still be streaming:true.
    //   - Thinking blocks: no separate "thinking complete" event exists; the
    //     runtime snapshot stops emitting them after the turn ends, but the
    //     client store still holds the last snapshot with streaming:true.
    //   - Backpressure drops: the live.assistant_complete patch may have been
    //     dropped, leaving the primary phase streaming:true.
    if (event.type === 'message_complete') {
      this.flushPending();
      this._finalizeStreamingForTurn(event.message_id, event.turn_id ?? null);
      // Return false — the page handles all message_complete side-effects
      // (context usage, queued count, turn state, scroll) directly.
      return false;
    }

    // Phase 2b simplification: short-circuit events that the legacy engine
    // treats as no-ops for the timeline (their live state arrives via
    // timeline_patch, or they are handled entirely in the page). Routing them
    // through applyWebSocketEvent + _reconcileMap would rebuild the whole array
    // and diff it for zero change. Skipping avoids that work and makes the
    // store→legacy-engine surface explicit. These are confirmed no-ops in
    // _applyWebSocketEventInner (see chat.ts) OR page-handled-then-returned.
    if (_TIMELINE_NOOP_EVENT_TYPES.has(event.type)) {
      return false;
    }

    // Phase 2b: the monolithic applyWebSocketEvent catch-all is gone. Mutating
    // event types are routed explicitly through a per-type dispatch so the
    // store→engine surface is enumerated, not implicit. Each listed type still
    // delegates to the pure per-event engine in chat.ts (behavior preserved
    // byte-for-byte — same ghost-tool-call avoidance, compaction card swap,
    // notice dedup, etc.) and the result lands via _reconcileMap (surgical diff,
    // no clear). Any event type not enumerated here and not already handled
    // above (timeline_patch, message_complete, no-ops) is a true no-op for the
    // timeline and returns false without rebuilding the array.
    if (_TIMELINE_MUTATING_EVENT_TYPES.has(event.type)) {
      this.flushPending();
      const current = sortByOrderKey([...this._map.values()]);
      const next = applyTimelineMutationEvent(current, event);
      return this._reconcileMap(next, { merge: false });
    }

    return false;
  }

  // ---------------------------------------------------------------------------
  // Optimistic user message operations
  // ---------------------------------------------------------------------------

  /**
   * Append an optimistic user message bubble.  The item is keyed by a
   * client-minted id and will be reconciled when the server echo arrives.
   */
  addOptimisticUser(
    content: string,
    attachments: AttachmentRef[] = [],
    clientMessageId: string | null = null,
    chatMode?: string,
    chatModeSource?: string,
  ): void {
    const current = sortByOrderKey([...this._map.values()]);
    const next = appendOptimisticUserMessage(current, content, attachments, clientMessageId, chatMode, chatModeSource);
    // Only the new item was appended — find and insert it.
    for (const item of next) {
      if (!this._map.has(item.id)) {
        this._map.set(item.id, item);
      }
    }
  }

  /**
   * Reconcile optimistic drafts against the current timeline.
   * Removes optimistic items whose server echo has arrived and re-adds any
   * that are still pending.
   */
  reconcileOptimisticDrafts(
    drafts: OptimisticUserMessageDraft[],
  ): { settledClientMessageIds: string[] } {
    this.flushPending();
    const current = sortByOrderKey([...this._map.values()]);
    const { items: next, settledClientMessageIds } = reconcileOptimisticUserMessageDraftItems(
      current,
      drafts,
    );
    this._reconcileMap(next, { merge: false });

    // Defense-in-depth (Issue A): after reconciliation, sweep for any leftover
    // local-user: items whose canonical echo is already in the map. This can
    // happen when replaceAll preserved the optimistic item (pre-fix) or when
    // the user_message echo arrived before reconcileOptimisticDrafts ran.
    // A local-user: item is stale if a non-local-user item with the same
    // clientMessageId exists, OR if a non-local-user user message with the
    // same content exists (content-hash fallback).
    const canonicalClientMsgIds = new Set<string>();
    for (const item of this._map.values()) {
      if (item.kind === 'message' && item.role === 'user' && !item.id.startsWith('local-user:')) {
        const cmid = (item as unknown as { clientMessageId?: string }).clientMessageId;
        if (cmid) canonicalClientMsgIds.add(cmid);
      }
    }
    for (const [id, item] of this._map) {
      if (!id.startsWith('local-user:')) continue;
      if (item.kind !== 'message' || item.role !== 'user') continue;
      const cmid = (item as unknown as { clientMessageId?: string }).clientMessageId;
      if (cmid && canonicalClientMsgIds.has(cmid)) {
        this._map.delete(id);
      }
    }

    return { settledClientMessageIds };
  }

  /**
   * Remove queued user message placeholders superseded by the server's
   * queued-messages snapshot.
   */
  removeQueuedUser(queuedMessages: QueuedMessage[]): void {
    const current = [...this._map.values()];
    const next = removeQueuedUserMessageTimelineItems(current, queuedMessages);
    const nextIds = new Set(next.map((item) => item.id));
    for (const item of current) {
      if (!nextIds.has(item.id)) {
        this._map.delete(item.id);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Annotation / optimistic resolution helpers
  // ---------------------------------------------------------------------------

  /**
   * Attach a notification ID to the latest unresolved step_request_questions
   * tool call.
   */
  annotateStepRequestInput(notificationId: string): void {
    const current = [...this._map.values()];
    const next = annotateStepRequestInputWithNotification(current, notificationId);
    if (next === current) return;
    for (const item of next) {
      const existing = this._map.get(item.id);
      if (existing !== item) {
        this._map.set(item.id, item);
      }
    }
  }

  /** Optimistically mark a step_request_questions tool call as resolved. */
  resolveStepRequestInput(toolId: string, response: string | QuestionSetReply): void {
    const current = [...this._map.values()];
    const next = optimisticallyResolveStepRequestInput(current, toolId, response);
    if (next === current) return;
    for (const item of next) {
      const existing = this._map.get(item.id);
      if (existing !== item) {
        this._map.set(item.id, item);
      }
    }
  }

  /** Optimistically cancel a step_request_questions tool call. */
  cancelStepRequestInput(toolId: string): void {
    const current = [...this._map.values()];
    const next = optimisticallyCancelStepRequestInput(current, toolId);
    if (next === current) return;
    for (const item of next) {
      const existing = this._map.get(item.id);
      if (existing !== item) {
        this._map.set(item.id, item);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Diagnostics / serialisation
  // ---------------------------------------------------------------------------

  /** Current item count (for tests and diagnostics). */
  get size(): number {
    return this._map.size;
  }

  /**
   * Snapshot of current items as a sorted array.
   * Used for conversation view cache serialisation.
   */
  toArray(): TimelineItem[] {
    return sortByOrderKey([...this._map.values()]);
  }

  // ---------------------------------------------------------------------------
  // Internal utilities
  // ---------------------------------------------------------------------------

  /**
   * Upsert a single item into the map with field-level merge.
   *
   * When an item with the same id already exists, the patch is merged via
   * `mergeTimelinePatchItem` rather than replacing wholesale. This preserves
   * fields that the patch omits (e.g. `arguments` in a tool_result patch,
   * `streamedOutput` in a truncated result, `evaluation` from an earlier
   * escalation event) and keeps the lower `orderKey` so the item does not
   * jump position when a follow-up patch carries a recomputed key.
   */
  private _upsertItem(item: TimelineItem): void {
    const existing = this._map.get(item.id);
    this._map.set(item.id, existing ? mergeTimelinePatchItem(existing, item) : item);
  }

  /**
   * Reconcile the map to match a target array without calling clear().
   *
   * - Deletes ids present in the map but absent from `next`.
   * - Sets ids that are new or whose value reference changed.
   * - Leaves unchanged ids (same object reference) untouched.
   *
   * When `merge: true`, existing items are merged via `mergeTimelinePatchItem`
   * rather than replaced. Used for patch paths; `merge: false` for full
   * authoritative snapshots (history load, sync event results).
   *
   * Returns true if any mutation was made.
   */
  private _reconcileMap(
    next: TimelineItem[],
    { merge, preserveIds }: { merge: boolean; preserveIds?: Set<string> },
  ): boolean {
    const nextById = new Map<string, TimelineItem>();
    for (const item of next) {
      nextById.set(item.id, item);
    }

    let changed = false;

    // Delete ids no longer present — except ids explicitly preserved (e.g.
    // unconfirmed live items that may be transiently absent from a refresh
    // projection; see replaceAll / _unconfirmedLiveIds).
    for (const id of this._map.keys()) {
      if (!nextById.has(id)) {
        if (preserveIds?.has(id)) continue;
        this._map.delete(id);
        changed = true;
      }
    }

    // Set new or changed ids.
    for (const [id, item] of nextById) {
      const existing = this._map.get(id);
      if (existing === undefined) {
        this._map.set(id, item);
        changed = true;
      } else if (existing !== item) {
        this._map.set(id, merge ? mergeTimelinePatchItem(existing, item) : item);
        changed = true;
      }
    }

    return changed;
  }

  /**
   * Finalize ALL streaming items belonging to the completed turn.
   *
   * Called after flushing pending patches on `message_complete`. The
   * `live.assistant_complete` patch (rAF path) normally sets streaming:false
   * on the primary assistant phase before this runs. This method is a safety
   * net that clears any remaining streaming state for the turn:
   *
   * - **All assistant phases**: a multi-phase turn (thinking → tool → assistant
   *   → tool → assistant) emits one completion patch for the final phase only.
   *   Earlier phases may still be streaming:true in the store.
   *
   * See also: `finalizeAllStreaming()` — the turn-agnostic variant used when a
   * runtime snapshot arrives with `has_active_turn:false`.
   *
   * - **Thinking blocks**: there is no separate "thinking complete" event. The
   *   runtime snapshot stops including thinking items after the turn ends, but
   *   the store still holds the last snapshot with streaming:true and incomplete
   *   blocks. Without this, thinking spinners hang indefinitely.
   *
   * - **Backpressure drops**: the live.assistant_complete patch may have been
   *   dropped under outbound buffer pressure, leaving the primary phase
   *   streaming:true.
   *
   * - **Tool call items stuck in 'started'**: in a multi-phase turn, a
   *   tool_result patch may arrive after message_complete (e.g. due to WS
   *   ordering or coalescing). Without this, the tool card stays in 'started'
   *   status indefinitely. We finalize any tool_call that belongs to this turn
   *   and is still in a non-terminal status.
   *
   * Matching: by `messageId` (primary) or `turnId` (fallback). Both are
   * carried on every streaming item so the match is reliable.
   *
   * No new items are created, no map.clear() is called — only targeted set()
   * on items that are still streaming:true or status:started.
   */
  private _finalizeStreamingForTurn(
    messageId: string | null | undefined,
    turnId: string | null,
  ): void {
    // Symptom 3 fix: when message_complete carries neither message_id nor
    // turn_id, we cannot scope the finalize to a turn — but a clean turn end
    // means nothing should still be streaming. Fall back to finalizing ALL
    // streaming items rather than leaving them hung. This closes the
    // "id-less message_complete leaves spinner hanging" gap.
    if (!messageId && !turnId) {
      this.finalizeStreaming();
      return;
    }
    this.finalizeStreaming({ messageId: messageId ?? null, turnId });
  }

  /**
   * Finalize ALL streaming items in the store, regardless of turn.
   *
   * Called when a `conversation_runtime_snapshot` arrives with
   * `has_active_turn:false` — the server is authoritative that no turn is
   * running, so any `streaming:true` / `status:started` items in the store
   * are stale and must be finalized immediately.
   *
   * This is the client-side defense against the reconnect re-injection bug:
   * if the server sends a snapshot with `has_active_turn:false` but the
   * snapshot's `timeline_items` still contains streaming items (e.g. due to
   * a race between turn teardown and snapshot generation), those items would
   * hang forever without this guard.
   *
   * Thin alias over {@link finalizeStreaming} with no scope.
   */
  finalizeAllStreaming(): void {
    this.finalizeStreaming();
  }

  /**
   * Single finalize routine — clears streaming state for the given scope.
   *
   * This is the one place that finalizes streaming items. It replaces the two
   * previously-separate walkers (`_finalizeStreamingForTurn` and
   * `finalizeAllStreaming`) which had drifting matching logic. The
   * merge-driven `streaming:false` in `mergeTimelinePatchItem` (a server
   * `live.assistant_complete` patch) is the other, complementary path and is
   * intentionally left in the merge layer — it is server-authoritative state,
   * not a client-side teardown.
   *
   * Scope:
   * - `undefined` (no scope): finalize EVERY streaming/non-terminal item. Used
   *   for `has_active_turn:false` snapshots and id-less `message_complete`.
   * - `{ messageId, turnId }`: finalize only items belonging to that turn.
   *   Matching is by `messageId` OR effective turn id (`turnId ?? messageId`).
   *   A tool_call is finalized unless it explicitly belongs to a DIFFERENT
   *   turn (an unattributed in-flight tool belongs to the turn that just
   *   completed — symptom 3).
   *
   * No new items are created, no map.clear() is called — only targeted set()
   * on items that are still streaming:true or status:started/running.
   */
  finalizeStreaming(scope?: { messageId: string | null; turnId: string | null }): void {
    const messageId = scope?.messageId ?? null;
    const effectiveTurnId = scope ? (scope.turnId ?? scope.messageId) : null;
    const scoped = scope !== undefined;

    for (const [id, item] of this._map) {
      if (item.kind === 'message' && item.role === 'assistant' && item.streaming === true) {
        const msg = item as MessageTimelineItem;
        if (scoped && msg.messageId !== messageId && msg.turnId !== effectiveTurnId) continue;
        this._map.set(id, { ...msg, streaming: false } satisfies MessageTimelineItem);
        continue;
      }

      if (item.kind === 'thinking' && item.streaming === true) {
        if (scoped) {
          const messageMismatch = messageId && item.messageId && item.messageId !== messageId;
          const turnMismatch = effectiveTurnId && item.turnId && item.turnId !== effectiveTurnId;
          if (messageMismatch || turnMismatch) continue;
        }
        // Mark the segment as not streaming and finalize any incomplete blocks.
        const finalizedBlocks = item.blocks.map((block) =>
          block.complete ? block : { ...block, complete: true },
        );
        this._map.set(id, {
          ...item,
          blocks: finalizedBlocks,
          streaming: false,
          activeTitle: null,
        } satisfies ThinkingTimelineItem);
        continue;
      }

      // Finalize tool_call items still in a non-terminal status. A tool_result
      // patch may arrive after message_complete due to WS ordering/coalescing.
      // Scoped: skip only tools that explicitly belong to a DIFFERENT turn
      // (an unattributed in-flight tool belongs to the just-completed turn).
      if (item.kind === 'tool_call') {
        const tool = item as ToolCallTimelineItem;
        if (scoped) {
          const toolTurnId = (tool as unknown as { turnId?: string }).turnId;
          if (toolTurnId && toolTurnId !== effectiveTurnId) continue;
        }
        if (tool.status === 'started' || tool.status === 'running') {
          this._map.set(id, { ...tool, status: 'completed' } as ToolCallTimelineItem);
        }
      }
    }
  }

  /**
   * Apply a `conversation_runtime_snapshot` to the store.
   *
   * This is the testable, router-faithful entry point for runtime snapshots.
   * The page's `applyConversationRuntimeSnapshot` delegates here so the
   * golden replay can drive the same code path as production.
   *
   * Behaviour:
   * - `hasActiveTurn === true`: enqueue the snapshot's `timeline_items` via
   *   rAF-batched `enqueuePatch` (existing behaviour — live streaming items
   *   are injected into the store).
   * - `hasActiveTurn === false`: flush pending patches, then call
   *   `finalizeAllStreaming()` to clear any stale streaming state.  Incoming
   *   `streaming:true` items from the snapshot are **ignored** — the server
   *   says no turn is running, so any streaming items in the snapshot are
   *   stale artifacts that must not be re-injected.
   *
   * Session filtering is applied by the caller before passing `items` here
   * (same as the existing `enqueuePatch` call in the page).
   */
  applyRuntimeSnapshot(
    items: TimelineProjectionItem[],
    hasActiveTurn: boolean,
  ): void {
    if (hasActiveTurn) {
      this._runtimeSettled = false;
      if (items.length > 0) {
        this.enqueuePatch(items);
      }
      return;
    }

    // No active turn: flush any pending rAF patches first (they may include
    // the live.assistant_complete patch that sets streaming:false on the
    // primary phase), then finalize everything that's still streaming.
    this.flushPending();
    this.finalizeAllStreaming();
    this._runtimeSettled = true;
    // Do NOT enqueue the snapshot's items — they may contain stale
    // streaming:true items from a race between turn teardown and snapshot
    // generation. The server's has_active_turn:false is authoritative.
  }

  /**
   * Collect ids of items that are unconfirmed live state and must not be
   * evicted by a refresh snapshot that doesn't yet contain them.
   *
   * An item is "unconfirmed live" when any of:
   *  - it is an assistant message or thinking item still streaming:true;
   *  - it is a tool_call in a non-terminal status (started/running);
   *  - it carries an unpersisted runtime sentinel orderKey (lineage 9998/9999).
   *
   * These are exactly the items a transient history-projection gap can drop
   * (the "message disappears after refresh" symptom). Persisted, terminal
   * items are NOT preserved here — a legitimate server-side removal still
   * applies to them.
   */
  private _unconfirmedLiveIds(): Set<string> {
    const ids = new Set<string>();
    for (const [id, item] of this._map) {
      // Regression guard (Issue A): optimistic USER messages must NEVER be
      // preserved across a refresh. They are reconciled by the clientMessageId/
      // draft path (user_message echo → applyEvent → _reconcileMap without
      // preserveIds). Canonical user_message echoes, however, may be absent from
      // a sparse refresh projection and must survive the refresh.
      if (item.kind === 'message' && item.role === 'user') {
        if (!item.optimistic) {
          ids.add(id);
        }
        continue;
      }

      if (isUnpersistedRuntimeOrderKey(item.orderKey)) {
        ids.add(id);
        continue;
      }
      if (item.kind === 'message' && item.role === 'assistant' && item.streaming === true) {
        ids.add(id);
        continue;
      }
      if (item.kind === 'thinking' && item.streaming === true) {
        ids.add(id);
        continue;
      }
      if (item.kind === 'tool_call') {
        const tool = item as ToolCallTimelineItem;
        if (tool.status === 'started' || tool.status === 'running') {
          ids.add(id);
        }
      }
    }
    return ids;
  }

  private _terminalizeProjection(projected: TimelineProjectionItem[]): TimelineProjectionItem[] {
    return projected.map((item) => {
      if (item.kind === 'message' && item.role === 'assistant') {
        return { ...item, streaming: false };
      }
      if (item.kind === 'stream') {
        return { ...item, streaming: false };
      }
      if (item.kind === 'thinking') {
        const thinking = item as unknown as ThinkingTimelineItem;
        return {
          ...thinking,
          activeTitle: null,
          streaming: false,
          blocks: thinking.blocks.map((block) => ({ ...block, complete: true })),
        };
      }
      if (item.kind === 'tool_call') {
        const status = typeof item.status === 'string' ? item.status : '';
        if (status === 'started' || status === 'running') {
          return { ...item, status: 'completed' };
        }
      }
      return item;
    });
  }

  private _cancelRaf(): void {
    if (this._rafHandle !== null) {
      cancelAnimationFrame(this._rafHandle);
      this._rafHandle = null;
    }
  }
}

// ---------------------------------------------------------------------------
// Module-level helpers
// ---------------------------------------------------------------------------

/**
 * Event types that DO mutate the timeline and are re-homed into the store as
 * explicit, enumerated routes (Phase 2b). Each one delegates to the matching
 * pure per-event branch in chat.ts via `applyTimelineMutationEvent` — the
 * branch bodies encode hard-won bug fixes (ghost tool_call avoidance,
 * compaction running→compacted card swap, notice dedup, thinking dedup) and
 * are preserved exactly. The result array is reconciled into the map by
 * `_reconcileMap` (surgical diff, no clear).
 *
 * This set replaces the old monolithic catch-all: any event type that is
 * neither here, in `_TIMELINE_NOOP_EVENT_TYPES`, nor handled earlier in
 * `applyEvent` (timeline_patch, message_complete) is a true no-op and never
 * triggers a full-array rebuild.
 */
const _TIMELINE_MUTATING_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user_message',
  'system_message',
  'escalation',
  'session_compacted',
  'session_compaction_started',
  'session_compaction_finished',
  'history_notice',
  'chunk_gap',
  'workflow_composed',
  'workflow_step_started',
  'workflow_step_completed',
  'workflow_completed',
  'workflow_failed',
  'workflow_cancelled',
  'workflow_gate',
  'workflow_step_question',
  'auth_challenge',
  'credential_request',
  'workflow_gate_resolved',
  'workflow_step_question_resolved',
  'auth_challenge_resolved',
  'credential_request_resolved',
  'task_paused',
  'delegation_started',
  'delegation_progress',
  'delegation_completed',
  'delegation_failed',
]);

/**
 * Explicit per-event-type dispatch for mutating timeline events.
 *
 * This is the store-side router that replaced the monolithic
 * `applyWebSocketEvent` catch-all in `applyEvent`. It is gated by
 * `_TIMELINE_MUTATING_EVENT_TYPES` so only enumerated mutating types reach it.
 *
 * Each route delegates to `applyWebSocketEvent` (chat.ts), which runs the exact
 * legacy per-event branch (`_applyWebSocketEventInner`) plus the normalisation
 * pass. The handler logic is MOVED, not rewritten: the bug-fix-laden branch
 * bodies stay in chat.ts and remain the single source of truth (also covered by
 * chat.test.ts). The store now routes to them explicitly per type instead of
 * blindly funnelling every non-patch event through one call.
 */
function applyTimelineMutationEvent(
  items: TimelineItem[],
  event: CognisWebSocketEvent,
): TimelineItem[] {
  switch (event.type) {
    case 'user_message':
    case 'system_message':
    case 'escalation':
    case 'session_compacted':
    case 'session_compaction_started':
    case 'session_compaction_finished':
    case 'history_notice':
    case 'chunk_gap':
    case 'workflow_composed':
    case 'workflow_step_started':
    case 'workflow_step_completed':
    case 'workflow_completed':
    case 'workflow_failed':
    case 'workflow_cancelled':
    case 'workflow_gate':
    case 'workflow_step_question':
    case 'auth_challenge':
    case 'credential_request':
    case 'workflow_gate_resolved':
    case 'workflow_step_question_resolved':
    case 'auth_challenge_resolved':
    case 'credential_request_resolved':
    case 'task_paused':
    case 'delegation_started':
    case 'delegation_progress':
    case 'delegation_completed':
    case 'delegation_failed':
      return applyWebSocketEvent(items, event);
    default:
      return items;
  }
}

/**
 * Event types that produce NO timeline-item mutation. Either:
 *  - their live state arrives via `timeline_patch` (tool_*, *_chunk), or
 *  - they are handled entirely in the page and would `return` before reaching
 *    the store in production (turn_started/settled, queued*, conversation_*,
 *    reconnected, history_*), so the engine branch is dead.
 *
 * Short-circuiting them in applyEvent documents intent and skips the
 * flushPending/sort work the mutating path performs. They are not strictly
 * required for correctness — any type not in `_TIMELINE_MUTATING_EVENT_TYPES`
 * already falls through to `return false` — but the explicit set keeps the
 * store→engine surface fully enumerated on both sides.
 */
const _TIMELINE_NOOP_EVENT_TYPES: ReadonlySet<string> = new Set([
  // Live tool state arrives via timeline_patch — these have no WS-engine branch.
  'tool_call',
  'tool_progress',
  'tool_result',
  'tool_result_chunk',
  'tool_output_chunk',
  // Turn/queue lifecycle — page-handled, no timeline mutation.
  'turn_started',
  'turn_settled',
  'queued',
  'queued_messages_updated',
  // Page-handled-then-returned in production (dead via the store).
  'conversation_updated',
  'reconnected',
  'session_recovered',
  'history_gap',
]);

/**
 * Filter a projected item to the active session.
 * Items without a sessionId (notices, system messages, compaction cards)
 * always pass through.
 *
 * @param activeSessionLineage - the set of session IDs in the active session's
 *   compaction lineage (previous_session_id chain, stopping at parent_session_id).
 *   Items from lineage sessions are allowed through during turnInProgress (the
 *   compaction in-flight case). Items from sub-sessions (not in the lineage)
 *   are rejected even during turnInProgress — this is the client backstop for
 *   the sub-session tool_call leak (server primary fix: _is_subsession gate in
 *   websocket.py _handle_event).
 */
function _itemBelongsToSession(
  item: TimelineProjectionItem,
  activeSessionId: string | null,
  turnInProgress = false,
  activeSessionLineage: ReadonlySet<string> | null = null,
): boolean {
  if (!activeSessionId) return true;
  const kind = item.kind;
  if (kind !== 'message' && kind !== 'tool_call' && kind !== 'thinking') return true;
  const sessionId = typeof item.sessionId === 'string' ? item.sessionId : null;
  if (!sessionId) return true;
  if (sessionId === activeSessionId) return true;
  // During an active turn, allow items from the active session's compaction
  // lineage (previous sessions in the same conversation, linked via
  // previous_session_id). These may still be in-flight when active_session_id
  // rotates to a new session after compaction.
  //
  // Sub-session items (from delegated/workflow child sessions) are NOT in the
  // lineage and must be rejected even during turnInProgress — they belong in
  // the sub-session detail panel, not the main timeline. The server-side fix
  // (_is_subsession gate) is the primary defense; this is the client backstop.
  if (turnInProgress) {
    if (activeSessionLineage !== null) {
      return activeSessionLineage.has(sessionId);
    }
    // No lineage provided (e.g. sessions not yet loaded): fall back to the
    // old wholesale bypass so we don't break the compaction in-flight case.
    return true;
  }
  return false;
}
