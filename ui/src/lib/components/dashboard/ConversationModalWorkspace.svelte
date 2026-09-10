<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import CompactConversationChat from '$lib/components/chat-v2/CompactConversationChat.svelte';
  import SharedInspectorTabs from '$lib/components/inspector/SharedInspectorTabs.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import {
    conversationTimelineScope,
    sessionTimelineScope,
    type CommandV2Response,
    type WorkstreamRef,
  } from '$lib/chat-v2/types';
  import {
    activeRootSessionLineageIds,
  } from '$lib/ongoing-work';
  import {
    FocusedSessionDiagnosticsController,
    type FocusedSessionDiagnosticsState,
  } from '$lib/focusedSessionDiagnostics';
  import { chatV2Api } from '$lib/chat-v2/api';
  import { api } from '$lib/api/client';
  import { wsClient } from '$lib/ws/client';
  import {
    INSPECTOR_MAX_WIDTH,
    INSPECTOR_MIN_WIDTH,
  } from '$lib/stores/conversationInfo.svelte';
  import type { Agent, Session } from '$lib/types/api';

  let {
    conversationId,
    sessionId = '',
    agent = null,
    agents = [],
    onInitialLoaded,
    inspectorOpen = true,
    inspectorControlInHeader = false,
    onInspectorStateChange,
  } = $props<{
    conversationId: string;
    sessionId?: string;
    agent?: Agent | null;
    agents?: Agent[];
    onInitialLoaded?: (conversationId: string) => void | Promise<void>;
    inspectorOpen?: boolean;
    inspectorControlInHeader?: boolean;
    onInspectorStateChange?: (open: boolean) => void;
  }>();

  const NARROW_WINDOW_WIDTH = 820;
  const DEFAULT_INSPECTOR_WIDTH = 352;
  const LEGACY_DEFAULT_INSPECTOR_WIDTH = 400;
  const MIN_CHAT_WIDTH = 480;
  const INSPECTOR_STORAGE_KEY = 'cognis.dashboardConversationInspector.width.v1';
  type SelectedSession = {
    sessionId: string;
    conversationId: string;
    title: string;
    node: WorkstreamRef | null;
  };

  let workspaceElement = $state<HTMLDivElement | null>(null);
  let narrowToggleContainer = $state<HTMLSpanElement | null>(null);
  let narrowInspectorElement = $state<HTMLElement | null>(null);
  let narrowLayout = $state(false);
  let containerWidth = $state(0);
  let inspectorWidth = $state(DEFAULT_INSPECTOR_WIDTH);
  let inspectorVisible = $state(true);
  let effectiveConversationId = $state('');
  let effectiveSessionId = $state('');
  let selectedSession = $state<SelectedSession | null>(null);
  let rootSessions = $state<Session[]>([]);
  let rootSessionsConversationId = $state('');
  let rootSessionsRequest = 0;
  let focusedDiagnostics = $state<FocusedSessionDiagnosticsState>({
    sessionId: null,
    contextUsage: null,
    freshness: 'unavailable',
  });
  let resizeCleanup: (() => void) | null = null;
  let resizeFrame: number | null = null;
  const scope = $derived(selectedSession
    ? sessionTimelineScope(selectedSession.sessionId, selectedSession.conversationId)
    : conversationTimelineScope(effectiveConversationId));
  const displayedSessionId = $derived(selectedSession?.sessionId ?? effectiveSessionId);
  const controllerSessionIds = $derived(selectedSession
    ? [
        displayedSessionId,
        ...(selectedSession.node?.backing_session_ids ?? []),
      ].filter(Boolean)
    : rootSessionsConversationId === effectiveConversationId
      ? [...activeRootSessionLineageIds(rootSessions, effectiveSessionId)]
      : [effectiveSessionId].filter(Boolean));
  const diagnosticsScope = $derived(displayedSessionId
    ? sessionTimelineScope(
        displayedSessionId,
        selectedSession?.conversationId ?? effectiveConversationId,
      )
    : null);
  const inspectorIdBase = $derived(`dashboard-conversation-inspector-${effectiveConversationId}`);
  export function getInspectorState(): { open: boolean; controlsId: string } {
    return { open: inspectorVisible, controlsId: inspectorIdBase };
  }

  const focusedDiagnosticsController = new FocusedSessionDiagnosticsController(
    wsClient,
    (selectedScope, options) => chatV2Api.snapshot(selectedScope, options),
    (state) => { focusedDiagnostics = state; },
  );

  $effect(() => {
    effectiveConversationId = conversationId;
    effectiveSessionId = sessionId;
    selectedSession = null;
  });

  $effect(() => {
    const currentConversationId = effectiveConversationId;
    if (!currentConversationId) return;
    const request = ++rootSessionsRequest;
    void api.conversations.sessions(currentConversationId, {
      rootOnly: true,
      limit: 200,
      order: 'desc',
    }).then((sessions) => {
      if (request !== rootSessionsRequest || currentConversationId !== effectiveConversationId) return;
      rootSessions = sessions;
      rootSessionsConversationId = currentConversationId;
    }).catch(() => {
      if (request !== rootSessionsRequest || currentConversationId !== effectiveConversationId) return;
      rootSessions = [];
      rootSessionsConversationId = '';
    });
  });

  $effect(() => {
    focusedDiagnosticsController.select(
      diagnosticsScope,
      selectedSession?.node ?? null,
      inspectorVisible,
    );
  });

  function handleCommandResponse(response: CommandV2Response): void {
    if (typeof response.data?.conversation_id === 'string' && response.data.conversation_id) {
      effectiveConversationId = response.data.conversation_id;
    }
    if (typeof response.data?.session_id === 'string') effectiveSessionId = response.data.session_id;
  }

  function viewSession(selectedSessionId: string, node?: WorkstreamRef): void {
    if (selectedSessionId === effectiveSessionId || (node && node.key === node.root_key)) {
      selectedSession = null;
      return;
    }
    selectedSession = {
      sessionId: selectedSessionId,
      conversationId: node?.conversation_id ?? effectiveConversationId,
      title: node?.title ?? `Session ${selectedSessionId.slice(0, 12)}`,
      node: node ?? null,
    };
  }

  function clampInspectorWidth(width: number): number {
    const containerMaximum = containerWidth > 0
      ? Math.max(INSPECTOR_MIN_WIDTH, containerWidth - MIN_CHAT_WIDTH)
      : INSPECTOR_MAX_WIDTH;
    return Math.round(Math.max(
      INSPECTOR_MIN_WIDTH,
      Math.min(INSPECTOR_MAX_WIDTH, containerMaximum, width),
    ));
  }

  function setContainerWidth(width: number): void {
    containerWidth = width;
    const nextNarrowLayout = width < NARROW_WINDOW_WIDTH;
    if (nextNarrowLayout && !narrowLayout) setInspectorOpen(false);
    narrowLayout = nextNarrowLayout;
    if (!nextNarrowLayout) inspectorWidth = clampInspectorWidth(inspectorWidth);
  }

  function focusableInspectorElements(): HTMLElement[] {
    if (!narrowInspectorElement) return [];
    return [...narrowInspectorElement.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((element) => !element.hasAttribute('hidden'));
  }

  function setInspectorOpen(open: boolean): void {
    if (inspectorVisible === open) return;
    inspectorVisible = open;
    onInspectorStateChange?.(open);
  }

  export async function toggleInspector(open?: boolean, restoreFocus = true): Promise<void> {
    const nextOpen = open ?? !inspectorVisible;
    setInspectorOpen(nextOpen);
    await tick();
    if (nextOpen && narrowLayout) {
      (focusableInspectorElements()[0] ?? narrowInspectorElement)?.focus({ preventScroll: true });
    } else if (!nextOpen && restoreFocus && !inspectorControlInHeader) {
      narrowToggleContainer?.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true });
    }
  }

  function handleNarrowInspectorKeydown(event: KeyboardEvent): void {
    if (!narrowLayout || !inspectorVisible) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      void toggleInspector(false);
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = focusableInspectorElements();
    if (focusable.length === 0) {
      event.preventDefault();
      narrowInspectorElement?.focus({ preventScroll: true });
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  export function handleEscape(): boolean {
    if (!narrowLayout || !inspectorVisible) return false;
    void toggleInspector(false, false);
    return true;
  }

  function cleanupResize(): void {
    if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
    resizeFrame = null;
    resizeCleanup?.();
    resizeCleanup = null;
  }

  function startResize(event: PointerEvent): void {
    if (event.button !== 0) return;
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startWidth = inspectorWidth;
    event.preventDefault();
    const move = (next: PointerEvent): void => {
      if (next.pointerId !== pointerId) return;
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => {
        inspectorWidth = clampInspectorWidth(startWidth + startX - next.clientX);
      });
    };
    const stop = (next: PointerEvent): void => {
      if (next.pointerId !== pointerId) return;
      const nextWidth = clampInspectorWidth(startWidth + startX - next.clientX);
      cleanupResize();
      inspectorWidth = nextWidth;
      try {
        window.localStorage.setItem(INSPECTOR_STORAGE_KEY, String(nextWidth));
      } catch {
        // Inspector width persistence is best effort.
      }
    };
    document.addEventListener('pointermove', move, true);
    document.addEventListener('pointerup', stop, true);
    document.addEventListener('pointercancel', stop, true);
    resizeCleanup = () => {
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', stop, true);
      document.removeEventListener('pointercancel', stop, true);
    };
  }

  function resizeWithKeyboard(event: KeyboardEvent): void {
    const step = event.shiftKey ? 40 : 10;
    let next = inspectorWidth;
    if (event.key === 'ArrowLeft') next += step;
    else if (event.key === 'ArrowRight') next -= step;
    else if (event.key === 'Home') next = INSPECTOR_MIN_WIDTH;
    else if (event.key === 'End') next = INSPECTOR_MAX_WIDTH;
    else return;
    event.preventDefault();
    inspectorWidth = clampInspectorWidth(next);
    try {
      window.localStorage.setItem(INSPECTOR_STORAGE_KEY, String(inspectorWidth));
    } catch {
      // Inspector width persistence is best effort.
    }
  }

  onMount(() => {
    inspectorVisible = inspectorOpen;
    try {
      const savedWidth = Number.parseFloat(window.localStorage.getItem(INSPECTOR_STORAGE_KEY) ?? '');
      if (Number.isFinite(savedWidth)) {
        inspectorWidth = savedWidth === LEGACY_DEFAULT_INSPECTOR_WIDTH
          ? DEFAULT_INSPECTOR_WIDTH
          : clampInspectorWidth(savedWidth);
      }
    } catch {
      // Keep the default width when browser storage is unavailable.
    }
    const element = workspaceElement;
    if (!element) return;
    setContainerWidth(element.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? element.getBoundingClientRect().width;
      setContainerWidth(width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  });

  onDestroy(() => {
    cleanupResize();
    focusedDiagnosticsController.dispose();
  });
</script>

<div
  bind:this={workspaceElement}
  class="flex min-h-0 flex-1 flex-col"
  data-layout={narrowLayout ? 'narrow' : 'wide'}
  data-testid="conversation-modal-workspace"
>
  {#if !inspectorControlInHeader && narrowLayout}
  <div class="flex shrink-0 items-center justify-end border-b border-slate-800 px-3 py-2" data-testid="conversation-mobile-inspector-control">
    <span bind:this={narrowToggleContainer} data-dashboard-conversation-inspector-toggle>
      <SessionDetailsButton
        open={inspectorVisible}
        ariaControls={inspectorIdBase}
        testId={`${inspectorIdBase}-mobile-toggle`}
        onclick={() => { void toggleInspector(); }}
      />
    </span>
  </div>
  {/if}
  <div class="relative flex min-h-0 flex-1">
    <div
      class="flex min-w-0 flex-1 flex-col overflow-hidden"
      data-testid="dashboard-conversation-chat"
      inert={narrowLayout && inspectorVisible}
      aria-hidden={narrowLayout && inspectorVisible}
    >
      {#if selectedSession}
        <div class="flex h-10 shrink-0 items-center gap-2 border-b border-slate-800 px-3" data-testid="dashboard-conversation-session-header">
          <button
            class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-white"
            type="button"
            aria-label="Back to parent conversation"
            onclick={() => { selectedSession = null; }}
          ><ArrowLeft class="h-4 w-4" /></button>
          <span class="min-w-0 truncate text-xs font-medium text-slate-200">{selectedSession.title}</span>
        </div>
      {/if}
      <div class="min-h-0 flex-1">
      <CompactConversationChat
        conversationId={effectiveConversationId}
        timelineScope={scope}
        {controllerSessionIds}
        {agent}
        embedded
        initialAutoTail
        onCommandResponse={handleCommandResponse}
        onInitialLoaded={() => onInitialLoaded?.(effectiveConversationId)}
        onViewSession={viewSession}
      />
      </div>
    </div>
    {#if narrowLayout && inspectorVisible}
      <button
        aria-label="Close conversation inspector"
        class="absolute inset-0 z-10 bg-slate-950/35 backdrop-blur-[1px]"
        type="button"
        onclick={() => { void toggleInspector(false); }}
      ></button>
      <div
        bind:this={narrowInspectorElement}
        id={inspectorIdBase}
        class="absolute inset-0 z-20 isolate w-full overflow-y-auto bg-slate-950 p-4"
        data-testid={`${inspectorIdBase}-mobile`}
        data-dashboard-conversation-inspector-overlay
        role="dialog"
        aria-label="Conversation inspector"
        aria-modal="false"
        tabindex="-1"
        onkeydown={handleNarrowInspectorKeydown}
      >
        <SharedInspectorTabs
          {scope}
          sessionId={displayedSessionId}
          {agents}
          initialTab="overview"
          testIdPrefix={inspectorIdBase}
          onViewSession={viewSession}
          contextUsage={focusedDiagnostics.contextUsage}
          diagnosticsFreshness={focusedDiagnostics.freshness}
        />
      </div>
    {/if}
    {#if !narrowLayout && inspectorVisible}
      <aside
        id={inspectorIdBase}
        class="relative shrink-0 overflow-y-auto border-l border-slate-800 p-4"
        style={`width:${inspectorWidth}px`}
        data-testid={`${inspectorIdBase}-desktop`}
      >
        <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
        <div
          role="separator"
          tabindex="0"
          aria-label="Resize conversation inspector"
          aria-orientation="vertical"
          aria-valuemin={INSPECTOR_MIN_WIDTH}
          aria-valuemax={INSPECTOR_MAX_WIDTH}
          aria-valuenow={inspectorWidth}
          aria-valuetext={`${inspectorWidth} pixels wide`}
          class="touch-resize-handle touch-resize-handle--left absolute -left-1 top-0 z-20 h-full w-2 cursor-col-resize touch-none focus-visible:bg-sky-400/40"
          data-testid={`${inspectorIdBase}-desktop-resizer`}
          onpointerdown={startResize}
          onkeydown={resizeWithKeyboard}
        ></div>
        <SharedInspectorTabs
          {scope}
          sessionId={displayedSessionId}
          {agents}
          initialTab="overview"
          testIdPrefix={inspectorIdBase}
          onViewSession={viewSession}
          contextUsage={focusedDiagnostics.contextUsage}
          diagnosticsFreshness={focusedDiagnostics.freshness}
        />
      </aside>
    {/if}
  </div>
</div>
