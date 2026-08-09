<script lang="ts">
  import Archive from 'lucide-svelte/icons/archive';
  import History from 'lucide-svelte/icons/history';
  import Library from 'lucide-svelte/icons/library';
  import Trash from 'lucide-svelte/icons/trash';

  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { statusToneClass } from '$lib/knowledge/format';
  import type { KnowledgebaseDiagnostics, KnowledgebaseModel } from '$lib/types/api';

  let {
    kb,
    diagnostics = null,
    canMutate = true,
    onOpen,
    onArchive,
    onReactivate,
    onDelete
  }: {
    kb: KnowledgebaseModel;
    diagnostics?: KnowledgebaseDiagnostics | null;
    canMutate?: boolean;
    onOpen: (kb: KnowledgebaseModel) => void;
    onArchive: (kb: KnowledgebaseModel) => void;
    onReactivate: (kb: KnowledgebaseModel) => void;
    onDelete: (kb: KnowledgebaseModel) => void;
  } = $props();

  const documentCount = $derived(
    diagnostics
      ? Object.entries(diagnostics.artifact_counts)
          .filter(([status]) => status !== 'detached' && status !== 'removed')
          .reduce((sum, [, count]) => sum + count, 0)
      : null
  );
  const failedJobCount = $derived(diagnostics?.job_counts?.failed ?? 0);
  const isArchived = $derived(kb.status === 'archived');
  const isShared = $derived(kb.access_level === 'shared');
</script>

<Card class="flex flex-col gap-4 p-5">
  <div class="flex items-start justify-between gap-3">
    <button
      type="button"
      class="flex items-start gap-3 text-left"
      onclick={() => onOpen(kb)}
      data-testid="knowledge-card-open"
    >
      <span class="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-sky-500/10 text-sky-300">
        <Library class="h-5 w-5" />
      </span>
      <span>
        <span class="block text-base font-semibold text-white">{kb.name}</span>
        <span class="mt-1 block text-xs text-slate-500">
          {isShared ? `Shared by ${kb.owner_email ?? 'another user'}` : 'Owned by you'}
        </span>
        {#if kb.description}
          <span class="mt-1 block max-w-md text-sm text-slate-400 line-clamp-2">{kb.description}</span>
        {/if}
      </span>
    </button>
    <div class="flex shrink-0 flex-col items-end gap-1">
      <span class={`rounded-full border px-2.5 py-1 text-xs font-medium capitalize ${statusToneClass(isArchived ? 'warning' : 'positive')}`}>
        {kb.status}
      </span>
      {#if isShared}<Badge>Shared with you</Badge>{/if}
    </div>
  </div>

  <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400">
    {#if documentCount !== null}
      <Badge>{documentCount} document{documentCount === 1 ? '' : 's'}</Badge>
    {/if}
    {#if diagnostics}
      <Badge>{diagnostics.chunk_count} chunk{diagnostics.chunk_count === 1 ? '' : 's'}</Badge>
    {/if}
    {#if failedJobCount > 0}
      <span class={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium ${statusToneClass('danger')}`}>
        {failedJobCount} failed job{failedJobCount === 1 ? '' : 's'}
      </span>
    {/if}
  </div>

  <div class="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800/80 pt-3">
    <span class="flex items-center gap-1.5 text-xs text-slate-500">
      <History class="h-3.5 w-3.5" />
      Updated {kb.updated_at ? new Date(kb.updated_at).toLocaleDateString() : '—'}
    </span>
    <div class="flex flex-wrap gap-2">
      <Button size="sm" variant="secondary" onclick={() => onOpen(kb)}>Open</Button>
      {#if canMutate && !isShared && isArchived}
        <Button size="sm" variant="secondary" onclick={() => onReactivate(kb)}>Reactivate</Button>
      {:else if canMutate && !isShared}
        <Button size="sm" variant="ghost" onclick={() => onArchive(kb)}>
          <Archive class="mr-1.5 h-3.5 w-3.5" /> Archive
        </Button>
      {/if}
      {#if canMutate && !isShared}
        <Button size="sm" variant="ghost" onclick={() => onDelete(kb)} class="text-rose-300 hover:text-rose-200">
          <Trash class="mr-1.5 h-3.5 w-3.5" /> Delete
        </Button>
      {/if}
    </div>
  </div>
</Card>
