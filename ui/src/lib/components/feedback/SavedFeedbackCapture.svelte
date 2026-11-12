<script lang="ts">
  import { untrack } from 'svelte';
  import VisualAnnotations from './annotations/VisualAnnotations.svelte';
  import type { FeedbackLocator, FeedbackSurfaceBinding } from '$lib/feedback/types';
  import { savedCaptureShapes } from '$lib/feedback/saved-capture';
  let { captureId, locator, threadId, binding, loadPixels }: {
    captureId: string; locator: Extract<FeedbackLocator, { kind: 'html' }>; threadId: string;
    binding: FeedbackSurfaceBinding | null; loadPixels: (id: string) => Promise<Blob>;
  } = $props();
  let url = $state('');
  let error = $state('');
  const viewBinding = $derived(binding ? { ...binding, mode: 'view' as const, onInteractionChange: () => {} } : null);
  const shapes = $derived(savedCaptureShapes(locator));
  $effect(() => {
    const id = captureId;
    void binding?.source.source_token;
    const load = untrack(() => loadPixels);
    let active = true;
    let ownedUrl = '';
    url = ''; error = '';
    void load(id).then(blob => {
      if (!active) return;
      if (!['image/png', 'image/webp'].includes(blob.type) || blob.size > 8 * 1024 * 1024) throw new Error('Saved observation is unavailable.');
      ownedUrl = URL.createObjectURL(blob);
      url = ownedUrl;
    }).catch(caught => { if (active) error = caught instanceof Error ? caught.message : 'Saved observation is unavailable.'; });
    return () => { active = false; if (ownedUrl) URL.revokeObjectURL(ownedUrl); };
  });
</script>
<figure class="my-2">
  <div class="relative">
    {#if url}<img src={url} alt="Saved observed screenshot associated with this comment" class="block h-auto w-full" />{/if}
    {#if url && viewBinding}<VisualAnnotations binding={viewBinding} makeLocator={() => locator} showWhileViewing
      highlights={[{ thread_id: threadId, label: 'Saved observation', shapes }]} />{/if}
  </div>
  {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
  <figcaption class="text-xs text-muted">Observed screenshot · Untrusted source evidence, not proof of layout or correctness.</figcaption>
</figure>
