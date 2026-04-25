<script lang="ts">
  import type { Snippet } from 'svelte';

  /**
   * Tap-or-hover popover. Replaces hover-only tooltips.
   *
   * - On pointer devices (`hover: hover`), the popover opens on hover and on
   *   keyboard focus of any interactive descendant.
   * - On touch devices (hover: none), the popover opens on pointer tap on the
   *   wrapper. It intentionally does NOT inject its own `<button>`; callers
   *   often pass a <Button> child already, and wrapping it would produce
   *   nested interactive controls / invalid HTML.
   * - Closes on Escape, on focus-out, and on outside tap.
   *
   * NOTE: This means a bare text/icon child (no inner button) still shows the
   * popover only on hover or focus — same contract as the original Tooltip.
   * For that case, wrap the child in a button yourself.
   */

  interface Props {
    text: string;
    children: Snippet;
    class?: string;
    placement?: 'top' | 'bottom' | 'left' | 'right';
  }

  let { text, children, class: className = '', placement = 'top' }: Props = $props();

  let open = $state(false);
  let root = $state<HTMLSpanElement | null>(null);
  let hoverCapable = $state(true);

  $effect(() => {
    if (typeof window !== 'undefined') {
      hoverCapable = window.matchMedia?.('(hover: hover)').matches ?? true;
    }
  });

  function onEnter(): void {
    if (hoverCapable) open = true;
  }

  function onLeave(): void {
    if (hoverCapable) open = false;
  }

  function onFocusIn(): void {
    open = true;
  }

  function onFocusOut(event: FocusEvent): void {
    const related = event.relatedTarget as Node | null;
    if (root && related && root.contains(related)) return;
    open = false;
  }

  function onPointerDown(event: PointerEvent): void {
    // Only handle touch/pen pointer types here; mouse is covered by hover.
    if (event.pointerType === 'mouse') return;
    // Toggle on tap. The child may itself be a button that handles the tap
    // separately — the popover just reveals; it does not steal the click.
    open = !open;
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key === 'Escape') open = false;
  }

  function onDocumentPointerDown(event: PointerEvent): void {
    if (!open) return;
    if (root && event.target instanceof Node && !root.contains(event.target)) {
      open = false;
    }
  }

  $effect(() => {
    if (!open) return;
    if (typeof document === 'undefined') return;
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    return () => document.removeEventListener('pointerdown', onDocumentPointerDown, true);
  });
</script>

<svelte:window onkeydown={onKey} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
  bind:this={root}
  class={`group relative inline-flex items-center ${className}`}
  onmouseenter={onEnter}
  onmouseleave={onLeave}
  onfocusin={onFocusIn}
  onfocusout={onFocusOut}
  onpointerdown={onPointerDown}
>
  {@render children()}
  {#if open}
    {@const isHorizontal = placement === 'left' || placement === 'right'}
    <span
      role="tooltip"
      class={`pointer-events-none absolute z-50 whitespace-normal rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-xs font-normal leading-relaxed text-slate-300 shadow-lg ${
        placement === 'top'
          ? 'bottom-full left-1/2 mb-2 -translate-x-1/2'
          : placement === 'bottom'
          ? 'top-full left-1/2 mt-2 -translate-x-1/2'
          : placement === 'right'
          ? 'left-full top-1/2 ml-2 -translate-y-1/2'
          : 'right-full top-1/2 mr-2 -translate-y-1/2'
      }`}
      style={isHorizontal
        ? 'width: max-content; max-width: min(16rem, calc(100vw - 2rem));'
        : 'width: max-content; max-width: min(20rem, calc(100vw - 2rem));'}
    >
      {text}
      {#if placement === 'top'}
        <span class="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-slate-700"></span>
      {:else if placement === 'bottom'}
        <span class="absolute left-1/2 bottom-full -translate-x-1/2 border-4 border-transparent border-b-slate-700"></span>
      {:else if placement === 'right'}
        <span class="absolute right-full top-1/2 -translate-y-1/2 border-4 border-transparent border-r-slate-700"></span>
      {:else}
        <span class="absolute left-full top-1/2 -translate-y-1/2 border-4 border-transparent border-l-slate-700"></span>
      {/if}
    </span>
  {/if}
</span>
