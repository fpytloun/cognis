<script lang="ts">
  import { renderDocsMarkdown } from '$lib/markdown';
  import Card from '$lib/components/ui/Card.svelte';
  import { docsOverview, getCategoryLabel, getDocsByCategory } from '$lib/docs';

  const groups = getDocsByCategory();
  const overviewHtml = renderDocsMarkdown(docsOverview.content);
</script>

<svelte:head>
  <title>Docs · Cognis</title>
</svelte:head>

<section class="space-y-6">
  <Card class="p-4 sm:p-6">
    <p class="text-sm uppercase tracking-[0.25em] text-sky-300">Docs</p>
    <h1 class="mt-2 text-2xl font-semibold text-white">Embedded user guides</h1>
    <p class="mt-3 max-w-3xl text-sm leading-6 text-slate-400">
      Read setup and workspace guidance directly inside Cognis. These pages are bundled with the UI, so they stay available in self-hosted deployments without opening GitHub.
    </p>
  </Card>

  <Card class="p-4 sm:p-6">
    <div class="docs-markdown min-w-0 max-w-full overflow-x-hidden break-words prose prose-invert max-w-none prose-headings:text-white prose-p:text-slate-300 prose-strong:text-white prose-li:text-slate-300 prose-code:text-sky-200 prose-code:before:content-none prose-code:after:content-none prose-pre:border prose-pre:border-slate-800 prose-pre:bg-slate-950/80 prose-table:text-slate-200">{@html overviewHtml}</div>
  </Card>

  {#each groups as group}
    <section class="space-y-4">
      <div>
        <p class="text-xs uppercase tracking-[0.25em] text-slate-400">{getCategoryLabel(group.category)}</p>
      </div>
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {#each group.docs as doc}
          <a class="rounded-2xl border border-slate-800 bg-slate-950/70 p-5 transition hover:border-sky-400/50 hover:bg-slate-950/90" href={`/docs/${doc.slug}`}>
            <p class="font-medium text-white">{doc.title}</p>
            <p class="mt-3 text-sm leading-6 text-slate-400">{doc.description}</p>
          </a>
        {/each}
      </div>
    </section>
  {/each}
</section>
