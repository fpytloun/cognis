<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import FileDiffViewer from './FileDiffViewer.svelte';
  import { chatV2Api, ChatV2ApiError } from '$lib/chat-v2/api';
  import type { FileDiffRef, TimelineScope, WorkFileHistoryRequest, WorkFileHistoryResponse, WorkMutationEvent } from '$lib/chat-v2/types';
  import { detectFileLanguage, type FileDiff } from '$lib/diff';
  import { formatCompactTime } from '$lib/time';
  import type { WorkInitialFocus } from '$lib/work/workFocus';

  type HistoryLoader = (request: WorkFileHistoryRequest, signal: AbortSignal) => Promise<WorkFileHistoryResponse>;
  const defaultHistoryLoader: HistoryLoader = (request, signal) => chatV2Api.fileHistory(request, { signal });
  let { event, file, scope, onOpenWork, loadHistory = defaultHistoryLoader } = $props<{
    event: WorkMutationEvent;
    file: FileDiffRef;
    scope: TimelineScope;
    onOpenWork?: (scope: TimelineScope, focus: WorkInitialFocus) => void;
    loadHistory?: HistoryLoader;
  }>();
  let expanded = $state(false);
  let loading = $state(false);
  let loaded = $state(false);
  let unavailable = $state(false);
  let error = $state<string | null>(null);
  let lazyDiffs = $state<FileDiffRef[]>([]);
  let controller: AbortController | null = null;
  let pathElement = $state<HTMLElement | null>(null);
  let pathObserver: ResizeObserver | null = null;
  const fileKey = $derived(file.path_generation_id ?? file.path_id ?? file.path);
  const panelId = $derived(`recent-file-edit-${event.id.replace(/[^a-zA-Z0-9_-]/g, '-')}-${fileKey.replace(/[^a-zA-Z0-9_-]/g, '-')}`);
  const additions = $derived(file.additions ?? 0);
  const deletions = $derived(file.deletions ?? 0);
  const visibleDiffs = $derived<FileDiff[]>([...event.file_diffs, ...lazyDiffs]
    .filter((diff) => (diff.path_generation_id ?? diff.path_id ?? diff.path) === fileKey)
    .filter((diff, index, all) => Boolean(diff.diff) && all.findIndex((candidate) =>
      (candidate.path_generation_id ?? candidate.path_id ?? candidate.path) === (diff.path_generation_id ?? diff.path_id ?? diff.path)
    ) === index)
    .map((diff) => ({ path: diff.relative_path ?? diff.path, diff: diff.diff, additions: diff.additions, deletions: diff.deletions, content_truncated: diff.content_truncated, truncated: diff.truncated })));
  const fileLabel = $derived(file.relative_path ?? file.path);
  const fileType = $derived(detectFileLanguage(fileLabel));
  const occurredAt = $derived(file.occurred_at ?? event.created_at ?? event.updated_at ?? null);
  const sourceScope = $derived(event.source_workstream?.session_id ? {
    key: `session:${event.source_workstream.session_id}`,
    kind: 'session' as const,
    session_id: event.source_workstream.session_id,
    conversation_id: event.source_workstream.conversation_id ?? scope.conversation_id,
  } : scope);

  async function loadExactDiffs(): Promise<void> {
    if (loaded || loading) return;
    loading = true; error = null; unavailable = false;
    controller?.abort(); controller = new AbortController();
    try {
      const found: FileDiffRef[] = [];
      if (!file.diff && file.path_generation_id) {
        let before: string | null = null;
        do {
          const response: WorkFileHistoryResponse = await loadHistory({ scope: sourceScope, path_generation_id: file.path_generation_id, before: before ?? undefined, limit: 50 }, controller.signal);
          found.push(...response.items.filter((item: FileDiffRef) => item.source_item_id === event.id));
          if (found.some((item) => item.path_generation_id === file.path_generation_id || item.path === file.path)) break;
          before = response.has_more_before ? response.before_cursor ?? null : null;
        } while (before);
      }
      lazyDiffs = found; loaded = true; unavailable = visibleDiffs.length === 0;
    } catch (nextError) {
      if (controller.signal.aborted) return;
      if (nextError instanceof ChatV2ApiError && (nextError.status === 404 || nextError.status === 410)) {
        unavailable = true; loaded = true;
      } else error = nextError instanceof Error ? nextError.message : 'Exact file changes are temporarily unavailable.';
    } finally { loading = false; }
  }
  function toggle(): void { expanded = !expanded; if (expanded && visibleDiffs.length === 0) void loadExactDiffs(); }
  function openInWork(): void {
    onOpenWork?.(sourceScope, {
      workItemId: event.id, sourceScope, sourceWorkstreamKey: event.source_workstream?.key ?? null,
      files: [{ path: file.path, pathId: file.path_id, pathGenerationId: file.path_generation_id }],
    });
  }

  function scrollPathToTail(): void {
    if (!pathElement) return;
    pathElement.scrollLeft = Math.max(0, pathElement.scrollWidth - pathElement.clientWidth);
  }

  $effect(() => {
    void fileLabel;
    const element = pathElement;
    if (!element) return;
    void tick().then(scrollPathToTail);
    pathObserver?.disconnect();
    if (typeof ResizeObserver !== 'undefined') {
      pathObserver = new ResizeObserver(scrollPathToTail);
      pathObserver.observe(element);
    }
    window.addEventListener('resize', scrollPathToTail);
    return () => {
      window.removeEventListener('resize', scrollPathToTail);
      pathObserver?.disconnect();
      pathObserver = null;
    };
  });

  onDestroy(() => {
    controller?.abort();
    pathObserver?.disconnect();
  });
