<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import Filter from 'lucide-svelte/icons/filter';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';

  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import { asApiError } from '$lib/api/client';
  import { ChatV2ApiError, chatV2Api } from '$lib/chat-v2/api';
  import { sessionTimelineScope } from '$lib/chat-v2/types';
  import type {
    AssistantDeliverableTimelineItem,
    TimelineScope,
    WorkDeliverable,
    WorkProjectionResponse,
    WorkstreamRef,
    WorkCategory,
  } from '$lib/chat-v2/types';
  import { ALL_TIME_RANGE, fromUtcIso, quickWorkRange, toUtcIso, workRangeOverlaps } from '$lib/work/workTimeRange';
  import type { AttachmentRef } from '$lib/types/api';
  import { commandToToolCall, mutationToToolCall } from '$lib/work/workEventAdapter';
  import {
    appendOlderWorkPage,
    createAccumulatedWorkState,
    orderedWorkDeliverables,
    refreshNewestWorkPage,
    restartAccumulatedWorkTraversal,
    resolvedAccumulatedWorkProjection,
    type AccumulatedWorkState,
  } from '$lib/work/workProjection';
  import {
    clearWorkResponseCache,
    getWorkResponseCache,
    restoreWorkViewState,
    saveWorkViewState,
    setWorkResponseCache,
    type WorkViewTab,
  } from '$lib/work/workViewState';
  import WorkFileTree from './WorkFileTree.svelte';

  type WorkTab = WorkViewTab;
  const PAGE_LIMIT = 100;
  const MAX_EMPTY_PAGE_REQUESTS_PER_TRIGGER = 12;
  const COMMAND_LABEL_MODE_KEY = 'cognis:work-command-label-mode';
  type CachedWorkCategory = { state: AccumulatedWorkState; shell: WorkProjectionResponse };

  let {
    scope,
    refreshIntervalMs = 15_000,
    live = true,
    onViewSession,
    initialTab = 'files',
    forceInitialTab = false,
    sessionId,
    onClearSessionFilter,
    loadWork = (
      nextScope: TimelineScope,
      signal: AbortSignal,
      before?: string,
      options?: { category: WorkCategory; from: string | null; to: string | null; sessionId?: string },
    ) => chatV2Api.work(nextScope, {
      signal,
      before,
      limit: PAGE_LIMIT,
      category: options?.category,
      from: options?.from ?? undefined,
      to: options?.to ?? undefined,
      sessionId: options?.sessionId,
    }),
  } = $props<{
    scope: TimelineScope;
    refreshIntervalMs?: number;
    live?: boolean;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    initialTab?: WorkViewTab;
    forceInitialTab?: boolean;
    sessionId?: string | undefined;
    onClearSessionFilter?: (() => void) | undefined;
    loadWork?: (
      scope: TimelineScope,
      signal: AbortSignal,
      before?: string,
      options?: { category: WorkCategory; from: string | null; to: string | null; sessionId?: string },
    ) => Promise<WorkProjectionResponse>;
  }>();

  let workState = $state<AccumulatedWorkState | null>(null);
  let shellProjection = $state<WorkProjectionResponse | null>(null);
  let initialized = $state(false);
  const categoryStates = new Map<WorkTab, AccumulatedWorkState>();
  const staleCategories = new Set<WorkTab>();
  let loading = $state(true);
  let refreshing = $state(false);
  let hydrating = $state(false);
  let hydrationAttempt = 0;
  let hydrationTimer: number | null = null;
  let loadingOlder = $state(false);
  let latestError = $state<string | null>(null);
  let olderError = $state<string | null>(null);
  let sentinelVisible = $state(false);
  let activeTab = $state<WorkTab>('files');
  let workstreamFilter = $state('all');
  let agentFilter = $state('all');
  let statusFilter = $state('all');
  let workstreamSearch = $state('');
  let filtersOpen = $state(false);
  let filterGraphFingerprint = '';
  let latestController: AbortController | null = null;
  let pageController: AbortController | null = null;
  let continuationTimer: number | null = null;
  let latestGeneration = 0;
  let pageGeneration = 0;
  let previousScope: TimelineScope | null = null;
  let previousSessionId: string | null = null;
  let restoredTab = false;
  let workRevision: number | null = null;
  let graphRevision: number | null = null;
  let timeRange = $state(ALL_TIME_RANGE);
  let timeRangeOpen = $state(false);
  let rangeFromInput = $state('');
  let rangeToInput = $state('');
  let rangeError = $state<string | null>(null);
  let commandLabelMode = $state<'command' | 'description'>('command');

  const stableScopeKey = $derived(scope.key);
  const rawProjection = $derived(workState ? resolvedAccumulatedWorkProjection(workState) : null);
  const materialization = $derived(rawProjection?.materialization);
  const quietLiveTail = $derived(Boolean(
    materialization
    && materialization.failed_streams === 0
    && materialization.total_streams - materialization.completed_streams <= 1
    && materialization.target_events - materialization.covered_events <= 5
  ));
  const historyPartial = $derived(
    Boolean(materialization && materialization.state !== 'caught_up' && !quietLiveTail)
  );
  const activeCategory = $derived<WorkCategory>(
    activeTab === 'results' ? 'deliverables' : activeTab
  );
  const showScrollStatus = $derived(Boolean(
    workState
    && (!workState.exhausted || workState.loadedPages > 1)
    && (activeTab !== 'files' || Boolean(
      rawProjection?.commands.length
      || rawProjection?.artifacts.length
      || rawProjection?.deliverables?.length
    ))
  ));
  const workstreams = $derived(rawProjection?.workstreams ?? shellProjection?.workstreams ?? []);
  const agentOptions = $derived([...new Set(workstreams.map((item) => item.agent_id))].sort());
  const statusOptions = $derived([...new Set(workstreams.map((item) => item.status))].sort());
  $effect(() => {
    const fingerprint = rawProjection?.graph_fingerprint ?? '';
    if (!fingerprint || fingerprint === filterGraphFingerprint) return;
    filterGraphFingerprint = fingerprint;
    if (
      workstreamFilter !== 'all'
      && workstreamFilter !== 'root'
      && !workstreams.some((item) => item.key === workstreamFilter)
    ) workstreamFilter = 'all';
    if (agentFilter !== 'all' && !agentOptions.includes(agentFilter)) {
      agentFilter = 'all';
    }
    if (statusFilter !== 'all' && !statusOptions.includes(statusFilter)) {
      statusFilter = 'all';
    }
  });
  const projection = $derived.by((): WorkProjectionResponse | null => {
    if (!rawProjection) return null;
    if (
      workstreamFilter === 'all'
      && agentFilter === 'all'
      && statusFilter === 'all'
      && !workstreamSearch.trim()
    ) return rawProjection;
    const matches = (source: WorkstreamRef | null | undefined) => {
      if (!source) return workstreamFilter === 'all' && agentFilter === 'all' && statusFilter === 'all';
      if (workstreamFilter === 'root' && source.key !== source.root_key) return false;
      if (workstreamFilter !== 'all' && workstreamFilter !== 'root' && source.key !== workstreamFilter) return false;
      if (agentFilter !== 'all' && source.agent_id !== agentFilter) return false;
      if (statusFilter !== 'all' && source.status !== statusFilter) return false;
      if (workstreamSearch.trim()) {
        const query = workstreamSearch.trim().toLowerCase();
        if (!`${source.title} ${source.agent_id}`.toLowerCase().includes(query)) return false;
      }
      return true;
    };
    const mutations = rawProjection.mutations.filter((item) => matches(item.source_workstream));
    const commands = rawProjection.commands.filter((item) => matches(item.source_workstream));
    const artifacts = rawProjection.artifacts.filter((item) => matches(item.source_workstream));
    const deliverables = (rawProjection.deliverables ?? []).filter((item) => matches(item.source_workstream));
    const fileIds = new Set(mutations.flatMap((event) => [
      ...(event.file_stats?.map((stat) => stat.path_id) ?? []),
      ...event.file_diffs.map((diff) => diff.path_id ?? diff.path),
    ]));
    return {
      ...rawProjection,
      mutations,
      commands,
      artifacts,
      deliverables,
      final_deliverable: deliverables.find((item) => item.deliverable_id === rawProjection.final_deliverable?.deliverable_id) ?? null,
      summary: {
        mutations: mutations.filter((event) => event.file_diffs.length === 0).length,
        commands: commands.length,
        changed_files: fileIds.size,
        artifacts: artifacts.length,
        deliverables: deliverables.length,
        additions: mutations.reduce((total, event) => total + (event.additions ?? 0), 0),
        deletions: mutations.reduce((total, event) => total + (event.deletions ?? 0), 0),
        omitted_files: mutations.reduce((total, event) => total + (event.omitted_file_count ?? 0), 0),
      },
    };
  });

  const fileDiffs = $derived(
    projection ? [...projection.mutations].reverse().flatMap((event) => {
      const previews = event.file_diffs.map((diff) => ({
        ...diff,
        source_workstream: event.source_workstream,
        truncated: diff.content_truncated === true || diff.truncated === true,
      }));
      const previewIds = new Set(previews.map((diff) => diff.path_id ?? diff.path));
      const metadataOnly = (event.file_stats ?? [])
        .filter((stat) => !previewIds.has(stat.path_id))
        .map((stat) => ({
          path: stat.path,
          path_id: stat.path_id,
          root_name: stat.root_name,
          root_id: stat.root_id,
          additions: stat.additions,
          deletions: stat.deletions,
          diff: '',
          preview_omitted: true,
          source_workstream: event.source_workstream,
        }));
      return [...previews, ...metadataOnly];
    }) : []
  );
  const genericMutations = $derived(
    projection?.mutations.filter((event) => event.file_diffs.length === 0) ?? []
  );
  const deliverables = $derived.by((): WorkDeliverable[] => {
    return orderedWorkDeliverables(
      projection?.deliverables ?? [],
      projection?.final_deliverable,
    );
  });
  const primaryResult = $derived(projection?.final_deliverable ?? null);
  const activeFilterCount = $derived(
    Number(workstreamFilter !== 'all')
    + Number(agentFilter !== 'all')
    + Number(statusFilter !== 'all')
    + Number(Boolean(workstreamSearch.trim())),
  );
  const omittedFileStats = $derived(
    projection?.mutations.reduce(
      (total, event) => total + (event.omitted_file_stat_count ?? 0),
      0,
    ) ?? 0
  );
  const tabs = $derived([
    {
      id: 'files',
      label: 'Files',
      count: (rawProjection ?? shellProjection)?.summary.changed_files ?? 0,
      suffix: (rawProjection ?? shellProjection) && ((rawProjection ?? shellProjection)!.summary.additions || (rawProjection ?? shellProjection)!.summary.deletions)
        ? `+${(rawProjection ?? shellProjection)!.summary.additions ?? 0}/-${(rawProjection ?? shellProjection)!.summary.deletions ?? 0}`
        : undefined,
    },
    { id: 'commands', label: 'Commands', count: (rawProjection ?? shellProjection)?.summary.commands ?? 0 },
    { id: 'mutations', label: 'Mutations', count: (rawProjection ?? shellProjection)?.summary.mutations ?? 0 },
    { id: 'artifacts', label: 'Artifacts', count: (rawProjection ?? shellProjection)?.summary.artifacts ?? 0 },
    { id: 'results', label: 'Deliverables', count: (rawProjection ?? shellProjection)?.summary.deliverables ?? 0 },
  ]);

  function deliverableItem(item: WorkDeliverable): AssistantDeliverableTimelineItem {
    return {
      id: `deliverable:${item.deliverable_id}`,
      kind: 'assistant_deliverable',
      deliverable_id: item.deliverable_id,
      format: item.format,
      title: item.title,
      content: item.content,
      render_metadata: item.render_metadata,
      export_metadata: item.export_metadata,
      sort_key: item.sort_key ?? '',
      source_refs: [],
      stable: true,
    };
  }

  function artifactAttachment(item: WorkProjectionResponse['artifacts'][number]): AttachmentRef {
    return {
      artifact_id: item.artifact_id,
      kind: item.mime_type?.startsWith('image/') ? 'image' : 'file',
      mime_type: item.mime_type ?? 'application/octet-stream',
      filename: item.filename || item.title || item.artifact_id,
      size_bytes: item.size_bytes ?? 0,
    };
  }

  function firstPopulatedTab(next: WorkProjectionResponse): WorkTab {
    if (next.summary.changed_files > 0) return 'files';
    if (next.commands.length > 0) return 'commands';
    if (next.mutations.some((event) => event.file_diffs.length === 0)) return 'mutations';
    if (next.artifacts.length > 0) return 'artifacts';
    return 'results';
  }

  async function loadLatest(background = false): Promise<void> {
    if (background && (loading || refreshing)) return;
    cancelHydration();
    latestController?.abort();
    latestController = new AbortController();
    const generation = ++latestGeneration;
    const signal = latestController.signal;
    const requestTab = activeTab;
    const requestCategory = activeCategory;
    const requestSessionId = sessionId ?? null;
    const requestState = categoryStates.get(requestTab) ?? workState;
    if (background) refreshing = true;
    else loading = true;
    try {
      const next = await loadWork(scope, signal, undefined, {
        category: requestCategory,
        from: timeRange.from,
        to: timeRange.to,
        sessionId: requestSessionId ?? undefined,
      });
      if (generation !== latestGeneration || signal.aborted) return;
      if (!requestState) {
        let compatibilityTab: WorkTab | null = null;
        if (
          !restoredTab
          && requestTab === 'files'
          && (next.commands.length || next.artifacts.length || (next.deliverables?.length ?? 0))
        ) {
          compatibilityTab = firstPopulatedTab(next);
        }
        const created = createAccumulatedWorkState(next);
        categoryStates.set(requestTab, created);
        // Old servers return every category in one response. Preserve that
        // behavior while independently paging modern category responses.
        if (
          requestTab === 'files'
          && (next.commands.length || next.artifacts.length || (next.deliverables?.length ?? 0))
        ) {
          for (const tab of ['commands', 'mutations', 'artifacts', 'results'] as WorkTab[]) {
            categoryStates.set(tab, created);
          }
        }
        if (compatibilityTab) activeTab = compatibilityTab;
      } else {
        const graphChanged = Boolean(
          requestState.projection.graph_fingerprint
          && next.graph_fingerprint
          && requestState.projection.graph_fingerprint !== next.graph_fingerprint,
        );
        if (graphChanged) {
          pageController?.abort();
          pageGeneration += 1;
          loadingOlder = false;
        }
        categoryStates.set(requestTab, refreshNewestWorkPage(requestState, next));
      }
      staleCategories.delete(requestTab);
      workState = categoryStates.get(activeTab) ?? (activeTab === requestTab
        ? categoryStates.get(requestTab) ?? null
        : null);
      workRevision = next.work_revision ?? workRevision;
      graphRevision = next.graph_revision ?? graphRevision;
      shellProjection = next;
      setWorkResponseCache<CachedWorkCategory>(scope, requestTab, {
        state: categoryStates.get(requestTab)!,
        shell: next,
      }, requestSessionId);
      initialized = true;
      cancelHydration();
      latestError = null;
    } catch (nextError) {
      if (generation !== latestGeneration || signal.aborted) return;
      const apiError = workApiError(nextError);
      if (isRetryableHydrationError(apiError.code)) {
        hydrating = true;
        latestError = null;
        scheduleHydrationRetry(generation);
      } else {
        hydrating = false;
        latestError = apiError.message;
        initialized = initialized || shellProjection !== null;
      }
    } finally {
      if (generation === latestGeneration) {
        loading = false;
        refreshing = false;
      }
    }
  }

  function evidenceCount(value: WorkProjectionResponse): number {
    return value.mutations.length
      + value.commands.length
      + value.artifacts.length
      + (value.deliverables?.length ?? 0);
  }

  function workApiError(value: unknown) {
    if (value instanceof ChatV2ApiError) return value;
    return asApiError(value);
  }

  function isRetryableHydrationError(code: string): boolean {
    return new Set([
      'event_store_unavailable',
      'work_watermark_timeout',
      'work_scan_timeout',
      'work_request_timeout',
    ]).has(code);
  }

  function scheduleHydrationRetry(generation: number): void {
    if (hydrationTimer !== null) window.clearTimeout(hydrationTimer);
    const delay = Math.min(3_000, 500 * (2 ** Math.min(hydrationAttempt, 3)));
    hydrationAttempt += 1;
    hydrationTimer = window.setTimeout(() => {
      hydrationTimer = null;
      if (generation !== latestGeneration || !hydrating) return;
      if (document.visibilityState !== 'visible') {
        scheduleHydrationRetry(generation);
        return;
      }
      void loadLatest();
    }, delay);
  }

  function cancelHydration(): void {
    if (hydrationTimer !== null) {
      window.clearTimeout(hydrationTimer);
      hydrationTimer = null;
    }
    hydrating = false;
    hydrationAttempt = 0;
  }

  async function loadOlder(): Promise<void> {
    if (!workState || loadingOlder || workState.exhausted || !workState.beforeCursor) return;
    pageController?.abort();
    pageController = new AbortController();
    const generation = ++pageGeneration;
    const signal = pageController.signal;
    const requestTab = activeTab;
    const requestCategory = activeCategory;
    const requestRange = timeRange;
    const requestSessionId = sessionId ?? null;
    let requestState = workState;
    loadingOlder = true;
    olderError = null;
    let restartedInvalidCursor = false;
    try {
      const initialEvidenceCount = evidenceCount(requestState.projection);
      let requests = 0;
      while (
        !requestState.exhausted
        && requestState.beforeCursor
      ) {
        const cursor = requestState.beforeCursor;
        let page: WorkProjectionResponse;
        try {
          page = await loadWork(scope, signal, cursor, {
            category: requestCategory,
            from: requestRange.from,
            to: requestRange.to,
            sessionId: requestSessionId ?? undefined,
          });
        } catch (nextError) {
          const apiError = workApiError(nextError);
          const invalidCursor = new Set([
            'cursor_expired',
            'cursor_invalid',
            'projection_version_changed',
            'unsupported_cursor',
            'lineage_changed',
          ]).has(apiError.code);
          if (
            invalidCursor
            && !restartedInvalidCursor
          ) {
            const newest = await loadWork(scope, signal, undefined, {
              category: requestCategory,
              from: requestRange.from,
              to: requestRange.to,
              sessionId: requestSessionId ?? undefined,
            });
            if (generation !== pageGeneration || signal.aborted) return;
            const graphChanged = Boolean(
              requestState.projection.graph_fingerprint
              && newest.graph_fingerprint
              && requestState.projection.graph_fingerprint !== newest.graph_fingerprint,
            );
            requestState = refreshNewestWorkPage(requestState, newest);
            if (graphChanged) {
              restartedInvalidCursor = true;
              requests = 0;
              break;
            }
            requestState = restartAccumulatedWorkTraversal(requestState);
            restartedInvalidCursor = true;
            requests = 0;
            await new Promise<void>((resolve) => window.setTimeout(resolve, 40));
            continue;
          }
          throw nextError;
        }
        if (generation !== pageGeneration || signal.aborted) return;
        requestState = appendOlderWorkPage(requestState, page);
        categoryStates.set(requestTab, requestState);
        setWorkResponseCache<CachedWorkCategory>(scope, requestTab, {
          state: requestState,
          shell: shellProjection ?? page,
        }, requestSessionId);
        if (activeTab === requestTab) workState = requestState;
        requests += 1;
        if (evidenceCount(requestState.projection) > initialEvidenceCount) break;
        if (!requestState.exhausted) {
          if (!sentinelVisible) break;
          const batchBoundary = requests % MAX_EMPTY_PAGE_REQUESTS_PER_TRIGGER === 0;
          await new Promise<void>((resolve) => window.setTimeout(
            resolve,
            batchBoundary ? 40 : 0,
          ));
          if (batchBoundary && !sentinelVisible) break;
        }
      }
    } catch (nextError) {
      if (generation !== pageGeneration || signal.aborted) return;
      olderError = workApiError(nextError).message;
    } finally {
      if (generation === pageGeneration) {
        loadingOlder = false;
        if (sentinelVisible && activeTab === requestTab && !requestState.exhausted && !olderError) {
          const continuationGeneration = generation;
          const continuationCursor = requestState.beforeCursor;
          continuationTimer = window.setTimeout(() => {
            continuationTimer = null;
            if (
              continuationGeneration === pageGeneration
              && sentinelVisible
              && workState
              && workState.beforeCursor === continuationCursor
              && !loadingOlder
              && !olderError
              && !workState.exhausted
            ) {
              void loadOlder();
            }
          }, 40);
        }
      }
    }
  }

  function clearFilters(): void {
    workstreamFilter = 'all';
    agentFilter = 'all';
    statusFilter = 'all';
    workstreamSearch = '';
  }

  function cancelContinuation(): void {
    if (continuationTimer === null) return;
    window.clearTimeout(continuationTimer);
    continuationTimer = null;
  }

  function infiniteScroll(node: HTMLElement) {
    if (typeof IntersectionObserver === 'undefined') return {};
    const observer = new IntersectionObserver((entries) => {
      sentinelVisible = entries.some((entry) => entry.isIntersecting);
      if (sentinelVisible) {
        void loadOlder();
      } else {
        cancelContinuation();
      }
    }, { rootMargin: '0px 0px 240px 0px' });
    observer.observe(node);
    return {
      destroy() {
        sentinelVisible = false;
        cancelContinuation();
        observer.disconnect();
      },
    };
  }

  function setActiveTab(id: string): void {
    const next = id as WorkTab;
    if (next === activeTab) return;
    if (workState) categoryStates.set(activeTab, workState);
    cancelHydration();
    latestController?.abort();
    latestGeneration += 1;
    loading = false;
    refreshing = false;
    pageController?.abort();
    pageGeneration += 1;
    cancelContinuation();
    sentinelVisible = false;
    loadingOlder = false;
    olderError = null;
    activeTab = next;
    const cached = categoryStates.get(next)
      ?? getWorkResponseCache<CachedWorkCategory>(scope, next, sessionId)?.state
      ?? null;
    if (cached) categoryStates.set(next, cached);
    workState = cached;
    if (!workState || staleCategories.has(next)) void loadLatest();
  }

  function applyTimeRange(next: typeof timeRange): void {
    cancelHydration();
    latestController?.abort();
    latestGeneration += 1;
    loading = false;
    refreshing = false;
    pageController?.abort();
    pageGeneration += 1;
    cancelContinuation();
    sentinelVisible = false;
    loadingOlder = false;
    timeRange = next;
    rangeFromInput = fromUtcIso(next.from);
    rangeToInput = fromUtcIso(next.to);
    rangeError = null;
    categoryStates.clear();
    clearWorkResponseCache(scope.key, sessionId ?? null);
    staleCategories.clear();
    workState = null;
    activeTab = 'files';
    latestError = null;
    olderError = null;
    timeRangeOpen = false;
    void loadLatest();
  }

  function applyAbsoluteTimeRange(): void {
    const from = rangeFromInput ? toUtcIso(rangeFromInput) : null;
    const to = rangeToInput ? toUtcIso(rangeToInput) : null;
    if ((rangeFromInput && !from) || (rangeToInput && !to) || (from && to && from > to)) {
      rangeError = 'The From value must be before the To value.';
      return;
    }
    applyTimeRange({ from, to, label: from || to ? 'Custom range' : 'All time' });
  }

  function outputScope(source: WorkstreamRef | null | undefined): TimelineScope {
    return source
      ? sessionTimelineScope(source.session_id, source.conversation_id)
      : scope;
  }

  function workstreamContext(source: WorkstreamRef | null | undefined): string | undefined {
    return source
      ? `${source.agent_id} · ${source.title} · ${source.status}`
      : undefined;
  }

  function stopScopeActivity(): void {
    cancelHydration();
    latestController?.abort();
    pageController?.abort();
    cancelContinuation();
    latestGeneration += 1;
    pageGeneration += 1;
  }

  $effect(() => {
    if (typeof window === 'undefined') return;
    const nextScope = scope;
    const nextSessionId = sessionId ?? null;
    if (previousScope?.key === nextScope.key && previousSessionId === nextSessionId) return;
    if (previousScope) {
      saveWorkViewState(previousScope, { activeTab, workstreamFilter, agentFilter, statusFilter, workstreamSearch, timeRange }, previousSessionId);
    }
    stopScopeActivity();
    const restored = restoreWorkViewState(nextScope, nextSessionId);
    previousScope = nextScope;
    previousSessionId = nextSessionId;
    workState = null;
    shellProjection = null;
    initialized = false;
    categoryStates.clear();
    filtersOpen = false;
    activeTab = forceInitialTab ? initialTab : restored?.activeTab ?? initialTab;
    restoredTab = restored !== null;
    workstreamFilter = restored?.workstreamFilter ?? 'all';
    agentFilter = restored?.agentFilter ?? 'all';
    statusFilter = restored?.statusFilter ?? 'all';
    workstreamSearch = restored?.workstreamSearch ?? '';
    timeRange = restored?.timeRange ?? ALL_TIME_RANGE;
    const cached = getWorkResponseCache<CachedWorkCategory>(nextScope, activeTab, nextSessionId);
    if (cached) {
      categoryStates.set(activeTab, cached.state);
      workState = cached.state;
      shellProjection = cached.shell;
      initialized = true;
      loading = false;
    }
    rangeFromInput = fromUtcIso(timeRange.from);
    rangeToInput = fromUtcIso(timeRange.to);
    latestError = null;
    olderError = null;
    hydrating = false;
    hydrationAttempt = 0;
    untrack(() => {
      if (!cached) void loadLatest();
    });
  });

  $effect(() => {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(COMMAND_LABEL_MODE_KEY);
    if (stored === 'command' || stored === 'description') commandLabelMode = stored;
  });

  function setCommandLabelMode(mode: 'command' | 'description'): void {
    commandLabelMode = mode;
    window.localStorage.setItem(COMMAND_LABEL_MODE_KEY, mode);
  }

  $effect(() => {
    const scopeKey = stableScopeKey;
    if (typeof window === 'undefined') return;
    const invalidate = (event: Event): void => {
      const detail = (event as CustomEvent<{
        scopeKey?: string;
        workRevision?: number;
        graphRevision?: number;
        reconnect?: boolean;
      }>).detail;
      if (detail?.scopeKey && detail.scopeKey !== scopeKey) return;
      if (
        !detail?.reconnect
        && detail?.workRevision
        && detail.workRevision === workRevision
        && (!detail.graphRevision || detail.graphRevision === graphRevision)
      ) return;
      for (const tab of categoryStates.keys()) {
        if (tab !== activeTab) staleCategories.add(tab);
      }
      void loadLatest(true);
    };
    window.addEventListener('cognis:work-invalidated', invalidate);
    const interval = live && refreshIntervalMs > 0
      ? window.setInterval(() => {
          if (document.visibilityState === 'visible') void loadLatest(true);
        }, refreshIntervalMs)
      : null;
    return () => {
      if (interval !== null) window.clearInterval(interval);
      window.removeEventListener('cognis:work-invalidated', invalidate);
    };
  });

  onDestroy(() => {
    stopScopeActivity();
    if (previousScope) {
      saveWorkViewState(previousScope, { activeTab, workstreamFilter, agentFilter, statusFilter, workstreamSearch, timeRange }, previousSessionId);
    }
  });
