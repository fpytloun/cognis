<script lang="ts">
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import { chatV2Api } from '$lib/chat-v2/api';
  import type { ActivityOverviewResponse, AssistantDeliverableTimelineItem, TimelineScope, WorkProjectionResponse } from '$lib/chat-v2/types';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import ActivitySummaryStrip from '$lib/components/ActivitySummaryStrip.svelte';
  import RecentWorkActivity from '$lib/components/RecentWorkActivity.svelte';
  import RecentActivityList from '$lib/components/RecentActivityList.svelte';
  import ActivityTree from '$lib/components/ActivityTree.svelte';
  import WorkView from '$lib/components/work/WorkView.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { StepRun } from '$lib/types/api';
  import { clearActivityOverview, getActivityOverview, setActivityOverview } from '$lib/activityOverviewCache';
  import { selectedWorkSubtreeScope } from '$lib/inspectorTreeNavigation';
  import type { Agent } from '$lib/types/api';

  let {
    stepRuns,
    canonicalDeliverableId = null,
    loadWork = (scope: TimelineScope) => chatV2Api.work(scope),
    loadOverview = (scope: TimelineScope) => chatV2Api.activityOverview(scope),
    onViewSession,
    onViewWork,
    agents = [],
  }: {
    stepRuns: StepRun[];
    canonicalDeliverableId?: string | null;
    loadWork?: (scope: TimelineScope) => Promise<WorkProjectionResponse>;
    loadOverview?: (scope: TimelineScope) => Promise<ActivityOverviewResponse>;
    onViewSession?: (sessionId: string) => void;
    onViewWork?: (scope: TimelineScope, category: 'files' | 'commands' | 'mutations' | 'artifacts' | 'deliverables') => void;
    agents?: Agent[];
  } = $props();
  let projections = $state<Array<{ run: StepRun; projection: WorkProjectionResponse }>>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let expanded = $state(false);
  let selectedStepRunId = $state<string | null>(null);
  let selectedWorkCategory = $state<'files' | 'commands' | 'mutations' | 'artifacts' | 'results'>('files');
  let generation = 0;
  let overviews = $state<Map<string, ActivityOverviewResponse>>(new Map());
  const overviewLoading = new Set<string>();
  const overviewGenerations = new Map<string, number>();

  const authorizedRuns = $derived(stepRuns.filter((run) => Boolean(run.step_run_id && (run.conversation_id || run.session_id))));
  const canonicalEntry = $derived(
    projections.find((entry) => entry.projection.final_deliverable?.deliverable_id === canonicalDeliverableId) ?? null
  );
  const selectedRun = $derived(
    authorizedRuns.find((run) => run.step_run_id === selectedStepRunId) ??
      canonicalEntry?.run ??
      authorizedRuns.reduce<StepRun | null>((latest, run) => {
        if (!latest) return run;
        return (run.updated_at ?? '') > (latest.updated_at ?? '') ? run : latest;
      }, null)
  );
  const selectedScope = $derived.by((): TimelineScope | null => selectedRun ? ({
    key: `task_step:${selectedRun.step_run_id}`,
    kind: 'task_step',
    step_run_id: selectedRun.step_run_id,
    conversation_id: selectedRun.conversation_id,
    session_id: selectedRun.session_id
  }) : null);
  const selectedRunLive = $derived(
    !['cancelled', 'completed', 'failed'].includes(selectedRun?.status ?? '')
  );
  const selectedOverview = $derived(selectedRun?.step_run_id ? overviews.get(selectedRun.step_run_id) ?? null : null);
  const finalResult = $derived.by((): AssistantDeliverableTimelineItem | null => {
    const deliverable = canonicalDeliverableId
      ? canonicalEntry?.projection.final_deliverable
      : projections.find((entry) => entry.projection.final_deliverable)?.projection.final_deliverable;
    if (!deliverable) return null;
    return {
      id: `deliverable:${deliverable.deliverable_id}`,
      kind: 'assistant_deliverable',
      deliverable_id: deliverable.deliverable_id,
      format: deliverable.format,
      title: deliverable.title,
      content: deliverable.content,
      render_metadata: deliverable.render_metadata,
      export_metadata: deliverable.export_metadata,
      sort_key: '',
      source_refs: [],
      stable: true
    };
  });

  async function refresh(): Promise<void> {
    const currentGeneration = ++generation;
    loading = true;
    const settled = await Promise.allSettled(authorizedRuns.map(async (run) => ({
      run,
      projection: await loadWork({
        key: `task_step:${run.step_run_id}`,
        kind: 'task_step',
        step_run_id: run.step_run_id,
        conversation_id: run.conversation_id,
        session_id: run.session_id
      })
    })));
    if (currentGeneration !== generation) return;
    const previousByRun = new Map(projections.map((entry) => [entry.run.step_run_id, entry]));
    const next: Array<{ run: StepRun; projection: WorkProjectionResponse }> = [];
    const failures: string[] = [];
    settled.forEach((result, index) => {
      const run = authorizedRuns[index];
      if (result.status === 'fulfilled') {
        next.push(result.value);
        return;
      }
      const previous = previousByRun.get(run.step_run_id);
      if (previous) next.push(previous);
      failures.push(result.reason instanceof Error ? result.reason.message : 'Failed to load task work');
    });
    projections = next;
    error = failures.length > 0 ? failures[0] : null;
    loading = false;
  }

  function scopeForRun(run: StepRun): TimelineScope {
    return {
      key: `task_step:${run.step_run_id}`,
      kind: 'task_step',
      step_run_id: run.step_run_id,
      conversation_id: run.conversation_id,
      session_id: run.session_id,
    };
  }

  function loadRunOverview(run: StepRun, force = false): void {
    const stepRunId = run.step_run_id;
    if (!stepRunId) return;
    if (!force && (overviews.has(stepRunId) || overviewLoading.has(stepRunId))) return;
    const scope = scopeForRun(run);
    if (!force) {
      const cached = getActivityOverview(scope);
      if (cached) {
        overviews = new Map(overviews).set(stepRunId, cached);
        return;
      }
    }
    const requestGeneration = (overviewGenerations.get(stepRunId) ?? 0) + 1;
    overviewGenerations.set(stepRunId, requestGeneration);
    overviewLoading.add(stepRunId);
    void loadOverview(scope).then((overview) => {
      if (overviewGenerations.get(stepRunId) !== requestGeneration) return;
      setActivityOverview(scope, overview);
      overviews = new Map(overviews).set(stepRunId, overview);
    }).catch(() => undefined).finally(() => {
      if (overviewGenerations.get(stepRunId) === requestGeneration) {
        overviewLoading.delete(stepRunId);
      }
    });
  }

  function openRecentCategory(category: 'files' | 'commands' | 'mutations' | 'artifacts' | 'deliverables'): void {
    if (!selectedScope) return;
    selectedWorkCategory = category === 'deliverables' ? 'results' : category;
    expanded = true;
    onViewWork?.(selectedScope, category);
  }

  function viewTaskSession(sessionId: string): void {
    onViewSession?.(sessionId);
  }

  function viewTaskWork(sessionId: string, category: 'files' | 'commands' | 'mutations' | 'artifacts' | 'deliverables' = 'files'): void {
    const selectedNode = selectedOverview?.workstreams.find((node) => node.session_id === sessionId);
    const conversationId = selectedNode?.conversation_id ?? selectedScope?.conversation_id;
    if (!conversationId) return;
    onViewWork?.(
      selectedWorkSubtreeScope(conversationId, sessionId),
      category,
    );
  }

  $effect(() => {
    authorizedRuns.map((run) => `${run.step_run_id}:${run.updated_at ?? ''}:${run.status}`).join('|');
    if (typeof window === 'undefined') return;
    void refresh();
  });
  $effect(() => {
    const run = selectedRun;
    if (run) loadRunOverview(run);
  });
  $effect(() => {
    const runsByScope = new Map(
      authorizedRuns
        .filter((run) => Boolean(run.step_run_id))
        .map((run) => [`task_step:${run.step_run_id}`, run]),
    );
    if (typeof window === 'undefined' || runsByScope.size === 0) return;
    const handleInvalidation = (event: Event): void => {
      const scopeKey = (event as CustomEvent<{ scopeKey?: string }>).detail?.scopeKey;
      if (!scopeKey) return;
      const run = runsByScope.get(scopeKey);
      if (!run?.step_run_id) return;
      clearActivityOverview(scopeKey);
      const nextOverviews = new Map(overviews);
      nextOverviews.delete(run.step_run_id);
      overviews = nextOverviews;
      loadRunOverview(run, true);
    };
    window.addEventListener('cognis:work-invalidated', handleInvalidation);
    return () => window.removeEventListener('cognis:work-invalidated', handleInvalidation);
  });
