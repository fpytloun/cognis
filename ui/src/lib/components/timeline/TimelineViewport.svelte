<script lang="ts">
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import type { Snippet } from 'svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { observeTimelineResizeAutoScroll } from '$lib/timeline-viewport';
  import type { TimelineItem } from '$lib/chat-v2/types';
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
    hasStreamingItems = undefined,
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
    onJumpToBottom = undefined,
    onJumpToActiveStart = undefined,
    autoScrollOnResize = true,
    testId = undefined,
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
    hasStreamingItems?: boolean | undefined;
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
    onJumpToBottom?: (() => void) | undefined;
    onJumpToActiveStart?: ((request: { scrollTop: number; rowKey: string | null }) => void) | undefined;
    autoScrollOnResize?: boolean;
    testId?: string | undefined;
    children?: Snippet;
  }>();

  let programmaticScroll = false;
  let lastScrollTop = 0;
  let wasStreaming = $state(false);
  let activeContextElement = $state<HTMLElement | null>(null);
  let activeContextScrollable = $state(false);
  let contextNavigationRequested = $state(false);
  const computedHasStreamingItems = $derived(
    items.some((item: TimelineItem) => (
      item.stable === false || item.status === 'running'
    ))
  );
  const effectiveHasStreamingItems = $derived(hasStreamingItems ?? computedHasStreamingItems);

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
      handleScroll(event);
      current.onScroll?.(event);
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

  function handleScroll(event: Event): void {
    if (!viewportElement) return;
    if (!event.isTrusted || !programmaticScroll) contextNavigationRequested = true;
    const currentScrollTop = viewportElement.scrollTop;
    const distanceFromBottom = viewportElement.scrollHeight - viewportElement.scrollTop - viewportElement.clientHeight;

    if (distanceFromBottom > 24) {
      userScrolledUp = true;
    } else {
      userScrolledUp = false;
      contextNavigationRequested = false;
    }

    lastScrollTop = currentScrollTop;
    updateActiveContext();
    if (currentScrollTop <= 24) onNearTop?.();
  }

  function updateActiveContext(): void {
    if (!viewportElement) return;
    const viewportRect = viewportElement.getBoundingClientRect();
    const probeY = viewportRect.top + Math.min(viewportRect.height * 0.35, 260);
    const candidates = Array.from(
      viewportElement.querySelectorAll(
        '[data-kind="message"][data-role="assistant"], [data-kind="assistant_deliverable"]',
      ) as NodeListOf<HTMLElement>
    ).filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.bottom > viewportRect.top && rect.top < viewportRect.bottom;
    });
    activeContextElement = candidates.find((element) => {
      const rect = element.getBoundingClientRect();
      return rect.top <= probeY && rect.bottom >= probeY;
    }) ?? candidates[0] ?? null;
    if (!activeContextElement) {
      activeContextScrollable = false;
      return;
    }
    activeContextScrollable =
      activeContextElement.getBoundingClientRect().height > viewportRect.height * 0.5
      || (activeContextElement.matches('[data-kind="message"]')
        && (activeContextElement.textContent?.length ?? 0) > 200);
  }

  function jumpToActiveStart(): void {
    if (!viewportElement || !activeContextElement) return;
    const viewportTop = viewportElement.getBoundingClientRect().top;
    const elementTop = activeContextElement.getBoundingClientRect().top;
    const row = activeContextElement.closest<HTMLElement>('[data-timeline-row-key]');
    const request = {
      scrollTop: Math.max(0, viewportElement.scrollTop + elementTop - viewportTop - 8),
      rowKey: row?.dataset.timelineRowKey ?? null,
    };
    if (onJumpToActiveStart) {
      onJumpToActiveStart(request);
      return;
    }
    // Standalone TimelineViewport consumers have no host scroll contract.
    userScrolledUp = true;
    viewportElement.scrollTop = request.scrollTop;
    lastScrollTop = viewportElement.scrollTop;
  }

  function jumpToBottom(): void {
    // When the page owns the render window (it slices the timeline and freezes
    // the tail while scrolled up), delegate so it can unfreeze the window and
    // set its own programmatic-scroll guards before scrolling. The local
    // fallback only scrolls the currently-rendered DOM, which may not reach the
    // true bottom when newer rows are windowed out.
    if (onJumpToBottom) {
      userScrolledUp = false;
      contextNavigationRequested = false;
      onJumpToBottom();
      return;
    }
    userScrolledUp = false;
    contextNavigationRequested = false;
    scrollToBottom(true);
  }

  $effect(() => {
    if (effectiveHasStreamingItems) {
      wasStreaming = true;
      return;
    }
    if (!wasStreaming) return;
    wasStreaming = false;
    // When the host owns pinning (autoScrollOnResize=false, e.g. the chat page
    // with its own ResizeObserver), do NOT add a second competing scroll driver
    // at streaming end — the two drivers fighting at settle produce a visible
    // flash. The host re-pins via its own observer.
    if (!autoScrollOnResize) return;
    if (userScrolledUp || !viewportElement) return;
    requestAnimationFrame(() => {
      if (!userScrolledUp) {
        scrollToBottom(true);
      }
    });
  });

  $effect(() => {
    return observeTimelineResizeAutoScroll({
      autoScrollOnResize,
      contentElement,
      viewportElement,
      scrollToBottom,
    }) ?? undefined;
  });

  $effect(() => {
    if (!viewportElement || !contentElement) return;
    const observer = new ResizeObserver(updateActiveContext);
    observer.observe(viewportElement);
    observer.observe(contentElement);
    const mobileQuery = typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 760px)')
      : null;
    mobileQuery?.addEventListener('change', updateActiveContext);
    requestAnimationFrame(updateActiveContext);
    return () => {
      observer.disconnect();
      mobileQuery?.removeEventListener('change', updateActiveContext);
    };
  });

