<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api } from '$lib/api/client';
  import DocumentCommentCount from '$lib/components/DocumentCommentCount.svelte';
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { imageLightbox } from '$lib/components/ui/private-view-definitions';

  /**
   * Compact image gallery for Work: square tiles in a dense grid, one lightbox for all images so
   * swiping moves between them. Signed URLs resolve only for tiles near the viewport and for the
   * image being viewed (and its neighbours), so a long history never fires one request per image.
   */
  export interface WorkImage {
    artifact_id: string;
    filename: string;
    revision?: number | null;
    revisions?: number;
    updated_at?: string | null;
  }
  let { images }: { images: WorkImage[] } = $props();

  let urls = $state<Record<string, string>>({});
  const pending = new Map<string, Promise<void>>();
  let alive = true;
  onDestroy(() => { alive = false; });

  function resolve(id: string): Promise<void> {
    if (urls[id]) return Promise.resolve();
    const existing = pending.get(id);
    if (existing) return existing;
    const request = api.artifacts.signedUrl(id, 3600, 'download')
      .then((result) => { if (alive) urls = { ...urls, [id]: result.url }; })
      .catch(() => { /* the tile stays a placeholder; opening it retries */ })
      .finally(() => { pending.delete(id); });
    pending.set(id, request);
    return request;
  }

  function nearViewport(node: HTMLElement, id: string) {
    let current = id;
    if (typeof IntersectionObserver === 'undefined') { void resolve(current); return {}; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { void resolve(current); observer.disconnect(); }
    }, { rootMargin: '240px' });
    observer.observe(node);
    return {
      update(next: string) { current = next; if (!urls[next]) { observer.disconnect(); observer.observe(node); } },
      destroy() { observer.disconnect(); },
    };
  }

  let openIndex = $state<number | null>(null);
  function show(index: number) {
    openIndex = index;
    for (const offset of [0, 1, -1]) {
      const image = images[(index + offset + images.length) % images.length];
      if (image) void resolve(image.artifact_id);
    }
  }
  const lightboxImages = $derived(images.map((image) => ({
    src: urls[image.artifact_id] ?? '',
    alt: image.filename,
    filename: image.filename,
    artifact_id: image.artifact_id,
    revisions: image.revisions,
  })));
</script>

<ul class="grid grid-cols-3 gap-1.5 sm:grid-cols-4" aria-label="Images">
  {#each images as image, index (image.artifact_id)}
    <li>
      <button
        type="button"
        use:nearViewport={image.artifact_id}
        class="group relative block aspect-square w-full overflow-hidden rounded-lg border border-separator bg-inset focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        title={image.filename}
        aria-label={`View ${image.filename}`}
        onclick={() => show(index)}
      >
        {#if urls[image.artifact_id]}
          <img src={urls[image.artifact_id]} alt="" loading="lazy" decoding="async" class="h-full w-full object-cover" />
        {/if}
        {#if (image.revisions ?? 1) > 1 || (image.revision ?? 1) > 1}
          <span class="pointer-events-none absolute bottom-1 right-1 rounded bg-scrim/70 px-1 text-[10px] font-medium tabular-nums text-white">v{image.revision ?? 1}</span>
        {/if}
      </button>
      <div class="mt-1 flex justify-center"><DocumentCommentCount id={image.artifact_id} /></div>
    </li>
  {/each}
</ul>

{#if openIndex !== null && images[openIndex]}
  {@const current = images[openIndex]}
  <DeferredPrivateView definition={imageLightbox} label="image preview" modal onClose={() => { openIndex = null; }} viewProps={{
    src: urls[current.artifact_id] ?? '', alt: current.filename, filename: current.filename,
    images: lightboxImages, index: openIndex,
    onIndexChange: (next: number) => show(next), onClose: () => { openIndex = null; },
  }} />
{/if}
