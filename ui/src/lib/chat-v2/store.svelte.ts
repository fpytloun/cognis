import type {
  ActivityOverviewResponse,
  CancelTurnV2Response,
  ChatRealtimeFrame,
  ChatSnapshot,
  ChatSyncResponse,
  QueueMessage,
  QueueMutationResponse,
  SendMessageV2Response,
  MessageAdmissionV2Response,
  TimelineBackfillResponse,
  TimelineItem,
  TurnCycleState
} from './types';
import type { AttachmentRef } from '$lib/types/api';
import { reconcileCompactionScope } from './compaction-scope';
import {
  addOptimisticUserMessage,
  addLocalSystemMessage,
  applyBackfillResult,
  applyCancelResponse,
  applyQueueMutationResponse,
  applyRealtimeFrame,
  applySendResponse,
  applyMessageAdmission,
  applySnapshot,
  applySyncResponse,
  deleteQueuedAdmission,
  emptyChatV2State,
  normalizeTimelineItems,
  markOptimisticUserMessageFailed,
  promoteQueuedUserMessage,
  replaceCanonicalQueue,
  updateQueuedAdmissionContent,
  visibleQueueMessages,
  visibleTimelineItems,
  type ChatV2ClientState,
  type ChatV2SyncResult
} from './sync-engine';

export interface ChatV2RefreshWatermark {
  cursor: string | null;
  runtimeRevision: number | null;
  mutationRevision: number;
  canonicalRevision: number;
}

export type PrepareTimelineMembership = (
  before: readonly TimelineItem[],
  after: readonly TimelineItem[],
) => (() => void) | void;

export class ChatV2Store {
  private _state: ChatV2ClientState = $state.raw(emptyChatV2State());
  private mutationRevision = 0;
  private canonicalRevision = 0;
  private membershipViews = new Set<PrepareTimelineMembership>();

  /** Views prepare against the old DOM and commit their own window after one
   * shared projection. Unsubscribing also fences a prepared commit. */
  observeMembership(prepare: PrepareTimelineMembership): () => void {
    this.membershipViews.add(prepare);
    return () => { this.membershipViews.delete(prepare); };
  }

  readonly visibleItems: TimelineItem[] = $derived(visibleTimelineItems(this._state));
  readonly visibleQueue = $derived(visibleQueueMessages(this._state));
  readonly cycleStates: TurnCycleState[] = $derived(this._state.cycleStates);

  get snapshot(): ChatV2ClientState {
    return this._state;
  }

  refreshWatermark(): ChatV2RefreshWatermark {
    return {
      cursor: this._state.cursor,
      runtimeRevision: this._state.runtime?.runtime_revision ?? null,
      mutationRevision: this.mutationRevision,
      canonicalRevision: this.canonicalRevision,
    };
  }

  replaceFromSnapshotIfUnchanged(snapshot: ChatSnapshot, watermark: ChatV2RefreshWatermark): boolean {
    const current = this.refreshWatermark();
    if (
      current.cursor !== watermark.cursor
      || current.runtimeRevision !== watermark.runtimeRevision
      || current.mutationRevision !== watermark.mutationRevision
    ) return false;
    this.replaceFromSnapshot(snapshot);
    return true;
  }

  replaceFromSnapshotForCanonicalBootstrap(
    snapshot: ChatSnapshot,
    watermark: ChatV2RefreshWatermark,
    bootstrapPending: boolean
  ): boolean {
    if (bootstrapPending) {
      this.replaceState(applySnapshot(
        snapshot,
        this._state,
        { adoptUnscopedLocalAdmissions: true },
      ), true, true);
      return true;
    }
    return this.replaceFromSnapshotIfUnchanged(snapshot, watermark);
  }

