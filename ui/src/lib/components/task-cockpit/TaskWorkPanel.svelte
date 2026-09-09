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
  import ActivityLifecycleStatus from '$lib/components/work/ActivityLifecycleStatus.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { api } from '$lib/api/client';
  import type { StepRun } from '$lib/types/api';
  import type { Deliverable } from '$lib/types/api';
  import {
    getActivityOverviewEntry,
    invalidateActivityOverview,
    isActivityOverviewAbort,
    requestActivityOverview,
  } from '$lib/activityOverviewCache';
  import { selectedWorkSubtreeScope } from '$lib/inspectorTreeNavigation';
  import type { Agent } from '$lib/types/api';
  import { classifyActivityOverviewPresentation } from '$lib/work/activityOverviewState';
  import type { WorkInitialFocus } from '$lib/work/workFocus';

  let {
    stepRuns,
    canonicalDeliverableId = null,
    loadWork = (scope: TimelineScope) => chatV2Api.work(scope),
    loadOverview = (scope: TimelineScope, signal?: AbortSignal) => (
      chatV2Api.activityOverview(scope, { signal })
    ),
    loadDeliverableForStepRun = (stepRunId: string, deliverableId: string) => api.deliverables.getForStepRun(stepRunId, deliverableId),
    onViewSession,
    onViewWork,
    agents = [],
    view = 'all',
    deliverableCollapsedByDefault = true,
  }: {
    stepRuns: StepRun[];
    canonicalDeliverableId?: string | null;
    loadWork?: (scope: TimelineScope) => Promise<WorkProjectionResponse>;
    loadOverview?: (
      scope: TimelineScope,
      signal?: AbortSignal,
    ) => Promise<ActivityOverviewResponse>;
    loadDeliverableForStepRun?: (stepRunId: string, deliverableId: string) => Promise<Deliverable>;
    onViewSession?: (sessionId: string) => void;
    onViewWork?: (scope: TimelineScope, category: 'files' | 'commands' | 'mutations' | 'artifacts' | 'deliverables') => void;
    agents?: Agent[];
    view?: 'all' | 'activity' | 'deliverable';
    deliverableCollapsedByDefault?: boolean;
  } = $props();
  let projections = $state<Array<{ run: StepRun; projection: WorkProjectionResponse }>>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let expanded = $state(false);
  let selectedStepRunId = $state<string | null>(null);
  let selectedWorkCategory = $state<'files' | 'commands' | 'mutations' | 'artifacts' | 'results'>('files');
  let selectedWorkInitialFocus = $state<WorkInitialFocus | null>(null);
  let generation = 0;
  let overviews = $state<Map<string, ActivityOverviewResponse>>(new Map());
  const overviewLoading = new Set<string>();
  const overviewGenerations = new Map<string, number>();
  const overviewAbortControllers = new Map<string, AbortController>();
  let selectedOverviewScopeKey: string | null = null;

  const authorizedRuns = $derived(stepRuns.filter((run) => Boolean(run.step_run_id && (run.conversation_id || run.session_id))));
  const resultEntry = $derived(
    (canonicalDeliverableId
      ? projections.find((entry) => entry.projection.final_deliverable?.deliverable_id === canonicalDeliverableId)
      : projections.find((entry) => entry.projection.final_deliverable)) ?? null
  );
  const selectedRun = $derived(
    authorizedRuns.find((run) => run.step_run_id === selectedStepRunId) ??
      resultEntry?.run ??
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
    const deliverable = resultEntry?.projection.final_deliverable;
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
        const previous = previousByRun.get(run.step_run_id);
        const incoming = result.value.projection;
        const existing = previous?.projection;
        const stale = Boolean(
          existing
          && (
            (
              existing.work_revision !== null
              && existing.work_revision !== undefined
              && incoming.work_revision !== null
              && incoming.work_revision !== undefined
              && incoming.work_revision < existing.work_revision
            )
            || (
              incoming.work_revision === existing.work_revision
              && existing.graph_revision !== null
              && existing.graph_revision !== undefined
              && incoming.graph_revision !== null
              && incoming.graph_revision !== undefined
              && incoming.graph_revision < existing.graph_revision
            )
          )
        );
        next.push(stale && previous ? previous : result.value);
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

  function loadRunOverview(
    run: StepRun,
    force = false,
    alreadyInvalidated = false,
  ): void {
    const stepRunId = run.step_run_id;
    if (!stepRunId) return;
    const scope = scopeForRun(run);
    if (force && !alreadyInvalidated) invalidateActivityOverview(scope.key);
    const cached = getActivityOverviewEntry(scope);
    if (cached) {
      if (overviews.get(stepRunId) !== cached.value) {
        overviews = new Map(overviews).set(stepRunId, cached.value);
      }
      if (!force && cached.state === 'fresh') return;
    }
    if (overviewLoading.has(stepRunId) && !alreadyInvalidated) return;
    overviewAbortControllers.get(stepRunId)?.abort();
    const controller = new AbortController();
    overviewAbortControllers.set(stepRunId, controller);
    const requestGeneration = (overviewGenerations.get(stepRunId) ?? 0) + 1;
    overviewGenerations.set(stepRunId, requestGeneration);
    overviewLoading.add(stepRunId);
    void requestActivityOverview(
      scope,
      (signal) => loadOverview(scope, signal),
      { signal: controller.signal },
    ).then((overview) => {
      if (overviewGenerations.get(stepRunId) !== requestGeneration) return;
      overviews = new Map(overviews).set(stepRunId, overview);
    }).catch((caught) => {
      if (!isActivityOverviewAbort(caught)) return undefined;
      return undefined;
    }).finally(() => {
      if (overviewAbortControllers.get(stepRunId) === controller) {
        overviewAbortControllers.delete(stepRunId);
      }
      if (overviewGenerations.get(stepRunId) === requestGeneration) {
        overviewLoading.delete(stepRunId);
      }
    });
  }

  function openExactFileWork(sourceScope: TimelineScope, focus: WorkInitialFocus): void {
    selectedWorkCategory = 'files';
    selectedWorkInitialFocus = focus;
    expanded = true;
    onViewWork?.(sourceScope, 'files');
  }

  function openRecentCategory(category: 'files' | 'commands' | 'mutations' | 'artifacts' | 'deliverables'): void {
    if (!selectedScope) return;
    selectedWorkInitialFocus = null;
    selectedWorkCategory = category === 'deliverables' ? 'results' : category;
    expanded = true;
    onViewWork?.(selectedScope, category);
  }

  function viewTaskSession(sessionId: string): void {
    onViewSession?.(sessionId);
  }

  function loadFinalDeliverable(deliverableId: string): Promise<Deliverable> {
    const stepRunId = resultEntry?.run.step_run_id;
    if (!stepRunId) return Promise.reject(new Error('The task result has no step run.'));
    return loadDeliverableForStepRun(stepRunId, deliverableId);
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
    const nextScopeKey = run ? scopeForRun(run).key : null;
    if (selectedOverviewScopeKey && selectedOverviewScopeKey !== nextScopeKey) {
      const previousStepRunId = selectedOverviewScopeKey.slice('task_step:'.length);
      overviewAbortControllers.get(previousStepRunId)?.abort();
      overviewAbortControllers.delete(previousStepRunId);
      overviewLoading.delete(previousStepRunId);
      overviewGenerations.set(
        previousStepRunId,
        (overviewGenerations.get(previousStepRunId) ?? 0) + 1,
      );
    }
    selectedOverviewScopeKey = nextScopeKey;
    if (run && view !== 'deliverable') loadRunOverview(run);
  });
  $effect(() => () => {
    for (const controller of overviewAbortControllers.values()) controller.abort();
    overviewAbortControllers.clear();
    overviewLoading.clear();
  });
  $effect(() => {
    const runsByScope = new Map(
      authorizedRuns
        .filter((run) => Boolean(run.step_run_id))
        .map((run) => [`task_step:${run.step_run_id}`, run]),
    );
    if (typeof window === 'undefined' || runsByScope.size === 0) return;
    const handleInvalidation = (event: Event): void => {
      const detail = (event as CustomEvent<{
        scopeKey?: string;
        overviewAdvanced?: boolean;
      }>).detail;
      const scopeKey = detail?.scopeKey;
      const runs = scopeKey ? [runsByScope.get(scopeKey)].filter((run): run is StepRun => Boolean(run)) : [...runsByScope.values()];
      if (runs.length === 0) return;
      if (detail?.overviewAdvanced !== false) {
        for (const run of runs) {
          loadRunOverview(run, true, true);
        }
      }
      void refresh();
    };
    window.addEventListener('cognis:work-invalidated', handleInvalidation);
    return () => window.removeEventListener('cognis:work-invalidated', handleInvalidation);
  });
