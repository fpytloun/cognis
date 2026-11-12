<script lang="ts">
  import type { FeedbackAnchorCandidate, FeedbackSurfaceBinding, FeedbackViewport, FeedbackLocator, FeedbackTargetGeometry } from '$lib/feedback/types';
  import { captureScreen, cropCapture, rasterDimensions } from '$lib/feedback/annotations/capture';
  import { bytesDigest, textDigest } from '$lib/feedback/annotations/digest';
  import VisualAnnotations from './VisualAnnotations.svelte';
  import { mapShapes } from '$lib/feedback/annotations/shapes';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { untrack } from 'svelte';
  let { binding, viewport, adapterId }: { binding: FeedbackSurfaceBinding; viewport: FeedbackViewport; adapterId?: string } = $props();
  let markup = $state<{ targetGeometry: (anchor: FeedbackLocator) => FeedbackTargetGeometry | null }>();
  export function targetGeometry(anchor: FeedbackLocator) { return markup?.targetGeometry(anchor) ?? null; }
  let blob = $state<Blob | null>(null), frozen = $state<Blob | null>(null);
  let url = $state(''), error = $state(''), busy = $state(false), unverified = $state(false);
  let width = $state(0), height = $state(0);
  let x = $state(0), y = $state(0), cropWidth = $state(1), cropHeight = $state(1);
  let stateDigest = $state('');
  let capturedViewport = $state<FeedbackViewport>({ width_css_px: 1, height_css_px: 1, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: 1 });
  let controller = new AbortController();
  let operation = 0;
  function discard(current: FeedbackSurfaceBinding = binding) {
    operation++; controller.abort(); controller = new AbortController();
    blob = null; frozen = null; busy = false; error = ''; confirmedViewport = false;
    current.onInteractionChange(false);
  }
  let confirmedViewport = $state(false);
  const identityKey = $derived(surfaceIdentity(binding));
  $effect(() => {
    identityKey;
    const current = untrack(() => binding);
    untrack(() => discard(current));
    return () => discard(current);
  });
  $effect(() => {
    const value = frozen ?? blob;
    if (!value) { url = ''; return; }
    const next = URL.createObjectURL(value); url = next;
    return () => URL.revokeObjectURL(next);
  });
  async function acquire(value: Promise<Blob>, userProvided: boolean) {
    const token = surfaceIdentity(binding), generation = ++operation;
    busy = true; error = ''; binding.onInteractionChange(true);
    try {
      const result = await value, size = await rasterDimensions(result);
      if (token !== surfaceIdentity(binding) || generation !== operation) return;
      blob = result; frozen = null; width = size.width; height = size.height;
      cropWidth = width; cropHeight = height; x = 0; y = 0; unverified = userProvided;
      capturedViewport = { ...viewport };
      confirmedViewport = false;
    } catch (cause) { if (generation === operation) error = `${(cause as Error).message} Choose or paste a PNG or WebP screenshot instead, or return to Comment mode for element/text feedback.`; }
    finally { if (generation === operation) { busy = false; binding.onInteractionChange(!!blob || !!frozen); } }
  }
  async function confirm() {
    if (!blob || !confirmedViewport) return;
    const signal = controller.signal, token = surfaceIdentity(binding), generation = ++operation;
    const crop = { x, y, width: cropWidth, height: cropHeight };
    busy = true;
    try {
      const value = await cropCapture(blob, crop, signal);
      const pixels = await bytesDigest(await value.arrayBuffer());
      const digest = await textDigest(`${binding.source.content_digest}:${JSON.stringify(capturedViewport)}:${unverified ? 'user-provided' : 'screen-capture'}:${JSON.stringify(crop)}:${pixels}`);
      if (signal.aborted || token !== surfaceIdentity(binding) || generation !== operation) return;
      frozen = value; blob = null; width = crop.width; height = crop.height; stateDigest = digest;
    } catch (cause) { if (generation === operation) error = (cause as Error).message; }
    finally { if (generation === operation) busy = false; }
  }
  function candidate(value: FeedbackAnchorCandidate) {
    if (!frozen) return;
    binding.onAnchorCandidate({ ...value, capture: { blob: frozen, width, height, viewport: capturedViewport, render_state_digest: stateDigest, method: 'observed_pixels' } });
  }
  const captureBinding = $derived({ ...binding, onAnchorCandidate: candidate, onTargetChange: (target: import('$lib/feedback/types').FeedbackTargetView | null) => {
    if (!target || !frozen) { binding.onTargetChange?.(null); return; }
    binding.onTargetChange?.({ ...target, candidate: { ...target.candidate, capture: { blob: frozen, width, height, viewport: capturedViewport, render_state_digest: stateDigest, method: 'observed_pixels' } } });
  } });