  replaceFromSnapshot(snapshot: ChatSnapshot, discardCachedHistory = false): void {
    const previous = discardCachedHistory ? {
      ...this._state, timelineItems: [], cycleStates: [],
      projectionVersion: snapshot.projection_version,
    } : this._state;
    const state = applySnapshot(snapshot, previous, {
      adoptUnscopedLocalAdmissions: discardCachedHistory,
    });
    if (discardCachedHistory && state.scopeKey === this._state.scopeKey) {
      // Preserve positive canonical confirmation before discarding its page.
      // HTTP acceptance alone never establishes this evidence.
      const confirmed = new Set([...this._state.timelineItems, ...state.timelineItems].flatMap(item =>
        item.kind === 'message' && item.role === 'user' && item.stable && item.client_message_id
          && item.source_refs.some(ref => ref.store === 'intaris') ? [item.client_message_id] : []));
      state.admissionPlacements = { ...state.admissionPlacements };
      for (const item of state.localItems) {
        if (item.kind !== 'message' || item.role !== 'user' || !item.client_message_id
          || !confirmed.has(item.client_message_id)) continue;
        const prior = state.admissionPlacements[item.client_message_id];
        if (prior?.placement === 'deleted') continue;
        state.admissionPlacements[item.client_message_id] = {
          ...(prior ?? { placement: 'timeline', status: 'complete' }), historyConfirmed: true,
        };
      }
    }
    this.replaceState(discardCachedHistory ? {
      ...state, localItems: state.localItems.filter(item => item.kind === 'message' && item.role === 'user'),
    } : state, true, true);
  }

  /** Runtime/local admissions do not establish a newer canonical base.
   * applySnapshot preserves those overlays using their own acceptance rules. */
  replaceFromSnapshotForRecovery(snapshot: ChatSnapshot, watermark: ChatV2RefreshWatermark, discardCachedHistory = false): boolean {
    if (this.canonicalRevision !== watermark.canonicalRevision) return false;
    this.replaceFromSnapshot(snapshot, discardCachedHistory);
    return true;
  }

  addOptimisticUser(input: {
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    createdAt?: string;
    chatMode?: 'default' | 'plan' | 'build';
  }): void {
    this.replaceState(addOptimisticUserMessage(this._state, input));
  }

  markOptimisticUserFailed(clientMessageId: string): void {
    this.replaceState(markOptimisticUserMessageFailed(this._state, clientMessageId));
  }

  promoteQueuedUser(input: {
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    createdAt?: string;
    chatMode?: 'default' | 'plan' | 'build';
  }): void {
    this.replaceState(promoteQueuedUserMessage(this._state, input));
  }

  addLocalSystemMessage(input: { id: string; content: string; noticeId?: string | null; createdAt?: string }): void {
    this.replaceState(addLocalSystemMessage(this._state, input));
  }

  applySync(response: ChatSyncResponse): ChatV2SyncResult {
    const result = applySyncResponse(this._state, response);
    this.replaceState(result.state, this.hasCanonicalChange(result.state)
      || result.outcome === 'reset_required' || result.outcome === 'cursor_mismatch');
    return result;
  }

  applyRealtime(frame: ChatRealtimeFrame): ChatV2SyncResult {
    const result = applyRealtimeFrame(this._state, frame);
    this.replaceState(result.state, this.hasCanonicalChange(result.state)
      || result.outcome === 'reset_required' || result.outcome === 'cursor_mismatch');
    return result;
  }

  applyBackfill(response: TimelineBackfillResponse): boolean {
    const result = applyBackfillResult(this._state, response);
    this.replaceState(result.state);
    return result.admitted;
  }

  applyAdmissionOutcome(response: MessageAdmissionV2Response): void {
    this.replaceState(applyMessageAdmission(this._state, response));
  }

  reconcileCompactions(overview: ActivityOverviewResponse): void {
    this.replaceState(reconcileCompactionScope(this._state, overview));
  }

  applySend(response: SendMessageV2Response): void {
    const state = applySendResponse(this._state, response);
    this.replaceState(state, this.hasCanonicalChange(state));
  }

  applyCancel(response: CancelTurnV2Response): void {
    const state = applyCancelResponse(this._state, response);
    this.replaceState(state, this.hasCanonicalChange(state));
  }

  applyQueueMutation(response: QueueMutationResponse): void {
    this.replaceState(applyQueueMutationResponse(this._state, response));
  }

  replaceQueue(queue: NonNullable<ChatV2ClientState['queue']>): void {
    this.replaceState(replaceCanonicalQueue(this._state, queue));
  }

  updateQueuedAdmission(
    queueId: string,
    content: string,
    source?: Pick<QueueMessage, 'client_message_id' | 'attachments' | 'created_at'>
  ): void {
    this.replaceState(updateQueuedAdmissionContent(this._state, queueId, content, {
      clientMessageId: source?.client_message_id,
      attachments: source?.attachments,
      createdAt: source?.created_at
    }));
  }

