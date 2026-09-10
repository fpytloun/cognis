<script lang="ts">
  import { api } from '$lib/api/client';
  import { chatV2Api } from '$lib/chat-v2/api';
  import type { ActivityOverviewResponse, TimelineScope, WorkCategory, WorkstreamRef } from '$lib/chat-v2/types';
  import InspectorOverview from '$lib/components/InspectorOverview.svelte';
  import SessionDetailsContent, { type SessionDetailsData } from '$lib/components/session/SessionDetailsContent.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import WorkView from '$lib/components/work/WorkView.svelte';
  import {
    getActivityOverviewEntry,
    isActivityOverviewAbort,
    requestActivityOverview,
  } from '$lib/activityOverviewCache';
  import type { Agent, ContextUsage } from '$lib/types/api';
  import { untrack } from 'svelte';
  import { nodeMatchesFocus } from '$lib/activityTreeState';

  let {
    scope,
    sessionId,
    agents = [],
    initialTab = 'overview',
    activeTab = $bindable<'overview' | 'work' | 'session'>('overview'),
    headerOnly = false,
    idPrefix = 'shared-inspector',
    testIdPrefix = 'shared-inspector',
    onChange,
    onViewSession,
    contextUsage = null,
    diagnosticsFreshness = 'unavailable',
    canArchive = false,
    canDelete = false,
    archived = false,
    archiveBusy = false,
    deleteBusy = false,
    onArchive,
    onRestore,
    onDelete,
  } = $props<{
    scope: TimelineScope;
    sessionId: string;
    agents?: Agent[];
    initialTab?: 'overview' | 'work' | 'session';
    activeTab?: 'overview' | 'work' | 'session';
    headerOnly?: boolean;
    idPrefix?: string;
    testIdPrefix?: string;
    onChange?: (tab: 'overview' | 'work' | 'session') => void;
    onViewSession?: (sessionId: string, node?: WorkstreamRef) => void;
    contextUsage?: ContextUsage | null;
    diagnosticsFreshness?: 'idle' | 'current' | 'updating' | 'reconnecting' | 'unavailable';
    canArchive?: boolean;
    canDelete?: boolean;
    archived?: boolean;
    archiveBusy?: boolean;
    deleteBusy?: boolean;
    onArchive?: () => void | Promise<void>;
    onRestore?: () => void | Promise<void>;
    onDelete?: () => void | Promise<void>;
  }>();

  let workCategory = $state<WorkCategory | 'results'>('files');
  let overview = $state<ActivityOverviewResponse | null>(null);
  let session = $state<SessionDetailsData | null>(null);
  let overviewLoading = $state(false);
  let sessionLoading = $state(false);
  let overviewError = $state<string | null>(null);
  let sessionError = $state<string | null>(null);
  let generation = 0;
  let overviewGeneration = 0;
  let sessionGeneration = 0;
  let overviewAbortController: AbortController | null = null;
  const focusedSession = $derived(
    overview?.workstreams.find((node) => nodeMatchesFocus(node, sessionId)) ?? null,
  );

  async function load(tab = activeTab): Promise<void> {
    if (headerOnly) return;
    const scopeGeneration = generation;
    const requestGeneration = tab === 'overview'
      ? ++overviewGeneration
      : ++sessionGeneration;
    let requestController: AbortController | null = null;
    if (tab !== 'overview') {
      overviewAbortController?.abort();
      overviewAbortController = null;
    }
    const blocksActiveTab = untrack(
      () => (tab === 'overview' && !overview) || (tab === 'session' && !session),
    );
    if (blocksActiveTab) {
      if (tab === 'overview') overviewLoading = true;
      if (tab === 'session') sessionLoading = true;
    }
    if (tab === 'overview') overviewError = null;
    if (tab === 'session') sessionError = null;
    try {
      if (tab === 'overview') {
        const cached = getActivityOverviewEntry(scope);
        if (cached) {
          overview = cached.value;
          if (cached.state === 'fresh') return;
        }
        overviewAbortController?.abort();
        requestController = new AbortController();
        overviewAbortController = requestController;
        const next = await requestActivityOverview(
          scope,
          (signal) => chatV2Api.activityOverview(scope, { signal }),
          { signal: requestController.signal },
        );
        if (scopeGeneration === generation && requestGeneration === overviewGeneration) overview = next;
      } else if (tab === 'session' && !session) {
        const next = await api.sessions.intarisDetail(sessionId);
        if (scopeGeneration === generation && requestGeneration === sessionGeneration) session = next;
      }
    } catch (caught) {
      const current = scopeGeneration === generation && (
        (tab === 'overview' && requestGeneration === overviewGeneration)
        || (tab === 'session' && requestGeneration === sessionGeneration)
      );
      if (current && !isActivityOverviewAbort(caught)) {
        const message = caught instanceof Error ? caught.message : 'Could not load inspector data.';
        if (tab === 'overview') overviewError = message;
        if (tab === 'session') sessionError = message;
      }
    } finally {
      if (overviewAbortController === requestController) {
        overviewAbortController = null;
      }
      if (scopeGeneration === generation && blocksActiveTab) {
        if (tab === 'overview' && requestGeneration === overviewGeneration) overviewLoading = false;
        if (tab === 'session' && requestGeneration === sessionGeneration) sessionLoading = false;
      }
    }
  }

  function selectTab(id: string): void {
    activeTab = id as typeof activeTab;
    onChange?.(activeTab);
  }

  function openWork(category: WorkCategory): void {
    workCategory = category === 'deliverables' ? 'results' : category;
    activeTab = 'work';
  }

  $effect(() => {
    const scopeKey = scope.key;
    const requestedSessionId = sessionId;
    void scopeKey;
    void requestedSessionId;
    overviewAbortController?.abort();
    overviewAbortController = null;
    overview = getActivityOverviewEntry(scope)?.value ?? null;
    session = null;
    overviewLoading = false;
    sessionLoading = false;
    overviewError = null;
    sessionError = null;
    if (!headerOnly) activeTab = initialTab;
    generation += 1;
  });

  $effect(() => {
    const tab = activeTab;
    void load(tab);
  });
  $effect(() => () => {
    generation += 1;
    overviewAbortController?.abort();
    overviewAbortController = null;
  });
  $effect(() => {
    const scopeKey = scope.key;
    if (typeof window === 'undefined') return;
    const refresh = (event: Event): void => {
      const detail = (event as CustomEvent<{
        scopeKey?: string;
        overviewAdvanced?: boolean;
      }>).detail;
      if (detail?.overviewAdvanced === false) return;
      const invalidated = detail?.scopeKey;
      if (!invalidated || invalidated === scopeKey) void load('overview');
    };
    window.addEventListener('cognis:work-invalidated', refresh);
    return () => window.removeEventListener('cognis:work-invalidated', refresh);
  });
