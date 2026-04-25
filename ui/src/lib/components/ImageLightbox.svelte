<script lang="ts">
  import { onMount } from 'svelte';

  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import ChevronLeft from 'lucide-svelte/icons/chevron-left';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import Download from 'lucide-svelte/icons/download';
  import X from 'lucide-svelte/icons/x';

  /**
   * Full-screen image lightbox.
   *
   * - Click the backdrop or the close button to dismiss.
   * - Press Escape to dismiss.
   * - The download button triggers a real save (uses an ``<a download>``
   *   so the user gets the actual filename, not a hashed artifact id).
   * - Locks the body scroll while open so touchmove on the backdrop on
   *   iOS Safari doesn't rubber-band the page behind the lightbox.
   *   Without the lock the user can drag the page underneath, and the
   *   backdrop-blur layer appears to shift / lose its fixed position,
   *   and the toolbar scrolls out of the visible area.
   * - Toolbar is absolutely positioned inside the fixed viewport with
   *   safe-area padding so it always lands below the Dynamic Island
   *   and stays visible regardless of the image's aspect ratio.
   */

  type LightboxImage = {
    src: string;
    alt?: string;
    filename?: string | null;
  };

  let { src, alt = 'Image', filename = null, images = null, index = 0, onIndexChange = undefined, onClose } = $props<{
    src: string;
    alt?: string;
    filename?: string | null;
    images?: LightboxImage[] | null;
    index?: number;
    onIndexChange?: (index: number) => void;
    onClose: () => void;
  }>();

  let overlayId = $state<string | null>(null);
  let currentIndex = $state(0);
  let scale = $state(1);
  let translateX = $state(0);
  let translateY = $state(0);
  let dragging = $state(false);

  type Point = { x: number; y: number };

  const pointers = new Map<number, Point>();
  let gestureMoved = false;
  let singleStart: Point | null = null;
  let dragStartTranslateX = 0;
  let dragStartTranslateY = 0;
  let pinchStartDistance = 0;
  let pinchStartScale = 1;
  let pinchStartMidpoint: Point = { x: 0, y: 0 };
  let pinchStartTranslateX = 0;
  let pinchStartTranslateY = 0;

  const items = $derived.by(() => (images && images.length > 0 ? images : [{ src, alt, filename }]));
  const active = $derived(items[Math.min(currentIndex, items.length - 1)] ?? { src, alt, filename });
  const canNavigate = $derived(items.length > 1);

  $effect(() => {
    if (index >= 0 && index < items.length && index !== currentIndex) {
      currentIndex = index;
      resetTransform();
    } else if (currentIndex >= items.length) {
      currentIndex = Math.max(0, items.length - 1);
      resetTransform();
    }
  });

  onMount(() => {
    const handle = registerOverlay({ kind: 'fullscreen', blocksChrome: true });
    overlayId = handle.id;
    return () => {
      handle.unregister();
      overlayId = null;
    };
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (canNavigate && event.key === 'ArrowLeft') {
      event.preventDefault();
      navigate(-1);
      return;
    }
    if (canNavigate && event.key === 'ArrowRight') {
      event.preventDefault();
      navigate(1);
    }
  }

  function clamp(value: number, min: number, max: number): number {
    return Math.min(max, Math.max(min, value));
  }

  function distance(a: Point, b: Point): number {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function midpoint(a: Point, b: Point): Point {
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }

  function resetTransform(): void {
    scale = 1;
    translateX = 0;
    translateY = 0;
  }

  function navigate(delta: number): void {
    if (!canNavigate) return;
    const next = (currentIndex + delta + items.length) % items.length;
    currentIndex = next;
    onIndexChange?.(next);
    resetTransform();
  }

  function beginPinch(): void {
    const [first, second] = Array.from(pointers.values());
    if (!first || !second) return;
    pinchStartDistance = Math.max(1, distance(first, second));
    pinchStartScale = scale;
    pinchStartMidpoint = midpoint(first, second);
    pinchStartTranslateX = translateX;
    pinchStartTranslateY = translateY;
  }

  function handlePointerDown(event: PointerEvent): void {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    dragging = true;
    gestureMoved = false;

    if (pointers.size === 1) {
      singleStart = { x: event.clientX, y: event.clientY };
      dragStartTranslateX = translateX;
      dragStartTranslateY = translateY;
    } else if (pointers.size === 2) {
      beginPinch();
    }
  }

  function handlePointerMove(event: PointerEvent): void {
    if (!pointers.has(event.pointerId)) return;
    event.preventDefault();
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size >= 2) {
      const [first, second] = Array.from(pointers.values());
      if (!first || !second) return;
      const nextMidpoint = midpoint(first, second);
      const nextScale = clamp(pinchStartScale * (distance(first, second) / pinchStartDistance), 1, 5);
      scale = nextScale;
      translateX = pinchStartTranslateX + (nextMidpoint.x - pinchStartMidpoint.x);
      translateY = pinchStartTranslateY + (nextMidpoint.y - pinchStartMidpoint.y);
      gestureMoved = true;
      return;
    }

    if (!singleStart) return;
    const dx = event.clientX - singleStart.x;
    const dy = event.clientY - singleStart.y;
    if (Math.hypot(dx, dy) > 6) gestureMoved = true;

    if (scale > 1) {
      translateX = dragStartTranslateX + dx;
      translateY = dragStartTranslateY + dy;
    } else if (canNavigate && Math.abs(dx) > Math.abs(dy)) {
      translateX = dx * 0.28;
    }
  }

  function handlePointerUp(event: PointerEvent): void {
    const point = pointers.has(event.pointerId) ? { x: event.clientX, y: event.clientY } : null;
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      /* ignore */
    }

    if (point && singleStart && pointers.size === 1 && scale <= 1.02) {
      const dx = point.x - singleStart.x;
      const dy = point.y - singleStart.y;
      if (canNavigate && Math.abs(dx) > 64 && Math.abs(dx) > Math.abs(dy) * 1.25) {
        navigate(dx < 0 ? 1 : -1);
      } else {
        resetTransform();
      }
    }

    pointers.delete(event.pointerId);
    if (scale <= 1.02) resetTransform();

    if (pointers.size === 1) {
      const remaining = Array.from(pointers.values())[0];
      singleStart = remaining ? { ...remaining } : null;
      dragStartTranslateX = translateX;
      dragStartTranslateY = translateY;
    } else {
      singleStart = null;
      dragging = false;
    }
  }

  function handleStageClick(event: MouseEvent): void {
    if (gestureMoved) return;
    if (event.target === event.currentTarget) onClose();
  }

  function handleDoubleClick(event: MouseEvent): void {
    event.stopPropagation();
    if (scale > 1) {
      resetTransform();
      return;
    }
    scale = 2;
    translateX = 0;
    translateY = 0;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_interactive_supports_focus -->
<div
  role="dialog"
  aria-modal="true"
  aria-label={active.filename ?? active.alt ?? 'Image'}
  tabindex="-1"
  class="app-viewport-overlay z-[95] overflow-hidden overscroll-contain touch-none bg-slate-950/95"
>
  <!-- Image area. Centered, fills the viewport. Tapping the image
       does not close the lightbox; tapping the surrounding backdrop
       does. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    class="absolute inset-0 flex items-center justify-center px-3 pb-4 pt-20 sm:px-4 sm:pt-24"
    onclick={handleStageClick}
    onpointerdown={handlePointerDown}
    onpointermove={handlePointerMove}
    onpointerup={handlePointerUp}
    onpointercancel={handlePointerUp}
    ondblclick={handleDoubleClick}
  >
    <img
      src={active.src}
      alt={active.alt ?? 'Image'}
      class="max-h-full max-w-full select-none rounded-2xl object-contain shadow-2xl"
      draggable="false"
      style={`transform: translate3d(${translateX}px, ${translateY}px, 0) scale(${scale}); transition: ${dragging ? 'none' : 'transform 180ms cubic-bezier(.32,.72,0,1)'};`}
      onclick={(event) => event.stopPropagation()}
    />
  </div>

  {#if canNavigate}
    <button
      type="button"
      aria-label="Previous image"
      class="absolute left-2 top-1/2 hidden h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-slate-950/70 text-slate-100 shadow-lg backdrop-blur transition hover:bg-slate-800/90 sm:inline-flex"
      onclick={(event) => { event.stopPropagation(); navigate(-1); }}
    >
      <ChevronLeft class="h-6 w-6" />
    </button>
    <button
      type="button"
      aria-label="Next image"
      class="absolute right-2 top-1/2 hidden h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-slate-950/70 text-slate-100 shadow-lg backdrop-blur transition hover:bg-slate-800/90 sm:inline-flex"
      onclick={(event) => { event.stopPropagation(); navigate(1); }}
    >
      <ChevronRight class="h-6 w-6" />
    </button>
  {/if}

  <!--
    Toolbar. Absolutely positioned inside the fixed lightbox so the
    surrounding image flex layout can never push it out of view, and
    it carries its own solid background so it's always readable
    against any image colour. The parent overlay already respects the
    shared app-shell offsets, so the close button stays below the
    mobile header and above the bottom tab bar.
  -->
  <div
    class="absolute inset-x-0 top-0 flex items-center justify-between gap-2 bg-slate-950/85 px-3 py-3 shadow-lg backdrop-blur sm:px-4 sm:py-4"
    onclick={(event) => event.stopPropagation()}
  >
    <p class="min-w-0 flex-1 truncate text-sm text-slate-200">
      {active.filename ?? active.alt ?? 'Image'}{#if canNavigate}<span class="ml-2 text-slate-500">{currentIndex + 1}/{items.length}</span>{/if}
    </p>
    <div class="flex shrink-0 items-center gap-2">
      <a
        aria-label="Download"
        title="Download"
        href={active.src}
        download={active.filename ?? ''}
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-800/80 text-slate-100 transition hover:bg-slate-700"
      >
        <Download class="h-5 w-5" />
      </a>
      <button
        aria-label="Close"
        title="Close"
        type="button"
        onclick={onClose}
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-800/80 text-slate-100 transition hover:bg-slate-700"
      >
        <X class="h-5 w-5" />
      </button>
    </div>
  </div>
</div>
