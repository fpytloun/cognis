<script lang="ts">
  import X from 'lucide-svelte/icons/x';
  import MonitorDown from 'lucide-svelte/icons/monitor-down';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import { portal } from '$lib/actions/portal';
  import type { Agent, Conversation } from '$lib/types/api';
  import {
    workspaceConversationForWindow,
    workspaceWindowActivityState,
    workspaceWindowTitle,
  } from '$lib/dashboard/workspace-activity';
  import { onDestroy, onMount } from 'svelte';
  import {
    activeWorkspaceWindowKey,
    type WorkspaceManager,
    type WorkspaceWindowState,
  } from '$lib/dashboard/workspace';
  import {
    workspaceDockInsertion,
    workspaceDockOrderLefts,
    type WorkspaceDockSlot,
  } from '$lib/dashboard/workspace-dock-drag';

  let {
    windows,
    manager,
    agents = [],
    conversations = [],
    left = 12,
    onCapacityCapped,
    onTopChange,
  } = $props<{
    windows: WorkspaceWindowState[];
    manager: WorkspaceManager;
    agents?: Agent[];
    conversations?: Conversation[];
    left?: number;
    onCapacityCapped?: (limit: number) => void;
    onTopChange?: (top: number | null) => void;
  }>();
  let publishedTop: number | null | undefined;
  let resizeObserver: ResizeObserver | null = null;
  let dock = $state<HTMLElement | null>(null);
  let dockStrip = $state<HTMLElement | null>(null);
  let dashboardSnapshotActive = $state(false);
  let drag = $state<{
    key: string;
    startX: number;
    startY: number;
    pointerId: number;
    initialOrder: string[];
    slots: WorkspaceDockSlot[];
    gap: number;
    insertionIndex: number;
    grabOffsetX: number;
    grabOffsetY: number;
    ghostWidth: number;
    ghostHeight: number;
    title: string;
  } | null>(null);
  let draggedKey = $state<string | null>(null);
  let provisionalOrder = $state<string[] | null>(null);
  let keyboardMotionKey = $state<string | null>(null);
  let keyboardOffsets = $state<Record<string, number>>({});
  let keyboardFlipActive = $state(false);
  let keyboardMotionTimer: ReturnType<typeof setTimeout> | null = null;
  let suppressActivationKey: string | null = null;
  let announcement = $state('');
  let dragPointerX = $state<number | null>(null);
  let dragPointerY = $state<number | null>(null);
  let autoScrollFrame: number | null = null;
  let dragListenersInstalled = false;
  let dragCaptureTarget: HTMLElement | null = null;
  let suppressActivationTimer: ReturnType<typeof setTimeout> | null = null;
  const DRAG_THRESHOLD = 6;

  function agentFor(window: WorkspaceWindowState): Agent | null {
    return agents.find((agent: Agent) => agent.agent_id === window.agentId) ?? null;
  }

  function conversationFor(window: WorkspaceWindowState): Conversation | null {
    return workspaceConversationForWindow(window, conversations);
  }

  const dockVisible = $derived(windows.length > 0);
  const activeWindowKey = $derived(activeWorkspaceWindowKey(windows));
  const switcherWindows = $derived(
    [...windows].sort((left, right) => left.switcherOrder - right.switcherOrder),
  );
  const visibleWindowCount = $derived(windows.filter((window: WorkspaceWindowState) => !window.minimized).length);

  function activate(key: string): void {
    if (suppressActivationKey === key) {
      suppressActivationKey = null;
      return;
    }
    if (activeWindowKey === key) {
      manager.minimize(key);
      return;
    }
    const result = manager.restore(key);
    if (result.status === 'capped') onCapacityCapped?.(result.limit);
  }

  function toggleDashboard(): void {
    if (dashboardSnapshotActive) {
      const result = manager.restoreDashboard();
      dashboardSnapshotActive = manager.hasDashboardSnapshot();
      if (result.status === 'restored' && result.capped) onCapacityCapped?.(result.limit);
      return;
    }
    const result = manager.showDashboard();
    dashboardSnapshotActive = result.status === 'minimized';
  }

  function announcePosition(key: string, title: string): void {
    const ordered: WorkspaceWindowState[] = manager.snapshot()
      .sort((left: WorkspaceWindowState, right: WorkspaceWindowState) => (
        left.switcherOrder - right.switcherOrder
      ));
    const position = ordered.findIndex((window: WorkspaceWindowState) => window.key === key) + 1;
    announcement = `${title} moved to position ${position} of ${ordered.length}.`;
  }

  function reorderWithKeyboard(event: KeyboardEvent, key: string, title: string): void {
    suppressActivationKey = null;
    if (!event.altKey || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const index = switcherWindows.findIndex((window) => window.key === key);
    const target = switcherWindows[index + (event.key === 'ArrowLeft' ? -1 : 1)];
    if (!target) return;
    const previousLefts = new Map(
      [...(dockStrip?.querySelectorAll<HTMLElement>('[data-workspace-switcher-key]') ?? [])]
        .map((element) => [
          element.dataset.workspaceSwitcherKey,
          element.getBoundingClientRect().left,
        ] as const),
    );
    if (manager.reorderSwitcher(key, target.key, event.key === 'ArrowLeft' ? 'before' : 'after')) {
      announcePosition(key, title);
      if (keyboardMotionTimer !== null) clearTimeout(keyboardMotionTimer);
      keyboardMotionKey = null;
      requestAnimationFrame(() => {
        if (!prefersReducedMotion()) {
          keyboardOffsets = Object.fromEntries(
            [...(dockStrip?.querySelectorAll<HTMLElement>('[data-workspace-switcher-key]') ?? [])]
              .map((element) => {
                const itemKey = element.dataset.workspaceSwitcherKey;
                const previousLeft = itemKey ? previousLefts.get(itemKey) : undefined;
                return [itemKey, previousLeft === undefined
                  ? 0
                  : previousLeft - element.getBoundingClientRect().left];
              })
              .filter(([itemKey]) => itemKey !== undefined),
          ) as Record<string, number>;
          keyboardFlipActive = true;
        }
        keyboardMotionKey = key;
        requestAnimationFrame(() => {
          keyboardFlipActive = false;
          requestAnimationFrame(() => {
            keyboardOffsets = {};
          });
        });
        keyboardMotionTimer = setTimeout(() => {
          keyboardOffsets = {};
          if (keyboardMotionKey === key) keyboardMotionKey = null;
          keyboardMotionTimer = null;
        }, 220);
      });
    }
  }

  function stableSlots(): WorkspaceDockSlot[] {
    if (!dockStrip) return [];
    const stripRect = dockStrip.getBoundingClientRect();
    return [...dockStrip.querySelectorAll<HTMLElement>('[data-workspace-switcher-key]')]
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const hasLayoutMetrics = element.offsetWidth > 0;
        return {
          key: element.dataset.workspaceSwitcherKey ?? '',
          left: hasLayoutMetrics
            ? stripRect.left + element.offsetLeft - dockStrip!.scrollLeft
            : rect.left,
          width: hasLayoutMetrics ? element.offsetWidth : rect.width,
        };
      })
      .filter((slot) => slot.key);
  }

  function updateInsertion(clientX: number): void {
    if (!drag) return;
    const slots = stableSlots();
    if (slots.length !== drag.initialOrder.length) return;
    const insertion = workspaceDockInsertion(
      slots,
      drag.key,
      clientX,
      drag.insertionIndex,
    );
    drag = { ...drag, slots, insertionIndex: insertion.index };
    provisionalOrder = insertion.order;
  }

  function stopAutoScroll(): void {
    if (autoScrollFrame !== null) cancelAnimationFrame(autoScrollFrame);
    autoScrollFrame = null;
    dragPointerX = null;
    dragPointerY = null;
  }

  function autoScroll(): void {
    autoScrollFrame = null;
    if (!dockStrip || !draggedKey || dragPointerX === null || dragPointerY === null) return;
    const rect = dockStrip.getBoundingClientRect();
    const delta = dragPointerX < rect.left + 32 ? -12 : dragPointerX > rect.right - 32 ? 12 : 0;
    if (!delta) return;
    dockStrip.scrollLeft += delta;
    updateInsertion(dragPointerX);
    autoScrollFrame = requestAnimationFrame(autoScroll);
  }

  function startAutoScroll(clientX: number, clientY: number): void {
    dragPointerX = clientX;
    dragPointerY = clientY;
    if (autoScrollFrame === null) autoScrollFrame = requestAnimationFrame(autoScroll);
  }

  function startDrag(event: PointerEvent, key: string): void {
    if (event.button !== 0) return;
    suppressActivationKey = null;
    const initialOrder = switcherWindows.map((window) => window.key);
    const slots = stableSlots();
    const sourceElement = dockStrip?.querySelector<HTMLElement>(
      `[data-workspace-switcher-key="${CSS.escape(key)}"]`,
    );
    const sourceRect = sourceElement?.getBoundingClientRect();
    const gap = initialOrder.length < 2
      ? 0
      : Math.max(0, slots[1].left - slots[0].left - slots[0].width);
    drag = {
      key,
      startX: event.clientX,
      startY: event.clientY,
      pointerId: event.pointerId,
      initialOrder,
      slots,
      gap,
      insertionIndex: initialOrder.indexOf(key),
      grabOffsetX: event.clientX - (sourceRect?.left ?? event.clientX),
      grabOffsetY: event.clientY - (sourceRect?.top ?? event.clientY),
      ghostWidth: sourceRect?.width ?? 0,
      ghostHeight: sourceRect?.height ?? 0,
      title: switcherWindows.find((window) => window.key === key)?.title ?? '',
    };
    dragCaptureTarget = event.currentTarget as HTMLElement;
    installDragListeners();
    dragCaptureTarget.setPointerCapture?.(event.pointerId);
  }

  function moveDrag(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!draggedKey && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) >= DRAG_THRESHOLD) {
      draggedKey = drag.key;
      provisionalOrder = drag.initialOrder;
      dragCaptureTarget?.blur();
    }
    if (!draggedKey) return;
    event.preventDefault();
    dragPointerX = event.clientX;
    dragPointerY = event.clientY;
    updateInsertion(event.clientX);
    startAutoScroll(event.clientX, event.clientY);
  }

  function endDrag(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const sourceKey = drag.key;
    if (draggedKey) {
      event.preventDefault();
      suppressNextActivation(sourceKey);
      const order = provisionalOrder ?? drag.initialOrder;
      if (order.some((key, index) => key !== drag!.initialOrder[index])) {
        const sourceIndex = order.indexOf(sourceKey);
        const targetKey = sourceIndex === 0 ? order[1] : order[sourceIndex - 1];
        const placement = sourceIndex === 0 ? 'before' : 'after';
        if (targetKey && manager.reorderSwitcher(sourceKey, targetKey, placement)) {
          announcePosition(sourceKey, drag.title);
        }
      }
    }
    clearDrag(event.pointerId);
  }

  function cancelDrag(event?: PointerEvent): void {
    if (event && drag && event.pointerId !== drag.pointerId) return;
    if (draggedKey && drag) suppressNextActivation(drag.key);
    clearDrag(event?.pointerId ?? drag?.pointerId);
  }

  function suppressNextActivation(key: string): void {
    suppressActivationKey = key;
    if (suppressActivationTimer !== null) clearTimeout(suppressActivationTimer);
    suppressActivationTimer = setTimeout(() => {
      if (suppressActivationKey === key) suppressActivationKey = null;
      suppressActivationTimer = null;
    }, 0);
  }

  function clearDrag(pointerId?: number): void {
    if (pointerId !== undefined && dragCaptureTarget?.hasPointerCapture?.(pointerId)) {
      dragCaptureTarget.releasePointerCapture(pointerId);
    }
    dragCaptureTarget = null;
    removeDragListeners();
    drag = null;
    draggedKey = null;
    provisionalOrder = null;
    stopAutoScroll();
  }

  function installDragListeners(): void {
    if (dragListenersInstalled) return;
    window.addEventListener('pointermove', moveDrag, { passive: false });
    window.addEventListener('pointerup', endDrag);
    window.addEventListener('pointercancel', cancelDrag);
    window.addEventListener('lostpointercapture', cancelDrag);
    dragListenersInstalled = true;
  }

  function removeDragListeners(): void {
    if (!dragListenersInstalled) return;
    window.removeEventListener('pointermove', moveDrag);
    window.removeEventListener('pointerup', endDrag);
    window.removeEventListener('pointercancel', cancelDrag);
    window.removeEventListener('lostpointercapture', cancelDrag);
    dragListenersInstalled = false;
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape' || !drag) return;
    event.preventDefault();
    cancelDrag();
  }

  function prefersReducedMotion(): boolean {
    return typeof window !== 'undefined'
      && (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);
  }

  function itemStyle(key: string): string {
    if (!draggedKey || !drag || !provisionalOrder) {
      const keyboardOffset = keyboardOffsets[key] ?? 0;
      return keyboardOffset ? `transform:translate3d(${keyboardOffset}px, 0, 0);` : '';
    }
    if (key === draggedKey) return '';
    const initialRect = drag.slots.find((slot) => slot.key === key);
    const previewLeft = provisionalLefts()[key];
    if (!initialRect || previewLeft === undefined) return '';
    const shift = previewLeft - initialRect.left;
    return `transform:translate3d(${shift}px, 0, 0);`;
  }

  function provisionalLefts(): Record<string, number> {
    const activeDrag = drag;
    const order = provisionalOrder;
    if (!activeDrag || !order?.length) return {};
    return workspaceDockOrderLefts(activeDrag.slots, order, activeDrag.gap);
  }

  function dropIndicatorStyle(): string {
    if (!draggedKey || !drag || !provisionalOrder || !dockStrip) return '';
    const slotLeft = provisionalLefts()[draggedKey];
    if (slotLeft === undefined) return '';
    const dockRect = dockStrip.getBoundingClientRect();
    return `left:${slotLeft - dockRect.left + dockStrip.scrollLeft - 2}px;`;
  }

  function ghostStyle(): string {
    if (!drag || dragPointerX === null || dragPointerY === null) return '';
    const scale = prefersReducedMotion() ? 1 : 1.03;
    return [
      `left:${dragPointerX - drag.grabOffsetX}px`,
      `top:${dragPointerY - drag.grabOffsetY - (prefersReducedMotion() ? 0 : 3)}px`,
      `width:${drag.ghostWidth}px`,
      `height:${drag.ghostHeight}px`,
      `transform:scale(${scale})`,
    ].join(';');
  }

  function publishTop(top: number | null): void {
    const next = top === null ? null : Math.max(0, top);
    if (next === publishedTop) return;
    publishedTop = next;
    onTopChange?.(next);
  }

  function measureDock(node: HTMLElement): { destroy(): void } {
    resizeObserver?.disconnect();
    publishTop(node.getBoundingClientRect().top);
    resizeObserver = new ResizeObserver(() => {
      publishTop(node.getBoundingClientRect().top);
    });
    resizeObserver.observe(node);
    return {
      destroy(): void {
        resizeObserver?.disconnect();
        resizeObserver = null;
        publishTop(null);
      },
    };
  }

  $effect(() => {
    if (!dockVisible) publishTop(null);
  });

  onMount(() => manager.subscribe(() => {
    dashboardSnapshotActive = manager.hasDashboardSnapshot();
  }));

  onDestroy(() => {
    resizeObserver?.disconnect();
    if (keyboardMotionTimer !== null) clearTimeout(keyboardMotionTimer);
    if (suppressActivationTimer !== null) clearTimeout(suppressActivationTimer);
    stopAutoScroll();
    removeDragListeners();
    publishTop(null);
  });
