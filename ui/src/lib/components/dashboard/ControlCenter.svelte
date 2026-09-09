<script lang="ts">
  import { onMount } from 'svelte';
  import Search from 'lucide-svelte/icons/search';
  import X from 'lucide-svelte/icons/x';

  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import AttentionActionDialog from '$lib/components/attention/AttentionActionDialog.svelte';
  import CreateTaskModal from '$lib/components/tasks/CreateTaskModal.svelte';
  import AgentQuickChatModal from '$lib/components/dashboard/AgentQuickChatModal.svelte';
  import ConversationsSection from '$lib/components/dashboard/ConversationsSection.svelte';
  import DashboardEntityModal from '$lib/components/dashboard/DashboardEntityModal.svelte';
  import IssuesStrip from '$lib/components/dashboard/IssuesStrip.svelte';
  import TasksSection from '$lib/components/dashboard/TasksSection.svelte';
  import WorkspaceLayer from '$lib/components/dashboard/WorkspaceLayer.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { api, asApiError } from '$lib/api/client';
  import {
    defaultNewChatAgentId,
    isDashboardConversationActive,
    isDashboardConversationWaiting,
    isDashboardTopicConversation,
  } from '$lib/dashboard/dashboard';
  import { addToast } from '$lib/stores/toasts';
  import { auth } from '$lib/stores/auth';
  import { deriveGettingStartedSteps, isGettingStartedDismissed } from '$lib/getting-started';
  import {
    gettingStartedIssue,
    mergeDashboardIssues,
    providerNoticeIssues,
  } from '$lib/provider-notices';
  import { workspaceHealth } from '$lib/system';
  import {
    loadUserPreferences,
    userPreferences,
    userPreferencesReady,
  } from '$lib/stores/userPreferences';
  import {
    dashboardWorkspaceStorageKey,
    serializeWorkspaceWindows,
    WorkspaceManager,
    workspaceDeviceClass,
    type WorkspaceWindowState,
  } from '$lib/dashboard/workspace';
  import { wsClient } from '$lib/ws/client';
  import {
    dashboardBatchRefreshesModal,
    subscribeDashboardRealtime,
    type DashboardRealtimeBatch,
  } from '$lib/dashboard/realtime';
  import {
    DashboardConversationProjectionGate,
    DashboardConversationReadTracker,
  } from '$lib/dashboard/conversation-read';
  import { loadCompletePausedColumn, reconcileTaskBoard } from '$lib/dashboard/task-board';
  import {
    AttentionActionTombstones,
    reconcileConversationProjection,
  } from '$lib/dashboard/conversation-reconciliation';
  import { LatestRequestGate, searchResetAction } from '$lib/dashboard/request-gate';
  import type {
    Agent,
    AttentionActionDetail,
    AttentionActionSummary,
    Conversation,
    DashboardIssuesResponse,
    Project,
    Schedule,
    Skill,
    SystemDiagnostics,
    TaskBoard,
    Workflow
  } from '$lib/types/api';

  /**
   * `/` — the Control Center: a lightweight, independently-loading dashboard
   * composed from existing bounded APIs (task board, schedules, conversation
   * sidebar, agents, dashboard issues). Every section loads and errors
   * independently, and section refresh retains prior data on partial
   * failure so one flaky source never blanks the whole page.
   */

  let issues = $state<DashboardIssuesResponse | null>(null);
  let issuesLoading = $state(true);
  let issuesError = $state<string | null>(null);
  let diagnostics = $state<SystemDiagnostics | null>(null);
  const displayedIssues = $derived(mergeDashboardIssues(issues, [
    ...providerNoticeIssues($workspaceHealth.health),
    ...gettingStartedIssue(Boolean(
      diagnostics
      && !isGettingStartedDismissed()
      && deriveGettingStartedSteps(diagnostics).some((step) => !step.done)
    )),
  ]));

  let agents = $state<Agent[]>([]);
  let agentsLoading = $state(true);
  let agentsError = $state<string | null>(null);

  let board = $state<TaskBoard | null>(null);
  let boardScopeKey = $state('');
  let boardLoading = $state(true);
  let boardError = $state<string | null>(null);
  let boardLoadGeneration = 0;
  let boardLoadController: AbortController | null = null;
  const recentTaskAppendGate = new LatestRequestGate();
  let recentTasksLoading = $state(false);
  let recentTasksError = $state<string | null>(null);

  let schedules = $state<Schedule[] | null>(null);
  let schedulesError = $state<string | null>(null);

  let conversations = $state<Conversation[] | null>(null);
  let workspaceConversationMetadata = $state<Record<string, Conversation>>({});
  const pendingConversationInitialLoads = new Set<string>();
  const pendingWorkspaceConversationPatches = new Map<string, {
    generation: number;
    patch: DashboardRealtimeBatch['conversationPatches'][number];
  }>();
  const appliedWorkspaceConversationGenerations = new Map<string, number>();
  let workspaceConversationGeneration = 0;
  const requestedWorkspaceConversationIds = new Set<string>();
  const attentionActionTombstones = new AttentionActionTombstones();
  let conversationsLoading = $state(true);
  let conversationsError = $state<string | null>(null);
  let conversationsAppendError = $state<string | null>(null);
  let conversationsCursor = $state<string | null>(null);
  let conversationsHasMore = $state(false);
  let conversationsAppendLoading = $state(false);
  let conversationsScopeKey = $state('');
  let conversationLoadController: AbortController | null = null;
  let conversationAppendController: AbortController | null = null;
  let searchInput = $state('');
  let searchQuery = $state('');
  let selectedProjectId = $state('');
  let projects = $state<Project[]>([]);
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;
  let pendingSearchReset = false;

  let selectedAgent = $state<Agent | null>(null);
  let selectedAttentionId = $state<string | null>(null);
  let selectedAttentionTitle = $state('');
  let selectedAttentionOrigin = $state<HTMLElement | null>(null);
  let selectedAttentionFallback = $state<HTMLElement | null>(null);
  const attentionOrigins = new Map<
    string,
    { origin: HTMLElement; fallback: HTMLElement | null }
  >();
  let selectedEntity = $state<
    | { kind: 'task' | 'conversation'; id: string }
    | { kind: 'schedule'; id: string }
    | null
  >(null);
  let modalRefreshToken = $state(0);
  let workspaceGateEnabled = $state(false);
  let workspaceGateCaptured = $state(false);
  let workspacePersistenceStarted = false;
  // Default to the safe blocking-modal mode until the browser viewport is measured.
  let workspaceDevice = $state<'desktop' | 'tablet' | 'phone'>('phone');
  const conversationProjectionGate = new DashboardConversationProjectionGate();
  const readTracker = new DashboardConversationReadTracker({
    markRead: (conversationId) => api.conversations.markRead(conversationId),
    optimisticRead: (conversationId, readThrough) => {
      if (!conversations) return;
      conversations = conversations.map((conversation) => (
        conversation.conversation_id === conversationId
          ? {
              ...conversation,
              has_unread: false,
              ...(readThrough ? { last_read_at: readThrough } : {}),
            }
          : conversation
      ));
    },
    reconcile: async () => {
      await loadConversations({ observeOpenUnread: false });
    },
    isRetryable: (error) => {
      const status = asApiError(error).status ?? 0;
      return status === 0 || status === 408 || status === 429 || status >= 500;
    },
  });
  const workspaceManager = new WorkspaceManager(
    'desktop',
    { x: 0, y: 0, width: 1440, height: 900 },
    (window: WorkspaceWindowState) => {
      if (window.kind === 'attention') {
        const focusTarget = attentionOrigins.get(window.key);
        attentionOrigins.delete(window.key);
        queueMicrotask(() => restoreAttentionFocus(focusTarget));
      }
      const conversationId = window.kind === 'conversation'
        ? window.entityId
        : window.resolvedConversationId;
      if (conversationId) {
        pendingConversationInitialLoads.delete(conversationId);
        readTracker.close(conversationId);
      }
    },
    (window: WorkspaceWindowState, visible: boolean) => {
      const conversationId = window.kind === 'conversation'
        ? window.entityId
        : window.resolvedConversationId;
      if (!conversationId) return;
      if (visible && effectiveWorkspaceDevice() !== 'phone') readTracker.open(conversationId);
      else readTracker.close(conversationId);
    },
  );
  const openWorkspaceConversationIds = $derived(
    workspaceDevice === 'phone'
      ? []
      : $workspaceManager.filter((window) => !window.minimized).flatMap((window) => (
          window.kind === 'conversation'
            ? [window.entityId]
            : window.resolvedConversationId ? [window.resolvedConversationId] : []
        )),
  );
  const canonicalWorkspaceConversations = $derived(
    Object.values(workspaceConversationMetadata),
  );

  let showCreateTaskModal = $state(false);
  let showNewChatModal = $state(false);
  let newChatCreating = $state(false);
  let newChatError = $state('');
  let newChatAgentId = $state('');
  let newChatAgentProfileId = $state('');
  let creatingTask = $state(false);
  let createTaskDeps = $state<{ workflows: Workflow[]; projects: Project[]; skills: Skill[]; conversations: Conversation[] } | null>(null);
  const waitingConversationCount = $derived(
    conversations?.filter(isDashboardConversationWaiting).length ?? 0
  );
  const activeConversationCount = $derived(
    conversations?.filter(
      (conversation) =>
        !isDashboardConversationWaiting(conversation) &&
        isDashboardConversationActive(conversation)
    ).length ?? 0
  );
  const waitingCount = $derived(
    waitingConversationCount + (board?.columns?.paused?.total_count ?? 0)
  );
  const unreadConversationCount = $derived(conversations?.filter((conversation) => conversation.has_unread).length ?? 0);

  async function loadIssues(): Promise<void> {
    issuesLoading = true;
    try {
      issues = await api.dashboard.issues();
      issuesError = null;
    } catch (caught) {
      issuesError = asApiError(caught).message || 'Could not load system issues.';
    } finally {
      issuesLoading = false;
    }
  }

  async function loadDiagnostics(): Promise<void> {
    if (auth.getSnapshot().user?.role !== 'admin') {
      diagnostics = null;
      return;
    }
    try {
      diagnostics = await api.system.diagnostics();
    } catch {
      diagnostics = null;
    }
  }

  async function loadAgents(): Promise<void> {
    agentsLoading = true;
    try {
      const items = await api.agents.listAll({ include_system: false });
      agents = items.filter((agent) => !agent.hidden && !agent.disabled && !agent.is_system);
      agentsError = null;
    } catch (caught) {
      agentsError = asApiError(caught).message || 'Could not load agents.';
    } finally {
      agentsLoading = false;
    }
  }

  async function loadBoard(
    { invalidatedTaskIds = new Set<string>() }: {
      invalidatedTaskIds?: Set<string>;
    } = {},
  ): Promise<void> {
    const generation = ++boardLoadGeneration;
    const requestScopeKey = `${searchQuery}\u0000${selectedProjectId}`;
    boardLoadController?.abort();
    const controller = new AbortController();
    boardLoadController = controller;
    boardLoading = true;
    try {
      let nextBoard = await api.tasks.board({
        limit: 5,
        q: searchQuery || null,
        project_id: selectedProjectId || null,
        include_progress_summary: 1,
        include_attention_actions: 1,
        progress_limit: 5
      }, { signal: controller.signal });
      if (controller.signal.aborted || generation !== boardLoadGeneration) return;
      const initialPaused = nextBoard.columns.paused;
      if (initialPaused?.has_more) {
        const completePaused = await loadCompletePausedColumn(
          initialPaused,
          (cursor, signal) => api.tasks.boardColumn('paused', {
            cursor,
            limit: 100,
            include_progress_summary: 1,
            progress_limit: 100,
            q: searchQuery || null,
            project_id: selectedProjectId || null,
            include_attention_actions: 1,
          }, { signal }),
          controller.signal,
          () => generation === boardLoadGeneration,
        );
        if (!completePaused) return;
        nextBoard.columns.paused = completePaused;
      }
      if (controller.signal.aborted || generation !== boardLoadGeneration) return;
      if (
        board?.columns.done
        && boardScopeKey === requestScopeKey
      ) {
        nextBoard = reconcileTaskBoard(board, nextBoard, invalidatedTaskIds);
      }
      board = nextBoard;
      boardScopeKey = requestScopeKey;
      boardError = null;
    } catch (caught) {
      if (controller.signal.aborted || generation !== boardLoadGeneration) return;
      boardError = asApiError(caught).message || 'Could not load tasks.';
    } finally {
      if (generation === boardLoadGeneration) {
        boardLoading = false;
        boardLoadController = null;
      }
    }
  }

  function mergeTaskItems(
    current: TaskBoard['columns'][string]['items'],
    incoming: TaskBoard['columns'][string]['items'],
  ): TaskBoard['columns'][string]['items'] {
    const seen = new Set(current.map((item) => item.task_id));
    return [...current, ...incoming.filter((item) => !seen.has(item.task_id))];
  }

  async function loadMoreRecentTasks(): Promise<void> {
    const done = board?.columns.done;
    if (boardLoading || !done?.has_more || !done.cursor || recentTasksLoading) return;
    const requestScope = `${searchQuery}\u0000${selectedProjectId}`;
    const appendTicket = recentTaskAppendGate.start();
    const appendController = appendTicket.controller;
    const requestCursor = done.cursor;
    recentTasksLoading = true;
    recentTasksError = null;
    try {
      const page = await api.tasks.boardColumn('done', {
        cursor: done.cursor,
        limit: 20,
        q: searchQuery || null,
        project_id: selectedProjectId || null,
        include_attention_actions: 1,
      }, { signal: appendController.signal });
      if (
        appendController.signal.aborted
        || !board
        || requestScope !== `${searchQuery}\u0000${selectedProjectId}`
        || !recentTaskAppendGate.isCurrent(appendTicket)
      ) return;
      board = {
        ...board,
        columns: {
          ...board.columns,
          done: {
            ...page,
            cursor: page.cursor === requestCursor ? null : page.cursor,
            has_more: page.has_more && page.cursor !== requestCursor,
            items: mergeTaskItems(board.columns.done.items, page.items),
            groups: [...(board.columns.done.groups ?? []), ...(page.groups ?? []).filter(
              (group) => !(board?.columns.done.groups ?? []).some((item) => item.key === group.key),
            )],
          },
        },
      };
    } catch (caught) {
      if (
        !appendController.signal.aborted
        && requestScope === `${searchQuery}\u0000${selectedProjectId}`
        && recentTaskAppendGate.isCurrent(appendTicket)
      ) {
        recentTasksError = asApiError(caught).message || 'Could not load more tasks.';
      }
    } finally {
      if (recentTaskAppendGate.finish(appendTicket)) {
        recentTasksLoading = false;
      }
    }
  }

  function invalidateRecentTaskAppend(): void {
    recentTaskAppendGate.invalidate();
    recentTasksLoading = false;
    recentTasksError = null;
    if (!board?.columns.done) return;
    board = {
      ...board,
      columns: {
        ...board.columns,
        done: {
          ...board.columns.done,
          cursor: null,
          has_more: false,
        },
      },
    };
  }

  async function loadSchedules(): Promise<void> {
    try {
      schedules = await api.schedules.list({ enabled: true });
      schedulesError = null;
    } catch (caught) {
      schedulesError = asApiError(caught).message || 'Could not load schedules.';
    }
  }

  async function loadConversations(
    { observeOpenUnread = true, append = false, reset = false, reconcileIds = new Set<string>() }: {
      observeOpenUnread?: boolean;
      append?: boolean;
      reset?: boolean;
      reconcileIds?: Set<string>;
    } = {},
  ): Promise<void> {
    if (append && (!conversationsHasMore || !conversationsCursor || conversationsAppendLoading)) {
      return;
    }
    const requestScopeKey = `${searchQuery}\u0000${selectedProjectId}`;
    const metadataGeneration = ++workspaceConversationGeneration;
    const loadGeneration = append ? null : conversationProjectionGate.begin();
    if (!append) {
      conversationLoadController?.abort();
      conversationLoadController = new AbortController();
      if (reset) {
        conversations = null;
        conversationsCursor = null;
        conversationsHasMore = false;
      }
    } else {
      conversationAppendController = new AbortController();
    }
    const requestController = append ? conversationAppendController : conversationLoadController;
    const requestCursor = append ? conversationsCursor : null;
    if (append) conversationsAppendLoading = true;
    else conversationsLoading = true;
    if (append) conversationsAppendError = null;
    try {
      const [page, reconciledDetails] = await Promise.all([
        api.conversations.list(requestCursor, {
          status: 'active',
          contextType: 'web',
          includeAgentDirect: false,
          includeAttentionActions: true,
        projectId: selectedProjectId || null,
        query: searchQuery || null,
        limit: 20,
        }, { signal: requestController?.signal }),
        Promise.all([...reconcileIds].map((conversationId) => (
          api.conversations.detail(conversationId, {
            includeState: false,
            includeAttentionActions: true,
          }).catch(() => null)
        ))),
      ]);
      if (
        requestScopeKey !== `${searchQuery}\u0000${selectedProjectId}`
        || (loadGeneration !== null && !conversationProjectionGate.isCurrent(loadGeneration))
      ) return;
      const next = page.items.filter(isDashboardTopicConversation);
      const canonicalNext = next.map((conversation) => (
        cacheConversationMetadata(conversation, metadataGeneration)
      ));
      if (append) {
        const seen = new Set((conversations ?? []).map((item) => item.conversation_id));
        const appended = reconcileConversationProjection(
          [],
          canonicalNext.filter((item) => !seen.has(item.conversation_id)),
          [],
          attentionActionTombstones,
        );
        conversations = [...(conversations ?? []), ...appended];
      } else {
        const reconciled = reconciledDetails
          .filter((item): item is Conversation => Boolean(item));
        conversations = reconcileConversationProjection(
          conversations ?? [],
          canonicalNext,
          reconciled,
          attentionActionTombstones,
          matchesDashboardConversationFilters,
        ).map((item) => (
          canonicalNext.some((canonical) => canonical.conversation_id === item.conversation_id)
            ? item
            : cacheConversationMetadata(item, metadataGeneration)
        ));
      }
      const repeatedCursor = append && page.cursor === requestCursor;
      const preserveLoadedTail = (
        !append
        && !reset
        && conversationsScopeKey === requestScopeKey
      );
      const retainedCursor = preserveLoadedTail ? conversationsCursor : null;
      const retainedHasMore = preserveLoadedTail ? conversationsHasMore : false;
      conversationsCursor = repeatedCursor ? null : (retainedCursor ?? page.cursor);
      conversationsHasMore = (retainedCursor ? retainedHasMore : page.has_more) && !repeatedCursor;
      conversationsScopeKey = requestScopeKey;
      conversationsError = null;
      if (observeOpenUnread) void readTracker.observe(canonicalNext);
    } catch (caught) {
      if (
        requestScopeKey !== `${searchQuery}\u0000${selectedProjectId}`
        || (loadGeneration !== null && !conversationProjectionGate.isCurrent(loadGeneration))
      ) return;
      if (append) {
        conversationsAppendError = asApiError(caught).message || 'Could not load more conversations.';
      } else {
        conversationsError = asApiError(caught).message || 'Could not load conversations.';
      }
    } finally {
      if (append && requestController === conversationAppendController) {
        conversationsAppendLoading = false;
        conversationAppendController = null;
      }
      else if (requestController === conversationLoadController) conversationsLoading = false;
    }
  }

  function matchesDashboardConversationFilters(conversation: Conversation): boolean {
    if (!isDashboardTopicConversation(conversation) || conversation.status !== 'active') return false;
    if (selectedProjectId && conversation.project_id !== selectedProjectId) return false;
    // Text search is server-owned. Previously loaded rows retain canonical
    // membership until a later server page supersedes them.
    return true;
  }

  function applySearchFilters(): void {
    const next = searchInput.trim();
    pendingSearchReset = false;
    if (next === searchQuery) return;
    searchQuery = next;
    void Promise.allSettled([loadBoard(), loadConversations({ reset: true })]);
  }

  function scheduleSearch(): void {
    if (searchDebounce) clearTimeout(searchDebounce);
    const action = searchResetAction(searchQuery, pendingSearchReset, searchInput);
    if (action === 'none') return;
    if (action === 'restore') {
      pendingSearchReset = false;
      void loadBoard();
      return;
    }
    if (action === 'invalidate') {
      invalidateRecentTaskAppend();
      pendingSearchReset = true;
    }
    searchDebounce = setTimeout(applySearchFilters, 250);
  }

  function clearSearch(): void {
    searchInput = '';
    if (searchDebounce) clearTimeout(searchDebounce);
    const action = searchResetAction(searchQuery, pendingSearchReset, searchInput);
    if (action === 'none') return;
    if (action === 'restore') {
      pendingSearchReset = false;
      void loadBoard();
      return;
    }
    if (action === 'invalidate') {
      invalidateRecentTaskAppend();
      pendingSearchReset = true;
    }
    applySearchFilters();
  }

  function changeProject(event: Event): void {
    invalidateRecentTaskAppend();
    selectedProjectId = (event.currentTarget as HTMLSelectElement).value;
    void Promise.allSettled([loadBoard(), loadConversations({ reset: true })]);
  }

  function cacheConversationMetadata(
    conversation: Conversation,
    responseGeneration = ++workspaceConversationGeneration,
  ): Conversation {
    const appliedGeneration = appliedWorkspaceConversationGenerations.get(
      conversation.conversation_id,
    ) ?? 0;
    const appliedConversation = workspaceConversationMetadata[conversation.conversation_id];
    if (responseGeneration < appliedGeneration && appliedConversation) {
      return appliedConversation;
    }
    const pendingPatch = pendingWorkspaceConversationPatches.get(conversation.conversation_id);
    const canonical = pendingPatch && pendingPatch.generation > responseGeneration
      ? { ...conversation, ...pendingPatch.patch }
      : conversation;
    if (pendingPatch && pendingPatch.generation <= responseGeneration) {
      pendingWorkspaceConversationPatches.delete(conversation.conversation_id);
    }
    appliedWorkspaceConversationGenerations.set(
      conversation.conversation_id,
      Math.max(appliedGeneration, responseGeneration),
    );
    workspaceConversationMetadata = {
      ...workspaceConversationMetadata,
      [canonical.conversation_id]: canonical,
    };
    if (pendingConversationInitialLoads.delete(canonical.conversation_id)) {
      void readTracker.initialLoaded(canonical);
    }
    for (const window of workspaceManager.snapshot()) {
      const conversationId = window.kind === 'conversation'
        ? window.entityId
        : window.resolvedConversationId;
      if (conversationId !== canonical.conversation_id) continue;
      const title = canonical.title ?? 'Conversation';
      const status = canonical.status ?? null;
      if (
        window.title !== title
        || window.status !== status
        || window.agentId !== (canonical.agent_id ?? null)
      ) {
        workspaceManager.updateMetadata(window.key, {
          title,
          status,
          agentId: canonical.agent_id ?? null,
        });
      }
    }
    return canonical;
  }

  function patchConversationMetadata(
    patch: DashboardRealtimeBatch['conversationPatches'][number],
  ): void {
    const generation = ++workspaceConversationGeneration;
    const current = workspaceConversationMetadata[patch.conversation_id];
    const relevant = Boolean(
      current
      || conversations?.some((item) => item.conversation_id === patch.conversation_id)
      || workspaceManager.snapshot().some((window) => (
        (window.kind === 'conversation' && window.entityId === patch.conversation_id)
        || window.resolvedConversationId === patch.conversation_id
      ))
    );
    if (relevant) {
      const previous = pendingWorkspaceConversationPatches.get(patch.conversation_id);
      pendingWorkspaceConversationPatches.set(patch.conversation_id, {
        generation,
        patch: { ...previous?.patch, ...patch },
      });
      appliedWorkspaceConversationGenerations.set(patch.conversation_id, generation);
    }
    if (conversations) {
      conversations = conversations.map((conversation) => (
        conversation.conversation_id === patch.conversation_id
          ? { ...conversation, ...patch }
          : conversation
      ));
    }
    if (current) {
      workspaceConversationMetadata = {
        ...workspaceConversationMetadata,
        [patch.conversation_id]: { ...current, ...patch },
      };
    }
    const title = typeof patch.title === 'string' ? patch.title : null;
    const status = typeof patch.status === 'string' ? patch.status : undefined;
    if (title === null && status === undefined) return;
    for (const window of workspaceManager.snapshot()) {
      const conversationId = window.kind === 'conversation'
        ? window.entityId
        : window.resolvedConversationId;
      if (conversationId !== patch.conversation_id) continue;
      workspaceManager.updateMetadata(window.key, {
        title: title ?? window.title,
        ...(status !== undefined ? { status } : {}),
      });
    }
  }

  async function refreshWorkspaceConversation(conversationId: string): Promise<void> {
    const isOpen = workspaceManager.snapshot().some((window) => (
      (window.kind === 'conversation' && window.entityId === conversationId)
      || window.resolvedConversationId === conversationId
    ));
    if (!isOpen || requestedWorkspaceConversationIds.has(conversationId)) return;
    const metadataGeneration = ++workspaceConversationGeneration;
    requestedWorkspaceConversationIds.add(conversationId);
    try {
      cacheConversationMetadata(
        await api.conversations.detail(conversationId, { includeState: false }),
        metadataGeneration,
      );
    } catch {
      // The canonical lane refresh remains the fallback for transient detail failures.
    } finally {
      requestedWorkspaceConversationIds.delete(conversationId);
    }
  }

  async function refreshAll(): Promise<void> {
    await Promise.allSettled([
      loadIssues(),
      loadDiagnostics(),
      loadAgents(),
      api.projects.list().then((items) => { projects = items; }),
      loadBoard(),
      loadSchedules(),
      loadConversations(),
    ]);
  }

  function flushRealtimeLoads(batch: DashboardRealtimeBatch): void {
    for (const patch of batch.conversationPatches) patchConversationMetadata(patch);
    if (batch.loads.has('conversations')) {
      const changedIds = new Set([
        ...batch.conversationPatches.map((patch) => patch.conversation_id),
        ...batch.modalDetails
          .filter((detail) => detail.kind === 'conversation')
          .map((detail) => detail.id),
      ]);
      void loadConversations({ reconcileIds: changedIds });
    }
    if (batch.loads.has('tasks')) {
      const invalidatedTaskIds = new Set(
        batch.modalDetails
          .filter((detail) => detail.kind === 'task')
          .map((detail) => detail.id),
      );
      void loadBoard({ invalidatedTaskIds });
    }
    if (batch.loads.has('schedules')) void loadSchedules();
    if (batch.loads.has('issues')) void loadIssues();
    if (dashboardBatchRefreshesModal(batch, selectedEntity)) modalRefreshToken += 1;
    for (const detail of batch.modalDetails) {
      workspaceManager.invalidate(detail.kind, detail.id);
      if (detail.kind === 'conversation') void refreshWorkspaceConversation(detail.id);
    }
  }

  function useWorkspaceWindows(): boolean {
    return workspaceGateEnabled && effectiveWorkspaceDevice() !== 'phone';
  }

  function effectiveWorkspaceDevice(): 'desktop' | 'tablet' | 'phone' {
    if (typeof window !== 'undefined' && window.innerWidth < 768) return 'phone';
    return workspaceDevice;
  }

  function showWorkspaceCap(limit: number): void {
    addToast(
      `Workspace limit reached (${limit}). Close a window before opening another.`,
      'error',
      4_000,
      'Could not open workspace window',
    );
  }

  function taskSummary(taskId: string): { title: string; status: string | null; agentId: string | null } {
    for (const column of Object.values(board?.columns ?? {})) {
      const task = column?.items?.find((item) => item.task_id === taskId);
      if (task) return { title: task.title, status: task.status, agentId: task.agent_id };
    }
    return { title: 'Task', status: null, agentId: null };
  }

  function openTask(taskId: string): void {
    if (useWorkspaceWindows()) {
      const summary = taskSummary(taskId);
      const result = workspaceManager.open({
        kind: 'task',
        entityId: taskId,
        title: summary.title,
        status: summary.status,
        agentId: summary.agentId,
        canonicalHref: `/tasks/${taskId}`,
      });
      if (result.status === 'capped') showWorkspaceCap(result.limit);
      return;
    }
    selectedEntity = { kind: 'task', id: taskId };
  }

  function openConversation(conversationId: string): void {
    conversationProjectionGate.invalidate();
    readTracker.open(conversationId);
    if (useWorkspaceWindows()) {
      const conversation = conversations?.find((item) => item.conversation_id === conversationId);
      const result = workspaceManager.open({
        kind: 'conversation',
        entityId: conversationId,
        title: conversation?.title ?? 'Conversation',
        status: conversation?.status ?? null,
        agentId: conversation?.agent_id ?? null,
        canonicalHref: `/chat/${conversationId}`,
      });
      if (result.status === 'capped') {
        readTracker.close(conversationId);
        showWorkspaceCap(result.limit);
      }
      return;
    }
    selectedEntity = { kind: 'conversation', id: conversationId };
  }

  function openSchedule(schedule: Schedule): void {
    if (useWorkspaceWindows()) {
      const result = workspaceManager.open({
        kind: 'schedule',
        entityId: schedule.schedule_id,
        title: schedule.name,
        status: schedule.consecutive_errors > 0 || schedule.last_run_status === 'failed'
          ? 'failed'
          : schedule.last_run_status,
        agentId: schedule.agent_id,
        canonicalHref: `/schedules/${schedule.schedule_id}`,
      });
      if (result.status === 'capped') showWorkspaceCap(result.limit);
      return;
    }
    selectedEntity = { kind: 'schedule', id: schedule.schedule_id };
  }

  function openAttention(action: AttentionActionSummary, origin: HTMLElement): void {
    const fallback = origin.closest<HTMLElement>(
      '[data-testid^="dashboard-task-row-"], [data-testid^="dashboard-conversation-row-"]',
    );
    if (useWorkspaceWindows()) {
      const result = workspaceManager.open({
        kind: 'attention',
        entityId: action.action_id,
        title: action.title,
        status: action.status,
        canonicalHref: action.source.task_id
          ? `/tasks/${action.source.task_id}`
          : `/chat/${action.source.conversation_id}`,
        ephemeral: true,
      });
      if (result.status === 'capped') showWorkspaceCap(result.limit);
      else attentionOrigins.set(result.key, { origin, fallback });
      return;
    }
    selectedAttentionOrigin = origin;
    selectedAttentionFallback = fallback;
    selectedAttentionTitle = action.title;
    selectedAttentionId = action.action_id;
  }

  function closeAttentionDialog(): void {
    selectedAttentionId = null;
    selectedAttentionTitle = '';
    const origin = selectedAttentionOrigin;
    const fallback = selectedAttentionFallback;
    selectedAttentionOrigin = null;
    selectedAttentionFallback = null;
    queueMicrotask(() => restoreAttentionFocus({ origin, fallback }));
  }

  function restoreAttentionFocus(
    target: { origin: HTMLElement | null; fallback: HTMLElement | null } | undefined,
  ): void {
    const element = target?.origin?.isConnected ? target.origin : target?.fallback;
    if (element?.isConnected) element.focus({ preventScroll: true });
  }

  async function attentionSettled(action: AttentionActionDetail): Promise<void> {
    if (selectedAttentionId === action.action_id) closeAttentionDialog();
    await Promise.allSettled([
      action.source.task_id
        ? loadBoard({
            invalidatedTaskIds: new Set([action.source.task_id]),
          })
        : Promise.resolve(),
      loadConversations({ reconcileIds: new Set([action.source.conversation_id]) }),
    ]);
  }

  async function attentionUnavailable(): Promise<void> {
    await Promise.allSettled([loadBoard(), loadConversations()]);
  }

  async function taskMutationSettled(taskId: string, changed: boolean): Promise<void> {
    await loadBoard({
      invalidatedTaskIds: changed ? new Set([taskId]) : new Set(),
    });
  }

  function closeEntityModal(): void {
    if (selectedEntity?.kind === 'conversation') {
      conversationProjectionGate.invalidate();
      pendingConversationInitialLoads.delete(selectedEntity.id);
      readTracker.close(selectedEntity.id);
    }
    selectedEntity = null;
  }

  function handleConversationInitialLoaded(conversationId: string): void {
    const conversation = conversations?.find((item) => item.conversation_id === conversationId)
      ?? workspaceConversationMetadata[conversationId];
    if (!conversation) {
      pendingConversationInitialLoads.add(conversationId);
      return;
    }
    void readTracker.initialLoaded(conversation);
  }

  function handleAgentConversationResolved(key: string, conversationId: string): void {
    const window = workspaceManager.snapshot().find((item) => item.key === key);
    if (window && !window.minimized) readTracker.open(conversationId);
    void refreshWorkspaceConversation(conversationId);
  }

  function selectAgent(agent: Agent): void {
    if (useWorkspaceWindows()) {
      const result = workspaceManager.open({
        kind: 'agent',
        entityId: agent.agent_id,
        title: agent.display_name ?? agent.name,
        status: 'direct chat',
        agentId: agent.agent_id,
        canonicalHref: `/chat/new?agent=${encodeURIComponent(agent.agent_id)}`,
      });
      if (result.status === 'capped') showWorkspaceCap(result.limit);
      return;
    }
    selectedAgent = agent;
  }

  function closeAgentModal(): void {
    selectedAgent = null;
  }

  async function ensureCreateTaskDeps(): Promise<void> {
    if (createTaskDeps) return;
    try {
      const [workflows, projects, skills, activeConversations] = await Promise.all([
        api.workflows.listAll().catch(() => []),
        api.projects.list().catch(() => []),
        api.skills.list().catch(() => []),
        api.conversations.list(null, { status: 'active' }).then((page) => page.items).catch(() => [])
      ]);
      createTaskDeps = { workflows, projects, skills, conversations: activeConversations };
    } catch {
      createTaskDeps = { workflows: [], projects: [], skills: [], conversations: [] };
    }
  }

  async function openNewTask(): Promise<void> {
    showCreateTaskModal = true;
    await ensureCreateTaskDeps();
  }

  async function handleCreateTask(form: Parameters<typeof api.tasks.create>[0]): Promise<void> {
    creatingTask = true;
    try {
      await api.tasks.create(form);
      showCreateTaskModal = false;
      addToast('Draft task created.', 'success');
      await loadBoard();
    } catch (caught) {
      addToast(asApiError(caught).message || 'Could not create the task.', 'error');
    } finally {
      creatingTask = false;
    }
  }

  function openNewChat(): void {
    newChatAgentId = defaultNewChatAgentId(agents) ?? '';
    newChatAgentProfileId = '';
    newChatError = '';
    showNewChatModal = true;
  }

  function closeNewChat(): void {
    if (newChatCreating) return;
    showNewChatModal = false;
    newChatError = '';
  }

  async function createNewChat(): Promise<void> {
    if (!newChatAgentId) {
      newChatError = 'Create or activate a primary agent before starting a conversation.';
      return;
    }
    newChatCreating = true;
    newChatError = '';
    try {
      const conversation = await api.conversations.create({
        agent_id: newChatAgentId,
        agent_profile_id: newChatAgentProfileId || null,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {},
        },
      });
      conversations = [conversation, ...(conversations ?? []).filter(
        (item) => item.conversation_id !== conversation.conversation_id,
      )];
      showNewChatModal = false;
      openConversation(conversation.conversation_id);
      void loadConversations();
    } catch (caught) {
      newChatError = asApiError(caught).message || 'Could not start a new chat.';
    } finally {
      newChatCreating = false;
    }
  }

  $effect(() => {
    void refreshAll();
  });

  onMount(() => {
    void loadUserPreferences(auth.getSnapshot().user?.email);
    const syncDevice = () => {
      const previousDevice = workspaceDevice;
      const nextDevice = workspaceDeviceClass(
        window.innerWidth,
        window.matchMedia?.('(any-pointer: coarse)').matches ?? false,
      );
      workspaceDevice = nextDevice;
      workspaceManager.setDevice(workspaceDevice);
      if (previousDevice !== nextDevice && (previousDevice === 'phone' || nextDevice === 'phone')) {
        for (const workspaceWindow of workspaceManager.snapshot()) {
          if (workspaceWindow.minimized) continue;
          const conversationId = workspaceWindow.kind === 'conversation'
            ? workspaceWindow.entityId
            : workspaceWindow.resolvedConversationId;
          if (!conversationId) continue;
          if (nextDevice === 'phone') readTracker.close(conversationId);
          else readTracker.open(conversationId);
        }
      }
    };
    syncDevice();
    window.addEventListener('resize', syncDevice, { passive: true });
    const unsubscribe = subscribeDashboardRealtime(wsClient, flushRealtimeLoads);
    return () => {
      boardLoadController?.abort();
      recentTaskAppendGate.invalidate();
      conversationLoadController?.abort();
      conversationAppendController?.abort();
      attentionActionTombstones.clear();
      if (searchDebounce) clearTimeout(searchDebounce);
      unsubscribe();
      window.removeEventListener('resize', syncDevice);
    };
  });

  $effect(() => {
    if (workspaceGateCaptured || !$userPreferencesReady) return;
    workspaceGateEnabled = $userPreferences.display.dashboard_workspace_windows;
    workspaceGateCaptured = true;
  });

  $effect(() => {
    if (!$userPreferencesReady || !workspaceGateEnabled) return;
    if (!workspaceGateEnabled || workspacePersistenceStarted || typeof window === 'undefined') return;
    workspacePersistenceStarted = true;
    const storageKey = dashboardWorkspaceStorageKey(auth.getSnapshot().user?.email);
    try {
      workspaceManager.hydrate(window.localStorage.getItem(storageKey));
    } catch {
      // Ignore unavailable browser storage and keep an empty workspace.
    }
    const unsubscribePersistence = workspaceManager.subscribe((windows) => {
      try {
        window.localStorage.setItem(
          storageKey,
          serializeWorkspaceWindows(workspaceManager.persistentSnapshot()),
        );
      } catch {
        // Workspace persistence is best effort when browser storage is unavailable.
      }
    });
    return unsubscribePersistence;
  });

  $effect(() => {
    for (const workspaceWindow of $workspaceManager) {
      const conversationId = workspaceWindow.kind === 'conversation'
        ? workspaceWindow.entityId
        : workspaceWindow.resolvedConversationId;
      if (conversationId && !workspaceConversationMetadata[conversationId]) {
        void refreshWorkspaceConversation(conversationId);
      }
    }
  });
