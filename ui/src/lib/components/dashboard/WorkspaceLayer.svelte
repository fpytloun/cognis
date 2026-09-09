<script lang="ts">
  import { onMount } from 'svelte';
  import { portal } from '$lib/actions/portal';
  import AgentQuickChatModal from './AgentQuickChatModal.svelte';
  import FocusedAttentionAction from '$lib/components/attention/FocusedAttentionAction.svelte';
  import DashboardEntityModal from './DashboardEntityModal.svelte';
  import WorkspaceDock from './WorkspaceDock.svelte';
  import WorkspaceWindow from './WorkspaceWindow.svelte';
  import {
    workspaceDeviceClass,
    workspaceSafeRectFromContent,
    workspaceSafeRectAboveDock,
    dispatchWorkspaceEscape,
    type WorkspaceManager,
    type WorkspaceSafeRect,
    type WorkspaceEscapeTarget,
    type WorkspaceWindowState,
  } from '$lib/dashboard/workspace';
  import type { Agent, AttentionActionDetail, Conversation } from '$lib/types/api';
  import { blockingOverlayActive } from '$lib/stores/overlays';

  let {
    manager,
    agents = [],
    conversations = [],
    onConversationInitialLoaded,
    onAgentConversationResolved,
    onCapacityCapped,
    onAttentionSettled,
    onAttentionUnavailable,
    onTaskMutationSettled,
    deviceClass,
  } = $props<{
    manager: WorkspaceManager;
    agents?: Agent[];
    conversations?: Conversation[];
    onConversationInitialLoaded?: (conversationId: string) => void | Promise<void>;
    onAgentConversationResolved?: (key: string, conversationId: string) => void;
    onCapacityCapped?: (limit: number) => void;
    onAttentionSettled?: (action: AttentionActionDetail) => void | Promise<void>;
    onAttentionUnavailable?: (actionId: string) => void | Promise<void>;
    onTaskMutationSettled?: (taskId: string, changed: boolean) => void | Promise<void>;
    deviceClass?: 'desktop' | 'tablet' | 'phone';
  }>();

  let measuredDevice = $state<'desktop' | 'tablet' | 'phone'>('desktop');
  const device = $derived(deviceClass ?? measuredDevice);
  type WorkspaceEntityRef = WorkspaceEscapeTarget & {
    toggleInspector?: (open?: boolean) => Promise<void> | undefined;
  };
  type WorkspaceWindowRef = {
    focusInspectorToggle(): void;
  };

  let entityRefs = $state<Record<string, WorkspaceEntityRef | undefined>>({});
  let windowRefs = $state<Record<string, WorkspaceWindowRef | undefined>>({});
  let inspectorStates = $state<Record<string, boolean | undefined>>({});
  let dockLeft = $state(12);
  let dockTop = $state<number | null>(null);
  let snapPreview = $state<'left' | 'right' | 'maximize' | null>(null);
  let currentSafeRect = $state<WorkspaceSafeRect>({ x: 0, y: 0, width: 0, height: 0 });
  const visibleWindows = $derived(
    $manager.filter((window: WorkspaceWindowState) => !window.minimized),
  );

  function closeWindow(key: string): void {
    entityRefs[key] = undefined;
    windowRefs[key] = undefined;
    inspectorStates[key] = undefined;
    manager.close(key);
  }

  function closeFocusedConversationInspector(): boolean {
    const key = manager.focusedTopVisibleKey();
    if (!key || inspectorStates[key] !== true) return false;
    const entity = entityRefs[key];
    if (!entity?.handleEscape()) return false;
    queueMicrotask(() => windowRefs[key]?.focusInspectorToggle());
    return true;
  }

  function inspectorOpen(key: string): boolean {
    return inspectorStates[key] ?? true;
  }

  function setInspectorOpen(key: string, open: boolean, restoreFocus = false): void {
    inspectorStates[key] = open;
    if (restoreFocus && !open) queueMicrotask(() => windowRefs[key]?.focusInspectorToggle());
  }

  function toggleInspector(key: string): void {
    const open = !inspectorOpen(key);
    setInspectorOpen(key, open);
    void entityRefs[key]?.toggleInspector?.(open);
  }

  function inspectorConversationId(windowState: WorkspaceWindowState): string | null {
    if (windowState.kind === 'conversation') return windowState.entityId;
    return windowState.resolvedConversationId ?? null;
  }

  function inspectorControlId(windowState: WorkspaceWindowState): string | undefined {
    const conversationId = inspectorConversationId(windowState);
    return conversationId ? `dashboard-conversation-inspector-${conversationId}` : undefined;
  }

  function inspectorToggleHandler(windowState: WorkspaceWindowState): (() => void) | undefined {
    return inspectorConversationId(windowState)
      ? () => toggleInspector(windowState.key)
      : undefined;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented) return;
    if (event.key !== 'Escape') return;
    if ($blockingOverlayActive || document.querySelector('[data-blocking-overlay]')) return;
    if (closeFocusedConversationInspector()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const handled = dispatchWorkspaceEscape(
      manager,
      entityRefs,
      device !== 'phone',
      $blockingOverlayActive,
    );
    if (!handled) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }

  $effect(() => {
    const mountedKeys = new Set(
      $manager.map((window: WorkspaceWindowState) => window.key),
    );
    for (const key of Object.keys(entityRefs)) {
      if (!mountedKeys.has(key)) entityRefs[key] = undefined;
    }
    for (const key of Object.keys(windowRefs)) {
      if (!mountedKeys.has(key)) windowRefs[key] = undefined;
    }
    for (const key of Object.keys(inspectorStates)) {
      if (!mountedKeys.has(key)) inspectorStates[key] = undefined;
    }
  });

  function safeRect(): WorkspaceSafeRect {
    const appContent = document.querySelector<HTMLElement>('[data-app-content="true"]');
    const bounds = appContent?.getBoundingClientRect();
    const contentStyle = appContent ? getComputedStyle(appContent) : null;
    const rootStyle = getComputedStyle(document.documentElement);
    const number = (value: string | null | undefined): number => Number.parseFloat(value ?? '') || 0;
    const shellTop = number(rootStyle.getPropertyValue('--app-shell-top-offset'));
    return workspaceSafeRectFromContent(
      bounds ?? { top: 0, right: window.innerWidth, bottom: window.innerHeight, left: 0 },
      {
        top: number(contentStyle?.paddingTop),
        right: number(contentStyle?.paddingRight),
        bottom: number(contentStyle?.paddingBottom),
        left: number(contentStyle?.paddingLeft),
      },
      shellTop,
      window.innerWidth,
    );
  }

  function syncViewport(): void {
    measuredDevice = workspaceDeviceClass(
      window.innerWidth,
      window.matchMedia?.('(any-pointer: coarse)').matches ?? false,
    );
    manager.setDevice(measuredDevice);
    const baseSafe = safeRect();
    const renderedDockTop = document.querySelector<HTMLElement>('[data-testid="workspace-dock"]')
      ?.getBoundingClientRect().top ?? dockTop;
    const safe = measuredDevice === 'phone'
      ? baseSafe
      : workspaceSafeRectAboveDock(baseSafe, renderedDockTop);
    currentSafeRect = safe;
    dockLeft = baseSafe.x;
    manager.setSafeRect(safe);
  }

  function handleDockTop(top: number | null): void {
    if (top === dockTop) return;
    dockTop = top;
    syncViewport();
  }

  onMount(() => {
    syncViewport();
    const observer = new ResizeObserver(syncViewport);
    const rootStyleObserver = new MutationObserver(syncViewport);
    const appContent = document.querySelector<HTMLElement>('[data-app-content="true"]');
    if (appContent) observer.observe(appContent);
    rootStyleObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['style'],
    });
    window.addEventListener('resize', syncViewport, { passive: true });
    return () => {
      observer.disconnect();
      rootStyleObserver.disconnect();
      window.removeEventListener('resize', syncViewport);
    };
  });
