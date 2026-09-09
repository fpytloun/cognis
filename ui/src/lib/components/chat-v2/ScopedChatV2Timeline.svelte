<script lang="ts">
  import { tick, untrack } from 'svelte';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import TaskStepOutcomeEpilogue from '$lib/components/tasks/TaskStepOutcomeEpilogue.svelte';
  import TimelineTodoDrawer from '$lib/components/timeline/TimelineTodoDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { chatV2Api, type ChatV2ApiClient } from '$lib/chat-v2/api';
  import { selectLatestTodoState } from '$lib/chat-v2/selectors';
  import { ChatV2Store } from '$lib/chat-v2/store.svelte';
  import type { QueueMessage, QueueMutationResponse, TimelineScope, TurnCycleState } from '$lib/chat-v2/types';
  import type { Agent, CognisWebSocketEvent, StepRun, UserPreferences } from '$lib/types/api';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import { userPreferences } from '$lib/stores/userPreferences';
  import { wsClient } from '$lib/ws/client';
  import type {
    ChatRealtimeFrame,
    ChatSyncResponse,
    SendMessageV2Response,
    TimelineBackfillResponse
  } from '$lib/chat-v2/types';
  import type { AttachmentRef } from '$lib/types/api';
  import type { TodoSnapshotItem } from '$lib/todos';

  export interface ScopedChatV2Realtime {
    subscribe: (listener: (event: any) => void) => () => void;
    acquireChatV2: (scope: TimelineScope, cursor: string) => void;
    updateChatV2Cursor: (scope: TimelineScope, cursor: string) => void;
    releaseChatV2: (scopeKey: string) => void;
  }

  let {
    scope,
    agent = null,
    preferences = undefined,
    compact = false,
    userScrolledUp = $bindable(false),
    onViewSession,
    emptyLabel = 'No events recorded yet.',
    onTodosChange,
    onMissingStream,
    activityStatus = '',
    stepRun = null,
    onRuntimeActiveChange,
    hasEditableComposer = false,
    showQueuedMessages = true,
    onQueueChange,
    onInitialLoaded,
    autoBackfill = undefined,
    api = chatV2Api,
    realtime = wsClient
  } = $props<{
    scope: TimelineScope;
    agent?: Agent | null;
    preferences?: UserPreferences;
    compact?: boolean;
    userScrolledUp?: boolean;
    onViewSession?: (sessionId: string) => void | Promise<void>;
    emptyLabel?: string;
    onTodosChange?: (todos: TodoSnapshotItem[]) => void;
    onMissingStream?: (() => void) | undefined;
    activityStatus?: string;
    stepRun?: StepRun | null;
    onRuntimeActiveChange?: ((active: boolean) => void) | undefined;
    hasEditableComposer?: boolean;
    showQueuedMessages?: boolean;
    onQueueChange?: ((queue: QueueMessage[]) => void) | undefined;
    onInitialLoaded?: (() => void | Promise<void>) | undefined;
    autoBackfill?: boolean | undefined;
    api?: Pick<ChatV2ApiClient, 'snapshot' | 'sync' | 'timeline'>;
    realtime?: ScopedChatV2Realtime;
  }>();

  let store = $state.raw(new ChatV2Store());
  let loading = $state(true);
  let error = $state('');
  let loadingOlder = $state(false);
  let olderError = $state('');
  let hasOlder = $state(false);
  let viewport = $state<HTMLDivElement | null>(null);
  let content = $state<HTMLDivElement | null>(null);
  let timelineViewport = $state<{
    setProgrammaticScrollTop(scrollTop: number): void;
    scrollProgrammaticallyToBottom(): void;
  } | null>(null);
  let todoDrawerOpen = $state(false);
  let trustedUpwardInteraction = false;
  let initialPinPending = $state(false);
  let initialLoadedPending = $state(false);
  let initialLoadedScopeKey = $state<string | null>(null);
  let touchY: number | null = null;
  let scopeGeneration = 0;
  let requestGeneration = 0;
  let fillCheckGeneration = 0;
  let autoFillAllowed = true;
  let restoringScrollAnchor = false;
  let initialPositionSettled = $state(false);
  let reconciliationTimer: ReturnType<typeof setTimeout> | null = null;
  let recentQueuedAdmissions = new Map<string, QueueMessage>();
  // The former child-session log loaded up to 1,000 raw events at bootstrap.
  // A scoped snapshot contains 200 events and backfill pages contain 100, so
  // eight automatic pages preserve that initial history budget.
  const AUTO_FILL_PAGE_BUDGET = 8;
  const SNAPSHOT_YIELD_WATCHDOG_MS = 100;
  // Bounded so a stalled/slow snapshot surfaces a recoverable error quickly
  // instead of leaving the child view stuck on "Loading timeline…"
  // indefinitely (was 30s).
  const INITIAL_SNAPSHOT_TIMEOUT_MS = 15_000;
  let autoFillPagesRemaining = AUTO_FILL_PAGE_BUDGET;
  let seenBackfillCursors = new Set<string>();
  let acquiredScopeKey: string | null = null;
  let snapshotController: AbortController | null = null;
  let syncController: AbortController | null = null;
  let backfillController: AbortController | null = null;
  let items = $derived(store.visibleItems);
  const effectivePreferences = $derived(preferences ?? $userPreferences ?? DEFAULT_USER_PREFERENCES);
  const cycleStates = $derived<TurnCycleState[]>(store.cycleStates);
  const todos = $derived(selectLatestTodoState(items));
  const queue = $derived(store.visibleQueue.messages);
  const effectiveAutoBackfill = $derived(autoBackfill ?? !hasEditableComposer);
  $effect(() => {
    const nextTodos = todos;
    untrack(() => onTodosChange?.(nextTodos));
  });
  $effect(() => {
    const nextQueue = queue;
    untrack(() => onQueueChange?.(nextQueue));
  });
  const runtimeActive = $derived(store.snapshot.runtime?.has_active_turn === true);
  const stepRunLive = $derived(stepRun?.status === 'running' || stepRun?.status === 'evaluating');
  const effectiveRuntimeActive = $derived(stepRun ? stepRunLive : runtimeActive);
  const hasStreamingItems = $derived(
    effectiveRuntimeActive && items.some((item) => item.stable === false || item.status === 'running')
  );
  const live = $derived(stepRun ? stepRunLive : runtimeActive || activityStatus === 'running' || activityStatus === 'evaluating');

  export function applyQueuedMessageUpdate(
    response: QueueMutationResponse,
    message: QueueMessage,
    content: string
  ): void {
    store.updateQueuedAdmission(message.queue_id, content, message);
    store.applyQueueMutation(response);
  }

  export function applyQueuedMessageDelete(
    response: QueueMutationResponse,
    message: QueueMessage
  ): void {
    store.deleteQueuedAdmission(message.queue_id, message.client_message_id);
    store.applyQueueMutation(response);
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof DOMException && error.name === 'AbortError';
  }

  // Yield once before every snapshot projection. Standalone Safari can pause
  // requestAnimationFrame even while the window remains visible, so use the
  // task scheduler with a timer fallback and an independent watchdog.
  function yieldToBrowser(): Promise<void> {
    return new Promise<void>((resolve) => {
      let settled = false;
      let taskTimer: ReturnType<typeof setTimeout> | null = null;
      const finish = (): void => {
        if (settled) return;
        settled = true;
        if (taskTimer !== null) clearTimeout(taskTimer);
        clearTimeout(watchdog);
        resolve();
      };
      const watchdog = setTimeout(finish, SNAPSHOT_YIELD_WATCHDOG_MS);
      const schedulerApi = (globalThis as typeof globalThis & {
        scheduler?: { yield?: () => Promise<void> };
      }).scheduler;
      if (schedulerApi?.yield) {
        try {
          void schedulerApi.yield().then(finish, finish);
        } catch {
          taskTimer = setTimeout(finish, 0);
        }
      } else {
        taskTimer = setTimeout(finish, 0);
      }
    });
  }

  function abortRequests(): void {
    snapshotController?.abort();
    syncController?.abort();
    backfillController?.abort();
    snapshotController = null;
    syncController = null;
    backfillController = null;
    if (reconciliationTimer) clearTimeout(reconciliationTimer);
    reconciliationTimer = null;
    recentQueuedAdmissions = new Map();
  }

  function resetBackfillPass(): void {
    loadingOlder = false;
    olderError = '';
    touchY = null;
    autoFillAllowed = true;
    autoFillPagesRemaining = AUTO_FILL_PAGE_BUDGET;
    seenBackfillCursors = new Set();
    fillCheckGeneration += 1;
  }

  async function recoverFor(
    currentScope: TimelineScope,
    currentStore: ChatV2Store,
    scopeVersion: number,
    requestVersion: number
  ): Promise<void> {
    if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
    loading = true;
    error = '';
    const refreshWatermark = currentStore.refreshWatermark();
    const controller = new AbortController();
    snapshotController?.abort();
    snapshotController = controller;
    try {
      const snapshot = await api.snapshot(currentScope, {
        signal: controller.signal,
        timeoutMs: INITIAL_SNAPSHOT_TIMEOUT_MS,
      });
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || controller.signal.aborted
        || currentStore !== store
      ) return;
      // Give the browser a chance to paint and process queued Back, Close, or
      // Inspector input before synchronous snapshot projection.
      await yieldToBrowser();
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || controller.signal.aborted
        || currentStore !== store
      ) return;
       if (!currentStore.replaceFromSnapshotIfUnchanged(snapshot, refreshWatermark)) {
        // A live frame won the race. Preserve its canonical/runtime state and
        // reconcile only the public subscription cursor.
        if (currentStore.snapshot.cursor && snapshot.scope?.missing_stream !== true) {
          realtime.updateChatV2Cursor(currentScope, currentStore.snapshot.cursor);
        }
         initialPinPending = true;
         initialLoadedPending = true;
         return;
      }
       hasOlder = currentStore.snapshot.hasMoreBefore;
       initialPinPending = true;
       initialLoadedPending = true;
       const resolvedScope = snapshot.scope ?? currentScope;
       if (resolvedScope.missing_stream !== true) {
          if (acquiredScopeKey === resolvedScope.key) {
            realtime.updateChatV2Cursor(resolvedScope, snapshot.cursor);
          } else {
            realtime.acquireChatV2(resolvedScope, snapshot.cursor);
            acquiredScopeKey = resolvedScope.key;
          }
       }
    } catch (caught) {
       if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
      if (controller.signal.aborted || isAbortError(caught)) return;
      error = caught instanceof Error ? caught.message : 'Could not load timeline.';
    } finally {
       if (snapshotController === controller) snapshotController = null;
       if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) loading = false;
    }
  }

  async function recover(): Promise<void> {
    abortRequests();
    resetBackfillPass();
    const requestVersion = ++requestGeneration;
    await recoverFor({ ...scope }, store, scopeGeneration, requestVersion);
  }

  async function sync(): Promise<void> {
    const currentScope = { ...scope };
    const currentStore = store;
    const scopeVersion = scopeGeneration;
    const requestVersion = requestGeneration;
    let cursor = currentStore.snapshot.cursor;
    if (!cursor) return recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
    const controller = new AbortController();
    syncController?.abort();
    syncController = controller;
    try {
      const seenCursors = new Set<string>();
      while (cursor && !seenCursors.has(cursor)) {
        seenCursors.add(cursor);
         const response: ChatSyncResponse = await api.sync(currentScope, cursor, {
           signal: controller.signal,
         });
         if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
        const result = currentStore.applySync(response);
        if (result.outcome === 'cursor_mismatch' || result.outcome === 'reset_required') {
           await recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
          return;
        }
         if (currentStore.snapshot.cursor) realtime.updateChatV2Cursor(currentScope, currentStore.snapshot.cursor);
        if (!response.has_more || response.cursor_after === cursor) return;
        cursor = response.cursor_after;
      }
    } catch (caught) {
       if (controller.signal.aborted || isAbortError(caught)) return;
       if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) {
          await recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
        }
    } finally {
      if (syncController === controller) syncController = null;
    }
  }

  export function stageOptimisticMessage(input: {
    scopeKey: string;
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    chatMode?: 'default' | 'plan' | 'build';
  }): boolean {
    if (input.scopeKey !== scope.key) return false;
    store.addOptimisticUser({
      content: input.content,
      attachments: input.attachments,
      clientMessageId: input.clientMessageId,
      chatMode: input.chatMode
    });
    return true;
  }

  export async function reconcileMessageAdmission(input: {
    scopeKey: string;
    response: SendMessageV2Response;
  }): Promise<boolean> {
    if (input.scopeKey !== scope.key) return false;
    const scopeVersion = scopeGeneration;
    const currentStore = store;
    currentStore.applySend(input.response);
    await sync();
    return input.scopeKey === scope.key
      && scopeVersion === scopeGeneration
      && currentStore === store;
  }

  export function markMessageAdmissionFailed(input: {
    scopeKey: string;
    clientMessageId: string;
  }): boolean {
    if (input.scopeKey !== scope.key) return false;
    store.markOptimisticUserFailed(input.clientMessageId);
    return true;
  }

  export async function refresh(): Promise<void> {
    await recover();
  }

  function restoreScrollAnchor(
    previousHeight: number,
    previousTop: number
  ): void {
    if (!viewport) return;
    restoringScrollAnchor = true;
    try {
      // Preserve the distance from the current content, independent of browser
      // scroll anchoring and variable-height prepended rows.
      timelineViewport?.setProgrammaticScrollTop(
        previousTop + viewport.scrollHeight - previousHeight,
      );
    } finally {
      // Scroll events dispatch synchronously in browsers. Defer the reset so a
      // synthetic or delayed event from anchor restoration cannot start another
      // backfill pass.
      queueMicrotask(() => { restoringScrollAnchor = false; });
    }
  }

  function beginUserBackfillPass(): void {
    if (loading || loadingOlder) return;
    autoFillAllowed = true;
    autoFillPagesRemaining = AUTO_FILL_PAGE_BUDGET - 1;
    void loadOlder(false);
  }

  async function loadOlder(automatic = false): Promise<void> {
    if (loading) return;
    const currentScope = { ...scope };
    const currentStore = store;
    const currentViewport = viewport;
    const scopeVersion = scopeGeneration;
    const requestVersion = requestGeneration;
    if (
      loadingOlder
      || !currentStore.snapshot.hasMoreBefore
      || currentStore.snapshot.beforeCursor === null
      || (automatic && autoFillPagesRemaining <= 0)
    ) return;
    const before = currentStore.snapshot.beforeCursor;
    if (seenBackfillCursors.has(before)) {
      autoFillAllowed = false;
      return;
    }
    seenBackfillCursors.add(before);
    if (automatic) autoFillPagesRemaining -= 1;
    const previousHeight = currentViewport?.scrollHeight ?? 0;
    const previousTop = currentViewport?.scrollTop ?? 0;
    loadingOlder = true;
    olderError = '';
    const controller = new AbortController();
    backfillController?.abort();
    backfillController = controller;
    try {
      const response: TimelineBackfillResponse = await api.timeline(currentScope, {
        before,
        signal: controller.signal,
      });
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || currentStore !== store
      ) return;
      if (!currentStore.applyBackfill(response)) {
        autoFillAllowed = false;
        return;
      }
      hasOlder = currentStore.snapshot.hasMoreBefore;
      const nextBefore = currentStore.snapshot.beforeCursor;
      if (hasOlder && (nextBefore === null || nextBefore === before || seenBackfillCursors.has(nextBefore))) {
        autoFillAllowed = false;
      }
      // Wait for the prepended rows to be committed, then preserve the
      // user's distance from the top of the existing content.
      await tick();
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || currentStore !== store
        || currentViewport !== viewport
      ) return;
      restoreScrollAnchor(previousHeight, previousTop);
    } catch (caught) {
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || currentStore !== store
      ) return;
      if (controller.signal.aborted || isAbortError(caught)) return;
      seenBackfillCursors.delete(before);
      olderError = caught instanceof Error ? caught.message : 'Could not load older events.';
    } finally {
      if (backfillController === controller) backfillController = null;
      if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) loadingOlder = false;
    }
  }

  function markTrustedUpwardInteraction(): void {
    trustedUpwardInteraction = true;
    if (!initialPositionSettled) initialPositionSettled = true;
  }

  function onNearTop(): void {
    // Browser-native scrollbar drags and momentum scrolling do not reliably
    // produce wheel, touch, or key events. The viewport scroll event is the
    // common signal for every input path, so it must own top-edge backfill.
    if (initialPositionSettled && !restoringScrollAnchor) beginUserBackfillPass();
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    timelineViewport?.scrollProgrammaticallyToBottom();
  }

  function containWheel(event: WheelEvent): void {
    if (!initialPositionSettled) return;
    if (event.deltaY < 0) markTrustedUpwardInteraction();
    if (!viewport) return;
    const atTop = viewport.scrollTop <= 0;
    const atBottom = viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 1;
    if (event.deltaY < 0 && viewport.scrollTop <= 24) beginUserBackfillPass();
    if ((event.deltaY < 0 && atTop) || (event.deltaY > 0 && atBottom)) event.preventDefault();
  }

  function startTouch(event: TouchEvent): void {
    if (!initialPositionSettled) return;
    touchY = event.touches[0]?.clientY ?? null;
  }

  function containTouch(event: TouchEvent): void {
    if (!initialPositionSettled) return;
    if (!viewport || touchY === null) return;
    const nextY = event.touches[0]?.clientY ?? touchY;
    const deltaY = touchY - nextY;
    const atTop = viewport.scrollTop <= 0;
    const atBottom = viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 1;
    if (deltaY < 0) markTrustedUpwardInteraction();
    if (deltaY < 0 && viewport.scrollTop <= 24) beginUserBackfillPass();
    if ((deltaY < 0 && atTop) || (deltaY > 0 && atBottom)) event.preventDefault();
    touchY = nextY;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!initialPositionSettled) return;
    if (!['ArrowUp', 'PageUp', 'Home'].includes(event.key)) return;
    markTrustedUpwardInteraction();
    if (viewport && viewport.scrollTop <= 24) beginUserBackfillPass();
  }

  $effect(() => {
    abortRequests();
    const currentScope = { ...scope };
    const currentStore = new ChatV2Store();
    const scopeVersion = ++scopeGeneration;
    const requestVersion = ++requestGeneration;
    acquiredScopeKey = null;
    store = currentStore;
    loading = true;
    error = '';
    hasOlder = false;
    userScrolledUp = false;
    trustedUpwardInteraction = false;
    initialPinPending = false;
    initialLoadedPending = false;
    initialLoadedScopeKey = null;
    initialPositionSettled = false;
    resetBackfillPass();
    const unsubscribe = currentScope.missing_stream === true
      ? () => undefined
      : realtime.subscribe((event: CognisWebSocketEvent | ChatRealtimeFrame) => {
          if (
            scopeVersion !== scopeGeneration ||
            ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentScope.conversation_id)
          ) return;
          if (event.type === 'chat_v2_frame') {
            if ((event.scope?.key ?? currentScope.key) !== currentScope.key) return;
            const result = currentStore.applyRealtime(event);
            if (result.outcome === 'cursor_mismatch') void sync();
            else realtime.updateChatV2Cursor(currentScope, event.cursor_after);
            return;
          }
          if (currentScope.kind !== 'conversation') return;
          if (event.type === 'queued_messages_updated') {
            const nextClientIds = new Set(
              event.messages.map((message) => message.client_message_id).filter(Boolean)
            );
            for (const queued of currentStore.visibleQueue.messages) {
              if (queued.client_message_id && !nextClientIds.has(queued.client_message_id)) {
                recentQueuedAdmissions.set(queued.client_message_id, queued);
              }
            }
            currentStore.replaceQueue({
              messages: event.messages,
              queued_count: event.queued_count,
            });
          }
          if (event.type === 'user_message' && event.client_message_id) {
            const admitted = currentStore.visibleQueue.messages.find(
              (queued) => queued.client_message_id === event.client_message_id
            ) ?? recentQueuedAdmissions.get(event.client_message_id);
            currentStore.promoteQueuedUser({
              content: event.content || admitted?.content || '',
              attachments: event.attachments ?? admitted?.attachments,
              clientMessageId: event.client_message_id,
              createdAt: admitted?.created_at ?? event.timestamp ?? undefined,
              chatMode: event.chat_mode,
            });
            recentQueuedAdmissions.delete(event.client_message_id);
          }
          if (['queued_messages_updated', 'user_message', 'turn_started', 'message_complete', 'turn_settled', 'reconnected'].includes(event.type)) {
            if (reconciliationTimer) clearTimeout(reconciliationTimer);
            reconciliationTimer = setTimeout(() => {
              reconciliationTimer = null;
              if (scopeVersion === scopeGeneration && currentStore === store) void sync();
            }, 40);
          }
        });
    void recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
    return () => {
      abortRequests();
      unsubscribe();
      if (acquiredScopeKey === currentScope.key) {
        realtime.releaseChatV2(currentScope.key);
        acquiredScopeKey = null;
      }
    };
  });

  const missingStream = $derived(store.snapshot.scope?.missing_stream === true || scope.missing_stream === true);

  $effect(() => {
    const active = effectiveRuntimeActive;
    untrack(() => onRuntimeActiveChange?.(active));
  });

  $effect(() => {
    if (!initialPinPending || loading || !viewport) return;
    initialPinPending = false;
    const pinScopeVersion = scopeGeneration;
    void tick().then(() => {
      if (!viewport || trustedUpwardInteraction) return;
      timelineViewport?.scrollProgrammaticallyToBottom();
      requestAnimationFrame(() => {
        if (pinScopeVersion === scopeGeneration && !trustedUpwardInteraction) {
          initialPositionSettled = true;
        }
      });
    });
  });

  $effect(() => {
    if (!initialLoadedPending || loading || error) return;
    const loadedScopeKey = scope.key;
    if (initialLoadedScopeKey === loadedScopeKey) {
      initialLoadedPending = false;
      return;
    }
    initialLoadedPending = false;
    initialLoadedScopeKey = loadedScopeKey;
    void tick().then(() => {
      if (
        loadedScopeKey !== scope.key
        || initialLoadedScopeKey !== loadedScopeKey
        || loading
        || error
      ) return;
      untrack(() => onInitialLoaded?.());
    });
  });

  $effect(() => {
    const currentScopeVersion = scopeGeneration;
    const currentRequestVersion = requestGeneration;
    const checkVersion = ++fillCheckGeneration;
    const canFill = (
      initialPositionSettled
      && (effectiveAutoBackfill || items.length === 0)
      && autoFillAllowed
      && !loading
      && !loadingOlder
      && !error
      && hasOlder
      && autoFillPagesRemaining > 0
    );
    if (!canFill) return;
    void tick().then(() => {
      requestAnimationFrame(() => {
        if (
          checkVersion !== fillCheckGeneration
          || currentScopeVersion !== scopeGeneration
          || currentRequestVersion !== requestGeneration
        ) return;
        if (!viewport) return;
        if (items.length > 0 && viewport.scrollHeight > viewport.clientHeight + 1) return;
        void loadOlder(true).then(() => {
          if (!userScrolledUp) timelineViewport?.scrollProgrammaticallyToBottom();
        });
      });
    });
  });