</script>

<div
  class={className}
  role="region"
  aria-label="Timeline"
  data-testid={testId}
  bind:this={viewportElement}
  use:viewportEvents={{ onScroll, onWheel, onTouchStart, onTouchMove, onTouchEnd, onKeydown, onPointerDown }}
  tabindex="-1"
>
  <div
    bind:this={contentElement}
    class={contentClass}
    data-testid={testId ? `${testId}-content` : undefined}
  >
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
    {/if}
  </div>

  {#if userScrolledUp || (contextNavigationRequested && activeContextScrollable)}
    <nav
      class="sticky bottom-[max(.25rem,env(safe-area-inset-bottom))] left-1/2 z-10 flex w-fit max-w-[calc(100%-env(safe-area-inset-left)-env(safe-area-inset-right)-1rem)] -translate-x-1/2 items-center gap-1 rounded-full border border-slate-700 bg-slate-900/95 p-1 shadow-lg backdrop-blur"
      aria-label="Message navigation"
      data-testid={testId ? `${testId}-navigation-cluster` : undefined}
    >
      {#if activeContextScrollable}
        <button class="grid h-11 w-11 place-items-center rounded-full transition hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400" onclick={jumpToActiveStart} type="button" title="Jump to active message start" aria-label="Jump to active message start" data-testid={testId ? `${testId}-scroll-to-active-start` : undefined}>
          <ArrowUp class="h-4 w-4 text-slate-300" />
        </button>
      {/if}
      <button class="grid h-11 w-11 place-items-center rounded-full transition hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400" onclick={jumpToBottom} type="button" title={followPausedLabel} aria-label={followPausedLabel} data-testid={testId ? `${testId}-scroll-to-bottom` : undefined}>
        <ArrowDown class="h-4 w-4 text-slate-300" />
      </button>
    </nav>
  {:else if !loading && !error && live}
    <nav class="sticky bottom-2 left-1/2 z-10 flex w-fit -translate-x-1/2 items-center gap-1 rounded-full border border-slate-700 bg-slate-900/95 p-1 shadow-lg backdrop-blur" aria-label="Live tail controls">
      <LiveDots label={liveLabel} size="sm" />
    </nav>
  {/if}
</div>

<style>
  :global(body:has([data-testid="rich-deliverable-toc"] [role="dialog"])) [data-testid$="-navigation-cluster"] {
    visibility: hidden;
  }
</style>
