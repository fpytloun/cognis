<script lang="ts">
  import type { TaskDeliverableFreshness } from '$lib/types/api';
  import { onDestroy, tick, untrack } from 'svelte';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import TaskStepOutcomeEpilogue from '$lib/components/tasks/TaskStepOutcomeEpilogue.svelte';
  import TimelineTodoDrawer from '$lib/components/timeline/TimelineTodoDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { chatV2Api, ChatV2ApiError, type ChatV2ApiClient } from '$lib/chat-v2/api';
  import { selectLatestTodoState } from '$lib/chat-v2/selectors';
  import { ChatV2Store } from '$lib/chat-v2/store.svelte';
  import { acquireTimeline, recoverTimeline, TimelineOwnershipError, TimelineProviderConflictError, type TimelineController } from '$lib/chat-v2/timeline-controller.svelte';
  import { isOfflineError } from '$lib/offline/cache';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';
  import type { QueueMessage, QueueMutationResponse, TimelineScope, TurnCycleState } from '$lib/chat-v2/types';
  import type { Agent, BackgroundWorkItem, CognisWebSocketEvent, StepRun, UserPreferences } from '$lib/types/api';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import { userPreferences } from '$lib/stores/userPreferences';
  import { wsClient } from '$lib/ws/client';
  import type {
    ChatRealtimeFrame,
    SendMessageV2Response,
  } from '$lib/chat-v2/types';
  import type { AttachmentRef } from '$lib/types/api';
  import type { TodoSnapshotItem } from '$lib/todos';
  import ScopedChatSearch from './ScopedChatSearch.svelte';
  import { currentCycleDelegations } from '$lib/ongoing-work';

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
    onOpenWork,
    emptyLabel = 'No events recorded yet.',
    onTodosChange,
    onOngoingWorkChange,
    onMissingStream,
    activityStatus = '',
    stepRun = null,
    deliverableFreshness,
    onRuntimeActiveChange,
    hasEditableComposer = false,
    showQueuedMessages = true,
    showTodoDrawer = true,
    showLiveIndicator = true,
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
    onOpenWork?: (category: import('$lib/chat-v2/types').WorkCategory, sessionId: string, path?: string, sourceItemId?: string) => void;
    emptyLabel?: string;
    onTodosChange?: (todos: TodoSnapshotItem[]) => void;
    onOngoingWorkChange?: (work: BackgroundWorkItem[]) => void;
    onMissingStream?: (() => void) | undefined;
    activityStatus?: string;
    stepRun?: StepRun | null;
    deliverableFreshness?: Record<string, TaskDeliverableFreshness>;
    onRuntimeActiveChange?: ((active: boolean) => void) | undefined;
    hasEditableComposer?: boolean;
    showQueuedMessages?: boolean;
    showTodoDrawer?: boolean;
    showLiveIndicator?: boolean;
    onQueueChange?: ((queue: QueueMessage[]) => void) | undefined;
    onInitialLoaded?: (() => void | Promise<void>) | undefined;
    autoBackfill?: boolean | undefined;
    api?: Pick<ChatV2ApiClient, 'snapshot' | 'sync' | 'timeline'>;
    realtime?: ScopedChatV2Realtime;
  }>();

  let store = $state.raw(new ChatV2Store());
  let search = $state<{ openSearch: (opener?: HTMLElement) => Promise<HTMLInputElement | null> } | null>(null);
  let searchQuery = $state('');
  let searchMatchedIds = $state<Set<string>>(new Set());
  let searchSelectedId = $state('');

  export function openSearch(opener?: HTMLElement): Promise<HTMLInputElement | null> {
    return search?.openSearch(opener) ?? Promise.resolve(null);
  }

  async function scrollToSearchResult(id: string): Promise<void> {
    const selectionScope = scope.key;
    await tick();
    if (scope.key !== selectionScope) return;
    const element = Array.from(viewport?.querySelectorAll<HTMLElement>('[data-message-id]') ?? [])
      .find(row => row.dataset.messageId === id);
    if (!element || !viewport) return;
    userScrolledUp = true;
    const top = viewport.scrollTop + element.getBoundingClientRect().top
      - viewport.getBoundingClientRect().top - viewport.clientHeight / 3;
    timelineViewport?.setProgrammaticScrollTop(top);
  }
  let timeline = $state.raw<TimelineController | null>(null);
  let identityRequired = $state(false);
  let ownerRecoveryRevision = $state(0);
  let recoveredTimeline: ReturnType<typeof acquireTimeline> | null = null;
  onDestroy(() => {
    recoveryController?.abort();
    recoveredTimeline?.release();
    recoveredTimeline = null;
  });
  let recoveryController: AbortController | null = null;
  let loading = $state(true);
  let error = $state('');
  let loadFailure: unknown = $state.raw(null);
  let loadingOlder = $state(false);
  let olderError = $state('');
  // Load errors stay hidden while the global connection banner applies; on
  // restore the timeline reloads (older-history errors just clear, the next
  // scroll fetches again).
  const net = useSectionConnectivity({
    hasError: () => !identityRequired && Boolean(error || olderError),
    retry: () => {
      if (identityRequired) return;
      olderError = '';
      if (error) return recover();
    },
  });
  const waitingOffline = $derived(Boolean(error && net.hideErrors && !identityRequired
    && !(loadFailure instanceof ChatV2ApiError && loadFailure.code.startsWith('event_store_'))));
  const hasOlder = $derived(store.snapshot.hasMoreBefore);
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
  let autoFillAllowed = $state(true);
  let restoringScrollAnchor = false;
  let initialPositionSettled = $state(false);
  let reconciliationTimer: ReturnType<typeof setTimeout> | null = null;
  let recentQueuedAdmissions = new Map<string, QueueMessage>();
  let viewSaveTimer: ReturnType<typeof setTimeout> | null = null;
  const VIEW_SAVE_DEBOUNCE_MS = 1_500;
  // The former child-session log loaded up to 1,000 raw events at bootstrap.
  // A scoped snapshot contains 200 events and backfill pages contain 100, so
  // eight automatic pages preserve that initial history budget.
  const AUTO_FILL_PAGE_BUDGET = 8;
  const SNAPSHOT_YIELD_WATCHDOG_MS = 100;
  // Bounded so a stalled/slow snapshot surfaces a recoverable error quickly
  // instead of leaving the child view stuck on "Loading timeline…"
  // indefinitely (was 30s).
  let autoFillPagesRemaining = $state(AUTO_FILL_PAGE_BUDGET);
  let seenBackfillCursors = new Set<string>();
  let acquiredScopeKey: string | null = null;
  let snapshotController: AbortController | null = null;
  let backfillController: AbortController | null = null;
  let items = $derived(store.visibleItems);
  const effectivePreferences = $derived(preferences ?? $userPreferences ?? DEFAULT_USER_PREFERENCES);
  const cycleStates = $derived<TurnCycleState[]>(store.cycleStates);
  const todos = $derived(selectLatestTodoState(items));
  const currentCycleWork = $derived(currentCycleDelegations(
    items,
    cycleStates,
    store.snapshot.runtime?.active_turn?.turn_id,
    scope.conversation_id ?? '',
  ));
  const queue = $derived(store.visibleQueue.messages);
  const effectiveAutoBackfill = $derived(
    autoBackfill ?? (!hasEditableComposer || store.snapshot.historyWindowReduced === true)
  );
  $effect(() => {
    const nextTodos = todos;
    untrack(() => onTodosChange?.(nextTodos));
  });
  $effect(() => {
    const nextWork = currentCycleWork;
    untrack(() => onOngoingWorkChange?.(nextWork));
  });
  $effect(() => {
    const nextQueue = queue;
    untrack(() => onQueueChange?.(nextQueue));
  });
  const displayedFreshness = $derived({ ...stepRun?.deliverable_freshness, ...deliverableFreshness });
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

  function abortRequests(): void {
    recoveryController?.abort();
    snapshotController?.abort();
    backfillController?.abort();
    snapshotController = null;
    backfillController = null;
    if (reconciliationTimer) clearTimeout(reconciliationTimer);
    reconciliationTimer = null;
    recentQueuedAdmissions = new Map();
  }

  // Cached copy of this timeline (memory + device), shared with the main chat
  // page for conversation scopes. Saved settled, after loads and changes.
  function persistView(currentScope: TimelineScope, currentStore: ChatV2Store): void {
    if (!currentStore.snapshot.cursor) return;
    if (timeline?.store === currentStore) {
      timeline.persist();
      void timeline.reconcileAdmissions();
      void timeline.reconcileCompactions();
    }
  }

  function scheduleViewSave(currentScope: TimelineScope, currentStore: ChatV2Store): void {
    if (viewSaveTimer) clearTimeout(viewSaveTimer);
    viewSaveTimer = setTimeout(() => {
      viewSaveTimer = null;
      if (currentStore === store) persistView(currentScope, currentStore);
    }, VIEW_SAVE_DEBOUNCE_MS);
  }

  /**
   * Open a scope: paint the cached copy right away and bring it up to date
   * with a cursor sync (which falls back to a snapshot on reset or cursor
   * mismatch). Without a cached copy, load a snapshot as before.
   */
  async function loadInitial(
    currentScope: TimelineScope,
    currentStore: ChatV2Store,
    scopeVersion: number,
    requestVersion: number,
  ): Promise<void> {
    await timeline?.hydrate();
    if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration || currentStore !== store) return;
    const cached = currentStore.snapshot;
    if (!cached.cursor) {
      await recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
      return;
    }
    loading = false;
    initialPinPending = true;
    realtime.acquireChatV2(currentScope, cached.cursor);
    acquiredScopeKey = currentScope.key;
    // Let cached rows complete their initial positioning before a slow catch-up
    // sets loading again; otherwise the entire viewport stays invisible.
    await tick();
    if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration || currentStore !== store) return;
    await sync();
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
    requestVersion: number,
    forceSnapshot = false,
    invalidate = false,
  ): Promise<void> {
    if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
    loading = true;
    error = '';
    loadFailure = null;
    const controller = new AbortController();
    const previousController = snapshotController;
    snapshotController = controller;
    try {
      const pending = timeline!.load({ signal: controller.signal, forceSnapshot, invalidate });
      // Join before releasing our previous interest so the shared flight does
      // not transiently lose its last consumer and abort its transport.
      previousController?.abort();
      const snapshot = await pending;
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || controller.signal.aborted
        || currentStore !== store
      ) return;
      // Give the browser a chance to paint and process queued Back, Close, or
      // Inspector input before synchronous snapshot projection.
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || controller.signal.aborted
        || currentStore !== store
      ) return;
        if (initialLoadedScopeKey !== currentScope.key || forceSnapshot) {
          if (!initialPositionSettled || forceSnapshot) initialPinPending = true;
          initialLoadedPending = true;
       }
       scheduleViewSave(currentScope, currentStore);
        const resolvedScope = currentStore.snapshot.scope ?? snapshot?.scope ?? currentScope;
       if (resolvedScope.missing_stream !== true) {
          if (acquiredScopeKey === resolvedScope.key) {
             realtime.updateChatV2Cursor(resolvedScope, currentStore.snapshot.cursor!);
          } else {
             realtime.acquireChatV2(resolvedScope, currentStore.snapshot.cursor!);
            acquiredScopeKey = resolvedScope.key;
          }
       }
    } catch (caught) {
       if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
      if (controller.signal.aborted || isAbortError(caught)) return;
      // Offline with a cached copy on screen: keep it readable; the reconnect
      // resync brings it up to date.
      if (currentStore.visibleItems.length > 0 && isOfflineError(caught)) {
        if (initialLoadedScopeKey !== currentScope.key) initialLoadedPending = true;
        return;
      }
      error = caught instanceof Error ? caught.message : 'Could not load timeline.';
      loadFailure = caught;
    } finally {
       if (snapshotController === controller) {
         snapshotController = null;
         if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) loading = false;
       }
    }
  }

  async function recover(): Promise<void> {
    if (loadFailure instanceof TimelineProviderConflictError) {
      ownerRecoveryRevision += 1;
      return;
    }
    if (timeline && !timeline.current) {
      const version = scopeGeneration;
      const currentScope = { ...scope };
      const controller = new AbortController();
      recoveryController?.abort();
      recoveryController = controller;
      loading = true;
      try {
        const recovered = await recoverTimeline(currentScope, api, controller.signal, timeline.owner);
        if (controller.signal.aborted || version !== scopeGeneration) {
          recovered.release();
          return;
        }
        recoveredTimeline = recovered;
        ownerRecoveryRevision += 1;
      } catch (caught) {
        if (controller.signal.aborted || version !== scopeGeneration) return;
        loadFailure = caught;
        error = caught instanceof Error ? caught.message : new TimelineOwnershipError().message;
        loading = false;
      } finally {
        if (recoveryController === controller) recoveryController = null;
      }
      return;
    }
    abortRequests();
    resetBackfillPass();
    const requestVersion = ++requestGeneration;
    await recoverFor({ ...scope }, store, scopeGeneration, requestVersion, true);
  }

  async function sync(invalidate = false): Promise<void> {
    await recoverFor({ ...scope }, store, scopeGeneration, requestGeneration, false, invalidate);
  }

  export function stageOptimisticMessage(input: {
    scopeKey: string;
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
    follow?: boolean;
    chatMode?: 'default' | 'plan' | 'build';
  }): boolean {
    if (input.scopeKey !== scope.key) return false;
    if (input.follow) userScrolledUp = false;
    const generation = scopeGeneration;
    store.addOptimisticUser({
      content: input.content,
      attachments: input.attachments,
      clientMessageId: input.clientMessageId,
      chatMode: input.chatMode
    });
    if (input.follow) {
      void tick().then(() => {
        if (generation === scopeGeneration && !userScrolledUp) {
          timelineViewport?.scrollProgrammaticallyToBottom();
        }
      });
    }
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

  /** Whether a message with this client id is already shown in `scopeKey`. */
  export function hasClientMessage(input: { scopeKey: string; clientMessageId: string }): boolean {
    if (input.scopeKey !== scope.key) return false;
    return store.visibleItems.some(
      (item) => item.kind === 'message' && item.client_message_id === input.clientMessageId
    );
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
    previousTop: number,
    rowKey: string | null,
    rowOffset: number,
  ): void {
    if (!viewport) return;
    restoringScrollAnchor = true;
    try {
      const row = Array.from(viewport.querySelectorAll<HTMLElement>('[data-timeline-row-key]'))
        .find(node => node.dataset.timelineRowKey === rowKey);
      const top = row
        ? viewport.scrollTop + row.getBoundingClientRect().top
          - viewport.getBoundingClientRect().top - rowOffset
        : previousTop + viewport.scrollHeight - previousHeight;
      timelineViewport?.setProgrammaticScrollTop(
        top,
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
    seenBackfillCursors = new Set();
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
    loadingOlder = true;
    olderError = '';
    const controller = new AbortController();
    backfillController?.abort();
    backfillController = controller;
    try {
      const admitted = automatic
        ? await timeline!.older(before, controller.signal)
        : await timeline!.olderVisible(before, controller.signal);
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || currentStore !== store
      ) return;
      // Capture the current reading position, not the position when the request
      // began: the user may have scrolled while the response was in flight.
      if (!admitted) {
        autoFillAllowed = false;
        return;
      }
      const nextBefore = currentStore.snapshot.beforeCursor;
      if (hasOlder && (nextBefore === null || nextBefore === before || seenBackfillCursors.has(nextBefore))) {
        autoFillAllowed = false;
      }
    } catch (caught) {
      if (
        scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || currentStore !== store
      ) return;
      if (controller.signal.aborted || isAbortError(caught)) return;
      seenBackfillCursors.delete(before);
      autoFillAllowed = false;
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

  const acquisitionIdentity = $derived(JSON.stringify([
    scope.key, scope.kind, scope.conversation_id ?? null, scope.session_id ?? null,
    scope.task_id ?? null, scope.step_run_id ?? null, scope.parent_session_id ?? null,
    scope.missing_stream === true,
  ]));
  const acquisitionApi = $derived(api);
  const acquisitionRealtime = $derived(realtime);

  $effect(() => {
    void ownerRecoveryRevision;
    void acquisitionIdentity;
    void acquisitionApi;
    void acquisitionRealtime;
    // Parent window publication may replace an equivalent scope object.
    // Only canonical identity or explicit owner recovery can reset this view.
    return untrack(() => {
    abortRequests();
    const currentScope = { ...scope };
    let acquired: ReturnType<typeof acquireTimeline>;
    try {
      acquired = recoveredTimeline ?? acquireTimeline(currentScope, api);
    } catch (caught) {
      if (!(caught instanceof TimelineProviderConflictError)) throw caught;
      timeline = null;
      store = new ChatV2Store();
      identityRequired = true;
      loading = false;
      loadingOlder = false;
      initialPinPending = false;
      initialLoadedPending = false;
      loadFailure = caught;
      error = caught.message;
      return;
    }
    recoveredTimeline = null;
    timeline = acquired.controller;
    const currentStore = timeline.store;
    const scopeVersion = ++scopeGeneration;
    const requestVersion = ++requestGeneration;
    acquiredScopeKey = null;
    store = currentStore;
    loading = true;
    error = '';
    loadFailure = null;
    identityRequired = false;
    userScrolledUp = false;
    trustedUpwardInteraction = false;
    initialPinPending = false;
    initialLoadedPending = false;
    initialLoadedScopeKey = null;
    initialPositionSettled = false;
    resetBackfillPass();
    let membershipRevision = 0;
    let attached = true;
    const stopMembership = currentStore.observeMembership(() => {
      if (!attached || !acquired.controller.current || currentStore !== store || !viewport
        || !initialPositionSettled || (!userScrolledUp && !loadingOlder)) return;
      const revision = ++membershipRevision;
      const currentViewport = viewport;
      const previousHeight = viewport.scrollHeight;
      const previousTop = viewport.scrollTop;
      const viewportTop = viewport.getBoundingClientRect().top;
      const row = Array.from(viewport.querySelectorAll<HTMLElement>('[data-timeline-row-key]'))
        .find(node => node.getBoundingClientRect().bottom > viewportTop);
      const rowKey = row?.dataset.timelineRowKey ?? null;
      const rowOffset = row ? row.getBoundingClientRect().top - viewportTop : 0;
      return () => {
        void tick().then(() => {
          if (!attached || !acquired.controller.current || revision !== membershipRevision
            || scopeVersion !== scopeGeneration || currentStore !== store || currentViewport !== viewport) return;
          restoreScrollAnchor(previousHeight, previousTop, rowKey, rowOffset);
        });
      };
    });
    const unsubscribe = currentScope.missing_stream === true
      ? () => undefined
      : realtime.subscribe((event: CognisWebSocketEvent | ChatRealtimeFrame) => {
          if (
            !acquired.controller.current ||
            scopeVersion !== scopeGeneration ||
            ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentScope.conversation_id)
          ) return;
          if (event.type === 'chat_v2_frame') {
            if ((event.scope?.key ?? currentScope.key) !== currentScope.key) return;
            const result = currentStore.applyRealtime(event);
            if (result.outcome === 'cursor_mismatch') void sync(true);
            else {
              realtime.updateChatV2Cursor(currentScope, event.cursor_after);
              scheduleViewSave(currentScope, currentStore);
            }
            return;
          }
          if (event.type === 'scope_invalidated') {
            if (acquired.controller.admitInvalidation(event) && !loading) void sync();
            return;
          }
          if (event.type === 'reconnected') {
            // Socket re-authenticated: catch up canonical history missed while
            // disconnected for every scope kind. Secondary (compact) views are
            // spread out so a reconnect does not burst REST syncs.
            if (reconciliationTimer) clearTimeout(reconciliationTimer);
            const delayMs = compact ? 150 + Math.round(Math.random() * 850) : 40;
            reconciliationTimer = setTimeout(() => {
              reconciliationTimer = null;
              if (scopeVersion === scopeGeneration && currentStore === store) void sync(true);
            }, delayMs);
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
              if (scopeVersion === scopeGeneration && currentStore === store) void sync(true);
            }, 40);
          }
        });
    let detached = false;
    const detach = () => {
      if (detached) return;
      detached = true;
      abortRequests();
      if (viewSaveTimer) clearTimeout(viewSaveTimer);
      viewSaveTimer = null;
      attached = false;
      stopMembership();
      unsubscribe();
      if (acquiredScopeKey === currentScope.key) {
        realtime.releaseChatV2(currentScope.key);
        acquiredScopeKey = null;
      }
      acquired.release();
    };
    const stopOwnership = acquired.controller.onInvalidated(caught => {
      if (scopeVersion !== scopeGeneration) return;
      ++requestGeneration;
      identityRequired = true;
      loadFailure = caught;
      error = caught.message;
      loading = false;
      loadingOlder = false;
      initialPinPending = false;
      initialLoadedPending = false;
      initialLoadedScopeKey = null;
      store = new ChatV2Store();
      detach();
    });
    void untrack(() => loadInitial(currentScope, currentStore, scopeVersion, requestVersion)).catch((caught) => {
      if (scopeVersion !== scopeGeneration || detached) return;
      // A current view must settle even when an upstream operation aborts.
      // Only obsolete generations and explicit teardown are silently ignored.
      loadFailure = caught;
      error = caught instanceof Error ? caught.message : 'Could not load timeline.';
      loading = false;
    });
    return () => {
      // Invalidate in-flight loads (cache read, snapshot) so none of them can
      // acquire a realtime subscription after this cleanup released it.
      scopeGeneration += 1;
      requestGeneration += 1;
      abortRequests();
      persistView(currentScope, currentStore);
      stopOwnership();
      detach();
    };
    });
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
    const pinViewport = viewport;
    void tick().then(() => {
      if (pinScopeVersion !== scopeGeneration || viewport !== pinViewport || trustedUpwardInteraction) return;
      timelineViewport?.scrollProgrammaticallyToBottom();
      requestAnimationFrame(() => {
        if (pinScopeVersion === scopeGeneration && !trustedUpwardInteraction) {
          initialPositionSettled = true;
        }
      });
      // A suspended animation clock must not keep settled content invisible.
      setTimeout(() => {
        if (pinScopeVersion === scopeGeneration) initialPositionSettled = true;
      }, SNAPSHOT_YIELD_WATCHDOG_MS);
    });
  });

  $effect(() => {
    if (!initialLoadedPending || loading || error) return;
    const loadedScopeKey = scope.key;
    if (initialLoadedScopeKey === loadedScopeKey) {
      initialLoadedPending = false;
      return;
    }
    const scopeVersion = scopeGeneration;
    const requestVersion = requestGeneration;
    void tick().then(() => {
      if (
        loadedScopeKey !== scope.key
        || scopeVersion !== scopeGeneration
        || requestVersion !== requestGeneration
        || initialLoadedScopeKey === loadedScopeKey
        || !initialLoadedPending
        || loading
        || error
      ) return;
      initialLoadedPending = false;
      initialLoadedScopeKey = loadedScopeKey;
      untrack(() => onInitialLoaded?.());
    });
  });

  $effect(() => {
    const currentScopeVersion = scopeGeneration;
    const currentRequestVersion = requestGeneration;
    const checkVersion = ++fillCheckGeneration;
    const canFill = (
      (initialPositionSettled || items.length === 0)
      && (effectiveAutoBackfill || items.length === 0)
      && autoFillAllowed
      && !loading
      && !loadingOlder
      && !error
      && !olderError
      && hasOlder
      && autoFillPagesRemaining > 0
    );
    if (!canFill) return;
    void tick().then(() => {
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
</script>

<div
  class="relative flex min-h-0 min-w-0 flex-1 flex-col"
  data-scope-key={store.snapshot.scopeKey ?? scope.key}
  data-cursor={store.snapshot.cursor ?? ''}
  data-has-older={hasOlder ? 'true' : 'false'}
  data-freshness={timeline?.reconciled ? 'current' : 'reconciling'}
>
  <ScopedChatSearch scopeKey={scope.key} {items} onSelect={scrollToSearchResult}
    bind:this={search} bind:query={searchQuery} bind:matchedIds={searchMatchedIds} bind:selectedId={searchSelectedId} />
  {#if missingStream}
    <div class="m-4 rounded-xl border border-warning-border bg-warning-subtle p-4 text-sm text-warning">
      This stream is not available yet.
      {#if onMissingStream}<button class="ml-2 underline" type="button" onclick={onMissingStream}>Refresh</button>{/if}
    </div>
  {/if}
  {#if !compact && (store.snapshot.syncStatus === 'gapped' || (store.snapshot.cursor && !timeline?.reconciled && timeline?.phase === 'error'))}
    <div class="flex items-center justify-between px-4 py-2 text-xs text-muted" data-testid="scoped-timeline-status-row">
      <span role="status">{store.snapshot.syncStatus === 'gapped'
        ? 'Reconnecting…'
        : store.snapshot.cursor && !timeline?.reconciled && timeline?.phase === 'error'
          ? 'Cached · refresh unavailable'
          : ''}</span>
      <Button size="sm" variant="ghost" onclick={recover} disabled={loading}>Retry connection</Button>
    </div>
  {/if}
  {#if loadingOlder}
    <span class="sr-only" role="status" aria-live="polite">Loading earlier timeline history.</span>
  {/if}
  {#if hasOlder}
    <div class="shrink-0 px-4 text-center">
      <Button size="sm" variant="ghost" onclick={beginUserBackfillPass} disabled={loading || loadingOlder}>
        {loadingOlder ? 'Loading earlier history…' : 'Load earlier history'}
      </Button>
    </div>
  {/if}
  {#if olderError && !net.hideErrors}
    <div class="absolute left-1/2 top-2 z-20 -translate-x-1/2 rounded-lg border border-danger-border bg-elevated/95 px-3 py-2 text-xs text-danger shadow-lg" role="alert">
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
    live={!hasEditableComposer && showLiveIndicator && live}
    {hasStreamingItems}
    liveLabel={activityStatus === 'evaluating' ? 'Evaluator is reviewing…' : 'Turn running'}
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
    class={`relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-2.5 py-1.5 sm:p-4 ${items.length > 0 && !initialPositionSettled && !error ? 'invisible' : ''}`}
    contentClass="space-y-3 [overflow-anchor:none]"
  >
    {#if waitingOffline}
      <div class="py-6 text-sm text-muted" role="status">Timeline unavailable. Waiting for connection.</div>
    {:else if error}
      <div class="py-6 text-sm text-danger" role="alert">
        <p>{error}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={recover} disabled={loading}>
          Try again
        </Button>
      </div>
    {:else if loading && items.length === 0}
      <div class="py-6 text-sm text-muted">Loading timeline…</div>
    {:else if items.length === 0 && hasOlder}
      {#if loadingOlder}
        <div class="py-6 text-sm text-muted" role="status">Loading earlier timeline history.</div>
      {:else if !autoFillAllowed || autoFillPagesRemaining <= 0 || olderError}
        <div class="py-6 text-sm text-muted" role="status">
          Earlier history could not be advanced.
          <Button size="sm" variant="secondary" onclick={beginUserBackfillPass}>Try again</Button>
        </div>
      {/if}
    {:else if items.length === 0 && !(scope.kind === 'task_step' && stepRun)}
      <div class="py-6 text-sm text-muted">{emptyLabel}</div>
    {:else}
      <ChatV2TimelineList {items} {agent} {compact} preferences={effectivePreferences} {cycleStates} runtime={store.snapshot.runtime} {onViewSession} {onOpenWork} {scope} {searchQuery} {searchMatchedIds} {searchSelectedId} deliverableFreshness={displayedFreshness} />
    {/if}
    {#if scope.kind === 'task_step' && stepRun}
      <TaskStepOutcomeEpilogue {stepRun} deliverableFreshness={displayedFreshness}
        timelineDeliverableIds={items.flatMap(item => item.kind === 'assistant_deliverable' ? [item.deliverable_id] : [])} />
    {/if}
  </TimelineViewport>
  {#if showTodoDrawer && todos.length > 0}
    <div class="shrink-0 border-t border-separator px-4 py-3">
      <TimelineTodoDrawer {todos} bind:open={todoDrawerOpen} />
    </div>
  {/if}
  {#if showQueuedMessages && queue.length > 0}
    <div class="shrink-0 border-t border-separator px-4 py-2" data-testid="scoped-timeline-queue">
      <p class="text-[10px] font-semibold uppercase tracking-wider text-quiet">Queued</p>
      {#each queue as queued (queued.queue_id)}
        <p class="mt-1 truncate text-xs text-secondary" data-testid={`scoped-queued-message-${queued.queue_id}`}>{queued.content}</p>
      {/each}
    </div>
  {/if}
</div>