</script>

<div
  class="relative flex min-h-0 flex-1 flex-col"
  data-scope-key={store.snapshot.scopeKey ?? scope.key}
  data-cursor={store.snapshot.cursor ?? ''}
  data-has-older={hasOlder ? 'true' : 'false'}
>
  {#if missingStream}
    <div class="m-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
      This stream is not available yet.
      {#if onMissingStream}<button class="ml-2 underline" type="button" onclick={onMissingStream}>Refresh</button>{/if}
    </div>
  {/if}
  {#if !compact}
    <div class="flex items-center justify-between px-4 py-2 text-xs text-slate-400" data-testid="scoped-timeline-status-row">
      <span>{store.snapshot.syncStatus === 'gapped' ? 'Reconnecting…' : ''}</span>
      <Button size="sm" variant="ghost" aria-label="Refresh timeline" onclick={recover} disabled={loading}><RefreshCw class="h-3.5 w-3.5" /></Button>
    </div>
  {/if}
  {#if loadingOlder}
    <span class="sr-only" role="status" aria-live="polite">Loading earlier timeline history.</span>
  {/if}
  {#if olderError}
    <div class="absolute left-1/2 top-2 z-20 -translate-x-1/2 rounded-lg border border-rose-500/30 bg-slate-950/95 px-3 py-2 text-xs text-rose-200 shadow-lg" role="alert">
      <span>{olderError}</span>
      <Button class="ml-2" size="sm" variant="secondary" onclick={beginUserBackfillPass} disabled={loading || loadingOlder}>
        Try again
      </Button>
    </div>
  {/if}
  <TimelineViewport
    bind:this={timelineViewport}
    {items}
    {agent}
    {compact}
    {loading}
    {loadingOlder}
    {error}
    {emptyLabel}
    live={hasEditableComposer ? false : live}
    {hasStreamingItems}
    liveLabel={activityStatus === 'evaluating' ? 'Evaluator is reviewing…' : 'Following latest logs'}
    followPausedLabel="Resume live follow"
    bind:viewportElement={viewport}
    bind:contentElement={content}
    bind:userScrolledUp
    onNearTop={onNearTop}
    onWheel={containWheel}
    onTouchStart={startTouch}
    onTouchMove={containTouch}
    onTouchEnd={() => { touchY = null; }}
    onKeydown={handleKeydown}
    onJumpToBottom={jumpToBottom}
    interactionEnabled={initialPositionSettled}
    {onViewSession}
    testId="scoped-timeline-viewport"
    class={`relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-2.5 py-1.5 sm:p-4 ${items.length > 0 && !initialPositionSettled ? 'invisible' : ''}`}
    contentClass="space-y-3 [overflow-anchor:none]"
  >
    {#if error}
      <div class="py-6 text-sm text-rose-300" role="alert">
        <p>{error}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={recover} disabled={loading}>
          Try again
        </Button>
      </div>
    {:else if loading && items.length === 0}
      <div class="py-6 text-sm text-slate-400">Loading timeline…</div>
    {:else if items.length === 0 && hasOlder}
      <div class="py-6 text-sm text-slate-400" role="status">Loading timeline…</div>
    {:else if items.length === 0 && !(scope.kind === 'task_step' && stepRun)}
      <div class="py-6 text-sm text-slate-400">{emptyLabel}</div>
    {:else}
      <ChatV2TimelineList {items} {agent} {compact} preferences={effectivePreferences} {cycleStates} {onViewSession} {scope} />
    {/if}
    {#if scope.kind === 'task_step' && stepRun}
      <TaskStepOutcomeEpilogue {stepRun} />
    {/if}
  </TimelineViewport>
  {#if todos.length > 0}
    <div class="shrink-0 border-t border-slate-800/80 px-4 py-3">
      <TimelineTodoDrawer {todos} bind:open={todoDrawerOpen} />
    </div>
  {/if}
  {#if showQueuedMessages && queue.length > 0}
    <div class="shrink-0 border-t border-slate-800/80 px-4 py-2" data-testid="scoped-timeline-queue">
      <p class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Queued</p>
      {#each queue as queued (queued.queue_id)}
        <p class="mt-1 truncate text-xs text-slate-300" data-testid={`scoped-queued-message-${queued.queue_id}`}>{queued.content}</p>
      {/each}
    </div>
  {/if}
</div>
