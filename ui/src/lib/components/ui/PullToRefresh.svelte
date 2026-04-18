<script lang="ts">
  import type { Snippet } from 'svelte';

  /**
   * Pull-to-refresh wrapper for scrollable mobile lists.
   *
   * Tracks pointer drag only when the container is at scrollTop === 0 and the
   * user drags downward. When the drag passes `threshold`, `onRefresh` is
   * invoked. While refreshing, the component shows a small spinner strip at
   * the top and disables further triggers until the promise resolves.
   *
   * Mouse input is ignored so desktops aren't affected.
   */

  interface Props {
    onRefresh: () => Promise<void>;
    threshold?: number;
    disabled?: boolean;
    class?: string;
    children: Snippet;
  }

  let { onRefresh, threshold = 72, disabled = false, class: className = '', children }: Props = $props();

  let scroller = $state<HTMLDivElement | null>(null);
  let startY = 0;
  let tracking = false;
  let pull = $state(0);
  let refreshing = $state(false);

  function onPointerDown(event: PointerEvent): void {
    if (disabled || refreshing) return;
    if (event.pointerType === 'mouse') return;
    if (!scroller || scroller.scrollTop > 0) return;
    tracking = true;
    startY = event.clientY;
  }

  function onPointerMove(event: PointerEvent): void {
    if (!tracking) return;
    const dy = event.clientY - startY;
    if (dy <= 0) {
      pull = 0;
      return;
    }
    // Rubber-band: cap at ~1.5x threshold.
    pull = Math.min(threshold * 1.5, dy);
  }

  async function onPointerUp(): Promise<void> {
    if (!tracking) return;
    tracking = false;
    const hit = pull >= threshold;
    pull = 0;
    if (hit) {
      refreshing = true;
      try {
        await onRefresh();
      } finally {
        refreshing = false;
      }
    }
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={scroller}
  class={`relative overflow-y-auto overscroll-contain ${className}`}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
>
  <div
    aria-hidden="true"
    class="pointer-events-none absolute inset-x-0 top-0 flex items-center justify-center overflow-hidden transition-[height] duration-150"
    style={`height: ${refreshing ? threshold : pull}px;`}
  >
    {#if pull > 0 || refreshing}
      <span
        class="inline-flex h-6 w-6 items-center justify-center"
        style={`opacity: ${refreshing ? 1 : Math.min(1, pull / threshold)};`}
      >
        <svg class={refreshing ? 'animate-spin' : ''} style={refreshing ? '' : `transform: rotate(${(pull / threshold) * 360}deg)`} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
          <path d="M21 12a9 9 0 1 1-6.219-8.56" />
        </svg>
      </span>
    {/if}
  </div>
  {@render children()}
</div>