</script>

<article class="rounded-lg border border-slate-800 bg-slate-950/30" data-testid={`recent-file-edit-${event.id}-${fileKey}`}>
  <div class="flex min-w-0 items-center gap-1 px-2 py-1.5 text-xs">
    <button type="button" class="flex min-w-0 flex-1 items-center gap-1.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400" onclick={toggle} aria-expanded={expanded} aria-controls={panelId} aria-label={`${expanded ? 'Collapse' : 'Expand'} ${fileLabel}`}>
      <span class="inline-flex h-4 min-w-5 shrink-0 items-center justify-center rounded bg-slate-800 px-1 font-mono text-[8px] font-semibold text-slate-300" title={fileType.label} aria-label={`${fileType.label} file`} data-testid="recent-file-type">{fileType.icon}</span>
      <span bind:this={pathElement} class="scrollbar-hidden-x min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-slate-300" title={fileLabel} data-testid="recent-file-path">{fileLabel}</span>
      {#if occurredAt}<span class="shrink-0 text-[9px] text-slate-500" title={occurredAt} data-testid="recent-file-time">{formatCompactTime(occurredAt)}</span>{/if}
      <span class="shrink-0 font-mono text-lime-300">+{additions}</span><span class="shrink-0 font-mono text-rose-300">−{deletions}</span>
      <ChevronDown class={`h-3.5 w-3.5 shrink-0 transition ${expanded ? 'rotate-180' : ''}`} aria-hidden="true" />
    </button>
    <button type="button" class="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded text-sky-300 hover:bg-slate-800" onclick={openInWork} aria-label={`Open ${fileLabel} in Work`} title="Open in Work"><ExternalLink class="h-3 w-3" aria-hidden="true" /></button>
  </div>
  {#if event.source_workstream}<p class="truncate px-2 pb-1 text-[10px] text-slate-500">{event.source_workstream.agent_id} · {event.source_workstream.title} · {event.source_workstream.status}</p>{/if}
  {#if expanded}
    <div id={panelId} class="space-y-2 border-t border-slate-800 p-2" role="region" aria-label={`Exact changes for ${fileLabel}`}>
      {#if loading}<p role="status" aria-live="polite" aria-busy="true" class="text-xs text-slate-400">Loading exact file changes…</p>
      {:else if error}<p role="alert" class="text-xs text-rose-300">{error} <button type="button" class="underline" onclick={() => void loadExactDiffs()}>Retry</button></p>
      {:else if visibleDiffs.length}<FileDiffViewer diffs={visibleDiffs} collapsible={false} />
      {:else if unavailable}<p role="status" class="text-xs text-amber-200">Exact diff content is no longer retained. Event statistics remain available.</p>{/if}
    </div>
  {/if}
</article>
