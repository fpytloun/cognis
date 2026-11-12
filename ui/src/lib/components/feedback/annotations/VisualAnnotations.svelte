<script lang="ts">
  import type { FeedbackLocator, FeedbackPoint, FeedbackShape, FeedbackSurfaceBinding } from '$lib/feedback/types';
  import { appendStroke, checkShapeBudget, clientPoint } from '$lib/feedback/annotations/geometry';
  import { onMount, untrack } from 'svelte';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { targetView } from '$lib/feedback/annotations/native-selection';
  import type { FeedbackTargetGeometry } from '$lib/feedback/types';
  let { binding, width = 1, height = 1, makeLocator, transformPoint = (point: FeedbackPoint) => point,
    highlights = [], displayShapes, showWhileViewing = false, adapterId }: {
    binding: FeedbackSurfaceBinding; width?: number; height?: number;
    makeLocator: (shapes: FeedbackShape[]) => FeedbackLocator;
    transformPoint?: (point: FeedbackPoint) => FeedbackPoint;
    highlights?: { thread_id: string; label: string; shapes: FeedbackShape[] }[];
    displayShapes?: (locator: FeedbackLocator) => FeedbackShape[];
    showWhileViewing?: boolean;
    adapterId?: string;
  } = $props();
  // Comment mode is one crosshair gesture: click places a pin, drag draws a freehand mark.
  const tool = 'freehand' as const;
  let shapes = $state<FeedbackShape[]>([]);
  let points = $state<FeedbackPoint[]>([]);
  let pointer: number | null = null;
  let error = $state('');
  let root = $state<HTMLDivElement>();
  const instance = crypto.randomUUID();
  const active = $derived(binding.source.capabilities.comment && binding.mode !== 'view');
  const automatic = $derived(active);
  function geometry(values: FeedbackShape[]): FeedbackTargetGeometry | null {
    if (!root) return null;
    const bounds = root.getBoundingClientRect();
    return { space: 'client', rects: values.map(shape => {
      const points = shape.kind === 'pin' ? [shape.point] : shape.kind === 'arrow' ? [shape.from, shape.to]
        : shape.kind === 'freehand' ? shape.points : [{ x: shape.x, y: shape.y }, { x: shape.x + shape.width, y: shape.y + shape.height }];
      const xs = points.map(point => point.x), ys = points.map(point => point.y);
      return { left: bounds.left + Math.min(...xs) / width * bounds.width, top: bounds.top + Math.min(...ys) / height * bounds.height,
        width: Math.max(4, (Math.max(...xs) - Math.min(...xs)) / width * bounds.width), height: Math.max(4, (Math.max(...ys) - Math.min(...ys)) / height * bounds.height) };
    }) };
  }
  export function targetGeometry(locator: FeedbackLocator) {
    const base = makeLocator([]);
    if (base.kind !== locator.kind || base.kind === 'pdf' && locator.kind === 'pdf' && base.page !== locator.page
      || locator.kind === 'html' && (locator.capture_id || locator.selector_path)) return null;
    if (JSON.stringify(makeLocator(shapes)) === JSON.stringify(locator)) return geometry(shapes);
    return geometry(displayShapes?.(locator) ?? ('shapes' in locator ? locator.shapes : []));
  }
  function publish() {
    if (!shapes.length) { binding.onTargetChange?.(null); return; }
    const snapshot = [...shapes];
    const label = snapshot.at(-1)?.kind === 'pin' ? 'Marked point' : 'Drawn mark';
    binding.onTargetChange?.(targetView(binding, adapterId ?? instance, { locator: makeLocator(snapshot) }, label, () => geometry(snapshot)));
  }
  function clear() { points = []; shapes = []; pointer = null; binding.onInteractionChange(false); }
  export function clearSelection() { clear(); }
  const identityKey = $derived(surfaceIdentity(binding));
  $effect(() => {
    identityKey;
    const current = untrack(() => binding);
    return () => untrack(() => current.onInteractionChange(false));
  });
  const interactionKey = $derived(`${identityKey}:${binding.mode}`);
  $effect(() => { interactionKey; untrack(clear); });
  function point(event: PointerEvent) {
    return transformPoint(clientPoint({ x: event.clientX, y: event.clientY }, root!.getBoundingClientRect()));
  }
  function add(shape: FeedbackShape) {
    checkShapeBudget(shapes.length + 1, [...shapes, shape].filter(value => value.kind === 'freehand').map(value => value.points.length));
    shapes = [...shapes, shape]; binding.onInteractionChange(true); publish();
  }
  function down(event: PointerEvent) {
    if (!active || pointer !== null || event.button !== 0) return;
    event.preventDefault(); event.stopPropagation();
    try {
      error = ''; points = [point(event)]; pointer = event.pointerId;
      root?.setPointerCapture(event.pointerId); binding.onInteractionChange(true);
    } catch (cause) { error = (cause as Error).message; }
  }
  function move(event: PointerEvent) {
    if (event.pointerId !== pointer) return;
    event.preventDefault(); event.stopPropagation();
    try { points = tool === 'freehand' ? appendStroke(points, point(event), { width, height }) : [points[0], point(event)]; }
    catch (cause) { error = (cause as Error).message; }
  }
  function up(event: PointerEvent) {
    if (pointer !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    try {
      if (!error) {
        const to = point(event);
        const xs = points.map(value => value.x), ys = points.map(value => value.y);
        const spread = Math.max((Math.max(...xs, to.x) - Math.min(...xs, to.x)) / width, (Math.max(...ys, to.y) - Math.min(...ys, to.y)) / height);
        // One gesture replaces the previous mark; a comment targets a single point or drawing.
        shapes = [];
        if (spread < 0.01 || points.length < 2) add({ kind: 'pin', point: to });
        else add({ kind: 'freehand', points });
      }
    } catch (cause) { error = (cause as Error).message; }
    finally { points = []; pointer = null; binding.onInteractionChange(shapes.length > 0); }
  }
  function cancel() { points = []; pointer = null; binding.onInteractionChange(shapes.length > 0); }
  let touch: { event: PointerEvent; timer: ReturnType<typeof setTimeout> } | null = null;
  function stopTouch() { if (touch) clearTimeout(touch.timer); touch = null; }
  function automaticDown(event: PointerEvent) {
    if (!automatic || !active || event.defaultPrevented || event.button !== 0) return;
    const target = event.target;
    if (!(target instanceof Element) || target.closest('button,a,input,textarea,select,[contenteditable],.textLayer span,[data-feedback-floating]')) return;
    // Comment mode disables panning on the surface (touch-action: none), so a finger draws
    // immediately like a mouse; a still tap still places a point.
    down(event);
  }
  onMount(() => {
    const parent = root?.parentElement;
    parent?.addEventListener('pointerdown', automaticDown);
    return () => { stopTouch(); parent?.removeEventListener('pointerdown', automaticDown); };
  });
  $effect(() => {
    const parent = root?.parentElement;
    if (!parent || !active) return;
    parent.style.cursor = 'crosshair'; parent.style.touchAction = 'none';
    return () => { parent.style.cursor = ''; parent.style.touchAction = ''; };
  });
  function pointerMove(event: PointerEvent) {
    if (touch && Math.hypot(event.clientX - touch.event.clientX, event.clientY - touch.event.clientY) > 8) stopTouch();
    move(event);
  }
  function path(shape: FeedbackShape): string {
    if (shape.kind === 'pin') return `M ${shape.point.x - width * 0.01} ${shape.point.y} h ${width * 0.02} M ${shape.point.x} ${shape.point.y - height * 0.01} v ${height * 0.02}`;
    if (shape.kind === 'arrow') {
      const angle = Math.atan2((shape.to.y - shape.from.y) / height, (shape.to.x - shape.from.x) / width);
      const wing = (delta: number) => `${shape.to.x - Math.cos(angle + delta) * width * 0.025} ${shape.to.y - Math.sin(angle + delta) * height * 0.025}`;
      return `M ${shape.from.x} ${shape.from.y} L ${shape.to.x} ${shape.to.y} M ${wing(-0.5)} L ${shape.to.x} ${shape.to.y} L ${wing(0.5)}`;
    }
    if (shape.kind === 'freehand') return shape.points.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' ');
    if (shape.kind === 'ellipse') return `M ${shape.x} ${shape.y + shape.height / 2} a ${shape.width / 2} ${shape.height / 2} 0 1 0 ${shape.width} 0 a ${shape.width / 2} ${shape.height / 2} 0 1 0 ${-shape.width} 0`;
    return `M ${shape.x} ${shape.y} h ${shape.width} v ${shape.height} h ${-shape.width} Z`;
  }
