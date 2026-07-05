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
  visibleTimelineItems,
  type ChatV2ClientState,
  type ChatV2SyncResult
} from './sync-engine';

export class ChatV2Store {
  private _state: ChatV2ClientState = $state.raw(emptyChatV2State());

  readonly visibleItems: TimelineItem[] = $derived(visibleTimelineItems(this._state));
  readonly cycleStates: TurnCycleState[] = $derived(this._state.cycleStates);

  get snapshot(): ChatV2ClientState {
    return this._state;
  }

  replaceFromSnapshot(snapshot: ChatSnapshot): void {
    this._state = applySnapshot(snapshot, this._state);
  }

  addOptimisticUser(input: {
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    createdAt?: string;
  }): void {
    this._state = addOptimisticUserMessage(this._state, input);
  }

  addLocalSystemMessage(input: { id: string; content: string; createdAt?: string }): void {
    this._state = addLocalSystemMessage(this._state, input);
  }

  applySync(response: ChatSyncResponse): ChatV2SyncResult {
    const result = applySyncResponse(this._state, response);
    this._state = result.state;
    return result;
  }

  applyRealtime(frame: ChatRealtimeFrame): ChatV2SyncResult {
    const result = applyRealtimeFrame(this._state, frame);
    this._state = result.state;
    return result;
  }

  applyBackfill(response: TimelineBackfillResponse): void {
    this._state = applyBackfill(this._state, response);
  }

  applySend(response: SendMessageV2Response): void {
    this._state = applySendResponse(this._state, response);
  }

  applyCancel(response: CancelTurnV2Response): void {
    this._state = applyCancelResponse(this._state, response);
  }

  applyQueueMutation(response: QueueMutationResponse): void {
    this._state = applyQueueMutationResponse(this._state, response);
  }

  reset(): void {
    this._state = emptyChatV2State();
  }

  /**
   * Snapshot the full client state for the conversation-view cache. Returns a
   * plain (non-reactive) deep copy so cached entries are not mutated by later
   * updates.
   *
   * The store owns immutable replacement state and keeps it raw to avoid deep
   * Svelte proxies over large timeline arrays. A structured clone is enough to
   * keep cached entries isolated from future replacements.
   */
  serializeState(): ChatV2ClientState {
    return structuredClone(this._state);
  }

  /** Restore client state previously captured via {@link serializeState}. */
  restoreState(state: ChatV2ClientState): void {
    // Deep-copy the cached entry so the restored raw state is not aliased with
    // the cache.
    const restored = structuredClone(state);
    this._state = { ...restored, cycleStates: restored.cycleStates ?? [] };
  }

  /**
   * Neutralize a runtime overlay so no streaming/volatile rows remain active.
   * Used when restoring a cached view whose turn may have settled since the
   * snapshot was taken; the authoritative runtime is refreshed afterwards.
   */
  settleRuntimeOverlay(): void {
    const runtime = this._state.runtime;
    if (!runtime || !runtime.has_active_turn) return;
    this._state = {
      ...this._state,
      runtime: { ...runtime, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
      cycleStates: this._state.cycleStates.filter((state) => state.turn_id !== runtime.active_turn?.turn_id)
    };
  }
}
