<script lang="ts">
  import { onDestroy, onMount, type Snippet } from 'svelte';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import LayoutGrid from 'lucide-svelte/icons/layout-grid';
  import Minus from 'lucide-svelte/icons/minus';
  import PanelRightClose from 'lucide-svelte/icons/panel-right-close';
  import PanelRightOpen from 'lucide-svelte/icons/panel-right-open';
  import X from 'lucide-svelte/icons/x';
  import { keepInViewport } from '$lib/actions/keep-in-viewport';

  import {
    type WorkspaceInteraction,
    type WorkspaceManager,
    type WorkspacePreset,
    type WorkspaceSnapTarget,
    type WorkspaceWindowState,
  } from '$lib/dashboard/workspace';

  let {
    window,
    manager,
    largeControls = false,
    onSnapPreview,
    inspectorExpanded,
    inspectorControlsId,
    onToggleInspector,
    children,
  } = $props<{
    window: WorkspaceWindowState;
    manager: WorkspaceManager;
    largeControls?: boolean;
    onSnapPreview?: (target: WorkspaceSnapTarget | null) => void;
    inspectorExpanded?: boolean;
    inspectorControlsId?: string;
    onToggleInspector?: () => void;
    children: Snippet;
  }>();

  let interaction = $state.raw<{
    token: WorkspaceInteraction;
    owner: WorkspaceManager;
    pointerId: number;
    captureTarget: HTMLElement;
  } | null>(null);
  let previewRect = $state<WorkspaceWindowState['rect'] | null>(null);
  let pendingPointer: { x: number; y: number } | null = null;
  let lastPointer: { x: number; y: number } | null = null;
  let interactionFrame: number | null = null;
  let lastSnapTarget: WorkspaceSnapTarget | null = null;
  let menuElement = $state<HTMLDetailsElement | null>(null);
  let panelElement = $state<HTMLDivElement | null>(null);
  let inspectorToggleElement = $state<HTMLButtonElement | null>(null);
  let opening = $state(true);
  let restoring = $state(false);
  let previousMinimized = false;
  let minimizationInitialized = false;
  let openingTimer: ReturnType<typeof setTimeout> | null = null;
  let restoreTimer: ReturnType<typeof setTimeout> | null = null;
  const presets: Array<[WorkspacePreset, string]> = [
    ['center', 'Center / default'],
    ['left', 'Left 50%'],
    ['right', 'Right 50%'],
    ['maximize', 'Maximize'],
    ['restore', 'Restore'],
  ];
  const current = $derived(
    $manager.find((item: WorkspaceWindowState) => item.key === window.key) ?? window,
  );
  const focused = $derived(
    !current.minimized
    && manager.focusedTopVisibleKey() === current.key,
  );
  const interacting = $derived(interaction !== null);
  const displayedRect = $derived(previewRect ?? current.rect);
  let seenFocusRevision = $state(0);

  $effect(() => {
    if (current.minimized || current.focusRevision === seenFocusRevision) return;
    seenFocusRevision = current.focusRevision;
    queueMicrotask(() => panelElement?.focus({ preventScroll: true }));
  });

  $effect(() => {
    const minimized = current.minimized;
    if (minimized) {
      opening = false;
      restoring = false;
      if (restoreTimer) clearTimeout(restoreTimer);
      restoreTimer = null;
    } else if (minimizationInitialized && previousMinimized) {
      restoring = true;
      if (restoreTimer) clearTimeout(restoreTimer);
      restoreTimer = setTimeout(() => {
        restoring = false;
        restoreTimer = null;
      }, 240);
    }
    previousMinimized = minimized;
    minimizationInitialized = true;
  });

  onMount(() => {
    openingTimer = setTimeout(() => {
      opening = false;
      openingTimer = null;
    }, 240);
  });

  onDestroy(() => {
    finishInteraction(false, true);
    if (openingTimer) clearTimeout(openingTimer);
    if (restoreTimer) clearTimeout(restoreTimer);
  });

  $effect(() => {
    // Observe canonical publications without treating metadata object identity as
    // a geometry revision. The manager decides whether the session is obsolete.
    $manager;
    if (interaction && (interaction.owner !== manager
        || interaction.token.key !== window.key
        || !manager.isInteractionActive(interaction.token))) {
      finishInteraction(false, true);
    } else if (interaction && lastPointer) {
      // Safe-area updates are reconciled by the manager without abandoning an
      // otherwise valid gesture, even if no new pointer sample arrives.
      pendingPointer = lastPointer;
      if (interactionFrame === null) interactionFrame = requestAnimationFrame(() => flushPreview());
    }
  });

  function minimize(): void {
    if (panelElement?.contains(document.activeElement)) {
      (document.activeElement as HTMLElement)?.blur();
    }
    manager.minimize(current.key);
  }

  export function focusInspectorToggle(): void {
    inspectorToggleElement?.focus({ preventScroll: true });
  }

  function startInteraction(event: PointerEvent, kind: 'move' | 'resize'): void {
    if (event.button !== 0 || interaction) return;
    if (
      kind === 'move'
      && event.target instanceof Element
      && event.target.closest('button, a, details, summary, [role="menu"]')
    ) return;
    event.preventDefault();
    event.stopPropagation();
    manager.raise(current.key);
    const token = manager.beginInteraction(current.key, kind, event.clientX, event.clientY);
    if (!token) return;
    const captureTarget = event.currentTarget as HTMLElement;
    try {
      captureTarget.setPointerCapture(event.pointerId);
    } catch {
      manager.abandonInteraction(token);
      return;
    }
    interaction = {
      token,
      owner: manager,
      pointerId: event.pointerId,
      captureTarget,
    };
  }

  function flushPreview(notify = true): void {
    interactionFrame = null;
    const point = pendingPointer;
    pendingPointer = null;
    if (!interaction || !point) return;
    const preview = interaction.owner.previewInteraction(interaction.token, point.x, point.y);
    if (!preview) return;
    previewRect = preview.rect;
    if (preview.snapTarget !== lastSnapTarget) {
      lastSnapTarget = preview.snapTarget;
      if (notify) onSnapPreview?.(lastSnapTarget);
    }
  }

  function moveInteraction(event: PointerEvent): void {
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    event.preventDefault();
    // Preserve the exact threshold-crossing anchor even when several pointer
    // samples arrive before the next visual frame. This does no publication.
    interaction.owner.previewInteraction(interaction.token, event.clientX, event.clientY);
    pendingPointer = { x: event.clientX, y: event.clientY };
    lastPointer = pendingPointer;
    if (interactionFrame === null) interactionFrame = requestAnimationFrame(() => flushPreview());
  }

  function endInteraction(event: PointerEvent, cancelled = false): void {
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    finishInteraction(cancelled);
  }

  function finishInteraction(cancelled: boolean, abandon = false): void {
    if (!interaction) return;
    if (interactionFrame !== null) cancelAnimationFrame(interactionFrame);
    interactionFrame = null;
    if (!abandon) flushPreview(false);
    const finished = interaction;
    // Clear ownership before release/callbacks: lostpointercapture can reenter.
    interaction = null;
    pendingPointer = null;
    lastPointer = null;
    previewRect = null;
    lastSnapTarget = null;
    if (abandon) finished.owner.abandonInteraction(finished.token);
    else finished.owner.finishInteraction(finished.token, cancelled);
    if (finished.captureTarget.hasPointerCapture?.(finished.pointerId)) {
      finished.captureTarget.releasePointerCapture(finished.pointerId);
    }
    onSnapPreview?.(null);
  }

  function applyPreset(preset: WorkspacePreset): void {
    manager.applyPreset(current.key, preset);
    menuElement?.removeAttribute('open');
  }
