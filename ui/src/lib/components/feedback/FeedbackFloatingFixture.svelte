<script lang="ts">
  import { page } from '$app/state';
  import FeedbackHost from './FeedbackHost.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import FeedbackWorkspace from './FeedbackWorkspace.svelte';
  import Source from './FeedbackFloatingFixtureSource.svelte';
  const text = 'Readable native source with syntax colors and overlapping comments.';
  const diff = 'diff --git a/report.txt b/report.txt\n--- a/report.txt\n+++ b/report.txt\n@@ -1,4 +1,4 @@\n first line\n-old value\n+new value\n third line\n fourth line\n';
  const kind = $derived(page.url.searchParams.get('surface') ?? 'text');
  let mounted = $state(true);
</script>
<main class="h-dvh overflow-auto bg-background p-6 text-foreground" data-app-content>
  <header class="mb-4 flex items-center gap-4"><h1>Shared floating feedback fixture</h1>
    <button type="button" class="min-h-11 border px-3" onclick={() => mounted = !mounted}>{mounted ? 'Unmount source' : 'Mount source'}</button>
  </header>
  {#if mounted}
    <div class="mx-auto max-w-5xl border border-separator" data-source-frame>
      <FeedbackHost source={{ kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' }}>
        <header class="flex min-h-16 items-center justify-between border-b border-separator px-4"><h2>Retained source</h2><FeedbackIndicator /></header>
        <FeedbackWorkspace><Source {kind} {text} {diff} /></FeedbackWorkspace>
      </FeedbackHost>
    </div>
  {/if}
  <div class="h-[800px]" aria-hidden="true"></div>
</main>
