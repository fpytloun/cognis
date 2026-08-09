<script lang="ts">
  import CircleAlert from 'lucide-svelte/icons/circle-alert';
  import Sparkles from 'lucide-svelte/icons/sparkles';

  import Card from '$lib/components/ui/Card.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { elementIdForMatch } from '$lib/knowledge/citations';
  import { renderMarkdown } from '$lib/markdown';
  import type { KnowledgebaseAskError, KnowledgebaseAskStatus, KnowledgebaseSearchMatch } from '$lib/types/api';

  let {
    status,
    answer,
    citedChunkIds,
    error,
    matches,
    onCitationClick, onRetry, onRunAsSearch
  }: {
    status: KnowledgebaseAskStatus;
    answer: string | null;
    citedChunkIds: string[];
    error: KnowledgebaseAskError | null;
    matches: KnowledgebaseSearchMatch[];
    onCitationClick: (chunkId: string) => void;
    onRetry?: () => void;
    onRunAsSearch?: () => void;
  } = $props();

  const citationMappings = $derived(
    citedChunkIds
      .map((chunkId) => matches.find((match) => match.chunk_id === chunkId))
      .filter((match): match is KnowledgebaseSearchMatch => Boolean(match))
  );
</script>

<div data-testid="knowledge-ask-answer-card">
<Card class="flex flex-col gap-3 p-5">
  <div class="flex items-center gap-2 text-sky-300">
    <Sparkles class="h-4 w-4" />
    <span class="text-sm font-semibold uppercase tracking-wide">Answer</span>
  </div>

  {#if status === 'answered' && answer}
    <div class="prose prose-invert prose-sm max-w-none">{@html renderMarkdown(answer)}</div>
    {#if citationMappings.length > 0}
      <div class="flex flex-wrap gap-1.5 border-t border-slate-800/80 pt-3">
        {#each citationMappings as match, index (match.chunk_id)}
          <button
            type="button"
            class="rounded-full border border-slate-700 bg-slate-800/70 px-2.5 py-1 text-xs text-slate-200 hover:border-sky-500 hover:text-sky-200"
            onclick={() => onCitationClick(match.chunk_id)}
            data-citation-target={elementIdForMatch(match.chunk_id)}
            aria-label={`Citation ${index + 1}: ${match.citation.filename ?? 'document'}`}
          >
            [{index + 1}] {match.citation.filename ?? 'document'}
          </button>
        {/each}
      </div>
    {/if}
  {:else if status === 'insufficient_evidence'}
    <p class="flex items-center gap-2 text-sm text-slate-400">
      <CircleAlert class="h-4 w-4 text-amber-300" /> No relevant documents were found for this question.
    </p>
  {:else}
    <p class="flex items-center gap-2 text-sm text-amber-300" role="alert">
      <CircleAlert class="h-4 w-4" />
       {error?.message ?? "The answer couldn't be synthesized, but the raw evidence below is still available."}
    </p>
    <div class="flex flex-wrap gap-2">
      {#if onRetry}<Button size="sm" onclick={onRetry}>Retry</Button>{/if}
      {#if onRunAsSearch}<Button size="sm" variant="secondary" onclick={onRunAsSearch}>Run as Search</Button>{/if}
    </div>
    {#if error?.correlation_id}
      <details class="text-xs text-slate-500"><summary class="cursor-pointer">Technical details</summary>
        <p class="mt-1">Error: {error.code}</p><p>Correlation ID: <code>{error.correlation_id}</code></p>
      </details>
    {/if}
  {/if}
</Card>
</div>
