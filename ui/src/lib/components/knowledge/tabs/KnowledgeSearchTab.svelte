<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import AskAnswerCard from '$lib/components/knowledge/AskAnswerCard.svelte';
  import FilterBuilder from '$lib/components/knowledge/FilterBuilder.svelte';
  import RawResultList from '$lib/components/knowledge/RawResultList.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { elementIdForMatch } from '$lib/knowledge/citations';
  import { searchStateFromParams, searchStateToParams } from '$lib/knowledge/filters';
  import { LatestRequestController } from '$lib/knowledge/requests';
  import type {
    KnowledgebaseAskResponse,
    KnowledgebaseFilter,
    KnowledgebaseModel,
    KnowledgebaseSearchMatch,
    KnowledgebaseSearchResponse
  } from '$lib/types/api';
  import Search from 'lucide-svelte/icons/search';
  import Sparkles from 'lucide-svelte/icons/sparkles';

  let {
    kb,
    searchReady,
    askReady,
    onOpenDocument
  }: {
    kb: KnowledgebaseModel;
    searchReady: boolean;
    askReady: boolean;
    onOpenDocument: (docId: string) => void;
  } = $props();

  const initialState = searchStateFromParams(new URLSearchParams(typeof window !== 'undefined' ? window.location.search : ''));

  let mode = $state<'search' | 'ask'>(initialState.mode);
  let query = $state(initialState.query);
  let limit = $state(initialState.limit);
  let filters = $state<KnowledgebaseFilter[]>(initialState.filters);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let matches = $state<KnowledgebaseSearchMatch[]>([]);
  let askResponse = $state<KnowledgebaseAskResponse | null>(null);
  let highlightedChunkId = $state<string | null>(null);
  let staleEvidence = $state(false);
  let filterRevision = $state('');

  const requests = new LatestRequestController();

  function syncUrl(): void {
    if (typeof window === 'undefined') return;
    const params = searchStateToParams({ mode, query, limit, filters });
    params.set('tab', 'search');
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState({}, '', url);
  }

  function cancelInFlight(): void {
    requests.cancel();
  }

  function restoreUrlState(): void {
    const restored = searchStateFromParams(new URLSearchParams(window.location.search));
    mode = restored.mode; query = restored.query; limit = restored.limit; filters = restored.filters;
    filterRevision = `${kb.knowledgebase_id}:${window.location.search}`;
  }
  onMount(() => {
    filterRevision = `${kb.knowledgebase_id}:${window.location.search}`;
    window.addEventListener('popstate', restoreUrlState);
  });
  onDestroy(() => { cancelInFlight(); window.removeEventListener('popstate', restoreUrlState); });

  $effect(() => {
    if (!askReady && mode === 'ask') {
      handleModeChange('search');
    }
  });

  async function runSearch(): Promise<void> {
    if (!query.trim() || (mode === 'search' && !searchReady) || (mode === 'ask' && !askReady)) return;
    const token = requests.begin();
    const controller = token.controller;
    loading = true;
    error = null;
    staleEvidence = false;
    askResponse = null;
    syncUrl();
    try {
      if (mode === 'search') {
        const response: KnowledgebaseSearchResponse = await api.knowledgebases.search(
          kb.knowledgebase_id,
          { query, limit, filters },
          { signal: controller.signal }
        );
        if (!requests.isCurrent(token)) return;
        matches = response.matches;
      } else {
        if (!askReady) return;
        const response = await api.knowledgebases.ask(
          kb.knowledgebase_id,
          { question: query, limit: Math.min(limit, 20), filters },
          { signal: controller.signal }
        );
        if (!requests.isCurrent(token)) return;
        askResponse = response;
        matches = response.matches;
      }
    } catch (err) {
      const isAbort = (err as { name?: string })?.name === 'AbortError';
      if (!isAbort && requests.isCurrent(token)) {
        const apiError = asApiError(err);
        error =
          apiError.code === 'request_timeout'
            ? 'The question took too long to answer. Try a narrower question or fewer filters.'
            : apiError.message;
        staleEvidence = matches.length > 0;
      }
    } finally {
      if (requests.finish(token)) {
        loading = false;
      }
    }
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void runSearch();
  }

  function handleModeChange(next: 'search' | 'ask'): void {
    if (next === 'ask' && !askReady) return;
    if (mode === next) return;
    cancelInFlight();
    loading = false;
    mode = next;
    if (next === 'ask') limit = Math.min(limit, 20);
    askResponse = null;
    matches = [];
    error = null;
    syncUrl();
  }

  function handleCitationClick(chunkId: string): void {
    highlightedChunkId = chunkId;
    if (typeof document === 'undefined') return;
    const target = document.getElementById(elementIdForMatch(chunkId));
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target?.focus({ preventScroll: true });
  }

  function handleOpenDocument(match: KnowledgebaseSearchMatch): void {
    onOpenDocument(match.kb_artifact_id);
  }

  function runAsSearch(): void {
    mode = 'search';
    askResponse = null;
    error = null;
    syncUrl();
    void runSearch();
  }
