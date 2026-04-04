<script lang="ts">
  import { page } from '$app/stores';

  import { getEmbeddedDoc, getRelatedDocs } from '$lib/docs';
  import { renderMarkdown } from '$lib/markdown';
  import Card from '$lib/components/ui/Card.svelte';

  let slug = $derived($page.params.slug ?? '');
  let doc = $derived(getEmbeddedDoc(slug));
  let relatedDocs = $derived(doc ? getRelatedDocs(doc) : []);
  let html = $derived(doc ? renderMarkdown(doc.content) : '');
</script>

<svelte:head>
  <title>{doc ? `${doc.title} · Docs · Cognis` : 'Doc Not Found · Docs · Cognis'}</title>
</svelte:head>

{#if doc}
  <section class="space-y-6">
    <Card class="p-6">
      <a class="text-sm text-sky-300" href="/docs">Back to docs</a>
      <p class="mt-4 text-sm uppercase tracking-[0.25em] text-slate-400">Docs</p>
      <h1 class="mt-2 text-2xl font-semibold text-white">{doc.title}</h1>
      <p class="mt-3 max-w-3xl text-sm leading-6 text-slate-400">{doc.description}</p>
    </Card>

    <Card class="p-6">
      <div class="chat-markdown prose prose-invert max-w-none prose-headings:text-white prose-p:text-slate-300 prose-strong:text-white prose-li:text-slate-300 prose-code:text-sky-200 prose-pre:border prose-pre:border-slate-800 prose-pre:bg-slate-950/80 prose-table:text-slate-200">{@html html}</div>
    </Card>

    {#if relatedDocs.length > 0}
      <Card class="p-6">
        <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Related guides</p>
        <div class="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {#each relatedDocs as related}
            <a class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4 transition hover:border-sky-400/50 hover:bg-slate-950/90" href={`/docs/${related.slug}`}>
              <p class="font-medium text-white">{related.title}</p>
              <p class="mt-2 text-sm leading-6 text-slate-400">{related.description}</p>
            </a>
          {/each}
        </div>
      </Card>
    {/if}
  </section>
{:else}
  <Card class="p-6">
    <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Docs</p>
    <h1 class="mt-2 text-2xl font-semibold text-white">Document not found</h1>
    <p class="mt-3 max-w-2xl text-sm leading-6 text-slate-400">
      The requested guide is not part of the bundled Cognis documentation set.
    </p>
    <a class="mt-4 inline-flex text-sm text-sky-300" href="/docs">Open the docs hub</a>
  </Card>
{/if}