  deleteQueuedAdmission(queueId: string, clientMessageId?: string | null): void {
    this.replaceState(deleteQueuedAdmission(this._state, queueId, clientMessageId));
  }

  reset(): void {
    this.replaceState(emptyChatV2State(), true, true);
  }

  /**
   * Snapshot the full client state for the conversation-view cache. Returns a
   * plain (non-reactive) deep copy so cached entries are not mutated by later
   * updates.
   *
   * The store owns immutable replacement state and keeps it raw to avoid deep
   * Svelte proxies over large timeline arrays. Use $state.snapshot instead of
   * structuredClone so any reactive proxy accidentally injected into local
   * state is converted to plain data before the browser clone algorithm sees it.
   */
  serializeState(options: { settleRuntimeOverlay?: boolean } = {}): ChatV2ClientState {
    const state = $state.snapshot(this._state) as ChatV2ClientState;
    const runtime = state.runtime;
    const localItems = state.localItems.filter(
      (item) => !(item.kind === 'message' && item.role === 'system')
    );
    if (!options.settleRuntimeOverlay || !runtime?.has_active_turn) {
      return { ...state, localItems };
    }
    const activeTurnId = runtime.active_turn?.turn_id;
    return {
      ...state,
      localItems,
      // Cache-only neutralization is not an authoritative terminal transition.
      runtime: { ...runtime, authority: null, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
      cycleStates: state.cycleStates.filter((cycleState) => cycleState.turn_id !== activeTurnId)
    };
  }

  /** Restore client state previously captured via {@link serializeState}. */
  restoreState(state: ChatV2ClientState): void {
    // Deep-copy the cached entry so the restored raw state is not aliased with
    // the cache. $state.snapshot is proxy-safe whether the input is plain data
    // or a reactive proxy from a caller.
    const restored = $state.snapshot(state) as ChatV2ClientState;
    const runtime = restored.runtime;
    // Repair persisted entries written before cache neutralization cleared authority.
    // Real terminal authority must survive restore to reject delayed active frames.
    const cachedActiveAuthority = runtime?.has_active_turn === false
      && runtime.authority?.protocol === 'runtime_authority_v1'
      && runtime.authority.lifecycle === 'active';
    this.replaceState({
      ...restored,
      timelineItems: normalizeTimelineItems(restored.timelineItems),
      runtime: cachedActiveAuthority ? { ...runtime, authority: null } : runtime,
      cycleStates: restored.cycleStates ?? [],
      admissionPlacements: restored.admissionPlacements ?? {}
    }, true, true);
  }

  /**
   * Neutralize a runtime overlay so no streaming/volatile rows remain active.
   * Used when restoring a cached view whose turn may have settled since the
   * snapshot was taken; the authoritative runtime is refreshed afterwards.
   */
  settleRuntimeOverlay(): void {
    const runtime = this._state.runtime;
    if (!runtime || !runtime.has_active_turn) return;
    this.replaceState({
      ...this._state,
      runtime: { ...runtime, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
      cycleStates: this._state.cycleStates.filter((state) => state.turn_id !== runtime.active_turn?.turn_id)
    });
  }

  private hasCanonicalChange(state: ChatV2ClientState): boolean {
    return state.cursor !== this._state.cursor
      || state.timelineItems !== this._state.timelineItems
      || state.state !== this._state.state
      || state.projectionVersion !== this._state.projectionVersion;
  }

  private replaceState(state: ChatV2ClientState, canonical = false, replacesLineage = false): void {
    if (state === this._state) return;
    const commits: Array<{ view: PrepareTimelineMembership; commit: () => void }> = [];
    if (this.membershipViews.size) {
      const before = visibleTimelineItems(this._state);
      const after = visibleTimelineItems(state);
      if (replacesLineage || before.length !== after.length || before.some((item, i) => item.id !== after[i]?.id)) {
        for (const view of this.membershipViews) {
          const commit = view(before, after);
          if (commit) commits.push({ view, commit });
        }
      }
    }
    this._state = state;
    this.mutationRevision += 1;
    if (canonical) this.canonicalRevision += 1;
    for (const { view, commit } of commits) {
      if (this.membershipViews.has(view)) commit();
    }
  }
}