</script>

<svelte:window onkeydown={handleWindowKeydown} />

{#if dockVisible}
  <nav
    use:measureDock
    aria-label="Workspace windows"
    class="workspace-dock pointer-events-auto fixed bottom-[calc(var(--app-shell-bottom-offset,0px)+0.75rem)] right-3 z-[78] flex max-w-[calc(100vw-1.5rem)] gap-1.5 overflow-hidden rounded-xl border border-slate-700 bg-slate-950/95 p-1.5 shadow-2xl backdrop-blur motion-safe:transition-[left] motion-safe:duration-220"
    data-testid="workspace-dock"
    bind:this={dock}
    style={`left:${left}px`}
    data-dragging={draggedKey !== null ? 'true' : 'false'}
    data-keyboard-flip={keyboardFlipActive ? 'true' : 'false'}
    data-provisional-order={provisionalOrder?.join(',') ?? undefined}
  >
    <div
      bind:this={dockStrip}
      class="workspace-dock-strip flex min-w-0 flex-1 gap-1.5 overflow-x-auto overscroll-x-contain"
      data-testid="workspace-dock-strip"
    >
      {#each switcherWindows as window (window.key)}
        {@const agent = agentFor(window)}
        {@const conversation = conversationFor(window)}
        {@const title = workspaceWindowTitle(window, conversation)}
        <div
        class={`workspace-dock-item flex shrink-0 items-center gap-0.5 rounded-lg border px-1 py-0.5 ${
          window.key === activeWindowKey
            ? 'border-sky-400 bg-sky-500/15'
            : 'border-slate-800 bg-slate-900'
        } ${window.minimized ? 'opacity-60' : ''} ${
          draggedKey === window.key ? 'workspace-dock-item--placeholder' : ''
        } ${keyboardMotionKey === window.key ? 'workspace-dock-item--keyboard-move' : ''}`}
        data-minimized={window.minimized}
        data-dragged={draggedKey === window.key ? 'true' : 'false'}
        data-workspace-switcher-key={window.key}
        data-testid={`workspace-dock-item-${window.key}`}
        style={itemStyle(window.key)}
      >
        <button
          aria-current={window.key === activeWindowKey ? 'true' : undefined}
          aria-label={`${window.minimized ? 'Restore' : 'Focus'} ${title}. Alt plus left or right arrow moves this item.`}
          aria-grabbed={draggedKey === window.key ? 'true' : undefined}
          class={`flex min-h-8 min-w-0 cursor-grab items-center gap-1.5 rounded-md px-1 py-0.5 text-left hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 active:cursor-grabbing ${draggedKey === window.key ? 'cursor-grabbing opacity-80' : ''}`}
          data-testid={`workspace-dock-restore-${window.key}`}
          type="button"
          onclick={() => activate(window.key)}
          onkeydown={(event) => reorderWithKeyboard(event, window.key, title)}
          onpointerdown={(event) => startDrag(event, window.key)}
        >
          <ActivityAvatar
            name={agent?.display_name ?? agent?.name ?? title}
            avatarUrl={agent?.avatar_url ?? null}
            class="h-5 w-5"
            state={workspaceWindowActivityState(window, conversation)}
          />
          <span class="max-w-36 truncate text-[11px] leading-4 text-slate-100">{title}</span>
        </button>
        <button
          aria-label={`Close ${title}`}
          class="grid h-8 w-8 place-items-center rounded-md text-slate-400 hover:bg-rose-500/10 hover:text-rose-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
          data-testid={`workspace-dock-close-${window.key}`}
          type="button"
          onclick={() => manager.close(window.key)}
        ><X class="h-3 w-3" /></button>
        </div>
      {/each}
      {#if draggedKey && provisionalOrder}
        <span
        aria-hidden="true"
        class="workspace-dock-drop-indicator"
        data-insertion-index={drag?.insertionIndex}
        data-testid="workspace-dock-drop-indicator"
        style={dropIndicatorStyle()}
        ></span>
      {/if}
    </div>
    <button
      aria-label={dashboardSnapshotActive ? 'Restore windows' : 'Show dashboard'}
      aria-pressed={dashboardSnapshotActive ? 'true' : 'false'}
      class="grid h-8 w-8 shrink-0 place-items-center rounded-md text-slate-300 hover:bg-slate-800 hover:text-sky-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 disabled:cursor-not-allowed disabled:opacity-45"
      data-testid="workspace-dock-dashboard"
      disabled={!dashboardSnapshotActive && visibleWindowCount === 0}
      onclick={toggleDashboard}
      title={dashboardSnapshotActive ? 'Restore windows' : 'Show dashboard'}
      type="button"
    ><MonitorDown class="h-4 w-4" /></button>
    {#if draggedKey && drag}
      <div
        use:portal
        aria-hidden="true"
        class="workspace-dock-drag-ghost"
        data-testid="workspace-dock-drag-ghost"
        style={ghostStyle()}
      >
        <span class="truncate px-2 text-[11px] leading-4 text-slate-100">{drag.title}</span>
      </div>
    {/if}
  </nav>
  <span aria-live="polite" class="sr-only" data-testid="workspace-dock-announcement">{announcement}</span>
{/if}

<style>
  .workspace-dock {
    user-select: none;
    scrollbar-color: rgb(71 85 105) transparent;
    scrollbar-width: thin;
  }

  .workspace-dock-item button:first-child {
    touch-action: none;
  }

  .workspace-dock-item {
    transform: translate3d(0, 0, 0);
    transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity 120ms ease, box-shadow 120ms ease;
    will-change: transform;
  }

  .workspace-dock[data-keyboard-flip='true'] .workspace-dock-item {
    transition: none;
  }

  .workspace-dock-item--placeholder {
    border-color: rgb(56 189 248 / 0.9);
    box-shadow: inset 0 0 0 1px rgb(56 189 248 / 0.35);
    opacity: 0.28;
  }

  .workspace-dock-drag-ghost {
    position: fixed;
    z-index: 80;
    display: flex;
    align-items: center;
    overflow: hidden;
    border: 1px solid rgb(56 189 248 / 0.9);
    border-radius: 0.5rem;
    background: rgb(15 23 42 / 0.98);
    box-shadow: 0 10px 22px rgb(2 6 23 / 0.55), 0 0 0 2px rgb(56 189 248 / 0.35);
    cursor: grabbing;
    opacity: 0.94;
    pointer-events: none;
    transform-origin: center;
  }

  .workspace-dock-drop-indicator {
    position: absolute;
    top: 50%;
    z-index: 3;
    width: 4px;
    height: 70%;
    border-radius: 999px;
    background: rgb(56 189 248);
    box-shadow: 0 0 0 2px rgb(56 189 248 / 0.2), 0 0 12px rgb(56 189 248 / 0.8);
    pointer-events: none;
    transform: translateY(-50%);
  }

  @media (prefers-reduced-motion: reduce) {
    .workspace-dock-item {
      transition: none;
    }
  }

  @media (prefers-reduced-motion: no-preference) {
    .workspace-dock-item {
      animation: workspace-dock-item-enter 200ms cubic-bezier(0.2, 0.8, 0.2, 1);
    }
  }

  @keyframes workspace-dock-item-enter {
    from {
      opacity: 0;
      transform: translateY(10px) scale(0.88);
    }
  }

</style>