</script>

<svelte:window
  onpointermove={moveInteraction}
  onpointerup={endInteraction}
  onpointercancel={(event) => endInteraction(event, true)}
  onblur={() => finishInteraction(true)}
/>

<div
  bind:this={panelElement}
  aria-label={current.title}
  aria-modal="false"
  class={`workspace-window pointer-events-auto fixed flex flex-col overflow-hidden rounded-2xl border bg-elevated ${focused ? 'border-control-border/60 shadow-card' : 'border-separator/60 shadow-card'}`}
  data-focused={focused}
  data-interacting={interacting}
  data-testid={`workspace-window-${current.key}`}
  data-window-key={current.key}
  data-minimized={current.minimized}
  data-opening={opening}
  data-restoring={restoring}
  role="dialog"
  style={`left:${displayedRect.x}px;top:${displayedRect.y}px;width:${displayedRect.width}px;height:${displayedRect.height}px;z-index:${70 + current.zOrder};pointer-events:${current.minimized ? 'none' : 'auto'};`}
  tabindex="-1"
  onpointerdown={() => manager.raise(current.key)}
  onanimationend={(event) => {
    if (event.animationName === 'workspace-window-open') opening = false;
    if (event.animationName === 'workspace-window-restore') restoring = false;
  }}
>
  <header
    role="presentation"
    class="app-keyboard-stable-header relative z-20 flex min-h-[40px] shrink-0 select-none items-center gap-1.5 border-b border-separator bg-chrome/95 material-chrome px-2.5"
    data-testid={`workspace-window-drag-${current.key}`}
    style="touch-action:none"
    onpointerdown={(event) => startInteraction(event, 'move')}
    onpointerup={endInteraction}
    onpointercancel={(event) => endInteraction(event, true)}
    onlostpointercapture={(event) => endInteraction(event, true)}
  >
    <div class="min-w-0 flex-1">
      <h2 class="truncate text-sm font-semibold text-strong">{current.title}</h2>
      {#if current.status}<p class="truncate text-[10px] uppercase tracking-wide text-muted">{current.status}</p>{/if}
    </div>
    {#if onToggleInspector}
      <button
        bind:this={inspectorToggleElement}
        aria-label={inspectorExpanded ? 'Close conversation inspector' : 'Open conversation inspector'}
        aria-expanded={inspectorExpanded}
        aria-controls={inspectorControlsId}
        class={`workspace-control grid place-items-center rounded-lg text-secondary hover:bg-raised hover:text-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
        data-testid={`workspace-window-inspector-${current.key}`}
        type="button"
        title={inspectorExpanded ? 'Close conversation inspector' : 'Open conversation inspector'}
        onclick={onToggleInspector}
      >
        {#if inspectorExpanded}<PanelRightClose class="h-4 w-4" />{:else}<PanelRightOpen class="h-4 w-4" />{/if}
      </button>
    {/if}
    <a
      aria-label="Open full page"
      class={`grid place-items-center rounded-lg text-secondary hover:bg-raised hover:text-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
      data-testid={`workspace-window-external-${current.key}`}
      href={current.canonicalHref}
      title="Open full page"
    ><ExternalLink class="h-4 w-4" /></a>
    <details bind:this={menuElement} class="relative" onpointerdown={(event) => event.stopPropagation()}>
      <summary
        aria-label="Window layout"
        class={`workspace-control workspace-control-layout grid cursor-pointer list-none place-items-center rounded-lg border border-success-border bg-success-subtle text-success/80 hover:border-success-border hover:bg-success-subtle hover:text-success focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-success/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
        data-testid={`workspace-window-menu-${current.key}`}
        title="Window layout"
      >
        <LayoutGrid class="h-3.5 w-3.5" />
      </summary>
      <div class="absolute right-0 top-full z-10 mt-1 w-40 rounded-xl border border-control-border bg-surface p-1 shadow-xl" role="menu" use:keepInViewport>
        {#each presets as item}
          <button
            class="block w-full rounded-lg px-3 py-2 text-left text-sm text-foreground hover:bg-raised"
            role="menuitem"
            type="button"
            onclick={() => applyPreset(item[0])}
          >{item[1]}</button>
        {/each}
      </div>
    </details>
    <button
      aria-label="Minimize"
      class={`workspace-control grid place-items-center rounded-lg border border-warning-border bg-warning-subtle text-warning/80 hover:border-warning-border hover:bg-warning-subtle hover:text-warning focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
      data-testid={`workspace-window-minimize-${current.key}`}
      type="button"
      title="Minimize"
      onclick={minimize}
    ><Minus class="h-4 w-4" /></button>
    <button
      aria-label="Close"
      class={`workspace-control grid place-items-center rounded-lg border border-danger-border bg-danger-subtle text-danger/80 hover:border-danger-border hover:bg-danger-subtle hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
      data-testid={`workspace-window-close-${current.key}`}
      type="button"
      title="Close"
      onclick={() => manager.close(current.key)}
    ><X class="h-4 w-4" /></button>
  </header>

  <div class="min-h-0 flex-1 overflow-hidden overscroll-contain" data-testid={`workspace-window-body-${current.key}`}>
    {@render children()}
  </div>

  <button
    aria-label="Resize window"
    class="touch-corner-resize absolute bottom-0 right-0 z-30 h-6 w-6 cursor-nwse-resize text-quiet hover:text-foreground focus-visible:bg-accent-subtle"
    data-testid={`workspace-window-resize-${current.key}`}
    style="touch-action:none;min-width:24px;min-height:24px"
    type="button"
    onpointerdown={(event) => startInteraction(event, 'resize')}
    onpointerup={endInteraction}
    onpointercancel={(event) => endInteraction(event, true)}
    onlostpointercapture={(event) => endInteraction(event, true)}
  >
    <svg
      aria-hidden="true"
      class="absolute bottom-0 right-0 h-3 w-3"
      fill="none"
      viewBox="0 0 16 16"
    >
      <path d="M5 14 14 5M10 14l4-4" stroke="currentColor" stroke-linecap="round" />
    </svg>
  </button>
</div>

<style>
  .workspace-window {
    opacity: 1;
    transform: translateY(0) scale(1);
    visibility: visible;
    transition:
      left 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      top 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      width 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      height 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      opacity 200ms ease,
      transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      border-color 150ms ease,
      box-shadow 150ms ease,
      visibility 0s linear;
  }

  .workspace-window[data-opening='true'] {
    animation: workspace-window-open 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }

  .workspace-window[data-interacting='true'] {
    transition:
      opacity 200ms ease,
      transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      border-color 150ms ease,
      box-shadow 150ms ease,
      visibility 0s linear;
  }

  .workspace-window[data-minimized='true'] {
    opacity: 0;
    transform: translateY(28px) scale(0.84);
    visibility: hidden;
    transition-delay: 0s, 0s, 0s, 0s, 0s, 0s, 0s, 0s, 220ms;
  }

  .workspace-window[data-restoring='true'] {
    animation: workspace-window-restore 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
    opacity: 1;
    visibility: visible;
    transition:
      left 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      top 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      width 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      height 220ms cubic-bezier(0.2, 0.8, 0.2, 1),
      border-color 150ms ease,
      box-shadow 150ms ease;
  }

  @keyframes workspace-window-open {
    from {
      opacity: 0;
      transform: translateY(18px) scale(0.91);
    }
  }

  @keyframes workspace-window-restore {
    from {
      transform: translateY(18px) scale(0.91);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .workspace-window,
    .workspace-window[data-opening='true'],
    .workspace-window[data-restoring='true'],
    .workspace-window[data-interacting='true'],
    .workspace-window[data-minimized='true'] {
      animation: none;
      transition: none;
    }
  }
</style>
