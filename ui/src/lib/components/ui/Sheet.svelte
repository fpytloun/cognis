<script lang="ts">
  import type { Snippet } from 'svelte';
  import { fade, fly } from 'svelte/transition';

  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import { cn } from '$lib/utils';

  /**
   * Mobile-first bottom sheet / side drawer / centered dialog primitive.
   *
   * - On mobile (`side="bottom"` default), renders as a bottom sheet with
   *   a drag handle and swipe-to-dismiss.
   * - On desktop or when `side="center"`, renders as a centered dialog.
   * - Traps focus, restores focus on close, closes on Escape, locks body scroll.
   * - Uses ordinary bottom padding; Cognis renders through the iOS bottom safe area.
   *
   * Props:
   *   open        required - visibility state (one-way; emit onClose to close)
   *   onClose     required - callback when user dismisses
   *   side        'bottom' | 'right' | 'center' (default 'bottom')
   *   label       required - accessible label
   *   class       optional extra classes on the panel
   *   children    content snippet
   *   header      optional header snippet
   */

  type Side = 'bottom' | 'right' | 'left' | 'center';

  interface Props {
    open: boolean;
    onClose: () => void;
    side?: Side;
    label: string;
    class?: string;
    maxHeight?: string;
    children: Snippet;
    header?: Snippet;
    headerDivider?: boolean;
    dismissible?: boolean;
    backdropDismissible?: boolean;
    panelId?: string;
    restoreFocusOnClose?: boolean;
    /** Inline presentation retains the same panel subtree without modal behavior. */
    modal?: boolean;
    panelStyle?: string;
  }

  let {
    open,
    onClose,
    side = 'bottom',
    label,
    class: className = '',
    maxHeight = '90dvh',
    children,
    header,
    headerDivider = true,
    dismissible = true,
    backdropDismissible = true,
    panelId,
    restoreFocusOnClose = true,
    modal = true,
    panelStyle
  }: Props = $props();

  let panelEl = $state<HTMLDivElement | null>(null);
  let previouslyFocused: HTMLElement | null = null;
  let dragStartY = 0;
  let dragOffsetY = $state(0);
  let dragging = $state(false);
  let overlayId = $state<string | null>(null);
  let unregisterOverlay: (() => void) | null = null;

  function focusPanel(): void {
    if (!panelEl) return;
    const focusable = panelEl.querySelector<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    if (focusable) {
      focusable.focus();
    } else {
      panelEl.focus();
    }
  }

  $effect(() => {
    if (open && modal) {
      previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const handle = registerOverlay({ kind: 'sheet', blocksChrome: false, root: () => panelEl });
      overlayId = handle.id;
      unregisterOverlay = handle.unregister;
      const frame = requestAnimationFrame(() => {
        if (!panelEl?.contains(document.activeElement)) focusPanel();
      });
      return () => { cancelAnimationFrame(frame); handle.unregister(); };
    } else {
      unregisterOverlay?.();
      unregisterOverlay = null;
      overlayId = null;
      if (!open && restoreFocusOnClose) previouslyFocused?.focus();
      previouslyFocused = null;
      dragOffsetY = 0;
      dragging = false;
    }
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!open || !modal || !isTopOverlay(overlayId)) return;
    if (event.key === 'Escape' && dismissible) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === 'Tab' && panelEl) {
      // Simple focus trap.
      const focusables = panelEl.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  // --- Drag-to-dismiss (bottom sheet only) --------------------------------

  function onPointerDown(event: PointerEvent): void {
    if (side !== 'bottom' || !dismissible || !backdropDismissible || !isTopOverlay(overlayId)) return;
    if (event.pointerType === 'mouse') return;
    dragStartY = event.clientY;
    dragging = true;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: PointerEvent): void {
    if (!dragging) return;
    const dy = event.clientY - dragStartY;
    dragOffsetY = Math.max(0, dy);
  }

  function onPointerUp(event: PointerEvent): void {
    if (!dragging) return;
    dragging = false;
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      /* ignore */
    }
    if (dragOffsetY > 96 && dismissible && backdropDismissible && isTopOverlay(overlayId)) {
      onClose();
    } else {
      dragOffsetY = 0;
    }
  }

  const sideClasses = $derived.by(() => {
    switch (side) {
      case 'bottom':
        return 'inset-x-0 bottom-0 rounded-t-[1.75rem]';
      case 'right':
        return 'inset-y-0 right-0 w-[min(22rem,100vw)] rounded-l-[1.75rem]';
      case 'left':
        return 'inset-y-0 left-0 w-[min(22rem,100vw)] rounded-r-[1.75rem]';
      case 'center':
        return 'left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(32rem,calc(100vw-2rem))] rounded-3xl';
      default:
        return '';
    }
  });
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
  <div
    class={modal ? 'app-viewport-overlay z-[80] isolate min-h-0 overflow-hidden' : 'contents'}
    style={modal ? '--sheet-top-inset: calc(var(--app-shell-notice-height, 0px) + var(--app-shell-top-offset, 0px)); top: calc(var(--app-viewport-offset-top, 0px) + var(--sheet-top-inset)); bottom: auto; height: max(0px, calc(var(--app-viewport-height, 100dvh) - var(--sheet-top-inset) - var(--app-shell-bottom-offset, 0px)));' : undefined}
    role={modal ? 'dialog' : undefined}
    aria-modal={modal ? 'true' : undefined}
    aria-label={modal ? label : undefined}
  >
    <!-- Backdrop. `touch-action: none` stops iOS Safari from
         rubber-band-scrolling the page behind when the user drags
         across the dimmed area. -->
    {#if modal}<button
      class="absolute inset-0 touch-none bg-scrim/75 backdrop-blur-sm"
      onclick={() => { if (dismissible && backdropDismissible && isTopOverlay(overlayId)) onClose(); }}
      type="button"
      aria-label="Dismiss"
      tabindex="-1"
      transition:fade={{ duration: 180 }}
    ></button>{/if}

    <div
      id={panelId}
      data-testid={panelId}
      bind:this={panelEl}
      tabindex="-1"
      role={!modal ? 'complementary' : undefined}
      aria-label={!modal ? label : undefined}
      class={cn(
        modal ? 'absolute flex min-h-0 max-h-full flex-col overflow-hidden border border-separator/80 bg-elevated shadow-overlay' : 'relative flex min-h-0 flex-col',
        modal ? sideClasses : '',
        modal && side === 'bottom' ? 'w-full' : '',
        className
      )}
      style={!modal ? panelStyle : side === 'bottom'
        ? `max-height: min(${maxHeight}, 100%); transform: translateY(${dragOffsetY}px); transition: ${dragging ? 'none' : 'transform 180ms cubic-bezier(.32,.72,0,1)'}; padding-bottom: var(--app-bottom-control-inset);`
        : side === 'right'
        ? `padding-top: calc(0.75rem + max(0px, calc(env(safe-area-inset-top) - var(--sheet-top-inset, 0px)))); padding-bottom: var(--app-bottom-control-inset); padding-right: env(safe-area-inset-right);`
        : side === 'left'
        ? `padding-top: calc(0.75rem + max(0px, calc(env(safe-area-inset-top) - var(--sheet-top-inset, 0px)))); padding-bottom: var(--app-bottom-control-inset); padding-left: env(safe-area-inset-left);`
        : undefined}
      in:fly={{
        x: side === 'right' ? 64 : side === 'left' ? -64 : 0,
        y: side === 'bottom' ? 64 : 0,
        duration: modal ? 180 : 0
      }}
    >
      {#if modal && side === 'bottom' && dismissible}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="flex shrink-0 justify-center pt-2 pb-1 touch-none"
          onpointerdown={onPointerDown}
          onpointermove={onPointerMove}
          onpointerup={onPointerUp}
          onpointercancel={onPointerUp}
        >
          <span aria-hidden="true" class="h-1.5 w-10 rounded-full bg-inset"></span>
        </div>
      {/if}

      {#if header}
        <div class={modal ? `shrink-0 bg-elevated px-5 py-4 ${headerDivider ? 'border-b border-separator/80' : ''}` : 'flex min-w-0 shrink-0 items-center px-3 py-2'} data-testid="sheet-header-surface">
          {@render header()}
        </div>
      {/if}

      <div class={`min-h-0 flex-1 overflow-y-auto overscroll-contain ${modal ? 'bg-elevated px-5 pb-5 pt-4' : 'px-4 pb-4 pt-6'}`} data-testid="sheet-content-surface">
        {@render children()}
      </div>
    </div>
  </div>
{/if}
