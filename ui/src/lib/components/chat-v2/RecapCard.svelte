<script lang="ts">
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { assistantDeliverable } from '$lib/components/ui/private-view-definitions';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import DocumentCommentCount from '$lib/components/DocumentCommentCount.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { renderMarkdown } from '$lib/markdown';
  import type { RecapTimelineItem, WorkCategory } from '$lib/chat-v2/types';

  let {
    item,
    onOpenWork
  } = $props<{
    item: RecapTimelineItem;
    onOpenWork?: (category: WorkCategory, sessionId: string, path?: string, sourceItemId?: string) => void;
  }>();
  let selectedDeliverable = $state<{ id: string; title: string } | null>(null);
  let showArtifacts = $state(false);
  const totalAdditions = $derived(item.files.reduce((sum: number, file: RecapTimelineItem['files'][number]) => sum + (file.additions ?? 0), 0));
  const totalDeletions = $derived(item.files.reduce((sum: number, file: RecapTimelineItem['files'][number]) => sum + (file.deletions ?? 0), 0));
  const hasCompleteFileStats = $derived(
    item.stats_version >= 2 && !item.file_diffs_omitted &&
    item.files.every((file: RecapTimelineItem['files'][number]) =>
      typeof file.additions === 'number' && typeof file.deletions === 'number'
    )
  );
</script>

<section class="recap-paint flex w-full max-h-[300px] flex-col overflow-hidden rounded-2xl border px-5 py-4 text-foreground" aria-label="Conversation recap">
  <header class="mb-2 flex shrink-0 items-center justify-between gap-3">
    <h3 class="legacy-recap-title text-xs font-semibold uppercase tracking-[0.18em] text-blue-tone">Recap</h3>
    <span class="legacy-quiet text-xs text-quiet">{item.scope === 'recent_window' ? 'Recent work' : 'Earlier recap'} · {item.auto ? 'Automatic' : 'On request'}</span>
  </header>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (the scrollable region needs keyboard access) -->
  <div class="legacy-recap-prose chat-markdown prose prose-sm min-h-0 max-w-none overflow-y-auto overscroll-contain break-words text-foreground prose-p:my-1 prose-a:text-accent hover:prose-a:text-accent-hover prose-a:underline prose-a:underline-offset-2" role="region" aria-label="Recap content" tabindex="0">
    {@html renderMarkdown(item.text)}
  </div>
  {#if item.deliverables.length || item.artifacts.length || item.files.length}
    <footer class="mt-3 flex shrink-0 flex-nowrap items-center gap-2 overflow-x-auto whitespace-nowrap border-t border-separator pt-3 text-xs">
      {#each item.deliverables as deliverable (deliverable.id)}
        <Button variant="ghost" size="sm" class="legacy-recap-control flex max-w-[280px] shrink-0 items-center gap-2 rounded-lg border border-control-border px-2.5 py-1.5 text-foreground hover:border-accent" title={deliverable.title} onclick={() => selectedDeliverable = deliverable}>
          <span class="truncate">{deliverable.title}</span><DocumentCommentCount id={deliverable.id} />
        </Button>
      {/each}
      {#if item.artifacts.length}
        <button type="button" class="legacy-recap-control shrink-0 rounded-lg border border-control-border px-2.5 py-1.5 text-foreground hover:border-accent" onclick={() => showArtifacts = true}>
          {item.artifacts.length} artifact{item.artifacts.length === 1 ? '' : 's'} ↗
        </button>
      {/if}
      {#if item.files.length}
        <button type="button" class="legacy-recap-control shrink-0 rounded-lg border border-control-border px-2.5 py-1.5 text-foreground hover:border-accent" onclick={() => onOpenWork?.('files', item.source_session_id, item.files[0].path, item.id)}>
          {item.files.length}{item.file_diffs_omitted ? '+' : ''} file{item.files.length === 1 && !item.file_diffs_omitted ? '' : 's'}
          {#if hasCompleteFileStats}
            <span class="legacy-additions text-success">+{totalAdditions}</span> <span class="legacy-code-deletion text-danger">−{totalDeletions}</span>
          {:else}
            <span class="text-muted">diff stats unavailable</span>
          {/if}
          ↗
        </button>
      {/if}
    </footer>
  {/if}
</section>

{#if selectedDeliverable}
  <DeferredPrivateView definition={assistantDeliverable}
    viewProps={{ item: { ...item, kind: 'assistant_deliverable', deliverable_id: selectedDeliverable.id, format: 'markdown', title: selectedDeliverable.title }, fullscreenOnly: true, onClose: () => { selectedDeliverable = null; } }}
    label="document" modal onClose={() => { selectedDeliverable = null; }} />
{/if}

{#if showArtifacts}
  <div class="fixed inset-0 z-[100] flex items-center justify-center bg-scrim/75 p-4" role="presentation" onclick={() => showArtifacts = false}>
    <div role="dialog" tabindex="-1" aria-modal="true" aria-label="Recap artifacts" class="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-2xl bg-elevated p-5 text-foreground shadow-2xl" onclick={(event) => event.stopPropagation()} onkeydown={(event) => { if (event.key === 'Escape') showArtifacts = false; }}>
      <div class="mb-4 flex items-center justify-between">
        <h3 class="text-lg font-semibold">Artifacts</h3>
        <button type="button" class="text-sm text-accent hover:text-accent-hover" onclick={() => showArtifacts = false}>Close</button>
      </div>
      <MessageAttachments attachments={item.artifacts.map((artifact: RecapTimelineItem['artifacts'][number]) => ({ artifact_id: artifact.id, filename: artifact.title, kind: 'file', mime_type: artifact.mime_type, size_bytes: artifact.size_bytes }))} />
      <button type="button" class="mt-4 text-sm text-accent hover:text-accent-hover hover:underline" onclick={() => { showArtifacts = false; onOpenWork?.('artifacts', item.source_session_id); }}>Open in Work sidebar →</button>
    </div>
  </div>
{/if}