</script>

<section class="work-view min-w-0 max-w-full space-y-4 overflow-x-hidden" aria-label="Conversation work" data-testid="work-view">
  <header class="flex flex-wrap items-center justify-between gap-3">
    <div class="min-w-0">
      <h2 class="text-sm font-semibold text-white">Work</h2>
      <p class="scrollbar-hidden-x mt-0.5 text-xs text-slate-500">Files, commands, mutations, artifacts, and deliverables.</p>
    </div>
    <div class="relative ml-auto flex max-w-full flex-wrap items-center justify-end gap-2" data-testid="work-toolbar">
      {#if workstreams.length > 1}
        <button
          type="button"
          class="work-filter-toggle inline-flex h-9 items-center gap-1.5 rounded-lg border border-slate-700 px-2 text-xs text-slate-200 hover:bg-slate-800"
          aria-expanded={filtersOpen}
          aria-controls="work-filter-panel"
          onclick={() => { filtersOpen = !filtersOpen; }}
          data-testid="work-filter-toggle"
        >
          <Filter class="h-3.5 w-3.5" aria-hidden="true" />
          <span>Filters{activeFilterCount ? ` (${activeFilterCount} active)` : ''}</span>
        </button>
      {/if}
      <button
        type="button"
        class="h-9 rounded-lg border border-slate-700 px-2 text-xs text-slate-300 hover:bg-slate-800"
        aria-label="Choose Work time range"
        aria-expanded={timeRangeOpen}
        data-testid="work-time-range-picker"
        onclick={() => { timeRangeOpen = !timeRangeOpen; }}
      >{timeRange.label}</button>
      {#if timeRangeOpen}
        <div class="absolute right-0 top-11 z-30 w-72 rounded-xl border border-slate-700 bg-slate-950 p-3 shadow-xl" data-testid="work-time-range-popover">
          <div class="mb-2 grid grid-cols-2 gap-1">
            {#each ['All time', 'Today', 'Yesterday', 'This week', 'Last 1h', 'Last 6h', 'Last 24h', 'Last 7d'] as preset}
              {@const next = preset === 'All time' ? ALL_TIME_RANGE : preset === 'Last 7d'
                ? { ...quickWorkRange('Last 168h'), label: preset } : quickWorkRange(preset)}
              {#if !['Today', 'Yesterday', 'This week'].includes(preset) || workRangeOverlaps(next, rawProjection?.available_range)}
                <button type="button" class="rounded border border-slate-700 px-2 py-1 text-left text-[11px] text-slate-300 hover:bg-slate-800" onclick={() => applyTimeRange(next)}>{preset}</button>
              {/if}
            {/each}
          </div>
          <label class="block text-[11px] text-slate-400">From<input aria-label="Work range from" type="datetime-local" bind:value={rangeFromInput} class="mt-1 h-8 w-full rounded border border-slate-700 bg-slate-900 px-2 text-xs" /></label>
          <label class="mt-2 block text-[11px] text-slate-400">To<input aria-label="Work range to" type="datetime-local" bind:value={rangeToInput} class="mt-1 h-8 w-full rounded border border-slate-700 bg-slate-900 px-2 text-xs" /></label>
          {#if rangeError}<p class="mt-2 text-xs text-rose-300" role="alert">{rangeError}</p>{/if}
          <button type="button" class="mt-3 w-full rounded bg-sky-500 px-2 py-1.5 text-xs font-medium text-slate-950" onclick={applyAbsoluteTimeRange}>Apply range</button>
        </div>
      {/if}
      <button type="button" class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800" disabled={refreshing || hydrating} onclick={() => void loadLatest(true)} aria-label="Refresh work" title="Refresh work">
        <RefreshCw class={`h-3.5 w-3.5 ${refreshing || hydrating ? 'animate-spin' : ''}`} />
      </button>
    </div>
  </header>
  {#if sessionId}
    <div class="flex min-w-0 items-center gap-2" data-testid="work-session-filter">
      <span class="scrollbar-hidden-x min-w-0 rounded-full border border-sky-500/30 bg-sky-500/10 px-2.5 py-1 font-mono text-[10px] text-sky-200">Session {sessionId}</span>
      {#if onClearSessionFilter}<button type="button" class="text-xs text-sky-300 underline" onclick={onClearSessionFilter}>Clear session filter</button>{/if}
    </div>
  {/if}

  {#if workstreams.length > 1}
    <div id="work-filter-panel" hidden={!filtersOpen} class="work-filters w-full min-w-0 gap-2 rounded-xl border border-slate-800/80 bg-transparent p-3" data-testid="workstream-filters">
      <div class="col-span-full flex items-center justify-between gap-2">
        <span class="text-xs font-medium text-slate-300">Filters</span>
        {#if activeFilterCount}<button type="button" class="text-xs text-sky-200 underline" onclick={clearFilters} data-testid="work-filter-clear">Clear filters</button>{/if}
      </div>
      <label class="min-w-0 text-[10px] text-slate-500">Workstream
        <select bind:value={workstreamFilter} class="mt-1 h-9 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs text-slate-200">
          <option value="all">All workstreams</option><option value="root">This conversation / root</option>
          {#each workstreams as item (item.key)}<option value={item.key}>{item.parent_key ? '↳ ' : ''}{item.title} · {item.agent_id}</option>{/each}
        </select>
      </label>
      <label class="min-w-0 text-[10px] text-slate-500">Agent<select bind:value={agentFilter} class="mt-1 h-9 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs text-slate-200"><option value="all">All agents</option>{#each agentOptions as agent}<option value={agent}>{agent}</option>{/each}</select></label>
      <label class="min-w-0 text-[10px] text-slate-500">Status<select bind:value={statusFilter} class="mt-1 h-9 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs text-slate-200"><option value="all">All statuses</option>{#each statusOptions as status}<option value={status}>{status}</option>{/each}</select></label>
      <label class="min-w-0 text-[10px] text-slate-500">Search<input bind:value={workstreamSearch} placeholder="Workstream or agent" class="mt-1 h-9 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs text-slate-200" /></label>
    </div>
  {/if}

  {#if !initialized && (loading || hydrating)}
    <div class="space-y-3" aria-live="polite" aria-label="Loading work">
      {#if hydrating}
        <div class="flex items-center gap-3 rounded-xl border border-sky-500/20 bg-sky-500/10 p-4 text-sm text-sky-100">
          <RefreshCw class="h-4 w-4 shrink-0 animate-spin" />
          <span>Loading Work history from cache…</span>
        </div>
      {/if}
      <div class="h-11 animate-pulse rounded-xl bg-slate-800/60"></div><div class="h-64 animate-pulse rounded-2xl bg-slate-800/40"></div>
    </div>
  {:else if !initialized && latestError}
    <div class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100"><p>{latestError}</p><button class="mt-2 underline" type="button" onclick={() => void loadLatest()}>Retry loading newest evidence</button></div>
  {:else if initialized && shellProjection}
    {#if latestError && workState}<div class="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-100"><p>{latestError}</p><button type="button" class="mt-1 underline" onclick={() => void loadLatest(true)}>Retry refreshing newest evidence</button></div>{/if}
    {#if materialization?.state === 'materializing' && !quietLiveTail}
      <div class="rounded-lg border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-xs text-sky-100" data-testid="work-materializing" role="status">
        <p>Building Work history — {materialization.completed_streams} of {materialization.total_streams} streams</p>
        <p class="mt-1">Results below are partial.</p>
      </div>
    {:else if (materialization?.state === 'repair' || materialization?.state === 'failed') && !quietLiveTail}
      <div class="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-100" data-testid="work-repair" role="status">
        <p>Work history is incomplete — {materialization.completed_streams} of {materialization.total_streams} streams are ready.</p>
        <p class="mt-1">{materialization.failed_streams} streams failed. Cognis will retry the background repair.</p>
      </div>
    {/if}

    {#if rawProjection?.graph_truncated}<p class="text-xs text-amber-200" data-testid="work-graph-truncated">The authorized work graph reached its safety limit.</p>{/if}

    <AccessibleTabs {tabs} activeId={activeTab} idPrefix="work" ariaLabel="Work content" onChange={setActiveTab} sticky={true} testIdPrefix="work-tab" />

    <div id={`work-panel-${activeTab}`} aria-labelledby={`work-tab-${activeTab}`} role="tabpanel" class="min-w-0" data-testid={`work-panel-${activeTab}`}>
      {#if !projection && (loading || hydrating)}
        <div class="rounded-xl border border-slate-700 p-8 text-center text-sm text-slate-400" data-testid="work-panel-loading" role="status">
          <RefreshCw class="mx-auto mb-2 h-4 w-4 animate-spin" />Loading {tabs.find((tab) => tab.id === activeTab)?.label.toLowerCase()}…
        </div>
      {:else if !projection && latestError}
        <div class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" data-testid="work-panel-error">
          <p>{latestError}</p><button class="mt-2 underline" type="button" onclick={() => void loadLatest()}>Retry loading newest evidence</button>
        </div>
      {:else if projection && activeTab === 'files'}
        {#if fileDiffs.length > 0}
          <WorkFileTree diffs={fileDiffs} cacheKey={`${scope.key}:${timeRange.from ?? ''}:${timeRange.to ?? ''}`} />
          {#if projection.summary.omitted_files}
            <p class="mt-2 text-xs text-amber-200">{projection.summary.omitted_files} changed files do not have diff previews. Exact page totals and available bounded file metadata remain visible.</p>
          {/if}
          {#if omittedFileStats}
            <p class="mt-2 text-xs text-amber-200">{omittedFileStats} older file metadata entries are outside this bounded page. Load older evidence to reach them.</p>
          {/if}
        {:else}<p class="rounded-xl border border-dashed border-slate-700 p-8 text-center text-sm text-slate-500">No changed files.</p>{/if}
      {:else if projection && activeTab === 'commands'}
        <div class="mb-2 flex justify-end" data-testid="work-command-label-mode">
          <div class="inline-flex rounded-lg border border-slate-700 p-0.5" role="group" aria-label="Command label">
            {#each ['command', 'description'] as mode}
              <button type="button" class={`rounded-md px-2 py-1 text-[11px] ${commandLabelMode === mode ? 'bg-slate-700 text-white' : 'text-slate-400'}`} aria-pressed={commandLabelMode === mode} onclick={() => setCommandLabelMode(mode as 'command' | 'description')}>{mode === 'command' ? 'Command' : 'Description'}</button>
            {/each}
          </div>
        </div>
        <ol class="work-evidence-list space-y-1">
          {#each projection.commands as command (command.id)}
            <li data-testid={`work-command-${command.call_id}`}>
              <ToolCallBlock
                item={commandToToolCall(command)}
                scope={outputScope(command.source_workstream)}
                {onViewSession}
                density="compact"
                compactLabelMode={commandLabelMode}
                contextLabel={workstreamContext(command.source_workstream)}
                contextSessionId={command.source_workstream?.session_id}
              />
            </li>
          {/each}
        </ol>
      {:else if projection && activeTab === 'mutations'}
        <ol class="work-evidence-list space-y-1">
          {#each genericMutations as event (event.id)}
            <li data-testid={`work-mutation-${event.call_id}`}>
              <ToolCallBlock
                item={mutationToToolCall(event)}
                scope={outputScope(event.source_workstream)}
                {onViewSession}
                density="compact"
                contextLabel={workstreamContext(event.source_workstream)}
                contextSessionId={event.source_workstream?.session_id}
              />
            </li>
          {/each}
        </ol>
      {:else if projection && activeTab === 'artifacts'}
        <div class="work-evidence-list">{#each projection.artifacts as artifact (artifact.artifact_id)}<div class="mb-2">{#if artifact.source_workstream}<p class="mb-1 text-[10px] text-slate-500">{artifact.source_workstream.agent_id} · {artifact.source_workstream.title}</p>{/if}<MessageAttachments attachments={[artifactAttachment(artifact)]} /></div>{/each}</div>
      {:else if projection}
        <div class="space-y-4">
          {#if deliverables.length}
            <section aria-label="Deliverables">
              <div class="work-evidence-list">
                {#each deliverables as item (item.deliverable_id)}
                  <div
                    class="mb-4"
                    data-testid={`work-deliverable-${item.deliverable_id}`}
                    data-primary={item.deliverable_id === primaryResult?.deliverable_id ? 'true' : undefined}
                  >
                    {#if item.deliverable_id === primaryResult?.deliverable_id}<span class="mb-1 inline-flex rounded bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-sky-200">Primary</span>{/if}
                    {#if item.source_workstream}<p class="mb-1 text-[10px] text-slate-500">{item.source_workstream.agent_id} · {item.source_workstream.title} · {item.source_workstream.status}</p>{/if}
                    <AssistantDeliverableBlock item={deliverableItem(item)} collapsedByDefault />
                  </div>
                {/each}
              </div>
            </section>
          {/if}
        </div>
      {/if}
    </div>

    {#if workState && projection && showScrollStatus}
      <div
        use:infiniteScroll
        class="min-h-8 py-2 text-center text-xs text-slate-500"
        data-testid="work-scroll-sentinel"
        role="status"
        aria-live="polite"
      >
        {#if loadingOlder}
          Loading older evidence…
        {:else if olderError && workState.beforeCursor}
          <span>{olderError} </span><button type="button" class="text-sky-200 underline" onclick={() => void loadOlder()}>Retry loading older evidence</button>
        {:else if historyPartial}
          Results below are partial.
        {:else if workState.exhausted}
          All Work history loaded.
        {:else}
          More evidence loads as you scroll.
        {/if}
      </div>
      {#if !workState.exhausted && workState.loadedPages > 1 && !loadingOlder}
        <span class="sr-only">Older evidence is available.</span>
      {/if}
    {/if}
    {#if projection && !historyPartial && !deliverables.length && fileDiffs.length === 0 && projection.commands.length === 0 && genericMutations.length === 0 && projection.artifacts.length === 0}<p class="text-center text-xs text-slate-500">No persisted work yet.</p>{/if}
  {/if}
</section>

<style>
  .work-view {
    container: work / inline-size;
  }
  .work-filters {
    grid-template-columns: minmax(0, 1fr);
  }
  .work-evidence-list > * {
    content-visibility: auto;
    contain-intrinsic-size: auto 6rem;
  }
  :global(.work-evidence-list),
  :global([data-testid="work-tabs"]) {
    scrollbar-width: none;
  }
  :global(.work-evidence-list::-webkit-scrollbar),
  :global([data-testid="work-tabs"]::-webkit-scrollbar) {
    display: none;
  }
  @container work (min-width: 35rem) {
    .work-filters {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
</style>