</script>

<div data-testid="task-work-compact">
<Card class="p-4">
  <div class="flex items-start justify-between gap-3">
    <div>
      <p class="text-xs font-semibold uppercase tracking-[0.22em] text-sky-300">{view === 'deliverable' ? 'Final deliverable' : 'Activity'}</p>
      <p class="mt-1 text-xs text-slate-500">{view === 'deliverable' ? 'Canonical task result.' : 'Persisted task-step evidence; no log reconstruction.'}</p>
    </div>
    {#if view !== 'deliverable'}
      <Button size="sm" variant="secondary" onclick={() => { selectedWorkInitialFocus = null; expanded = true; }}>Explore <ChevronRight class="ml-1 h-3.5 w-3.5" /></Button>
    {/if}
  </div>
  {#if view !== 'activity' && finalResult}
    <section class="mt-4 border-t border-slate-800 pt-4" aria-label="Task result" data-testid="task-final-result">
      <p class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-emerald-300">Task result</p>
      <AssistantDeliverableBlock
        item={finalResult}
        loadDeliverable={resultEntry?.run.step_run_id ? loadFinalDeliverable : undefined}
         displayOnly={!resultEntry?.run.step_run_id}
         collapsedByDefault={deliverableCollapsedByDefault}
       />
    </section>
  {/if}
  {#if view === 'deliverable' && !finalResult && !loading && !error}
    <p class="mt-3 rounded-xl border border-dashed border-slate-700 p-6 text-center text-sm text-slate-400" data-testid="task-final-result-empty">
      The final deliverable is not available yet.
    </p>
  {/if}
  {#if error}
    <div class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100" role="alert">
      <p>{view === 'deliverable' ? 'Final deliverable could not be loaded' : 'Task work refresh failed'}: {error}</p>
      <button class="mt-1 underline" type="button" onclick={() => void refresh()}>Try again</button>
    </div>
  {/if}
  {#if view !== 'deliverable' && selectedOverview}
    {@const presentation = classifyActivityOverviewPresentation(selectedOverview)}
    <div class="mt-4 space-y-3">
      <ActivityLifecycleStatus materialization={selectedOverview.materialization} onRetry={() => void refresh()} />
      {#if !presentation.hasContent}
        {#if !selectedOverview.materialization || selectedOverview.materialization.state === 'live'}
          <p class="rounded-xl border border-dashed border-slate-700 p-6 text-center text-xs text-slate-400" data-testid="task-work-overview-empty">No activity yet.</p>
        {/if}
      {:else}
        <ActivitySummaryStrip metrics={[
          { id: 'files', label: 'Files', value: `${selectedOverview.summary.changed_files} ${selectedOverview.summary.changed_files === 1 ? 'file' : 'files'}`, additions: selectedOverview.summary.additions, deletions: selectedOverview.summary.deletions },
          { label: 'Commands', value: selectedOverview.summary.commands },
          { label: 'Mutations', value: selectedOverview.summary.mutations },
          { label: 'Artifacts', value: selectedOverview.summary.artifacts },
        ]} />
        {#if selectedScope && selectedOverview.recent_work}
          <RecentWorkActivity overview={selectedOverview} scope={selectedScope} limit={5} onSeeAll={openRecentCategory} onOpenWork={openExactFileWork} />
        {:else}
          <RecentActivityList items={Object.values(selectedOverview.recent).flat().slice(0, 5).map((item) => ({ id: item.id, title: item.title ?? item.category, detail: item.category }))} />
        {/if}
        <details class="rounded-xl border border-slate-800 p-3">
          <summary class="cursor-pointer text-xs font-medium text-slate-300">Execution sessions</summary>
          <div class="mt-3"><ActivityTree nodes={selectedOverview.workstreams} {agents} onViewSession={viewTaskSession} onViewWork={viewTaskWork} /></div>
        </details>
      {/if}
    </div>
  {/if}
  {#if view !== 'deliverable' && loading && !selectedOverview}<p class="mt-3 text-xs text-slate-500">Loading persisted work…</p>
  {:else if view !== 'deliverable' && !selectedOverview && !error}<p class="mt-3 text-xs text-slate-500">No persisted task work yet.</p>{/if}
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
            <button type="button" role="tab" aria-selected={selectedRun?.step_run_id === run.step_run_id} class={`shrink-0 rounded-lg border px-3 py-2 text-xs ${selectedRun?.step_run_id === run.step_run_id ? 'border-sky-500/50 bg-sky-500/10 text-sky-100' : 'border-slate-700 text-slate-400'}`} onclick={() => { selectedWorkInitialFocus = null; selectedStepRunId = run.step_run_id; }}>{run.step_name}</button>
          {/each}
        </div>
      {/if}
      {#if selectedScope}<WorkView scope={selectedWorkInitialFocus?.sourceScope ?? selectedScope} live={selectedRunLive} initialTab={selectedWorkCategory} forceInitialTab initialFocus={selectedWorkInitialFocus} />{:else}<p class="rounded-xl border border-dashed border-slate-700 p-8 text-center text-sm text-slate-400">No authorized task-step work scope is available.</p>{/if}
    </div>
  {/snippet}
</BlockingDialog>
