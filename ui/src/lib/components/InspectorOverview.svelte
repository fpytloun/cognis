<script lang="ts">
  import ActivitySummaryStrip from './ActivitySummaryStrip.svelte';
  import RecentActivityList from './RecentActivityList.svelte';
  import RecentWorkActivity from './RecentWorkActivity.svelte';
  import ActivityTree from './ActivityTree.svelte';
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import { onDestroy } from 'svelte';
  import type { ActivityOverviewResponse, WorkCategory, WorkstreamRef } from '$lib/chat-v2/types';
  import type { ContextUsage } from '$lib/types/api';
  import ActivityAvatar from './ActivityAvatar.svelte';
  type AgentMeta = { agent_id: string; display_name?: string | null; name?: string | null; avatar_url?: string | null };
  let { overview, agents = [], focusedSession = null, focusedSessionId = null, focusedSessionRuntimeActive = false, focusedSessionLoading = false, narrow = false, contextUsage = null, contextSessionId = null, reasoningEffort = null, updating = false, onOpenWork, onRefresh, onViewSession }: {
    overview: ActivityOverviewResponse;
    agents?: AgentMeta[];
    focusedSession?: WorkstreamRef | null;
    focusedSessionId?: string | null;
    focusedSessionRuntimeActive?: boolean;
    focusedSessionLoading?: boolean;
    narrow?: boolean;
    contextUsage?: ContextUsage | null;
    contextSessionId?: string | null;
    reasoningEffort?: string | null;
    updating?: boolean;
    onOpenWork?: (category: WorkCategory, sessionId?: string) => void;
    onRefresh?: (() => void) | undefined;
    onViewSession?: (sessionId: string, node?: WorkstreamRef) => void;
  } = $props();
  const metrics = $derived([
    { id: 'files', label: 'Files', value: `${overview.summary.changed_files} ${overview.summary.changed_files === 1 ? 'file' : 'files'}`, additions: overview.summary.additions, deletions: overview.summary.deletions },
    { id: 'commands', label: 'Commands', value: overview.summary.commands },
    { id: 'mutations', label: 'Mutations', value: overview.summary.mutations },
    { id: 'artifacts', label: 'Artifacts', value: overview.summary.artifacts },
    { id: 'deliverables', label: 'Deliverables', value: overview.summary.deliverables ?? 0 },
  ]);
  const recent = $derived(Object.entries(overview.recent).flatMap(([category, items]) =>
    (items ?? []).map((item) => ({ id: item.id, title: item.title ?? category, detail: `${category} · ${item.status ?? 'complete'}`, timestamp: item.occurred_at, category: category as WorkCategory, sessionId: item.session_id }))
  ).sort((a, b) => b.timestamp.localeCompare(a.timestamp)).slice(0, narrow ? 5 : 10));
  function selectRecent(item: (typeof recent)[number]): void {
    onOpenWork?.(item.category, item.sessionId);
  }
  function openMetric(metric: { id?: string }): void {
    if (metric.id) onOpenWork?.(metric.id as WorkCategory, focusedSession?.session_id);
  }
  function openTreeWork(sessionId: string, category?: WorkCategory): void {
    onOpenWork?.(category ?? 'files', sessionId);
  }
  function seeAll(category: WorkCategory): void {
    onOpenWork?.(category, focusedSession?.session_id);
  }
  function selectFallbackRecent(item: { id: string }): void {
    const match = recent.find((candidate) => candidate.id === item.id);
    if (match) selectRecent(match);
  }
  const contextLabel = $derived(contextUsage
    ? `${contextUsage.prompt_tokens.toLocaleString()} / ${contextUsage.max_context_tokens.toLocaleString()} (${Math.round((contextUsage.prompt_tokens / contextUsage.max_context_tokens) * 100)}%)`
    : 'Unavailable');
  const contextWidth = $derived(contextUsage ? `${Math.min(100, (contextUsage.prompt_tokens / contextUsage.max_context_tokens) * 100)}%` : '0%');
  const displayedReasoningEffort = $derived(
    (focusedSession?.session_id && contextSessionId === focusedSession.session_id
      ? reasoningEffort ?? contextUsage?.reasoning_effort
      : null)
      ?? focusedSession?.reasoning_effort
  );
  const displayedProfile = $derived(
    (focusedSession?.session_id && contextSessionId === focusedSession.session_id ? contextUsage?.agent_profile_id : null)
      ?? focusedSession?.agent_profile_id
      ?? 'default'
  );
  const displayedModel = $derived(
    (focusedSession?.session_id && contextSessionId === focusedSession.session_id ? contextUsage?.model : null)
      ?? focusedSession?.model
      ?? null
  );
  const focusedAgent = $derived(focusedSession ? agents.find((agent) => agent.agent_id === focusedSession.agent_id) : undefined);
  const focusedAgentName = $derived(focusedSession?.agent_display_name ?? focusedAgent?.display_name ?? focusedAgent?.name ?? focusedSession?.agent_id ?? '');
  const focusedAvatarUrl = $derived(focusedSession?.agent_avatar_url ?? focusedAgent?.avatar_url ?? null);
  let copiedSessionId = $state<string | null>(null);
  let copyResetTimer: number | null = null;
  function sessionStatus(): string {
    if (focusedSession?.activity_state === 'ongoing') return 'Running';
    if (focusedSession?.activity_state === 'active') return 'Active';
    if (focusedSession?.status === 'failed') return 'Failed';
    if (focusedSession?.status === 'cancelled') return 'Cancelled';
    if (focusedSession?.activity_state === 'closed' || focusedSession?.status === 'completed') return 'Closed';
    return focusedSessionRuntimeActive ? 'Running' : 'Closed';
  }
  async function copySessionId(sessionId: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(sessionId);
      copiedSessionId = sessionId;
      if (copyResetTimer) clearTimeout(copyResetTimer);
      copyResetTimer = window.setTimeout(() => {
        if (copiedSessionId === sessionId) copiedSessionId = null;
        copyResetTimer = null;
      }, 1_500);
    } catch {
      // Clipboard access can be unavailable outside a secure browser context.
    }
  }
  onDestroy(() => {
    if (copyResetTimer) clearTimeout(copyResetTimer);
  });
