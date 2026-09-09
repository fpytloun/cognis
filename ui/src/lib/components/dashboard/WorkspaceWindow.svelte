<script lang="ts">
  import { onDestroy, onMount, type Snippet } from 'svelte';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import LayoutGrid from 'lucide-svelte/icons/layout-grid';
  import Minus from 'lucide-svelte/icons/minus';
  import PanelRightClose from 'lucide-svelte/icons/panel-right-close';
  import PanelRightOpen from 'lucide-svelte/icons/panel-right-open';
  import X from 'lucide-svelte/icons/x';

  import {
    workspaceSnapTarget,
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

  let interaction = $state<{
    kind: 'move' | 'resize';
    pointerId: number;
    startX: number;
    startY: number;
    rect: WorkspaceWindowState['rect'];
    captureTarget: HTMLElement;
    pointerRatioX: number;
    titleOffsetY: number;
    detached: boolean;
    snapTarget: WorkspaceSnapTarget | null;
  } | null>(null);
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
    if (openingTimer) clearTimeout(openingTimer);
    if (restoreTimer) clearTimeout(restoreTimer);
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
    if (event.button !== 0) return;
    if (
      kind === 'move'
      && event.target instanceof Element
      && event.target.closest('button, a, details, summary, [role="menu"]')
    ) return;
    event.preventDefault();
    event.stopPropagation();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    manager.raise(current.key);
    interaction = {
      kind,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      rect: { ...current.rect },
      captureTarget: event.currentTarget as HTMLElement,
      pointerRatioX: Math.max(0, Math.min(1, (event.clientX - current.rect.x) / current.rect.width)),
      titleOffsetY: Math.max(0, event.clientY - current.rect.y),
      detached: current.restoreRect === null,
      snapTarget: null,
    };
  }

  function moveInteraction(event: PointerEvent): void {
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    event.preventDefault();
    let dx = event.clientX - interaction.startX;
    let dy = event.clientY - interaction.startY;
    if (interaction.kind === 'move') {
      if (!interaction.detached && Math.hypot(dx, dy) < 16) return;
      if (!interaction.detached) {
        const restored = manager.restoreForDrag(
          current.key,
          event.clientX,
          event.clientY,
          interaction.pointerRatioX,
          interaction.titleOffsetY,
        );
        if (restored) {
          interaction = {
            ...interaction,
            startX: event.clientX,
            startY: event.clientY,
            rect: restored,
            detached: true,
          };
          dx = 0;
          dy = 0;
        }
      }
      const snapTarget = workspaceSnapTarget(
        event.clientX,
        event.clientY,
        manager.getSafeRect(),
      );
      interaction = { ...interaction, snapTarget };
      onSnapPreview?.(snapTarget);
      manager.move(current.key, interaction.rect.x + dx, interaction.rect.y + dy);
    } else {
      manager.resize(current.key, interaction.rect.width + dx, interaction.rect.height + dy);
    }
  }

  function endInteraction(event: PointerEvent, cancelled = false): void {
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    const finished = interaction;
    if (interaction.captureTarget.hasPointerCapture?.(event.pointerId)) {
      interaction.captureTarget.releasePointerCapture(event.pointerId);
    }
    interaction = null;
    const snap = finished.kind === 'move' && !cancelled ? finished.snapTarget : null;
    onSnapPreview?.(null);
    if (snap) manager.applyPreset(current.key, snap);
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
/>

<div
  bind:this={panelElement}
  aria-label={current.title}
  aria-modal="false"
  class={`workspace-window pointer-events-auto fixed flex flex-col overflow-hidden rounded-2xl border bg-slate-950 ${focused ? 'border-sky-400/70 shadow-[0_24px_70px_rgba(0,0,0,0.65),0_0_0_1px_rgba(56,189,248,0.18)]' : 'border-slate-700/90 shadow-2xl'}`}
  data-focused={focused}
  data-interacting={interacting}
  data-testid={`workspace-window-${current.key}`}
  data-window-key={current.key}
  data-minimized={current.minimized}
  data-opening={opening}
  data-restoring={restoring}
  role="dialog"
  style={`left:${current.rect.x}px;top:${current.rect.y}px;width:${current.rect.width}px;height:${current.rect.height}px;z-index:${70 + current.zOrder};pointer-events:${current.minimized ? 'none' : 'auto'};`}
  tabindex="-1"
  onpointerdown={() => manager.raise(current.key)}
  onanimationend={(event) => {
    if (event.animationName === 'workspace-window-open') opening = false;
    if (event.animationName === 'workspace-window-restore') restoring = false;
  }}
>
  <header
    role="presentation"
    class="app-keyboard-stable-header relative z-20 flex min-h-[40px] shrink-0 select-none items-center gap-1.5 border-b border-slate-800 bg-slate-900/95 px-2.5"
    data-testid={`workspace-window-drag-${current.key}`}
    style="touch-action:none"
    onpointerdown={(event) => startInteraction(event, 'move')}
    onpointerup={endInteraction}
    onpointercancel={(event) => endInteraction(event, true)}
  >
    <div class="min-w-0 flex-1">
      <h2 class="truncate text-sm font-semibold text-white">{current.title}</h2>
      {#if current.status}<p class="truncate text-[10px] uppercase tracking-wide text-slate-400">{current.status}</p>{/if}
    </div>
    {#if onToggleInspector}
      <button
        bind:this={inspectorToggleElement}
        aria-label={inspectorExpanded ? 'Close conversation inspector' : 'Open conversation inspector'}
        aria-expanded={inspectorExpanded}
        aria-controls={inspectorControlsId}
        class={`workspace-control grid place-items-center rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
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
      class={`grid place-items-center rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
      data-testid={`workspace-window-external-${current.key}`}
      href={current.canonicalHref}
      title="Open full page"
    ><ExternalLink class="h-4 w-4" /></a>
    <details bind:this={menuElement} class="relative" onpointerdown={(event) => event.stopPropagation()}>
      <summary
        aria-label="Window layout"
        class={`workspace-control workspace-control-layout grid cursor-pointer list-none place-items-center rounded-lg border border-emerald-400/20 bg-emerald-400/5 text-emerald-300/80 hover:border-emerald-300/50 hover:bg-emerald-400/15 hover:text-emerald-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
        data-testid={`workspace-window-menu-${current.key}`}
        title="Window layout"
      >
        <LayoutGrid class="h-3.5 w-3.5" />
      </summary>
      <div class="absolute right-0 top-full z-10 mt-1 w-40 rounded-xl border border-slate-700 bg-slate-900 p-1 shadow-xl" role="menu">
        {#each presets as item}
          <button
            class="block w-full rounded-lg px-3 py-2 text-left text-sm text-slate-200 hover:bg-slate-800"
            role="menuitem"
            type="button"
            onclick={() => applyPreset(item[0])}
          >{item[1]}</button>
        {/each}
      </div>
    </details>
    <button
      aria-label="Minimize"
      class={`workspace-control grid place-items-center rounded-lg border border-amber-400/20 bg-amber-400/5 text-amber-300/80 hover:border-amber-300/50 hover:bg-amber-400/15 hover:text-amber-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
      data-testid={`workspace-window-minimize-${current.key}`}
      type="button"
      title="Minimize"
      onclick={minimize}
    ><Minus class="h-4 w-4" /></button>
    <button
      aria-label="Close"
      class={`workspace-control grid place-items-center rounded-lg border border-rose-400/20 bg-rose-400/5 text-rose-300/80 hover:border-rose-300/50 hover:bg-rose-400/15 hover:text-rose-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-300/60 ${largeControls ? 'h-[40px] w-[40px]' : 'h-8 w-8'}`}
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
    class="touch-corner-resize absolute bottom-0 right-0 z-30 h-3 w-3 cursor-nwse-resize text-slate-500 hover:text-slate-200 focus-visible:bg-sky-400/30"
    data-testid={`workspace-window-resize-${current.key}`}
    style="touch-action:none;width:12px;height:12px"
    type="button"
    onpointerdown={(event) => startInteraction(event, 'resize')}
    onpointerup={endInteraction}
    onpointercancel={(event) => endInteraction(event, true)}
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
