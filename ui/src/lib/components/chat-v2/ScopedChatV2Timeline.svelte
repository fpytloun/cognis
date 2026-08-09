<script lang="ts">
  import { tick } from 'svelte';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import TaskStepOutcomeEpilogue from '$lib/components/tasks/TaskStepOutcomeEpilogue.svelte';
  import TimelineTodoDrawer from '$lib/components/timeline/TimelineTodoDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { chatV2Api, type ChatV2ApiClient } from '$lib/chat-v2/api';
  import { selectLatestTodoState } from '$lib/chat-v2/selectors';
  import { ChatV2Store } from '$lib/chat-v2/store.svelte';
  import type { TimelineScope, TurnCycleState } from '$lib/chat-v2/types';
  import type { Agent, StepRun, UserPreferences } from '$lib/types/api';
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
    subscribe: (listener: (event: ChatRealtimeFrame) => void) => () => void;
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
    api?: Pick<ChatV2ApiClient, 'snapshot' | 'sync' | 'timeline'>;
    realtime?: ScopedChatV2Realtime;
  }>();

  let store = $state.raw(new ChatV2Store());
  let loading = $state(true);
  let error = $state('');
  let loadingOlder = $state(false);
  let hasOlder = $state(false);
  let viewport = $state<HTMLDivElement | null>(null);
  let content = $state<HTMLDivElement | null>(null);
  let todoDrawerOpen = $state(false);
  let trustedInteraction = false;
  let initialPinPending = false;
  let touchY: number | null = null;
  let scopeGeneration = 0;
  let requestGeneration = 0;
  let acquiredScopeKey: string | null = null;
  let items = $derived(store.visibleItems);
  const effectivePreferences = $derived(preferences ?? $userPreferences ?? DEFAULT_USER_PREFERENCES);
  const cycleStates = $derived<TurnCycleState[]>(store.cycleStates);
  const todos = $derived(selectLatestTodoState(items));
  $effect(() => {
    onTodosChange?.(todos);
  });
  const runtimeActive = $derived(store.snapshot.runtime?.has_active_turn === true);
  const stepRunLive = $derived(stepRun?.status === 'running' || stepRun?.status === 'evaluating');
  const effectiveRuntimeActive = $derived(stepRun ? stepRunLive : runtimeActive);
  const hasStreamingItems = $derived(
    effectiveRuntimeActive && items.some((item) => item.stable === false || item.status === 'running')
  );
  const live = $derived(stepRun ? stepRunLive : runtimeActive || activityStatus === 'running' || activityStatus === 'evaluating');

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
    try {
      const snapshot = await api.snapshot(currentScope);
      if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
      if (!currentStore.replaceFromSnapshotIfUnchanged(snapshot, refreshWatermark)) {
        // A live frame won the race. Preserve its canonical/runtime state and
        // reconcile only the public subscription cursor.
        if (currentStore.snapshot.cursor && snapshot.scope?.missing_stream !== true) {
          realtime.updateChatV2Cursor(currentScope, currentStore.snapshot.cursor);
        }
        return;
      }
       hasOlder = currentStore.snapshot.hasMoreBefore;
       initialPinPending = true;
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
      error = caught instanceof Error ? caught.message : 'Could not load timeline.';
    } finally {
       if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) loading = false;
    }
  }

  async function recover(): Promise<void> {
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
    try {
      const seenCursors = new Set<string>();
      while (cursor && !seenCursors.has(cursor)) {
        seenCursors.add(cursor);
         const response: ChatSyncResponse = await api.sync(currentScope, cursor);
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
    } catch {
       if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) {
         await recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
       }
    }
  }

  export function stageOptimisticMessage(input: {
    scopeKey: string;
    content: string;
    attachments?: AttachmentRef[];
    clientMessageId: string;
  }): boolean {
    if (input.scopeKey !== scope.key) return false;
    store.addOptimisticUser({
      content: input.content,
      attachments: input.attachments,
      clientMessageId: input.clientMessageId
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

  async function loadOlder(): Promise<void> {
    const currentScope = { ...scope };
    const currentStore = store;
    const scopeVersion = scopeGeneration;
    const requestVersion = requestGeneration;
    if (loadingOlder || !currentStore.snapshot.hasMoreBefore || currentStore.snapshot.beforeCursor === null || items.length === 0) return;
    const before = currentStore.snapshot.beforeCursor;
    const previousHeight = viewport?.scrollHeight ?? 0;
    const previousTop = viewport?.scrollTop ?? 0;
    loadingOlder = true;
    try {
       const response: TimelineBackfillResponse = await api.timeline(currentScope, { before });
       if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
       currentStore.applyBackfill(response);
       hasOlder = currentStore.snapshot.hasMoreBefore;
        // Wait for the prepended rows to be committed, then preserve the
        // user's distance from the top of the existing content.
        await tick();
        if (viewport) viewport.scrollTop = previousTop + viewport.scrollHeight - previousHeight;
    } catch (caught) {
       if (scopeVersion !== scopeGeneration || requestVersion !== requestGeneration) return;
      error = caught instanceof Error ? caught.message : 'Could not load older events.';
    } finally {
       if (scopeVersion === scopeGeneration && requestVersion === requestGeneration) loadingOlder = false;
    }
  }

  function markTrustedInteraction(): void {
    trustedInteraction = true;
  }

  function onNearTop(): void {
    if (trustedInteraction) void loadOlder();
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }

  function containWheel(event: WheelEvent): void {
    markTrustedInteraction();
    if (!viewport) return;
    const atTop = viewport.scrollTop <= 0;
    const atBottom = viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 1;
    if ((event.deltaY < 0 && atTop) || (event.deltaY > 0 && atBottom)) event.preventDefault();
  }

  function startTouch(event: TouchEvent): void {
    markTrustedInteraction();
    touchY = event.touches[0]?.clientY ?? null;
  }

  function containTouch(event: TouchEvent): void {
    if (!viewport || touchY === null) return;
    const nextY = event.touches[0]?.clientY ?? touchY;
    const deltaY = touchY - nextY;
    const atTop = viewport.scrollTop <= 0;
    const atBottom = viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 1;
    if ((deltaY < 0 && atTop) || (deltaY > 0 && atBottom)) event.preventDefault();
    touchY = nextY;
  }

  $effect(() => {
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
    trustedInteraction = false;
    initialPinPending = false;
    const unsubscribe = currentScope.missing_stream === true
      ? () => undefined
      : realtime.subscribe((event: ChatRealtimeFrame) => {
          if (
            scopeVersion !== scopeGeneration ||
            event.type !== 'chat_v2_frame' ||
            (event.scope?.key ?? currentScope.key) !== currentScope.key
          ) return;
          const result = currentStore.applyRealtime(event);
          if (result.outcome === 'cursor_mismatch') void sync();
          else realtime.updateChatV2Cursor(currentScope, event.cursor_after);
        });
    void recoverFor(currentScope, currentStore, scopeVersion, requestVersion);
    return () => {
      unsubscribe();
      if (acquiredScopeKey === currentScope.key) {
        realtime.releaseChatV2(currentScope.key);
        acquiredScopeKey = null;
      }
    };
  });

  const missingStream = $derived(store.snapshot.scope?.missing_stream === true || scope.missing_stream === true);

  $effect(() => {
    onRuntimeActiveChange?.(effectiveRuntimeActive);
  });

  $effect(() => {
    if (!initialPinPending || loading || !viewport) return;
    initialPinPending = false;
    void tick().then(() => {
      requestAnimationFrame(() => {
        if (!viewport || trustedInteraction) return;
        userScrolledUp = false;
        viewport.scrollTop = viewport.scrollHeight;
      });
    });
  });
</script>

<div
  class="flex min-h-0 flex-1 flex-col"
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
  <div class="flex items-center justify-between px-4 py-2 text-xs text-slate-400">
    <span>{store.snapshot.syncStatus === 'gapped' ? 'Reconnecting…' : ''}</span>
    <div class="flex gap-2">
      {#if hasOlder}
        <Button size="sm" variant="ghost" onclick={loadOlder} disabled={loadingOlder}>
          <ArrowUp class="mr-1 h-3.5 w-3.5" /> {loadingOlder ? 'Loading…' : 'Load older'}
        </Button>
      {/if}
       <Button size="sm" variant="ghost" aria-label="Refresh timeline" onclick={recover} disabled={loading}><RefreshCw class="h-3.5 w-3.5" /></Button>
    </div>
  </div>
  <TimelineViewport
    {items}
    {agent}
    {compact}
    {loading}
    {loadingOlder}
    {error}
    {emptyLabel}
    {live}
    {hasStreamingItems}
    liveLabel={activityStatus === 'evaluating' ? 'Evaluator is reviewing…' : 'Following latest logs'}
    followPausedLabel="Resume live follow"
    bind:viewportElement={viewport}
    bind:contentElement={content}
    bind:userScrolledUp
    onNearTop={onNearTop}
    onPointerDown={markTrustedInteraction}
    onWheel={containWheel}
    onTouchStart={startTouch}
    onTouchMove={containTouch}
    onTouchEnd={() => { touchY = null; }}
    onKeydown={markTrustedInteraction}
    onJumpToBottom={jumpToBottom}
    {onViewSession}
    testId="scoped-timeline-viewport"
    class="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-2.5 py-1.5 sm:p-4"
    contentClass="space-y-3 [overflow-anchor:auto]"
  >
    {#if error}
      <div class="py-6 text-sm text-rose-300">{error}</div>
    {:else if loading && items.length === 0}
      <div class="py-6 text-sm text-slate-400">Loading timeline…</div>
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
</div>