</script>

<div class="bg-slate-950" data-testid="shared-inspector-tabs">
  <AccessibleTabs
    tabs={[{ id: 'overview', label: 'Overview' }, { id: 'work', label: 'Work' }, { id: 'session', label: 'Session' }]}
    activeId={activeTab}
    {idPrefix}
    ariaLabel="Session inspector"
    onChange={selectTab}
    {testIdPrefix}
  />
  {#if !headerOnly}
  <div id={`${idPrefix}-panel-${activeTab}`} class="mt-4" role="tabpanel" aria-labelledby={`${idPrefix}-tab-${activeTab}`}>
    {#if (activeTab === 'overview' && overviewLoading && !overview) || (activeTab === 'session' && sessionLoading && !session)}
      <p class="text-xs text-slate-500">Loading inspector…</p>
    {:else if activeTab === 'overview' && overviewError && !overview}
      <p class="text-xs text-rose-300" role="alert">{overviewError}</p>
    {:else if activeTab === 'session' && sessionError && !session}
      <p class="text-xs text-rose-300" role="alert">{sessionError}</p>
    {:else if activeTab === 'overview' && overview}
      <InspectorOverview {overview} {agents} {focusedSession} focusedSessionId={sessionId} {contextUsage} {diagnosticsFreshness} onOpenWork={openWork} {onViewSession} narrow />
    {:else if activeTab === 'work'}
      <WorkView {scope} initialTab={workCategory === 'deliverables' ? 'results' : workCategory} forceInitialTab {onViewSession} />
    {:else if activeTab === 'session' && session}
      <SessionDetailsContent
        detail={session}
        {sessionId}
        {canArchive}
        {canDelete}
        {archived}
        {archiveBusy}
        {deleteBusy}
        {onArchive}
        {onRestore}
        {onDelete}
      />
    {/if}
  </div>
  {/if}
</div>