</script>

<div data-testid="task-work-compact">
<Card class="p-4">
  <div class="flex items-start justify-between gap-3">
    <div><p class="text-xs font-semibold uppercase tracking-[0.22em] text-sky-300">Work</p><p class="mt-1 text-xs text-slate-500">Persisted task-step evidence; no log reconstruction.</p></div>
    <Button size="sm" variant="secondary" onclick={() => { expanded = true; }}>Explore <ChevronRight class="ml-1 h-3.5 w-3.5" /></Button>
  </div>
  {#if finalResult}
    <section class="mt-4 border-t border-slate-800 pt-4" aria-label="Task result" data-testid="task-final-result">
      <p class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-emerald-300">Task result</p>
      <AssistantDeliverableBlock item={finalResult} />
    </section>
  {/if}
  {#if error}
    <div class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100" role="alert">
      <p>Task work refresh failed: {error}</p>
      <button class="mt-1 underline" type="button" onclick={() => void refresh()}>Try again</button>
    </div>
  {/if}
  {#if selectedOverview}
    <div class="mt-4 space-y-3">
      <ActivitySummaryStrip metrics={[
        { id: 'files', label: 'Files', value: `${selectedOverview.summary.changed_files} ${selectedOverview.summary.changed_files === 1 ? 'file' : 'files'}`, additions: selectedOverview.summary.additions, deletions: selectedOverview.summary.deletions },
        { label: 'Commands', value: selectedOverview.summary.commands },
        { label: 'Mutations', value: selectedOverview.summary.mutations },
        { label: 'Artifacts', value: selectedOverview.summary.artifacts },
      ]} />
      {#if selectedScope && selectedOverview.recent_work}
        <RecentWorkActivity overview={selectedOverview} scope={selectedScope} limit={5} onSeeAll={openRecentCategory} />
      {:else}
        <RecentActivityList items={Object.values(selectedOverview.recent).flat().slice(0, 5).map((item) => ({ id: item.id, title: item.title ?? item.category, detail: item.category }))} />
      {/if}
      <details class="rounded-xl border border-slate-800 p-3">
        <summary class="cursor-pointer text-xs font-medium text-slate-300">Execution sessions</summary>
        <div class="mt-3"><ActivityTree nodes={selectedOverview.workstreams} {agents} onViewSession={viewTaskSession} onViewWork={viewTaskWork} /></div>
      </details>
    </div>
  {/if}
  {#if loading && !selectedOverview}<p class="mt-3 text-xs text-slate-500">Loading persisted work…</p>
  {:else if !selectedOverview && !error}<p class="mt-3 text-xs text-slate-500">No persisted task work yet.</p>{/if}
</Card>
</div>

<BlockingDialog open={expanded} onClose={() => { expanded = false; }} label="Task work" panelClass="max-w-6xl">
  {#snippet header()}
    <div class="flex min-w-0 flex-1 items-center justify-between gap-3">
      <div><h2 class="text-lg font-semibold text-white">Task Work</h2><p class="text-xs text-slate-500">Final deliverable, diffs, commands, and mutations by authorized step scope.</p></div>
      <Button size="sm" variant="secondary" onclick={() => { expanded = false; }}>Close</Button>
    </div>
  {/snippet}
  {#snippet children()}
    <div class="min-h-[60vh]">
      {#if authorizedRuns.length > 1}
        <div class="mb-4 flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Task step work">
          {#each authorizedRuns as run (run.step_run_id)}
            <button type="button" role="tab" aria-selected={selectedRun?.step_run_id === run.step_run_id} class={`shrink-0 rounded-lg border px-3 py-2 text-xs ${selectedRun?.step_run_id === run.step_run_id ? 'border-sky-500/50 bg-sky-500/10 text-sky-100' : 'border-slate-700 text-slate-400'}`} onclick={() => { selectedStepRunId = run.step_run_id; }}>{run.step_name}</button>
          {/each}
        </div>
      {/if}
      {#if selectedScope}<WorkView scope={selectedScope} live={selectedRunLive} initialTab={selectedWorkCategory} forceInitialTab />{:else}<p class="rounded-xl border border-dashed border-slate-700 p-8 text-center text-sm text-slate-400">No authorized task-step work scope is available.</p>{/if}
    </div>
  {/snippet}
</BlockingDialog>
