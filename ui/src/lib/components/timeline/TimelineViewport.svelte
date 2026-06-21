<script lang="ts">
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import type { Snippet } from 'svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import TimelineList from '$lib/components/timeline/TimelineList.svelte';
  import type { TimelineItem } from '$lib/chat';
  import type { Agent } from '$lib/types/api';

  let {
    items,
    agent = null,
    compact = false,
    loading = false,
    loadingOlder = false,
    error = '',
    emptyLabel = 'No events recorded yet.',
    live = false,
    liveLabel = 'Reading latest logs',
    followPausedLabel = 'Scroll to latest',
    viewportElement = $bindable<HTMLDivElement | null>(null),
    contentElement = $bindable<HTMLDivElement | null>(null),
    userScrolledUp = $bindable(false),
    class: className = 'relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-4 py-4 pb-4',
    contentClass = 'space-y-4',
    onNearTop,
    onPointerDown,
    onScroll,
    onWheel,
    onTouchStart,
    onTouchMove,
    onTouchEnd,
    onKeydown,
    onViewSession,
    children
  } = $props<{
    items: TimelineItem[];
    agent?: Agent | null;
    compact?: boolean;
    loading?: boolean;
    loadingOlder?: boolean;
    error?: string;
    emptyLabel?: string;
    live?: boolean;
    liveLabel?: string;
    followPausedLabel?: string;
    viewportElement?: HTMLDivElement | null;
    contentElement?: HTMLDivElement | null;
    userScrolledUp?: boolean;
    class?: string;
    contentClass?: string;
    onNearTop?: (() => void) | undefined;
    onPointerDown?: (() => void) | undefined;
    onScroll?: ((event: Event) => void) | undefined;
    onWheel?: ((event: WheelEvent) => void) | undefined;
    onTouchStart?: ((event: TouchEvent) => void) | undefined;
    onTouchMove?: ((event: TouchEvent) => void) | undefined;
    onTouchEnd?: ((event: TouchEvent) => void) | undefined;
    onKeydown?: ((event: KeyboardEvent) => void) | undefined;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    children?: Snippet;
  }>();

  let programmaticScroll = false;
  let lastScrollTop = 0;

  type ViewportEventHandlers = {
    onScroll?: ((event: Event) => void) | undefined;
    onWheel?: ((event: WheelEvent) => void) | undefined;
    onTouchStart?: ((event: TouchEvent) => void) | undefined;
    onTouchMove?: ((event: TouchEvent) => void) | undefined;
    onTouchEnd?: ((event: TouchEvent) => void) | undefined;
    onKeydown?: ((event: KeyboardEvent) => void) | undefined;
    onPointerDown?: (() => void) | undefined;
  };

  function viewportEvents(node: HTMLDivElement, handlers: ViewportEventHandlers) {
    let current = handlers;
    const handleScrollEvent = (event: Event): void => {
      if (current.onScroll) {
        current.onScroll(event);
        return;
      }
      handleScroll();
    };
    const handleWheelEvent = (event: WheelEvent): void => current.onWheel?.(event);
    const handleTouchStartEvent = (event: TouchEvent): void => current.onTouchStart?.(event);
    const handleTouchMoveEvent = (event: TouchEvent): void => current.onTouchMove?.(event);
    const handleTouchEndEvent = (event: TouchEvent): void => current.onTouchEnd?.(event);
    const handleKeydownEvent = (event: KeyboardEvent): void => current.onKeydown?.(event);
    const handlePointerDownEvent = (): void => current.onPointerDown?.();

    node.addEventListener('scroll', handleScrollEvent);
    node.addEventListener('wheel', handleWheelEvent);
    node.addEventListener('touchstart', handleTouchStartEvent);
    node.addEventListener('touchmove', handleTouchMoveEvent);
    node.addEventListener('touchend', handleTouchEndEvent);
    node.addEventListener('keydown', handleKeydownEvent);
    node.addEventListener('pointerdown', handlePointerDownEvent);

    return {
      update(next: ViewportEventHandlers): void {
        current = next;
      },
      destroy(): void {
        node.removeEventListener('scroll', handleScrollEvent);
        node.removeEventListener('wheel', handleWheelEvent);
        node.removeEventListener('touchstart', handleTouchStartEvent);
        node.removeEventListener('touchmove', handleTouchMoveEvent);
        node.removeEventListener('touchend', handleTouchEndEvent);
        node.removeEventListener('keydown', handleKeydownEvent);
        node.removeEventListener('pointerdown', handlePointerDownEvent);
      }
    };
  }

  function scrollToBottom(force = false): void {
    if (!viewportElement || (!force && userScrolledUp)) return;
    programmaticScroll = true;
    requestAnimationFrame(() => {
      if (viewportElement) {
        viewportElement.scrollTop = viewportElement.scrollHeight;
        lastScrollTop = viewportElement.scrollTop;
      }
      programmaticScroll = false;
    });
  }

  function handleScroll(): void {
    if (!viewportElement || programmaticScroll) return;
    const currentScrollTop = viewportElement.scrollTop;
    const distanceFromBottom = viewportElement.scrollHeight - viewportElement.scrollTop - viewportElement.clientHeight;

    if (currentScrollTop < lastScrollTop - 2 && distanceFromBottom > 0) {
      userScrolledUp = true;
    } else if (distanceFromBottom <= 24) {
      userScrolledUp = false;
    } else if (distanceFromBottom > 80) {
      userScrolledUp = true;
    }

    lastScrollTop = currentScrollTop;
    if (currentScrollTop <= 24) onNearTop?.();
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    scrollToBottom(true);
  }

  $effect(() => {
    if ((!contentElement && !viewportElement) || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => {
      requestAnimationFrame(() => scrollToBottom());
    });
    if (contentElement) observer.observe(contentElement);
    if (viewportElement) observer.observe(viewportElement);
    return () => observer.disconnect();
  });

</script>

<div
  class={className}
  role="region"
  aria-label="Timeline"
  bind:this={viewportElement}
  use:viewportEvents={{ onScroll, onWheel, onTouchStart, onTouchMove, onTouchEnd, onKeydown, onPointerDown }}
  tabindex="-1"
>
  <div bind:this={contentElement} class={contentClass}>
    {#if children}
      {@render children()}
    {:else if loadingOlder}
      <p class="px-4 py-2 text-center text-xs text-slate-500">Loading older messages…</p>
    {/if}

    {#if !children && loading}
      <LoadingState />
    {:else if !children && error}
      <p class="text-sm text-rose-400">{error}</p>
    {:else if !children && items.length === 0}
      <p class="text-sm text-slate-500">{emptyLabel}</p>
    {:else if !children}
      <TimelineList {items} {agent} {compact} {onViewSession} />
    {/if}
  </div>

  {#if userScrolledUp}
    <button
      class="sticky bottom-2 left-1/2 z-10 -translate-x-1/2 rounded-full border border-slate-700 bg-slate-900/90 p-2 shadow-lg transition hover:bg-slate-800"
      onclick={jumpToBottom}
      type="button"
      title={followPausedLabel}
    >
      <ArrowDown class="h-4 w-4 text-slate-300" />
    </button>
  {:else if !loading && !error && live}
    <div class="sticky bottom-2 left-1/2 z-10 w-fit -translate-x-1/2">
      <LiveDots label={liveLabel} size="sm" />
    </div>
  {/if}
</div>
