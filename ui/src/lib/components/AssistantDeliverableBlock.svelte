<script lang="ts">
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { extractMarkdownHeadings } from '$lib/markdown';
  import { privateDeliverableMediaUrl } from '$lib/rich-deliverable';
  import type { AssistantDeliverableTimelineItem as RenderAssistantDeliverableTimelineItem } from '$lib/timeline-render-model';
  import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
  import type { Deliverable } from '$lib/types/api';

  // Matches the threshold the old bespoke markdown card used before every
  // format was unified onto RichDeliverable.
  const MIN_HEADINGS_FOR_TOC = 3;

  export let item: AssistantDeliverableTimelineItem | RenderAssistantDeliverableTimelineItem;
  export let loadDeliverable: ((deliverableId: string) => Promise<Deliverable>) | undefined = undefined;

  let deliverable: Deliverable | null = null;
  let loading = true;
  let error: string | null = null;
  let accessorConversationId = '';
  $: deliverableId = 'deliverable_id' in item ? item.deliverable_id : item.deliverableId;

  $: deliverableFormat = deliverable?.format ?? item.format ?? 'markdown';
  $: deliverableTitle = deliverable?.title ?? item.title ?? 'Deliverable';
  $: standaloneUrl = deliverable ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/view` : '';
  $: pdfUrl = deliverable ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/download.pdf` : '';
  // Every deliverable format renders through RichDeliverable now, giving
  // markdown/plain/html the same unified toolbar (TOC, full-view, share,
  // copy, PDF) and chrome that rich payloads already have -- previously
  // these formats rendered through a separate, far more limited
  // `assistant-deliverable-card` with only "Open standalone page"/
  // "Download PDF"/"Copy share link" actions, no TOC beyond a flat
  // heading list, and no full-view. `rich` payloads pass through as-is;
  // every other format is wrapped as a single block so the existing block
  // renderers do the work: `markdown` already renders full markdown
  // (headings, tables, code with syntax highlighting, links) via
  // MarkdownBlock; `plain` uses the `code` block so literal text is never
  // misinterpreted as markdown; `html` uses the `raw_html` block, a
  // client-only synthetic type (see RawHtmlBlock.svelte) that sanitizes
  // and renders already-HTML content directly instead of running it
  // through the markdown parser, which would otherwise escape it.
  // A single wrapped `markdown` block only ever contributes ONE top-level
  // (level 2) TOC entry -- its own title, taken from the first heading --
  // with every other `#`/`##`/`###` heading nested one level deeper. The
  // shared `isSubstantialDocument()` auto-TOC heuristic gates on
  // `topLevelHeadings >= 4`, which a single block can never satisfy
  // regardless of how many internal headings it has, so a markdown
  // deliverable would silently lose the TOC the old bespoke card used to
  // show. Detect real heading count directly and force `metadata.toc`
  // on/off explicitly (the same threshold the old card used) rather than
  // relying on the generic multi-block heuristic here.
  $: markdownHeadingCount = deliverableFormat === 'markdown' && deliverable
    ? extractMarkdownHeadings(deliverable.content).length
    : 0;
  $: richPayload = deliverable?.format === 'rich' && deliverable.rich_payload
    ? deliverable.rich_payload
    : deliverable
      ? {
          // `toc: true` alone would only enable the TOC at the default
          // depth (2), which is exactly the depth of the wrapped
          // markdown block's own single entry -- nested heading
          // extraction requires `depth > level` (see buildTocItems),
          // so an explicit depth is required for the block's internal
          // `#`/`##`/`###` headings to actually appear as TOC items.
          metadata: deliverableFormat === 'markdown'
            ? { toc: { enabled: markdownHeadingCount >= MIN_HEADINGS_FOR_TOC, depth: 4 } }
            : {},
          blocks: [
            {
              type: deliverableFormat === 'plain' ? 'code' : deliverableFormat === 'html' ? 'raw_html' : 'markdown',
              content: deliverable.content,
            },
          ],
        }
      : null;

  function authenticatedMediaUrlFor(mediaKey: string): string {
    if (!deliverable) return '';
    return privateDeliverableMediaUrl(deliverable.deliverable_id, mediaKey, accessorConversationId);
  }

  function currentConversationId(): string {
    const match = typeof window !== 'undefined' ? window.location.pathname.match(/\/chat\/([^/]+)/) : null;
    return match?.[1] ? decodeURIComponent(match[1]) : '';
  }

  async function createShareLink(): Promise<string> {
    if (!deliverable) throw new Error('Deliverable is not loaded');
    const result = await api.deliverables.shareLink(deliverable.deliverable_id);
    return result.url;
  }

  onMount(() => {
    let cancelled = false;
    loading = true;
    error = null;
    accessorConversationId = currentConversationId();
    const loader = loadDeliverable
      ? loadDeliverable(deliverableId)
      : api.deliverables.get(deliverableId, { accessorConversationId });
    loader
      .then((result: Deliverable) => {
        if (!cancelled) deliverable = result;
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          error = err instanceof Error ? err.message : 'Failed to load deliverable';
        }
      })
      .finally(() => {
        if (!cancelled) loading = false;
      });
    return () => {
      cancelled = true;
    };
  });
</script>

<div class="assistant-deliverable-wrapper">
  {#if loading}
    <div class="text-sm text-slate-400">Loading deliverable…</div>
  {:else if error}
    <div class="space-y-2">
      <div class="text-sm font-medium text-rose-300">Could not load deliverable</div>
      <div class="text-xs text-slate-500">{error}</div>
      {#if item.content}
        <div class="rounded-xl border border-slate-700/70 bg-slate-900/60 p-3 text-sm text-slate-200">
          {item.content}
        </div>
      {/if}
    </div>
  {:else if deliverable && richPayload}
    <RichDeliverable
      title={deliverableTitle}
      content={deliverable.content}
      payload={richPayload}
      instanceId={deliverable.deliverable_id}
      mediaUrlFor={authenticatedMediaUrlFor}
      standaloneUrl={standaloneUrl}
      pdfUrl={pdfUrl}
      shareLinkCallback={createShareLink}
      surface="embedded"
      compact
    />
  {/if}
</div>

<style>
  .assistant-deliverable-wrapper {
    width: 100%;
    min-width: 0;
    max-width: 100%;
    margin: 0.75rem 0;
    /* RichDeliverable owns all its own document chrome now for every
       format; avoid a nested frame here. */
    padding: 0;
  }
</style>
