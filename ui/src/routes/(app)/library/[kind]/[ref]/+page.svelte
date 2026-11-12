<script lang="ts">
  import { page } from '$app/state';
  import { api } from '$lib/api/client';
  import ResourceReader from '$lib/components/library/ResourceReader.svelte';
  import type { LibraryItem } from '$lib/library';
  let item = $state<LibraryItem | null>(null);
  let error = $state('');
  let retry = $state(0);
  const isWindowMode = $derived(page.params.kind === 'deliverable' && page.url.searchParams.get('window') === '1');
  $effect(() => {
    const { kind, ref } = page.params;
    void retry;
    let cancelled = false;
    item = null; error = '';
    if (!ref || (kind !== 'artifact' && kind !== 'deliverable')) { error = 'Unknown resource type.'; return; }
    void api.library.item(kind, ref).then((result) => {
      if (!cancelled) item = result;
    }).catch((caught) => { if (!cancelled) error = caught instanceof Error ? caught.message : 'Resource unavailable.'; });
    return () => { cancelled = true; };
  });
</script>

<svelte:head><title>{item?.title ?? 'Resource'} · Library · Cognis</title></svelte:head>
<section class="mx-auto flex h-full min-h-0 w-full max-w-[1500px] flex-col">
  {#if !isWindowMode}<a href="/library" class="inline-flex min-h-10 items-center text-sm text-accent hover:underline">← Library</a>{/if}
  {#if item}
    <div class="min-h-0 flex-1"><ResourceReader {item} /></div>
  {:else if error}
    <div role="alert" class="rounded-xl border border-separator p-6"><h1 class="text-lg text-foreground">Resource unavailable</h1><p class="mt-2 text-sm text-muted">{error}</p><button class="mt-4 min-h-11 text-sm text-accent" onclick={() => retry++}>Retry</button></div>
  {:else}<p role="status" class="py-10 text-sm text-muted">Loading resource…</p>{/if}
</section>