</script>
<div class="border border-separator p-3 text-sm text-foreground">
  <p>Capture the observed HTML view. Crop out private application chrome before confirming. Pixels are untrusted user observations, not authenticated render evidence.</p>
  <button class="min-h-11 px-3" disabled={busy} onclick={() => void acquire(captureScreen(controller.signal), false)}>Capture current view</button>
  <button class="min-h-11 px-3" onclick={() => discard()}>Discard local capture</button>
  <button class="min-h-11 px-3" onclick={() => binding.onAnchorCandidate({ locator: { kind: 'whole' } })}>Comment on whole source</button>
  <label class="inline-block min-h-11 p-2">Choose PNG/WebP screenshot
    <input type="file" accept="image/png,image/webp" disabled={busy} onchange={event => { const file = event.currentTarget.files?.[0]; if (file) void acquire(Promise.resolve(file), true); event.currentTarget.value = ''; }} />
  </label>
  <div role="textbox" aria-readonly="true" aria-label="Paste screenshot" tabindex="0" class="border border-separator p-2" onpaste={event => {
    if (busy) { event.preventDefault(); return; }
    const file = [...(event.clipboardData?.files ?? [])].find(file => ['image/png', 'image/webp'].includes(file.type));
    if (file) { event.preventDefault(); void acquire(Promise.resolve(file), true); }
  }}>Focus here to paste a PNG/WebP screenshot.</div>
  {#if error}<p role="alert" class="text-danger">{error}</p>{/if}
  {#if blob}
    <img src={url} alt="Unconfirmed screenshot, local only" class="max-h-80 max-w-full" />
    <p>{unverified ? 'Unverified user-provided screenshot' : 'User-selected screen/tab capture'} — crop and confirm the intended artifact view.</p>
    <label>Left <input type="number" bind:value={x} min="0" max={width} /></label>
    <label>Top <input type="number" bind:value={y} min="0" max={height} /></label>
    <label>Width <input type="number" bind:value={cropWidth} min="1" max={width} /></label>
    <label>Height <input type="number" bind:value={cropHeight} min="1" max={height} /></label>
    <label class="block"><input type="checkbox" bind:checked={confirmedViewport} /> The crop shows the entire intended artifact viewport, excluding private application chrome.</label>
    <button class="min-h-11 px-3" disabled={busy || !confirmedViewport} onclick={() => void confirm()}>Confirm crop and freeze</button>
  {/if}
  {#if frozen}
    <p>Frozen confirmed pixels. Markup refers to this captured view, not the live HTML.</p>
    <p>{unverified ? 'Unverified user-provided screenshot. Its authenticity is not asserted.' : 'User-confirmed browser capture. Its authenticity is not asserted.'}</p>
    <div class="pb-24"><div class="relative w-fit">
      <img src={url} alt="Confirmed frozen HTML observation" class="max-h-[70vh] max-w-full" />
      <VisualAnnotations bind:this={markup} {adapterId} binding={captureBinding}
        makeLocator={shapes => ({ kind: 'html', element_key: null, selector_path: null, range: null, viewport: capturedViewport, render_state_digest: stateDigest,
          shapes: mapShapes(shapes, point => ({ x: point.x * capturedViewport.width_css_px + capturedViewport.scroll_x_css_px, y: point.y * capturedViewport.height_css_px + capturedViewport.scroll_y_css_px })), capture_id: null })} />
    </div></div>
  {/if}
</div>