</script>

<svelte:head>
  <title>Cognis</title>
</svelte:head>

<div
  class="mx-auto flex w-full min-w-0 max-w-[100rem] flex-col gap-4 px-3 py-3 sm:px-5 lg:px-6 md:h-full md:min-h-0"
  data-testid="control-center"
>
  <header class="shrink-0">
    <div>
      <h1 class="text-xl font-semibold text-white">Cognis</h1>
      <p class="text-xs text-slate-400" data-testid="control-center-summary">
        {board?.columns?.running?.total_count ?? 0} running · {activeConversationCount} active chats
        {#if waitingCount > 0} · {waitingCount} waiting for you{/if}
        {#if unreadConversationCount > 0} · {unreadConversationCount} unread{/if}
      </p>
    </div>
  </header>

  <div class="shrink-0">
    <IssuesStrip
      issues={displayedIssues}
      loading={issuesLoading}
      error={issuesError}
      onRetry={() => void refreshAll()}
      onDismiss={loadIssues}
    />
  </div>

  <div class="flex shrink-0 items-center gap-2" data-testid="dashboard-search-filters">
    <div class="relative min-w-0 flex-1">
      <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
      <Input
        bind:value={searchInput}
        class="pl-9 pr-9"
        placeholder="Search tasks and conversations"
        aria-label="Search tasks and conversations"
        oninput={scheduleSearch}
      />
      {#if searchInput}
        <button type="button" aria-label="Clear dashboard search" class="absolute right-2 top-1.5 rounded-lg p-1 text-slate-500 hover:bg-slate-800 hover:text-white" onclick={clearSearch}>
          <X class="h-4 w-4" />
        </button>
      {/if}
    </div>
    <select class="h-9 max-w-48 rounded-xl border border-slate-700 bg-slate-950 px-3 text-xs text-slate-200" aria-label="Filter dashboard by project" value={selectedProjectId} onchange={changeProject}>
      <option value="">All projects</option>
      {#each projects as project (project.project_id)}
        <option value={project.project_id}>{project.name}</option>
      {/each}
    </select>
    {#if boardLoading || conversationsLoading}<span class="sr-only" role="status">Loading search results</span>{/if}
  </div>

  <div
    class="grid min-w-0 grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)] md:min-h-0 md:flex-1"
    data-testid="control-center-primary-grid"
  >
    <TasksSection
      {board}
      {schedules}
      {agents}
      loading={boardLoading}
      error={boardError}
      {schedulesError}
      onOpenTask={openTask}
      onOpenSchedule={openSchedule}
      onOpenAttention={openAttention}
      onNewTask={() => void openNewTask()}
      onRetry={() => void loadBoard()}
      onSchedulesRetry={() => void loadSchedules()}
      recentHasMore={board?.columns.done?.has_more ?? false}
      recentLoading={recentTasksLoading}
      recentError={recentTasksError}
      onLoadMoreRecent={() => void loadMoreRecentTasks()}
    />

    <ConversationsSection
      {conversations}
      {agents}
      loading={conversationsLoading}
      error={conversationsError}
      {agentsLoading}
      {agentsError}
      openConversationId={selectedEntity?.kind === 'conversation' ? selectedEntity.id : null}
      openConversationIds={openWorkspaceConversationIds}
      onOpenConversation={openConversation}
      onOpenAttention={openAttention}
      onSelectAgent={selectAgent}
      onNewChat={openNewChat}
      onRetry={() => void loadConversations()}
      onAgentsRetry={() => void loadAgents()}
      recentHasMore={conversationsHasMore}
      recentLoading={conversationsAppendLoading}
      recentError={conversationsAppendError}
      onLoadMoreRecent={() => void loadConversations({ append: true })}
    />
  </div>

</div>

{#if selectedEntity}
  <DashboardEntityModal
    kind={selectedEntity.kind}
    taskId={selectedEntity.kind === 'task' ? selectedEntity.id : null}
    conversationId={selectedEntity.kind === 'conversation' ? selectedEntity.id : null}
    scheduleId={selectedEntity.kind === 'schedule' ? selectedEntity.id : null}
    {agents}
    {modalRefreshToken}
    onClose={closeEntityModal}
    onConversationInitialLoaded={handleConversationInitialLoaded}
    onTaskMutationSettled={taskMutationSettled}
  />
{/if}

{#if workspaceGateEnabled && effectiveWorkspaceDevice() !== 'phone'}
  <WorkspaceLayer
    manager={workspaceManager}
    {agents}
    conversations={canonicalWorkspaceConversations}
    onConversationInitialLoaded={handleConversationInitialLoaded}
    onAgentConversationResolved={handleAgentConversationResolved}
    onCapacityCapped={showWorkspaceCap}
    onAttentionSettled={attentionSettled}
    onAttentionUnavailable={attentionUnavailable}
    onTaskMutationSettled={taskMutationSettled}
    deviceClass={effectiveWorkspaceDevice()}
  />
{/if}

{#if selectedAttentionId}
  <AttentionActionDialog
    actionId={selectedAttentionId}
    title={selectedAttentionTitle}
    onClose={closeAttentionDialog}
    onSettled={attentionSettled}
    onUnavailable={attentionUnavailable}
  />
{/if}

{#if selectedAgent}
  <AgentQuickChatModal agent={selectedAgent} onClose={closeAgentModal} />
{/if}

{#if showNewChatModal}
  <NewChatModal
    {agents}
    bind:selectedAgentId={newChatAgentId}
    bind:selectedAgentProfileId={newChatAgentProfileId}
    title="Start a new chat"
    description="Choose the primary agent and profile for this new web conversation."
    busy={newChatCreating}
    error={newChatError}
    oncancel={closeNewChat}
    onconfirm={() => void createNewChat()}
  />
{/if}

{#if showCreateTaskModal && createTaskDeps}
  <CreateTaskModal
    {agents}
    workflows={createTaskDeps.workflows}
    projects={createTaskDeps.projects}
    skills={createTaskDeps.skills}
    conversations={createTaskDeps.conversations}
    creating={creatingTask}
    onclose={() => { showCreateTaskModal = false; }}
    oncreate={handleCreateTask}
  />
{/if}