</script>

<div class="flex flex-col gap-5">
  <div class="flex gap-1 self-start rounded-xl border border-slate-800/80 bg-slate-900/60 p-1 text-sm" role="tablist" aria-label="Search mode">
    <button
      type="button"
      role="tab"
      aria-selected={mode === 'search'}
      disabled={!searchReady}
      class={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${mode === 'search' ? 'bg-sky-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800'}`}
      onclick={() => handleModeChange('search')}
    >
      <Search class="h-3.5 w-3.5" /> Search
    </button>
    <button
      type="button"
      role="tab"
      aria-selected={mode === 'ask'}
      disabled={!askReady}
      class={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${mode === 'ask' ? 'bg-sky-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800'}`}
      onclick={() => handleModeChange('ask')}
    >
      <Sparkles class="h-3.5 w-3.5" /> Ask
    </button>
  </div>

  <form class="flex flex-col gap-3" onsubmit={handleSubmit}>
    <div class="flex flex-wrap gap-2">
      <Input
        bind:value={query}
        placeholder={mode === 'search' ? 'Search documents…' : 'Ask a question…'}
        class="flex-1 min-w-[220px]"
        data-testid="knowledge-search-query-input"
      />
      <label class="flex items-center gap-2 text-sm text-slate-400">
        Limit
        <input
          type="number"
          min="1"
          max={mode === 'ask' ? 20 : 50}
          value={limit}
          oninput={(event) => {
            const value = Number((event.currentTarget as HTMLInputElement).value);
            limit = Math.max(1, Math.min(mode === 'ask' ? 20 : 50, Number.isFinite(value) ? value : 10));
          }}
          class="w-16 rounded-xl border border-slate-700 bg-slate-950/80 px-2 py-2 text-sm text-slate-100"
        />
      </label>
      <Button
        type="submit"
        disabled={loading || !query.trim() || (mode === 'search' ? !searchReady : !askReady)}
        data-testid="knowledge-search-submit"
      >
        {loading ? 'Searching…' : mode === 'search' ? 'Search' : 'Ask'}
      </Button>
    </div>

    <FilterBuilder knowledgebaseId={kb.knowledgebase_id} metadataSchema={kb.metadata_schema} {filters}
      revision={filterRevision}
      onChange={(next) => { filters = next; syncUrl(); }} />
  </form>

  <div aria-live="polite">
    {#if error}
      <div class="flex flex-col gap-3">
        <div class="rounded-2xl border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-300" role="alert">{error}</div>
        {#if matches.length > 0}
          <p class="text-xs text-amber-300">Evidence from the previous request.</p>
          <RawResultList knowledgebaseId={kb.knowledgebase_id} {matches} {highlightedChunkId} onOpenDocument={handleOpenDocument} />
        {/if}
      </div>
    {:else if loading}
      <p class="text-sm text-slate-400">{mode === 'search' ? 'Searching…' : 'Thinking…'}</p>
    {:else if mode === 'ask' && askResponse}
      <div class="flex flex-col gap-4">
        <AskAnswerCard
          status={askResponse.status}
          answer={askResponse.answer}
          citedChunkIds={askResponse.cited_chunk_ids}
          error={askResponse.error}
          {matches}
          onCitationClick={handleCitationClick}
          onRetry={() => void runSearch()}
          onRunAsSearch={runAsSearch}
        />
        {#if matches.length > 0}
          <div>
            <h3 class="mb-2 text-sm font-semibold text-white">Evidence</h3>
            <RawResultList knowledgebaseId={kb.knowledgebase_id} {matches} {highlightedChunkId} onOpenDocument={handleOpenDocument} />
          </div>
        {/if}
      </div>
    {:else if mode === 'search' && matches.length > 0}
      {#if staleEvidence}<p class="mb-2 text-xs text-amber-300">Evidence from the previous request.</p>{/if}
      <RawResultList knowledgebaseId={kb.knowledgebase_id} {matches} {highlightedChunkId} onOpenDocument={handleOpenDocument} />
    {:else if query.trim() && !loading}
      <p class="rounded-2xl border border-dashed border-slate-800/80 px-4 py-8 text-center text-sm text-slate-400">
        No results yet — try a different query or fewer filters.
      </p>
    {/if}
  </div>
</div>
