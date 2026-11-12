<script lang="ts">
  import { dev } from '$app/environment';
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { auth } from '$lib/stores/auth';
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import FeedbackIndicator from '$lib/components/feedback/FeedbackIndicator.svelte';
  import FeedbackWorkspace from '$lib/components/feedback/FeedbackWorkspace.svelte';
  import TextAnnotations from '$lib/components/feedback/annotations/TextAnnotations.svelte';
  import RichBlockList from '$lib/components/rich/RichBlockList.svelte';
  import HtmlPreview from '$lib/components/HtmlPreview.svelte';
  import ImageAnnotations from '$lib/components/feedback/annotations/ImageAnnotations.svelte';
  let BottomTabBar = $state<typeof import('$lib/components/BottomTabBar.svelte').default>();
  let NativeFixture = $state<typeof import('$lib/components/feedback/FeedbackNativeFixture.svelte').default>();
  let PdfFixture = $state<typeof import('$lib/components/feedback/FeedbackPdfFixture.svelte').default>();
  import { documentCoordination } from '$lib/document-coordination';
  import { annotateRichPayload } from '$lib/feedback/annotations/rich';
  import type { RichBlock } from '$lib/rich-deliverable';
  import '$lib/components/rich/rich-blocks.css';
  let ready = $state(false);
  const ending = 'Repeat this sentence. Repeat this sentence. Native selection remains precise.';
  const text = $derived(page.url.searchParams.has('end') ? Array.from({ length: 35 }, (_, index) => `Source paragraph ${index + 1}.`).join('\n') + '\n' + ending : ending);
  const blocks = (annotateRichPayload({ blocks: [{ type: 'table', id: 'station-status', sortable: true,
    columns: ['station', 'count'], rows: [['Zulu', 3], { values: ['Alpha', 4] }, { station: 'Beta', count: 5 }] }] }) as { blocks: RichBlock[] }).blocks;
  const kind = $derived(page.url.searchParams.get('surface') ?? 'text');
  const shell = $derived(page.url.searchParams.has('shell'));
  const retained = $derived(page.url.searchParams.has('retained'));
  let suspended = $state(false);
  const html = '<h1>Observed source</h1><p>Repeated text. Repeated text.</p><table><tr><td>Alpha</td><td>4</td></tr></table><a href="#destination">Follow link</a><div style="height:900px"></div><p id="destination">Destination</p>';
  // Prune dev-only chrome before production chunk planning, not after import resolution.
  onMount(() => { if (import.meta.env.DEV && dev) void (async () => {
    BottomTabBar = (await import('$lib/components/BottomTabBar.svelte')).default;
    if (page.url.searchParams.has('native-layout')) NativeFixture = (await import('$lib/components/feedback/FeedbackNativeFixture.svelte')).default;
    if (page.url.searchParams.get('surface') === 'pdf') PdfFixture = (await import('$lib/components/feedback/FeedbackPdfFixture.svelte')).default;
    await auth.bootstrap(); ready = true;
  })(); });
</script>
{#if dev && ready}
  {#if PdfFixture}<PdfFixture />{:else if NativeFixture}<NativeFixture mode={page.url.searchParams.get('native-layout') ?? 'reader'} {shell} />{:else}
  <div class={shell ? 'app-shell-viewport fixed inset-0' : 'contents'}>
  <main class="mx-auto h-dvh max-w-6xl overflow-y-auto p-4 text-foreground" class:relative={shell} class:isolate={shell} class:z-10={shell} data-app-content={shell || undefined}>
    <p class="mb-3 text-xs text-muted">Synthetic local content · real application components · intercepted API fixtures</p>
    {#if retained}
      <button type="button" class="fixed right-3 top-3 z-[100] min-h-[44px] rounded bg-surface p-2" onclick={() => suspended = !suspended}>{suspended ? 'Restore retained source' : 'Suspend retained source'}</button>
      {#if suspended}<label>Child workstream input<input class="border p-2" /></label>{/if}
    {/if}
    <div class:contents={!suspended} class:hidden={suspended} inert={suspended ? true : undefined} aria-hidden={suspended ? 'true' : undefined} data-retained-source>
    <FeedbackHost source={{ kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' }}>
      <header class="mb-4 flex items-center justify-between"><h1>Source-first feedback fixture</h1><div><FeedbackIndicator /></div></header>
      <FeedbackWorkspace>
        <article class="min-w-0 flex-1 rounded border border-separator bg-surface p-4">
          {#if kind === 'rich'}<div class="rich-deliverable"><RichBlockList {blocks} /></div>
          {:else if kind === 'html'}<HtmlPreview source={{ text: html, guard: documentCoordination().capture() }} />
          {:else if kind === 'image'}<ImageAnnotations alt="Synthetic source" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='800' height='600'%3E%3Crect width='800' height='600' fill='%231e3a5f'/%3E%3Ctext x='80' y='180' fill='white' font-size='42'%3ESynthetic source%3C/text%3E%3C/svg%3E" />
          {:else}<FeedbackWorkspace><TextAnnotations {text}><p data-testid="native-text" class="whitespace-pre-wrap text-lg leading-8">{text}</p></TextAnnotations></FeedbackWorkspace>{/if}
        </article>
      </FeedbackWorkspace>
    </FeedbackHost>
    </div>
  </main>
  {#if shell && BottomTabBar}<BottomTabBar />{/if}
  </div>
  {/if}
{:else}<p>Development-only source-first feedback fixture.</p>{/if}
