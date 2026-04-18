<script lang="ts">
  import type { Snippet } from 'svelte';

  /**
   * Edge-swipe to navigate back (iOS-style interactive-back).
   *
   * Listens for touch pointerdown within `edgeWidth` pixels of the left edge
   * and, if the user drags past `threshold` pixels rightward, invokes `onBack`.
   * Mouse/trackpad input is ignored so desktops don't accidentally trigger.
   *
   * The component renders its children with no wrapper markup overhead beyond
   * a single `<div>` that fills its parent.
   *
   * Usage (in a chat detail page):
   *   <SwipeBack onBack={() => history.back()}>...</SwipeBack>
   */

  interface Props {
    onBack: () => void;
    edgeWidth?: number;
    threshold?: number;
    children: Snippet;
  }

  let { onBack, edgeWidth = 24, threshold = 80, children }: Props = $props();

  let tracking = false;
  let startX = 0;
  let startY = 0;

  /**
   * Walks up the pointer target looking for an ancestor that can scroll
   * horizontally with room to move. If we find one, the gesture belongs to
   * that scroller (code block, wide table, carousel), not to navigation.
   */
  function hasHorizontalScrollAncestor(target: EventTarget | null): boolean {
    let el = target instanceof Element ? target : null;
    while (el) {
      if (el.scrollWidth > el.clientWidth + 1) {
        const style = window.getComputedStyle(el);
        const overflowX = style.overflowX;
        if (overflowX === 'auto' || overflowX === 'scroll') return true;
      }
      el = el.parentElement;
    }
    return false;
  }

  function onPointerDown(event: PointerEvent): void {
    if (event.pointerType === 'mouse') return;
    if (event.clientX > edgeWidth) return;
    if (hasHorizontalScrollAncestor(event.target)) return;
    tracking = true;
    startX = event.clientX;
    startY = event.clientY;
  }

  function onPointerMove(event: PointerEvent): void {
    if (!tracking) return;
    const dx = event.clientX - startX;
    const dy = event.clientY - startY;
    // Abort on mostly-vertical gestures (avoid hijacking scrolls).
    if (Math.abs(dy) > Math.abs(dx)) {
      tracking = false;
      return;
    }
    if (dx >= threshold) {
      tracking = false;
      onBack();
    }
  }

  function onPointerUp(): void {
    tracking = false;
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="contents"
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
>
  {@render children()}
</div>
