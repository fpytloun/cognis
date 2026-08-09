<script lang="ts">
  import { onDestroy, tick, type Snippet } from 'svelte';
  import Maximize2 from 'lucide-svelte/icons/maximize-2';
  import Minimize2 from 'lucide-svelte/icons/minimize-2';
  import X from 'lucide-svelte/icons/x';

  import Sheet from '$lib/components/ui/Sheet.svelte';
  import {
    INSPECTOR_MAX_WIDTH,
    INSPECTOR_MIN_WIDTH,
    type ConversationInfoPresentation,
  } from '$lib/stores/conversationInfo.svelte';

  let {
    open,
    presentation,
    width,
    onWidthChange,
    onWidthCommit,
    onFocusChange,
    onClose,
    header,
    children
  }: {
    open: boolean;
    presentation: ConversationInfoPresentation;
    width: number;
    onWidthChange: (width: number) => void;
    onWidthCommit: (width: number) => void;
    onFocusChange: (focus: boolean) => void;
    onClose: () => void;
    header?: Snippet;
    children: Snippet;
  } = $props();

  let resizing = $state(false);
  let resizeFrame: number | null = null;
  let activeResizeCleanup: (() => void) | null = null;
  let activeResizeHandle: HTMLElement | null = null;
  let activePointerId: number | null = null;

  function cleanupResize(): void {
    if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
    resizeFrame = null;
    activeResizeCleanup?.();
    activeResizeCleanup = null;
    if (
      activeResizeHandle
      && activePointerId !== null
      && activeResizeHandle.hasPointerCapture?.(activePointerId)
    ) {
      activeResizeHandle.releasePointerCapture(activePointerId);
    }
    activeResizeHandle = null;
    activePointerId = null;
    resizing = false;
  }

  function startResize(event: PointerEvent): void {
    const handle = event.currentTarget as HTMLElement;
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startWidth = width;
    resizing = true;
    handle.setPointerCapture(pointerId);
    activeResizeHandle = handle;
    activePointerId = pointerId;
    const move = (next: PointerEvent): void => {
      if (next.pointerId !== pointerId) return;
      const candidate = startWidth + startX - next.clientX;
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => onWidthChange(candidate));
    };
    const stop = (next: PointerEvent): void => {
      if (next.pointerId !== pointerId) return;
      cleanupResize();
      onWidthCommit(startWidth + startX - next.clientX);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', stop);
    handle.addEventListener('pointercancel', stop);
    activeResizeCleanup = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', stop);
      handle.removeEventListener('pointercancel', stop);
    };
  }

  function toggleFocus(): void {
    const exiting = presentation === 'focus';
    onFocusChange(!exiting);
    if (exiting) {
      void tick().then(() => requestAnimationFrame(() => {
        document.querySelector<HTMLButtonElement>(
          '#conversation-info-drawer button[aria-label="Expand inspector"]',
        )?.focus();
      }));
    }
  }

  onDestroy(cleanupResize);

  function resizeWithKeyboard(event: KeyboardEvent): void {
    const step = event.shiftKey ? 40 : 10;
    let next = width;
    if (event.key === 'ArrowLeft') next += step;
    else if (event.key === 'ArrowRight') next -= step;
    else if (event.key === 'Home') next = INSPECTOR_MIN_WIDTH;
    else if (event.key === 'End') next = INSPECTOR_MAX_WIDTH;
    else return;
    event.preventDefault();
    onWidthCommit(next);
  }
</script>

<svelte:window onkeydown={(event) => {
  if (!open || presentation !== 'pinned' || event.key !== 'Escape') return;
  event.preventDefault();
  onClose();
}} />

{#snippet panelHeader()}
  <div class="flex min-w-0 flex-1 items-center gap-2" data-testid="conversation-info-header">
    {#if header}
      {@render header()}
    {:else}
      <h2 id="conversation-info-heading" class="text-sm font-semibold text-slate-100">Context</h2>
    {/if}
    <div class="ml-auto flex shrink-0 items-center gap-1">
      {#if presentation === 'pinned' || presentation === 'focus'}
      <button
        class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800 hover:text-white"
        type="button"
        aria-label={presentation === 'focus' ? 'Exit expanded inspector' : 'Expand inspector'}
        title={presentation === 'focus' ? 'Exit expanded view' : 'Expand'}
        onclick={toggleFocus}
      >
        {#if presentation === 'focus'}<Minimize2 class="h-4 w-4" />{:else}<Maximize2 class="h-4 w-4" />{/if}
      </button>
      {/if}
      {#if presentation === 'overlay'}
      <button
        class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800 hover:text-white"
        type="button"
        aria-label="Close conversation information"
        onclick={onClose}
      ><X class="h-4 w-4" /></button>
      {/if}
    </div>
  </div>
{/snippet}

{#if presentation === 'overlay' || presentation === 'focus'}
  <Sheet
    {open}
    {onClose}
    side="right"
    label="Conversation information"
    panelId="conversation-info-drawer"
    restoreFocusOnClose={false}
    class={presentation === 'focus'
      ? 'app-fullscreen-safe w-screen max-w-none rounded-none border-0'
      : 'w-[min(30rem,calc(100vw-1rem))] rounded-l-[1.25rem] border border-slate-800/80 bg-slate-900/95 shadow-card backdrop-blur'}
  >
    {#snippet header()}{@render panelHeader()}{/snippet}
    {@render children()}
  </Sheet>
{:else if presentation === 'pinned'}
  <aside
    id="conversation-info-drawer"
    class={`relative col-start-2 row-span-3 row-start-1 flex min-h-0 min-w-0 flex-col border-l border-slate-800/60 bg-transparent ${resizing ? 'select-none' : ''}`}
    style={`width:${width}px`}
    aria-labelledby="conversation-info-heading"
    data-testid="conversation-info-drawer"
  >
    <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
    <div
      role="separator"
      tabindex="0"
      aria-label="Resize conversation inspector"
      aria-orientation="vertical"
      aria-valuemin={INSPECTOR_MIN_WIDTH}
      aria-valuemax={INSPECTOR_MAX_WIDTH}
      aria-valuenow={width}
      aria-valuetext={`${width} pixels wide`}
      class="absolute -left-1 top-0 z-20 h-full w-2 cursor-col-resize touch-none focus-visible:bg-sky-400/40"
      onpointerdown={startResize}
      onkeydown={resizeWithKeyboard}
      data-testid="conversation-info-resizer"
    ></div>
    <div class="flex min-w-0 shrink-0 items-center border-b border-slate-800/80 px-3 py-2">
      {@render panelHeader()}
    </div>
    <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-4 pt-6">
      {@render children()}
    </div>
  </aside>
{/if}