</script>
<div class="space-y-4" data-testid="inspector-overview">
  {#if focusedSession}
    <section class="focused-session-card rounded-xl border border-slate-800 bg-slate-950/30 p-3" data-testid="focused-session-card">
      <p class="text-[10px] uppercase tracking-wider text-slate-500">Focused session</p>
      <p class="scrollbar-hidden-x mt-1 text-sm font-medium text-slate-100">{focusedSession.title}</p>
      <div class="mt-1 flex min-w-0 items-center gap-1">
        <p class="scrollbar-hidden-x min-w-0 font-mono text-[10px] text-slate-500">{focusedSession.session_id}</p>
        <button type="button" class="shrink-0 rounded p-1 text-slate-600 transition hover:bg-slate-800 hover:text-sky-300" aria-label={copiedSessionId === focusedSession.session_id ? 'Session ID copied' : 'Copy session ID'} title={copiedSessionId === focusedSession.session_id ? 'Copied' : 'Copy session ID'} onclick={() => void copySessionId(focusedSession.session_id)}>
          {#if copiedSessionId === focusedSession.session_id}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
        </button>
      </div>
      <div class="focused-session-metadata mt-2 flex min-w-0 items-center gap-2 text-xs text-slate-400">
        <ActivityAvatar name={focusedAgentName} avatarUrl={focusedAvatarUrl} turnInProgress={focusedSession.activity_state === 'ongoing'} class="h-7 w-7" />
        <span class="scrollbar-hidden-x min-w-0 text-slate-200">{focusedAgentName}</span>
        <span>{sessionStatus()}</span>
        <span class="scrollbar-hidden-x min-w-0">{displayedProfile}</span>
        <span class="scrollbar-hidden-x min-w-0">{displayedModel ?? 'Model unavailable'}</span>
        {#if displayedReasoningEffort}<span>Thinking {displayedReasoningEffort}</span>{/if}
        {#if focusedSessionLoading}<span class="text-sky-300">Loading details…</span>{/if}
      </div>
    </section>
  {:else if focusedSessionId}
    <section class="rounded-xl border border-slate-800 bg-slate-950/30 p-3" data-testid="focused-session-loading"><p class="text-[10px] uppercase tracking-wider text-slate-500">Focused session</p><p class="mt-1 text-sm text-slate-300">Loading session details…</p><div class="mt-1 flex min-w-0 items-center gap-1"><p class="min-w-0 truncate font-mono text-[10px] text-slate-500">{focusedSessionId}</p><button type="button" class="shrink-0 rounded p-1 text-slate-600 transition hover:bg-slate-800 hover:text-sky-300" aria-label={copiedSessionId === focusedSessionId ? 'Session ID copied' : 'Copy session ID'} title={copiedSessionId === focusedSessionId ? 'Copied' : 'Copy session ID'} onclick={() => void copySessionId(focusedSessionId)}>{#if copiedSessionId === focusedSessionId}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}</button></div><p class="mt-2 text-xs text-slate-500">The conversation tree remains available while this session loads.</p></section>
  {/if}
  <section class="rounded-xl border border-slate-800 bg-slate-950/30 p-3" data-testid="overview-context-window">
    <div class="mb-2 flex items-center justify-between text-[10px] uppercase tracking-wide text-slate-500"><span>Context window</span><span>{contextLabel}</span></div>
    <div class="h-2 overflow-hidden rounded-full bg-slate-800"><div class="h-full rounded-full bg-sky-400 transition-[width]" style:width={contextWidth}></div></div>
  </section>
  <div class="relative">{#if updating}<span class="absolute right-0 top-0 h-2 w-2 animate-pulse rounded-full bg-sky-300" aria-label="Updating overview"></span>{/if}<ActivitySummaryStrip {metrics} onSelect={openMetric} /></div>
  {#if overview.workstreams.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Execution sessions</h3><ActivityTree nodes={overview.workstreams} {agents} focusedSessionId={focusedSessionId ?? focusedSession?.session_id ?? null} {onViewSession} onViewWork={openTreeWork} /></section>
  {/if}
  <section>
    <div class="mb-2 flex items-center justify-between gap-2">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Recent activity</h3>
      <div class="flex items-center gap-3">
        {#if onRefresh}<button type="button" class="text-xs text-slate-400 hover:text-slate-200" onclick={onRefresh}>Refresh</button>{/if}
        <button type="button" class="text-xs text-sky-300" onclick={() => onOpenWork?.('files', focusedSession?.session_id)}>Open Work</button>
      </div>
    </div>
    {#if overview.recent_work}
      <RecentWorkActivity {overview} scope={overview.scope} limit={narrow ? 5 : 10} onSeeAll={seeAll} />
    {:else}
      <RecentActivityList items={recent} onSelect={selectFallbackRecent} />
    {/if}
  </section>
</div>
<style>
  .focused-session-card { container-type: inline-size; }
  @container (max-width: 32rem) {
    .focused-session-metadata {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
    }
    .focused-session-metadata > :nth-child(n + 4) { grid-row: 2; }
  }
</style>
