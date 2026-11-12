<script lang="ts">
  import RecapCard from '$lib/components/chat-v2/RecapCard.svelte';
  import DeliverableReferenceActions from '$lib/components/DeliverableReferenceActions.svelte';
  import StandaloneViewer from '../../../src/StandaloneViewer.svelte';
  import PublicSourcePreview from '$lib/components/feedback/PublicSourcePreview.svelte';
  import type { RecapTimelineItem, AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
  import type { Deliverable } from '$lib/types/api';

  const text = Array.from({ length: 20 }, (_, i) => `## Section ${i + 1}\n\nA readable paragraph about validation, ownership, and reliable delivery.`).join('\n\n');
  const document: Deliverable = {
    deliverable_id: 'dlv_11111111111111111111111111111111',
    format: 'markdown', title: 'Browser delivery review', content: text,
    revision: 1, latest_revision: 1, is_latest: true,
  } as Deliverable;
  const reference = {
    id: 'fixture:document', kind: 'assistant_deliverable',
    deliverable_id: document.deliverable_id, title: document.title,
    format: document.format, deliverable: document,
  } as AssistantDeliverableTimelineItem;
  const recap = {
    id: 'fixture:recap', kind: 'recap', scope: 'recent_window', auto: true,
    text, source_session_id: 'fixture-session', stats_version: 4, file_diffs_omitted: false,
    deliverables: new URLSearchParams(window.location.search).has('many')
      ? Array.from({ length: 30 }, (_, i) => ({ id: `dlv_${i}`, title: `Delivery review ${i + 1}` }))
      : [{ id: document.deliverable_id, title: document.title }],
    artifacts: [{ id: 'art_11111111111111111111111111111111', title: 'Trace' }],
    files: [{ path: 'src/main.ts', additions: 4, deletions: 2 }],
  } as RecapTimelineItem;
  const standalone = new URLSearchParams(window.location.search).has('standalone');
  const recapOnly = new URLSearchParams(window.location.search).has('recap');
  const publicComment = new URLSearchParams(window.location.search).has('comment');
</script>

{#if publicComment}
  <PublicSourcePreview content={{ format: 'markdown', mime_type: 'text/markdown', filename: 'Comment-enabled review',
    content: text, rich_payload: null, bytes_path: null, theme_family: 'gruvbox' }}
    mediaUrl={null} guard={{ signal: new AbortController().signal, assertCurrent() {}, validate: async () => {} }} />
{:else if standalone}
  <StandaloneViewer viewerProps={{ title: 'Published review', payload: { blocks: [{ type: 'markdown', content: text }] }, publicationThemeFamily: 'gruvbox', exportMode: true, surface: 'standalone' }} />
{:else}
  <main class="mx-auto flex max-w-4xl flex-col gap-8 p-4">
    <h1 class="text-xl font-semibold">Deliverable UI acceptance</h1>
    {#if !recapOnly}<div class="rounded-xl border border-separator p-4" data-testid="reference-actions">
      <h2>Browser delivery review</h2>
      <DeliverableReferenceActions item={reference} />
    </div>{/if}
    <RecapCard item={recap} />
    <textarea aria-label="Message input" class="rounded-xl border p-3" placeholder="Message input"></textarea>
  </main>
{/if}