</script>
<svelte:window onpointermove={pointerMove} onpointerup={event => { stopTouch(); up(event); }} onpointercancel={() => { stopTouch(); cancel(); }} />
{#if active}
  <p class="annotation-hint pointer-events-none absolute left-1/2 top-2 z-20 -translate-x-1/2 rounded-full bg-accent px-3 py-1 text-xs font-medium text-on-accent shadow">Click to comment, drag to draw</p>
  <!-- Keyboard alternative to clicking: visually hidden until focused. -->
  <button type="button" class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded focus:bg-surface focus:px-2 focus:text-xs focus:text-foreground"
    onclick={() => { try { shapes = []; add({ kind: 'pin', point: transformPoint(clientPoint({ x: 50, y: 50 }, { left: 0, top: 0, width: 100, height: 100 })) }); } catch (cause) { error = (cause as Error).message; } }}>Add pin</button>
  {#if error}<p role="alert" class="absolute bottom-2 left-1/2 z-20 -translate-x-1/2 rounded bg-surface px-2 py-1 text-xs text-danger">{error}</p>{/if}
{/if}
<div bind:this={root} class="pointer-events-none absolute inset-0">
  <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" class="h-full w-full" aria-label="Annotation highlights">
    {#each [...(binding.mode === 'view' && !showWhileViewing ? [] : highlights.flatMap(value => value.shapes)), ...(active ? shapes : [])] as shape}
      <path d={path(shape)} fill="none" stroke="#eab308" stroke-width="3" vector-effect="non-scaling-stroke" />
    {/each}
    {#if points.length}<path d={path({ kind: 'freehand', points })} fill="none" stroke="#eab308" stroke-width="3" vector-effect="non-scaling-stroke" />{/if}
  </svg>
  {#if showWhileViewing}<div class="pointer-events-auto absolute right-0 top-0 flex flex-col">
    {#each highlights as value, index}<button type="button" class="min-h-11 min-w-11 bg-surface p-2 text-foreground" aria-label={`Comment ${index + 1}: ${value.label}`} onclick={() => binding.onSelectThread(value.thread_id)}>{index + 1}</button>{/each}
  </div>{/if}
</div>
<style>button { min-height: 44px; min-width: 44px; }</style>
