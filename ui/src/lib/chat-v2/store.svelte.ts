import type {
  CancelTurnV2Response,
  ChatRealtimeFrame,
  ChatSnapshot,
  ChatSyncResponse,
  QueueMutationResponse,
  SendMessageV2Response,
  TimelineBackfillResponse,
  TimelineItem,
  TurnCycleState
} from './types';
import type { AttachmentRef } from '$lib/types/api';
import {
  addOptimisticUserMessage,
  addLocalSystemMessage,
  applyBackfill,
  applyCancelResponse,
  applyQueueMutationResponse,
  applyRealtimeFrame,
  applySendResponse,
  applySnapshot,
  applySyncResponse,
  emptyChatV2State,
  markOptimisticUserMessageFailed,
  visibleTimelineItems,
  type ChatV2ClientState,
  type ChatV2SyncResult
} from './sync-engine';

export interface ChatV2RefreshWatermark {
  cursor: string | null;
  runtimeRevision: number | null;
  mutationRevision: number;
}

export class ChatV2Store {
  private _state: ChatV2ClientState = $state.raw(emptyChatV2State());
  private mutationRevision = 0;

  readonly visibleItems: TimelineItem[] = $derived(visibleTimelineItems(this._state));
  readonly cycleStates: TurnCycleState[] = $derived(this._state.cycleStates);

  get snapshot(): ChatV2ClientState {
    return this._state;
  }

  refreshWatermark(): ChatV2RefreshWatermark {
    return {
      cursor: this._state.cursor,
      runtimeRevision: this._state.runtime?.runtime_revision ?? null,
      mutationRevision: this.mutationRevision
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

  replaceFromSnapshot(snapshot: ChatSnapshot): void {
    this.replaceState(applySnapshot(snapshot, this._state));
  }

  addOptimisticUser(input: {
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    createdAt?: string;
  }): void {
    this.replaceState(addOptimisticUserMessage(this._state, input));
  }

  markOptimisticUserFailed(clientMessageId: string): void {
    this.replaceState(markOptimisticUserMessageFailed(this._state, clientMessageId));
  }

  addLocalSystemMessage(input: { id: string; content: string; noticeId?: string | null; createdAt?: string }): void {
    this.replaceState(addLocalSystemMessage(this._state, input));
  }

  applySync(response: ChatSyncResponse): ChatV2SyncResult {
    const result = applySyncResponse(this._state, response);
    this.replaceState(result.state);
    return result;
  }

  applyRealtime(frame: ChatRealtimeFrame): ChatV2SyncResult {
    const result = applyRealtimeFrame(this._state, frame);
    this.replaceState(result.state);
    return result;
  }

  applyBackfill(response: TimelineBackfillResponse): void {
    this.replaceState(applyBackfill(this._state, response));
  }

  applySend(response: SendMessageV2Response): void {
    this.replaceState(applySendResponse(this._state, response));
  }

  applyCancel(response: CancelTurnV2Response): void {
    this.replaceState(applyCancelResponse(this._state, response));
  }

  applyQueueMutation(response: QueueMutationResponse): void {
    this.replaceState(applyQueueMutationResponse(this._state, response));
  }

  reset(): void {
    this.replaceState(emptyChatV2State());
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
      runtime: { ...runtime, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
      cycleStates: state.cycleStates.filter((cycleState) => cycleState.turn_id !== activeTurnId)
    };
  }

  /** Restore client state previously captured via {@link serializeState}. */
  restoreState(state: ChatV2ClientState): void {
    // Deep-copy the cached entry so the restored raw state is not aliased with
    // the cache. $state.snapshot is proxy-safe whether the input is plain data
    // or a reactive proxy from a caller.
    const restored = $state.snapshot(state) as ChatV2ClientState;
    this.replaceState({ ...restored, cycleStates: restored.cycleStates ?? [] });
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

  private replaceState(state: ChatV2ClientState): void {
    if (state === this._state) return;
    this._state = state;
    this.mutationRevision += 1;
  }
}
