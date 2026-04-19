<script lang="ts">
  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';

  import { cn } from '$lib/utils';

  /**
   * Mobile-first bottom sheet / side drawer / centered dialog primitive.
   *
   * - On mobile (`side="bottom"` default), renders as a bottom sheet with
   *   a drag handle and swipe-to-dismiss.
   * - On desktop or when `side="center"`, renders as a centered dialog.
   * - Traps focus, restores focus on close, closes on Escape, locks body scroll.
   * - Respects safe-area-inset-bottom.
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
    dismissible?: boolean;
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
    dismissible = true
  }: Props = $props();

  let panelEl = $state<HTMLDivElement | null>(null);
  let previouslyFocused: HTMLElement | null = null;
  let dragStartY = 0;
  let dragOffsetY = $state(0);
  let dragging = $state(false);

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

  /**
   * iOS Safari (including PWAs) does not reliably honour
   * ``document.body.style.overflow = 'hidden'`` — rubber-band scroll
   * still bubbles to the page behind the sheet, making the background
   * feel like it takes precedence when the user scrolls inside the
   * sheet. The proven workaround is to pin the body at the current
   * scroll offset with ``position: fixed`` and restore the offset on
   * close.
   */
  let savedScrollY = 0;

  function lockBodyScroll(): void {
    if (typeof document === 'undefined') return;
    savedScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.style.position = 'fixed';
    document.body.style.top = `-${savedScrollY}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';
  }

  function unlockBodyScroll(): void {
    if (typeof document === 'undefined') return;
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    if (savedScrollY > 0) {
      window.scrollTo(0, savedScrollY);
      savedScrollY = 0;
    }
  }

  $effect(() => {
    if (open) {
      previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      lockBodyScroll();
      requestAnimationFrame(() => focusPanel());
    } else {
      unlockBodyScroll();
      previouslyFocused?.focus();
      previouslyFocused = null;
      dragOffsetY = 0;
      dragging = false;
    }
  });

  onMount(() => {
    return () => {
      unlockBodyScroll();
    };
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!open) return;
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
    if (side !== 'bottom' || !dismissible) return;
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
    if (dragOffsetY > 96) {
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
    class="fixed inset-0 z-[80] isolate"
    role="dialog"
    aria-modal="true"
    aria-label={label}
  >
    <!-- Backdrop. `touch-action: none` stops iOS Safari from
         rubber-band-scrolling the page behind when the user drags
         across the dimmed area. -->
    <button
      class="absolute inset-0 touch-none bg-slate-950/75 backdrop-blur-sm"
      onclick={dismissible ? onClose : undefined}
      type="button"
      aria-label="Dismiss"
      tabindex="-1"
      transition:fade={{ duration: 180 }}
    ></button>

    <div
      bind:this={panelEl}
      tabindex="-1"
      class={cn(
        'absolute flex flex-col border border-slate-800/80 bg-slate-950 shadow-card',
        sideClasses,
        side === 'bottom' ? 'w-full' : '',
        className
      )}
      style={side === 'bottom'
        ? `max-height: ${maxHeight}; transform: translateY(${dragOffsetY}px); transition: ${dragging ? 'none' : 'transform 180ms cubic-bezier(.32,.72,0,1)'}; padding-bottom: calc(env(safe-area-inset-bottom) + 0.75rem);`
        : side === 'right'
        ? `padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); padding-right: env(safe-area-inset-right);`
        : side === 'left'
        ? `padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); padding-left: env(safe-area-inset-left);`
        : undefined}
    >
      {#if side === 'bottom' && dismissible}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="flex shrink-0 justify-center pt-2 pb-1 touch-none"
          onpointerdown={onPointerDown}
          onpointermove={onPointerMove}
          onpointerup={onPointerUp}
          onpointercancel={onPointerUp}
        >
          <span aria-hidden="true" class="h-1.5 w-10 rounded-full bg-slate-700"></span>
        </div>
      {/if}

      {#if header}
        <div class="shrink-0 border-b border-slate-800/80 px-5 py-4">
          {@render header()}
        </div>
      {/if}

      <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-5 pt-4">
        {@render children()}
      </div>
    </div>
  </div>
{/if}
