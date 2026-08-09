<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ExternalLink from 'lucide-svelte/icons/external-link';

  import { api, asApiError } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import { elementIdForMatch } from '$lib/knowledge/citations';
  import type { KnowledgebaseSearchMatch, KnowledgebaseSourceContextResponse } from '$lib/types/api';

  let {
    knowledgebaseId,
    matches,
    highlightedChunkId = null,
    onOpenDocument
  }: {
    knowledgebaseId: string;
    matches: KnowledgebaseSearchMatch[];
    highlightedChunkId?: string | null;
    onOpenDocument: (match: KnowledgebaseSearchMatch) => void;
  } = $props();

  let expandedChunkId = $state<string | null>(null);
  let sourceContext = $state<Record<string, KnowledgebaseSourceContextResponse | 'loading' | 'error'>>({});

  async function toggleContext(match: KnowledgebaseSearchMatch): Promise<void> {
    if (expandedChunkId === match.chunk_id) {
      expandedChunkId = null;
      return;
    }
    expandedChunkId = match.chunk_id;
    if (sourceContext[match.chunk_id]) return;
    sourceContext = { ...sourceContext, [match.chunk_id]: 'loading' };
    try {
      const response = await api.knowledgebases.sourceContext(knowledgebaseId, { chunk_id: match.chunk_id });
      sourceContext = { ...sourceContext, [match.chunk_id]: response };
    } catch (err) {
      sourceContext = { ...sourceContext, [match.chunk_id]: 'error' };
      asApiError(err);
    }
  }

  function locatorLabel(match: KnowledgebaseSearchMatch): string {
    const locator = match.citation.locator;
    if (locator.page_start !== null) return `page ${locator.page_start}${locator.page_end && locator.page_end !== locator.page_start ? `–${locator.page_end}` : ''}`;
    if (locator.line_start !== null) return `line ${locator.line_start}${locator.line_end && locator.line_end !== locator.line_start ? `–${locator.line_end}` : ''}`;
    if (locator.paragraph_start !== null) return `paragraph ${locator.paragraph_start}`;
    return `chunk ${locator.chunk_index}`;
  }
</script>

<ul class="flex flex-col gap-3" data-testid="knowledge-raw-result-list">
  {#each matches as match, index (`${match.chunk_id}:${index}`)}
    <li
      id={elementIdForMatch(match.chunk_id)}
      tabindex="-1"
      class={`rounded-2xl border px-4 py-3 transition ${highlightedChunkId === match.chunk_id ? 'border-sky-500 bg-sky-500/5' : 'border-slate-800/80 bg-slate-900/60'}`}
      data-testid="knowledge-raw-result"
    >
      <div class="flex flex-wrap items-start justify-between gap-2">
        <div class="min-w-0">
          <p class="truncate text-sm font-medium text-white" title={match.citation.filename ?? match.artifact_id}>
            {match.citation.filename ?? match.artifact_id}
          </p>
          <p class="text-xs text-slate-500">{locatorLabel(match)}</p>
        </div>
        <div class="flex shrink-0 items-center gap-2 text-xs text-slate-400">
          <span title={JSON.stringify(match.score_breakdown)}>score {match.score.toFixed(3)}</span>
          <Button size="sm" variant="ghost" onclick={() => onOpenDocument(match)}>
            <ExternalLink class="mr-1.5 h-3.5 w-3.5" /> Open
          </Button>
        </div>
      </div>

      <p class="mt-2 text-sm leading-6 text-slate-300">{match.snippet}</p>

      {#if Object.keys(match.metadata).length > 0}
        <div class="mt-2 flex flex-wrap gap-1.5">
          {#each Object.entries(match.metadata) as [key, value] (key)}
            <span class="rounded-full border border-slate-700 bg-slate-800/70 px-2 py-0.5 text-xs text-slate-300">
              {key}: {typeof value === 'object' ? JSON.stringify(value) : String(value)}
            </span>
          {/each}
        </div>
      {/if}

      <button
        type="button"
        class="mt-2 flex items-center gap-1 text-xs text-sky-300 hover:text-sky-200"
        onclick={() => toggleContext(match)}
        aria-expanded={expandedChunkId === match.chunk_id}
      >
        <ChevronDown class={`h-3.5 w-3.5 transition ${expandedChunkId === match.chunk_id ? 'rotate-180' : ''}`} />
        {expandedChunkId === match.chunk_id ? 'Hide' : 'Show'} source context
      </button>

      {#if expandedChunkId === match.chunk_id}
        <div class="mt-2 rounded-xl border border-slate-800/80 bg-slate-950/60 px-3 py-2 text-sm text-slate-300">
          {#if sourceContext[match.chunk_id] === 'loading'}
            <p class="text-slate-500">Loading context…</p>
          {:else if sourceContext[match.chunk_id] === 'error'}
            <p class="text-rose-300">Failed to load source context.</p>
          {:else if sourceContext[match.chunk_id]}
            <p class="whitespace-pre-wrap">{(sourceContext[match.chunk_id] as KnowledgebaseSourceContextResponse).text}</p>
          {/if}
        </div>
      {/if}
    </li>
  {/each}
</ul>
