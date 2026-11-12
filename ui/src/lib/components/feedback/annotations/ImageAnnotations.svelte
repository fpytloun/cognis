<script lang="ts">
  import { getFeedbackSurface } from '$lib/feedback/context';
  import VisualAnnotations from './VisualAnnotations.svelte';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';
  let { src, alt = 'Image' }: { src: string; alt?: string } = $props();
  const surface = getFeedbackSurface();
  const binding = $derived(surface?.binding);
  let image = $state<HTMLImageElement>();
  let width = $state(0), height = $state(0);
  let markup = $state<{ clearSelection: () => void; targetGeometry: (locator: import('$lib/feedback/types').FeedbackLocator) => import('$lib/feedback/types').FeedbackTargetGeometry | null }>();
  // A cached image can finish loading before the load listener runs, which left the drawing
  // layer unmounted. Read the size from the element whenever it is already complete.
  function measure() {
    width = image?.complete ? image.naturalWidth : 0;
    height = image?.complete ? image.naturalHeight : 0;
  }
  $effect(() => { src; measure(); });
  const instance = crypto.randomUUID();
  const highlights = $derived(binding?.highlights.filter(h => h.locator.kind === 'image' && h.category !== 'not_located')
    .map(h => ({ ...h, shapes: h.locator.kind === 'image' ? h.locator.shapes : [] })) ?? []);
  $effect(() => {
    if (!binding) return;
    return binding.registerAdapter(instance, {
      clearSelection: () => markup?.clearSelection(),
      targetGeometry: anchor => anchor.kind === 'image' ? markup?.targetGeometry(anchor) ?? null : null,
      exposeTarget: (anchor, visible) => exposeGeometry(image?.parentElement ?? undefined, () => anchor.kind === 'image' ? markup?.targetGeometry(anchor) ?? null : null, visible),
      locate(anchor) {
        if (anchor.kind !== 'image' || anchor.intrinsic_width !== width || anchor.intrinsic_height !== height) return { status: 'not_located' };
        image?.scrollIntoView({ block: 'center' }); image?.focus(); return { status: 'exact' };
      },
    });
  });
</script>
<div class="relative mx-auto w-fit max-w-full">
  <div class="relative">
    <img bind:this={image} {src} {alt} draggable="false" tabindex="-1" class="block max-h-[75vh] max-w-full object-contain"
      onload={measure} />
    {#if binding && width && height && binding.supported_locator_kinds.includes('image')}
      {#key src}<VisualAnnotations bind:this={markup} adapterId={instance} {binding} {highlights} makeLocator={shapes => ({ kind: 'image', intrinsic_width: width, intrinsic_height: height, orientation: 1, shapes })} />{/key}
    {/if}
  </div>
</div>
