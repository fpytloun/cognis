<script lang="ts">
  import FileText from 'lucide-svelte/icons/file-text';
  import Image from 'lucide-svelte/icons/image';
  import Upload from 'lucide-svelte/icons/upload';
  import Bot from 'lucide-svelte/icons/bot';
  import ArrowUpRight from 'lucide-svelte/icons/arrow-up-right';
  import DocumentCommentCount from '../DocumentCommentCount.svelte';
  import DocumentFavoriteButton from '../DocumentFavoriteButton.svelte';
  import { formatLabel, libraryHref, originLabel, type LibraryItem } from '$lib/library';
  let { item, onOpen }: { item: LibraryItem; onOpen?: (item: LibraryItem) => void } = $props();
  const Icon = $derived(item.kind === 'artifact' && item.format.startsWith('image/') ? Image : FileText);
  const OriginIcon = $derived(item.origin === 'user' ? Upload : Bot);
  const date = $derived(new Date(item.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }));
</script>

<div class="flex min-w-0 items-center border-b border-separator/70">
<a href={libraryHref(item)} onclick={(event) => {
  if (onOpen && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
    event.preventDefault(); onOpen(item);
  }
}} class="resource-row group flex min-w-0 flex-1 items-start gap-3 px-3 py-4 text-left transition hover:bg-raised/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent sm:items-center sm:px-4" data-testid="resource-row">
  <span class="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-control-border/70 bg-surface text-accent"><Icon size={20} /></span>
  <span class="min-w-0 flex-1">
    <span class="block break-words text-sm font-medium leading-5 text-foreground">{item.title || 'Untitled deliverable'}</span>
    <span class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
      <span class="inline-flex items-center gap-1" title={originLabel(item.origin)}><OriginIcon size={12} />{originLabel(item.origin)}</span>
      {#if item.agent_name}<span>· {item.agent_name}</span>{/if}
      {#if item.project_name}<span>· {item.project_name}</span>{/if}
      {#if item.source_title || item.task_title || item.conversation_title}<span class="max-w-64 truncate">· {item.source_title || (item.task_title ? `Task: ${item.task_title}` : item.conversation_title)}</span>{/if}
      {#if item.is_readonly}<span class="text-quiet">· Read only</span>{/if}
      {#if !item.is_readonly}<DocumentCommentCount id={item.id} />{/if}
    </span>
    <span class="mt-1 block text-xs text-quiet sm:hidden">{formatLabel(item.format)} · {date}{item.revision > 1 ? ` · Rev ${item.revision}` : ''}</span>
  </span>
  <span class="hidden shrink-0 text-right text-xs text-muted sm:block">
    <span class="block">{date}</span>
    <span class="mt-1 block text-quiet">{formatLabel(item.format)}{item.revision > 1 ? ` · Rev ${item.revision}` : ''}</span>
  </span>
  <ArrowUpRight size={16} class="mt-1 shrink-0 text-quiet group-hover:text-accent-hover" />
</a>
<div class="shrink-0 pr-2"><DocumentFavoriteButton kind={item.kind} id={item.id} initial={item} /></div>
</div>