</script>

<svelte:window onkeydowncapture={handleKeydown} />

  <div
    use:portal
    class="pointer-events-none fixed inset-0 z-[70]"
    class:hidden={device === 'phone'}
    data-testid="workspace-layer"
  >
    {#if device !== 'phone' && visibleWindows.length > 0}
      <div
        aria-hidden="true"
        class="pointer-events-none fixed inset-0 bg-slate-950/10 backdrop-blur-[1px] motion-safe:transition-opacity motion-safe:duration-150"
        data-testid="workspace-veil"
      ></div>
    {/if}
    {#if device !== 'phone' && snapPreview}
      {@const previewRect = snapPreview === 'maximize'
        ? currentSafeRect
        : {
            ...currentSafeRect,
            x: snapPreview === 'right'
              ? currentSafeRect.x + currentSafeRect.width / 2
              : currentSafeRect.x,
            width: currentSafeRect.width / 2,
          }}
      <div
        aria-hidden="true"
        class="workspace-snap-preview pointer-events-none fixed rounded-2xl border-2 border-sky-300/70 bg-sky-400/15 shadow-[0_0_40px_rgba(56,189,248,0.2)]"
        data-snap-target={snapPreview}
        data-testid="workspace-snap-preview"
        style={`left:${previewRect.x}px;top:${previewRect.y}px;width:${previewRect.width}px;height:${previewRect.height}px;z-index:79`}
      ></div>
    {/if}
    {#each device !== 'phone' ? $manager : [] as workspaceWindow (workspaceWindow.key)}
      <WorkspaceWindow
        bind:this={windowRefs[workspaceWindow.key]}
        window={workspaceWindow}
        {manager}
        largeControls={device === 'tablet'}
        onSnapPreview={(target) => snapPreview = target}
        inspectorExpanded={inspectorOpen(workspaceWindow.key)}
        inspectorControlsId={inspectorControlId(workspaceWindow)}
        onToggleInspector={inspectorToggleHandler(workspaceWindow)}
      >
        {#snippet children()}
           {#if workspaceWindow.kind === 'attention'}
              <FocusedAttentionAction
                actionId={workspaceWindow.entityId}
                onUnavailable={onAttentionUnavailable}
                onDismiss={() => closeWindow(workspaceWindow.key)}
                onSettled={async (action) => {
                 closeWindow(workspaceWindow.key);
                 await onAttentionSettled?.(action);
               }}
             />
           {:else if workspaceWindow.kind === 'agent'}
            {@const agent = agents.find((item: Agent) => item.agent_id === workspaceWindow.agentId)}
            {#if agent}
              <AgentQuickChatModal
                bind:this={entityRefs[workspaceWindow.key]}
                {agent}
                embedded
                onClose={() => closeWindow(workspaceWindow.key)}
                onResolved={(conversationId) => {
                  manager.setResolvedConversation(workspaceWindow.key, conversationId);
                  manager.updateMetadata(workspaceWindow.key, {
                    title: agent.display_name ?? agent.name,
                    status: 'active',
                  });
                  onAgentConversationResolved?.(workspaceWindow.key, conversationId);
                }}
                {onConversationInitialLoaded}
                inspectorOpen={inspectorOpen(workspaceWindow.key)}
                inspectorControlInHeader
                onInspectorStateChange={(open) => setInspectorOpen(workspaceWindow.key, open, true)}
              />
            {/if}
          {:else}
            <DashboardEntityModal
              bind:this={entityRefs[workspaceWindow.key]}
              kind={workspaceWindow.kind}
              taskId={workspaceWindow.kind === 'task' ? workspaceWindow.entityId : null}
              conversationId={workspaceWindow.kind === 'conversation' ? workspaceWindow.entityId : null}
              scheduleId={workspaceWindow.kind === 'schedule' ? workspaceWindow.entityId : null}
              {agents}
              modalRefreshToken={workspaceWindow.refreshToken}
              embedded
               onClose={() => closeWindow(workspaceWindow.key)}
               {onConversationInitialLoaded}
               {onTaskMutationSettled}
              onMetadataChange={(metadata) => manager.updateMetadata(workspaceWindow.key, metadata)}
              inspectorOpen={inspectorOpen(workspaceWindow.key)}
              inspectorControlInHeader={workspaceWindow.kind === 'conversation'}
              onInspectorStateChange={(open) => setInspectorOpen(workspaceWindow.key, open, true)}
            />
          {/if}
        {/snippet}
      </WorkspaceWindow>
    {/each}
    {#if device !== 'phone'}
      <WorkspaceDock
        windows={$manager}
        {manager}
        {agents}
        {conversations}
        left={dockLeft}
        onTopChange={handleDockTop}
        {onCapacityCapped}
      />
    {/if}
  </div>

<style>
  .workspace-snap-preview {
    transition:
      left 180ms cubic-bezier(0.2, 0.8, 0.2, 1),
      top 180ms cubic-bezier(0.2, 0.8, 0.2, 1),
      width 180ms cubic-bezier(0.2, 0.8, 0.2, 1),
      height 180ms cubic-bezier(0.2, 0.8, 0.2, 1),
      opacity 180ms ease;
  }

  @media (prefers-reduced-motion: reduce) {
    .workspace-snap-preview {
      transition: none;
    }
  }
</style>
