<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount, untrack } from 'svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';

  import { api, asApiError } from '$lib/api/client';
  import { clearPersistedScroll } from '$lib/actions/scrollPersist';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import TaskCard from '$lib/components/tasks/TaskCard.svelte';
  import CreateTaskModal from '$lib/components/tasks/CreateTaskModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { workspaceHealth } from '$lib/system';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import { TASK_BOARD_COLUMNS, boardColumnForStatus, taskBoardColumnFromSearchParams, taskBoardUrlForState, taskFiltersFromSearchParams, type TaskFilterState, type TaskBoardColumnId } from '$lib/tasks';
  import type { Agent, Conversation, Project, Skill, TaskBoardDoneGroup, TaskBoardItem, Workflow } from '$lib/types/api';

  // ---------------------------------------------------------------------------
  // Reactive state
  // ---------------------------------------------------------------------------

  let loading = $state(true);
  let loadingTimedOut = $state(false);
  let creating = $state(false);
  let error = $state('');
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let filterWorkflows = $state<Workflow[]>([]);
  let projects = $state<Project[]>([]);
  let skills = $state<Skill[]>([]);
  let conversations = $state<Conversation[]>([]);
  let showCreateModal = $state(false);
  let showEmptyDesktopColumns = $state(false);
  let expandedDesktopColumns = $state<Set<TaskBoardColumnId>>(new Set());
  let collapsedDesktopColumns = $state<Set<TaskBoardColumnId>>(new Set());

  // Multi-select
  let selectedIds = $state<Set<string>>(new Set());
  let lastClickedId = $state<string | null>(null);

  // Drag state
  let dragState = $state<{ taskId: string; column: TaskBoardColumnId } | null>(null);
  let dropTargetColumn = $state<TaskBoardColumnId | null>(null);

  // Mobile column picker: on small screens we show one column at a time with
  // a segmented-style selector instead of a horizontally scrollable kanban.
  let mobileActiveColumn = $state<TaskBoardColumnId>('running');

  let pollTimer: number | null = null;
  let visibilityHandler: (() => void) | null = null;
  let loadTimeoutTimer: number | null = null;
  let boardReplaceRequestId = 0;
  let boardRefreshRequestId = 0;
  let boardStateVersion = 0;
  let workflowFilterLoadKey = 0;
  let filterReloadTimer: number | null = null;

  const TASK_BOARD_LOAD_TIMEOUT_MS = 10000;
  const TASK_BOARD_PAGE_SIZE = 20;

  type BoardColumnState = {
    items: TaskBoardItem[];
    groups: TaskBoardDoneGroup[];
    cursor: string | null;
    hasMore: boolean;
    totalCount: number;
    loading: boolean;
  };

  type DoneGroupHistoryState = {
    items: TaskBoardItem[];
    cursor: string | null;
    hasMore: boolean;
    totalCount: number;
    loading: boolean;
    loaded: boolean;
    filterKey: string;
  };

  function emptyBoardColumnState(): BoardColumnState {
    return { items: [], groups: [], cursor: null, hasMore: false, totalCount: 0, loading: false };
  }

  function emptyDoneGroupHistoryState(): DoneGroupHistoryState {
    return { items: [], cursor: null, hasMore: false, totalCount: 0, loading: false, loaded: false, filterKey: '' };
  }

  function initialBoardColumns(): Record<TaskBoardColumnId, BoardColumnState> {
    return Object.fromEntries(
      TASK_BOARD_COLUMNS.map((column) => [column.id, emptyBoardColumnState()])
    ) as Record<TaskBoardColumnId, BoardColumnState>;
  }

  let boardColumns = $state<Record<TaskBoardColumnId, BoardColumnState>>(initialBoardColumns());
  let expandedDoneGroups = $state<Set<string>>(new Set());
  let doneGroupHistories = $state<Record<string, DoneGroupHistoryState>>({});

  let filters = $state<TaskFilterState>({
    search: '',
    agentId: '',
    workflowId: '',
    projectId: '',
    status: ''
  });

  // ---------------------------------------------------------------------------
  // URL-persisted filters + mobile column selection
  //
  // Filters live in the URL query so switching tabs and coming back keeps
  // the user's view, and reloading the page preserves the state. The
  // `mobileActiveColumn` choice is persisted as `?col=` because it
  // changes which column is visible on small screens.
  // ---------------------------------------------------------------------------

  let urlHydrated = false;
  let urlSyncTimer: number | null = null;

  function hydrateFiltersFromUrl(searchParams: URLSearchParams): void {
    const next = taskFiltersFromSearchParams(searchParams);
    if (
      next.search !== filters.search ||
      next.agentId !== filters.agentId ||
      next.workflowId !== filters.workflowId ||
      next.projectId !== filters.projectId ||
      next.status !== filters.status
    ) {
      filters = next;
    }
    const column = taskBoardColumnFromSearchParams(searchParams);
    if (column !== mobileActiveColumn) {
      mobileActiveColumn = column;
    }
  }

  function buildFiltersUrl(): string {
    return taskBoardUrlForState(filters, mobileActiveColumn);
  }

  function scheduleFiltersUrlSync(): void {
    if (typeof window === 'undefined') return;
    if (!urlHydrated) return;
    if (urlSyncTimer !== null) window.clearTimeout(urlSyncTimer);
    urlSyncTimer = window.setTimeout(() => {
      urlSyncTimer = null;
      const next = buildFiltersUrl();
      const current = untrack(() => $page.url.pathname + $page.url.search);
      if (next !== current) {
        void goto(next, { replaceState: true, noScroll: true, keepFocus: true });
      }
    }, 200);
  }

  // Sync: URL → local state. Fires when the user navigates (back/forward
  // buttons or active-tab tap). The hydrate guards against assignment
  // when the URL already matches local state, so this does not loop with
  // `scheduleFiltersUrlSync`.
  $effect(() => {
    const search = $page.url.search;
    untrack(() => {
      hydrateFiltersFromUrl(new URLSearchParams(search));
      urlHydrated = true;
    });
  });

  // Sync: local state → URL (debounced). Reads all filter fields so
  // Svelte tracks them; writes through `scheduleFiltersUrlSync`.
  $effect(() => {
    void filters.search;
    void filters.agentId;
    void filters.workflowId;
    void filters.projectId;
    void filters.status;
    void mobileActiveColumn;
    scheduleFiltersUrlSync();
  });

  // ---------------------------------------------------------------------------
  // Derived state
  // ---------------------------------------------------------------------------

  let filtersActive = $derived(Boolean(filters.search || filters.agentId || filters.workflowId || filters.projectId || filters.status));
  let tasks = $derived([
    ...TASK_BOARD_COLUMNS.flatMap((column) => boardColumns[column.id].items),
    ...Object.values(doneGroupHistories).flatMap((history) => history.items)
  ]);
  let selectedCount = $derived(selectedIds.size);

  // Bulk action counts
  let submitCount = $derived([...selectedIds].filter((id) => tasks.find((t) => t.task_id === id)?.status === 'draft').length);
  let cancelCount = $derived([...selectedIds].filter((id) => {
    const s = tasks.find((t) => t.task_id === id)?.status;
    return s && ['queued', 'ready', 'running', 'paused', 'draft'].includes(s);
  }).length);
  let deleteCount = $derived([...selectedIds].filter((id) => {
    const s = tasks.find((t) => t.task_id === id)?.status;
    return s && ['draft', 'completed', 'failed', 'cancelled'].includes(s);
  }).length);

  // ---------------------------------------------------------------------------
  // Drag transitions
  // ---------------------------------------------------------------------------

  const DRAG_TRANSITIONS: Record<string, 'submit' | 'pause' | 'resume' | 'cancel'> = {
    'draft->queued': 'submit',
    'running->paused': 'pause',
    'paused->queued': 'resume',
  };

  function isDragTransitionValid(source: TaskBoardColumnId, target: TaskBoardColumnId): boolean {
    if (source === target) return true;
    if (target === 'done') return source !== 'done';
    return `${source}->${target}` in DRAG_TRANSITIONS;
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function isLlmUnavailableForSetup(): boolean {
    const llmDetails = JSON.stringify($workspaceHealth.health?.providers?.llm ?? {}).toLowerCase();
    return llmDetails.includes('no llm model configured') || llmDetails.includes('not configured');
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) return 'auto';
    return workflows.find((w) => w.workflow_id === workflowId)?.name ?? workflowId;
  }

  function taskActivityLabel(task: TaskBoardItem): string | null {
    return task.updated_at ?? task.completed_at ?? task.started_at ?? task.created_at;
  }

  let doneTaskGroups = $derived(boardColumns.done.groups);

  function tasksForColumn(columnId: TaskBoardColumnId): TaskBoardItem[] {
    return boardColumns[columnId].items;
  }

  function taskCountForColumn(columnId: TaskBoardColumnId): number {
    return boardColumns[columnId].totalCount;
  }

  function loadedCountForColumn(columnId: TaskBoardColumnId): number {
    return columnId === 'done' ? doneTaskGroups.length : tasksForColumn(columnId).length;
  }

  function isDesktopColumnCollapsed(columnId: TaskBoardColumnId): boolean {
    if (dragState && isDragTransitionValid(dragState.column, columnId)) {
      return false;
    }
    if (taskCountForColumn(columnId) > 0) {
      return false;
    }
    if (collapsedDesktopColumns.has(columnId)) {
      return true;
    }
    return !(showEmptyDesktopColumns || expandedDesktopColumns.has(columnId));
  }

  function desktopBoardGridTemplate(): string {
    return TASK_BOARD_COLUMNS.map((column) => (isDesktopColumnCollapsed(column.id) ? '3.25rem' : 'minmax(15rem, 1fr)')).join(' ');
  }

  function expandDesktopColumn(columnId: TaskBoardColumnId): void {
    expandedDesktopColumns = new Set([...expandedDesktopColumns, columnId]);
    const nextCollapsed = new Set(collapsedDesktopColumns);
    nextCollapsed.delete(columnId);
    collapsedDesktopColumns = nextCollapsed;
  }

  function collapseDesktopColumn(columnId: TaskBoardColumnId): void {
    if (taskCountForColumn(columnId) > 0) return;
    const nextExpanded = new Set(expandedDesktopColumns);
    nextExpanded.delete(columnId);
    expandedDesktopColumns = nextExpanded;
    collapsedDesktopColumns = new Set([...collapsedDesktopColumns, columnId]);
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  function boardQueryParams(extra: Record<string, string | number | null | undefined> = {}): Record<string, string | number | null | undefined> {
    return {
      limit: TASK_BOARD_PAGE_SIZE,
      q: filters.search || null,
      agent_id: filters.agentId || null,
      workflow_id: filters.workflowId || null,
      project_id: filters.projectId || null,
      status: filters.status || null,
      ...extra
    };
  }

  function boardFilterKey(): string {
    return JSON.stringify({
      search: filters.search,
      agentId: filters.agentId,
      workflowId: filters.workflowId,
      projectId: filters.projectId,
      status: filters.status
    });
  }

  function applyBoardResponse(columns: Record<string, {
    items: TaskBoardItem[];
    groups?: TaskBoardDoneGroup[];
    cursor: string | null;
    has_more: boolean;
    total_count: number;
  }>): void {
    const next = initialBoardColumns();
    for (const column of TASK_BOARD_COLUMNS) {
      const payload = columns[column.id];
      next[column.id] = payload
        ? {
            items: payload.items,
            groups: payload.groups ?? [],
            cursor: payload.cursor,
            hasMore: payload.has_more,
            totalCount: payload.total_count,
            loading: false
          }
        : emptyBoardColumnState();
    }
    boardColumns = next;
    const visibleDoneGroups = new Set(next.done.groups.map((group) => group.key));
    const currentFilterKey = boardFilterKey();
    const nextDoneGroupHistories = Object.fromEntries(
      Object.entries(doneGroupHistories).filter(
        ([key, history]) => visibleDoneGroups.has(key) && history.filterKey === currentFilterKey
      )
    );
    doneGroupHistories = nextDoneGroupHistories;
    expandedDoneGroups = new Set(
      [...expandedDoneGroups].filter((key) => visibleDoneGroups.has(key) && key in nextDoneGroupHistories)
    );
    boardStateVersion += 1;
  }

  async function loadBoardData(): Promise<void> {
    const requestId = ++boardReplaceRequestId;
    const requestFilterKey = boardFilterKey();
    loading = true;
    loadingTimedOut = false;
    error = '';
    if (loadTimeoutTimer !== null) {
      window.clearTimeout(loadTimeoutTimer);
    }
    loadTimeoutTimer = window.setTimeout(() => {
      if (requestId === boardReplaceRequestId && loading) {
        loadingTimedOut = true;
      }
    }, TASK_BOARD_LOAD_TIMEOUT_MS);
    try {
      const [nextBoard, nextAgents, nextWorkflows, nextProjects, nextSkills] = await Promise.all([
        api.tasks.board(boardQueryParams()),
        api.agents.listAll(),
        api.workflows.listAll(),
        api.projects.list(),
        api.skills.list()
      ]);
      if (requestId !== boardReplaceRequestId || requestFilterKey !== boardFilterKey()) return;
      applyBoardResponse(nextBoard.columns);
      agents = nextAgents;
      workflows = nextWorkflows;
      filterWorkflows = nextWorkflows;
      projects = nextProjects;
      skills = nextSkills;
    } catch (caughtError) {
      if (requestId !== boardReplaceRequestId) return;
      error = asApiError(caughtError).message;
    } finally {
      if (requestId === boardReplaceRequestId) {
        if (loadTimeoutTimer !== null) {
          window.clearTimeout(loadTimeoutTimer);
          loadTimeoutTimer = null;
        }
        loading = false;
      }
    }
  }

  async function loadWorkflowFilterOptions(projectId: string): Promise<void> {
    const key = ++workflowFilterLoadKey;
    try {
      const next = await api.workflows.listAll({ project_id: projectId || null });
      if (key !== workflowFilterLoadKey) return;
      filterWorkflows = next;
      if (filters.workflowId && !next.some((workflow) => workflow.workflow_id === filters.workflowId)) {
        filters.workflowId = '';
      }
    } catch {
      if (key === workflowFilterLoadKey) filterWorkflows = workflows;
    }
  }

  $effect(() => {
    void loadWorkflowFilterOptions(filters.projectId);
  });

  $effect(() => {
    void filters.search;
    void filters.agentId;
    void filters.workflowId;
    void filters.projectId;
    void filters.status;
    if (!urlHydrated || typeof window === 'undefined') return;
    if (filterReloadTimer !== null) window.clearTimeout(filterReloadTimer);
    filterReloadTimer = window.setTimeout(() => {
      filterReloadTimer = null;
      void loadBoardData();
    }, 250);
  });

  async function refreshTasksOnly(): Promise<void> {
    if (document.hidden || loading) return;
    const requestFilterKey = boardFilterKey();
    const replaceRequestId = boardReplaceRequestId;
    const requestId = ++boardRefreshRequestId;
    try {
      const nextBoard = await api.tasks.board(boardQueryParams());
      if (
        requestId !== boardRefreshRequestId ||
        replaceRequestId !== boardReplaceRequestId ||
        requestFilterKey !== boardFilterKey() ||
        loading
      ) return;
      applyBoardResponse(nextBoard.columns);
    } catch (caughtError) {
      if (
        requestId !== boardRefreshRequestId ||
        replaceRequestId !== boardReplaceRequestId ||
        requestFilterKey !== boardFilterKey() ||
        loading
      ) return;
      error = asApiError(caughtError).message;
    }
  }

  async function loadMoreColumn(columnId: TaskBoardColumnId): Promise<void> {
    const current = boardColumns[columnId];
    if (current.loading || !current.hasMore || !current.cursor) return;
    const requestFilterKey = boardFilterKey();
    const requestCursor = current.cursor;
    const requestStateVersion = boardStateVersion;
    boardColumns = {
      ...boardColumns,
      [columnId]: { ...current, loading: true }
    };
    try {
      const nextPage = await api.tasks.boardColumn(columnId, boardQueryParams({ cursor: current.cursor }));
      if (
        requestFilterKey !== boardFilterKey() ||
        boardStateVersion !== requestStateVersion ||
        boardColumns[columnId].cursor !== requestCursor
      ) {
        return;
      }
      const seenTasks = new Set(current.items.map((task) => task.task_id));
      const appendedItems = nextPage.items.filter((task) => !seenTasks.has(task.task_id));
      const seenGroups = new Set(current.groups.map((group) => group.key));
      const appendedGroups = (nextPage.groups ?? []).filter((group) => !seenGroups.has(group.key));
      boardColumns = {
        ...boardColumns,
        [columnId]: {
          items: [...current.items, ...appendedItems],
          groups: [...current.groups, ...appendedGroups],
          cursor: nextPage.cursor,
          hasMore: nextPage.has_more,
          totalCount: nextPage.total_count,
          loading: false
        }
      };
      boardStateVersion += 1;
    } catch (caughtError) {
      boardColumns = {
        ...boardColumns,
        [columnId]: { ...boardColumns[columnId], loading: false }
      };
      error = asApiError(caughtError).message;
    }
  }

  async function loadDoneGroupHistory(group: TaskBoardDoneGroup, append = false): Promise<void> {
    const current = doneGroupHistories[group.key] ?? emptyDoneGroupHistoryState();
    if (current.loading) return;
    if (append && (!current.hasMore || !current.cursor)) return;
    const requestFilterKey = boardFilterKey();
    const requestCursor = append ? current.cursor : null;
    doneGroupHistories = {
      ...doneGroupHistories,
      [group.key]: { ...current, filterKey: requestFilterKey, loading: true }
    };
    try {
      const page = await api.tasks.doneGroupTasks(
        group.key,
        boardQueryParams({ cursor: requestCursor })
      );
      if (requestFilterKey !== boardFilterKey() || !boardColumns.done.groups.some((item) => item.key === group.key)) {
        return;
      }
      const existingItems = append ? current.items : [];
      const seen = new Set(existingItems.map((task) => task.task_id));
      const newItems = page.items.filter((task) => !seen.has(task.task_id));
      doneGroupHistories = {
        ...doneGroupHistories,
        [group.key]: {
          items: [...existingItems, ...newItems],
          cursor: page.cursor,
          hasMore: page.has_more,
          totalCount: group.task_count,
          loading: false,
          loaded: true,
          filterKey: requestFilterKey
        }
      };
    } catch (caughtError) {
      doneGroupHistories = {
        ...doneGroupHistories,
        [group.key]: { ...(doneGroupHistories[group.key] ?? current), loading: false }
      };
      error = asApiError(caughtError).message;
    }
  }

  function toggleDoneGroup(group: TaskBoardDoneGroup): void {
    const next = new Set(expandedDoneGroups);
    if (next.has(group.key)) {
      next.delete(group.key);
      expandedDoneGroups = next;
      return;
    }
    next.add(group.key);
    expandedDoneGroups = next;
    const history = doneGroupHistories[group.key];
    if (!history?.loaded || history.filterKey !== boardFilterKey()) {
      void loadDoneGroupHistory(group);
    }
  }

  function columnInfiniteScroll(node: HTMLElement, columnId: TaskBoardColumnId) {
    if (typeof IntersectionObserver === 'undefined') {
      return {};
    }
    const root = node.closest<HTMLElement>('[data-task-column-scroll]');
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        void loadMoreColumn(columnId);
      }
    }, { root, rootMargin: '200px 0px' });
    observer.observe(node);
    return {
      destroy() {
        observer.disconnect();
      }
    };
  }

  async function openCreateTaskModal(): Promise<void> {
    showCreateModal = true;
    if (conversations.length > 0) return;
    try {
      conversations = (await api.conversations.list(null, { status: 'active' })).items;
    } catch {
      conversations = [];
    }
  }

  function stopPolling(): void {
    if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling(): void {
    stopPolling();
    if (document.hidden) return;
    pollTimer = window.setInterval(() => { void refreshTasksOnly(); }, 15000);
  }

  // ---------------------------------------------------------------------------
  // Task actions
  // ---------------------------------------------------------------------------

  async function handleCreateTask(form: Parameters<typeof api.tasks.create>[0]): Promise<void> {
    creating = true;
    error = '';
    try {
      await api.tasks.create(form);
      showCreateModal = false;
      await loadBoardData();
      addToast('Draft task created.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create draft');
    } finally {
      creating = false;
    }
  }

  async function changeTaskState(taskId: string, action: 'submit' | 'pause' | 'resume' | 'cancel'): Promise<void> {
    if (action === 'cancel') {
      const confirmed = await confirmAction({
        title: 'Cancel task?',
        message: 'This stops the task and marks it as cancelled.',
        confirmLabel: 'Cancel task'
      });
      if (!confirmed) return;
    }
    try {
      if (action === 'submit') await api.tasks.submit(taskId);
      else if (action === 'pause') await api.tasks.pause(taskId);
      else if (action === 'resume') await api.tasks.resume(taskId);
      else await api.tasks.cancel(taskId);
      await refreshTasksOnly();
      addToast(`Task ${action} completed.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update task');
    }
  }

  // ---------------------------------------------------------------------------
  // Bulk actions
  // ---------------------------------------------------------------------------

  async function bulkSubmit(): Promise<void> {
    const ids = [...selectedIds].filter((id) => tasks.find((t) => t.task_id === id)?.status === 'draft');
    if (ids.length === 0) return;
    try {
      await api.tasks.batchSubmit(ids);
      selectedIds = new Set();
      await refreshTasksOnly();
      addToast(`${ids.length} task(s) submitted.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to submit tasks');
    }
  }

  async function bulkCancel(): Promise<void> {
    const ids = [...selectedIds].filter((id) => {
      const s = tasks.find((t) => t.task_id === id)?.status;
      return s && ['queued', 'ready', 'running', 'paused', 'draft'].includes(s);
    });
    if (ids.length === 0) return;
    const confirmed = await confirmAction({
      title: `Cancel ${ids.length} task(s)?`,
      message: 'This stops the selected tasks and marks them as cancelled.',
      confirmLabel: 'Cancel tasks'
    });
    if (!confirmed) return;
    try {
      await Promise.all(ids.map((id) => api.tasks.cancel(id)));
      selectedIds = new Set();
      await refreshTasksOnly();
      addToast(`${ids.length} task(s) cancelled.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to cancel tasks');
    }
  }

  async function bulkDelete(): Promise<void> {
    const ids = [...selectedIds].filter((id) => {
      const s = tasks.find((t) => t.task_id === id)?.status;
      return s && ['draft', 'completed', 'failed', 'cancelled'].includes(s);
    });
    if (ids.length === 0) return;
    const confirmed = await confirmAction({
      title: `Delete ${ids.length} task(s)?`,
      message: 'This permanently removes the selected tasks.',
      confirmLabel: 'Delete tasks'
    });
    if (!confirmed) return;
    try {
      await Promise.all(ids.map((id) => api.tasks.remove(id)));
      selectedIds = new Set();
      await refreshTasksOnly();
      addToast(`${ids.length} task(s) deleted.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete tasks');
    }
  }

  // ---------------------------------------------------------------------------
  // Multi-select
  // ---------------------------------------------------------------------------

  function handleCardClick(event: MouseEvent, taskId: string, columnId: TaskBoardColumnId): void {
    if (event.metaKey || event.ctrlKey) {
      if (event.shiftKey && lastClickedId) {
        // Range select within column
        const columnTasks = tasksForColumn(columnId);
        const lastIdx = columnTasks.findIndex((t) => t.task_id === lastClickedId);
        const curIdx = columnTasks.findIndex((t) => t.task_id === taskId);
        if (lastIdx >= 0 && curIdx >= 0) {
          const start = Math.min(lastIdx, curIdx);
          const end = Math.max(lastIdx, curIdx);
          const next = new Set(selectedIds);
          for (let i = start; i <= end; i++) {
            next.add(columnTasks[i].task_id);
          }
          selectedIds = next;
        }
      } else {
        // Toggle single
        const next = new Set(selectedIds);
        if (next.has(taskId)) next.delete(taskId);
        else next.add(taskId);
        selectedIds = next;
      }
    } else {
      // Plain click — select only this one
      selectedIds = new Set([taskId]);
    }
    lastClickedId = taskId;
  }

  // ---------------------------------------------------------------------------
  // Drag-and-drop
  // ---------------------------------------------------------------------------

  async function handleColumnDrop(targetColumnId: TaskBoardColumnId, targetTaskId?: string): Promise<void> {
    dropTargetColumn = null;
    if (!dragState) return;
    const { taskId: sourceTaskId, column: sourceColumnId } = dragState;
    dragState = null;

    if (sourceColumnId === targetColumnId && targetTaskId) {
      await reorderWithinColumn(targetTaskId, targetColumnId, sourceTaskId);
      return;
    }
    if (sourceColumnId === targetColumnId) return;

    if (targetColumnId === 'done') {
      await changeTaskState(sourceTaskId, 'cancel');
      return;
    }
    const key = `${sourceColumnId}->${targetColumnId}`;
    const action = DRAG_TRANSITIONS[key];
    if (action) {
      await changeTaskState(sourceTaskId, action);
    } else {
      addToast(`Cannot move task from "${sourceColumnId}" to "${targetColumnId}".`, 'error', 3_000);
    }
  }

  async function reorderWithinColumn(targetTaskId: string, columnId: TaskBoardColumnId, sourceTaskId: string): Promise<void> {
    if (filtersActive || !sourceTaskId || sourceTaskId === targetTaskId) return;
    const previousColumns = boardColumns;
    const columnTasks = tasksForColumn(columnId);
    const sourceIndex = columnTasks.findIndex((t) => t.task_id === sourceTaskId);
    const targetIndex = columnTasks.findIndex((t) => t.task_id === targetTaskId);
    if (sourceIndex < 0 || targetIndex < 0) return;

    const reordered = [...columnTasks];
    const [moved] = reordered.splice(sourceIndex, 1);
    reordered.splice(targetIndex, 0, moved);
    const updated = reordered.map((task, i) => ({ ...task, priority: reordered.length - i }));
    boardColumns = {
      ...boardColumns,
      [columnId]: {
        ...boardColumns[columnId],
        items: updated
      }
    };

    try {
      await Promise.all(updated.map((t) => api.tasks.update(t.task_id, { priority: t.priority })));
      await refreshTasksOnly();
    } catch (caughtError) {
      boardColumns = previousColumns;
      error = asApiError(caughtError).message;
    }
  }

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  onMount(() => {
    visibilityHandler = () => {
      if (document.hidden) stopPolling();
      else { void refreshTasksOnly(); startPolling(); }
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    void loadBoardData().then(() => startPolling());

    // Same-tab tap on the bottom tab bar: clear filters, drop expanded
    // UI state, and scroll the content shell to the top. The tab bar
    // has already navigated to `/tasks` (bare path) at this point, so
    // the URL-hydrate effect will clear the `filters` object reactively;
    // we only need to reset ephemeral state and the scroll container.
    const unsubTabReset = onTabReset('/tasks', () => {
      selectedIds = new Set();
      mobileActiveColumn = 'running';
      clearPersistedScroll('/tasks');
      const el = document.querySelector<HTMLElement>('[data-app-content="true"]');
      if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
    });

    return () => {
      stopPolling();
      if (loadTimeoutTimer !== null) {
        window.clearTimeout(loadTimeoutTimer);
      }
      if (urlSyncTimer !== null) {
        window.clearTimeout(urlSyncTimer);
        urlSyncTimer = null;
      }
      if (filterReloadTimer !== null) {
        window.clearTimeout(filterReloadTimer);
        filterReloadTimer = null;
      }
      if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
      unsubTabReset();
    };
  });
</script>

<svelte:head>
  <title>Tasks · Cognis</title>
</svelte:head>

{#if loading}
  {#if loadingTimedOut}
    <Card class="mx-auto max-w-3xl p-6 sm:p-8">
      <div class="space-y-4 text-center">
        <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Task board</p>
        <h1 class="text-xl font-semibold text-white">Still loading task board</h1>
        <p class="text-sm leading-6 text-slate-400">
          Fetching tasks is taking longer than expected. You can retry now or open diagnostics to check provider health.
        </p>
        <div class="flex flex-wrap justify-center gap-3">
          <Button onclick={() => void loadBoardData()}>Retry</Button>
          <Button variant="secondary" onclick={() => goto('/settings/system')}>Open diagnostics</Button>
        </div>
      </div>
    </Card>
  {:else}
    <LoadingState label="Loading task board" description="Fetching draft, queued, running, paused, and completed work items." />
  {/if}
{:else}
  <section class={`min-w-0 space-y-5 ${selectedCount > 0 ? 'pb-[calc(var(--app-shell-bottom-offset,0px)+5.5rem)]' : ''}`}>
    {#if agents.length === 0}
      <Card class="p-5">
        <p class="text-sm font-medium text-white">Create an agent before using the task board.</p>
        <p class="mt-2 text-sm leading-6 text-slate-400">Tasks need an execution agent and workflow before they can be queued.</p>
        <div class="mt-4">
          <Button onclick={() => goto('/agents/new')}>Create agent</Button>
        </div>
      </Card>
    {:else if isLlmUnavailableForSetup()}
      <Card class="p-5">
        <p class="text-sm font-medium text-white">Configure an LLM provider before submitting tasks.</p>
        <p class="mt-2 text-sm leading-6 text-slate-400">Background workflows need a configured provider to execute.</p>
        <div class="mt-4">
          <Button onclick={() => goto('/settings?tab=providers')}>Open providers</Button>
        </div>
      </Card>
    {/if}

    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Workflow queue</p>
        <h1 class="mt-1 text-2xl font-semibold text-white">Task board</h1>
      </div>
      <Button onclick={() => void openCreateTaskModal()}>Create task</Button>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <Card class="p-4 sm:p-5">
      <div class="grid gap-4 md:grid-cols-5">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Search</span>
          <Input bind:value={filters.search} placeholder="title or description" />
        </label>
        <div class="space-y-2 text-sm font-medium text-slate-200">
          <span>Agent</span>
          <AgentSelect
            agents={agents}
            value={filters.agentId || ''}
            onchange={(next) => { filters.agentId = next; }}
            allowAll
            allValue=""
            allLabel="All"
          />
        </div>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Workflow</span>
          <select bind:value={filters.workflowId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">All</option>
            {#each filterWorkflows as workflow}
              <option value={workflow.workflow_id}>{workflow.name}</option>
            {/each}
          </select>
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Project</span>
          <select bind:value={filters.projectId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">All</option>
            {#each projects as project}
              <option value={project.project_id}>{project.name}</option>
            {/each}
          </select>
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Status</span>
          <select bind:value={filters.status} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">All</option>
            <option value="draft">draft</option>
            <option value="queued">queued</option>
            <option value="ready">ready</option>
            <option value="running">running</option>
            <option value="paused">paused</option>
            <option value="completed">completed</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
        </label>
      </div>
      {#if filtersActive}
        <p class="mt-3 text-sm text-sky-200">Priority drag reordering is disabled while filters are active.</p>
      {/if}
    </Card>

    <!-- Mobile column picker (below lg) -->
    <div class="lg:hidden">
      <div class="flex gap-2 overflow-x-auto pb-2" role="tablist" aria-label="Task columns">
        {#each TASK_BOARD_COLUMNS as column}
          {@const count = taskCountForColumn(column.id)}
          <button
            type="button"
            role="tab"
            aria-selected={mobileActiveColumn === column.id}
            onclick={() => (mobileActiveColumn = column.id)}
            class={`shrink-0 rounded-full border px-4 py-2 text-sm font-medium transition ${mobileActiveColumn === column.id ? 'border-sky-400 bg-sky-500/15 text-white' : 'border-slate-800 bg-slate-900/60 text-slate-300'}`}
          >
            {column.label}
            <span class="ml-1.5 rounded-full bg-slate-950/60 px-2 py-0.5 text-xs text-slate-400">{count}</span>
          </button>
        {/each}
      </div>

      {#each TASK_BOARD_COLUMNS as column}
        {#if mobileActiveColumn === column.id}
          <section class="mt-3 space-y-2" aria-label={column.label}>
            <div class="space-y-2">
              {#if column.id === 'done'}
                {#each doneTaskGroups as group (group.key)}
                  {@const history = doneGroupHistories[group.key] ?? emptyDoneGroupHistoryState()}
                  {@const expanded = expandedDoneGroups.has(group.key)}
                  {@const olderExecutions = history.items.filter((task) => task.task_id !== group.latest.task_id)}
                  <article class="rounded-2xl border border-slate-800 bg-slate-950/70 p-3">
                    <div class="flex items-start gap-3">
                      <div class="flex h-8 w-8 shrink-0 items-start justify-center">
                        {#if group.task_count > 1}
                          <button
                            type="button"
                            class="mt-1 rounded-full border border-slate-700 bg-slate-900 p-1 text-slate-400 transition hover:border-sky-500/60 hover:text-sky-300"
                            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${group.title} executions`}
                            aria-expanded={expanded}
                            onclick={() => toggleDoneGroup(group)}
                          >
                            <ChevronDown class="h-3.5 w-3.5 transition {expanded ? 'rotate-180' : ''}" />
                          </button>
                        {/if}
                      </div>
                      <div class="min-w-0 flex-1 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2 px-1">
                          <span class="text-xs uppercase tracking-[0.2em] text-slate-500">{group.title}</span>
                          {#if group.task_count > 1}
                            <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">{group.task_count}</span>
                          {/if}
                        </div>
                        <TaskCard
                          task={group.latest}
                          workflowName={workflowName(group.latest.workflow_id)}
                          selected={selectedIds.has(group.latest.task_id)}
                          onclick={(event) => handleCardClick(event, group.latest.task_id, column.id)}
                        />
                        {#if taskActivityLabel(group.latest)}
                          <p class="px-1 text-[11px] text-slate-500" title={formatAbsoluteTime(taskActivityLabel(group.latest)!)}>
                            Last activity {formatRelativeTime(taskActivityLabel(group.latest)!)}
                          </p>
                        {/if}
                        {#if expanded}
                          <div class="space-y-2 border-l border-slate-800 pl-3">
                            {#if history.loading && !history.loaded}
                              <p class="px-1 text-xs text-slate-500">Loading executions…</p>
                            {:else if olderExecutions.length === 0}
                              <p class="px-1 text-xs text-slate-500">No earlier executions loaded.</p>
                            {:else}
                              {#each olderExecutions as task (task.task_id)}
                                <TaskCard
                                  {task}
                                  workflowName={workflowName(task.workflow_id)}
                                  selected={selectedIds.has(task.task_id)}
                                  onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                                />
                              {/each}
                            {/if}
                            {#if history.hasMore}
                              <div class="py-1 text-center">
                                <Button
                                  size="sm"
                                  variant="secondary"
                                  disabled={history.loading}
                                  onclick={() => void loadDoneGroupHistory(group, true)}
                                >
                                  {history.loading ? 'Loading…' : `Load more executions (${history.items.length}/${history.totalCount || group.task_count})`}
                                </Button>
                              </div>
                            {/if}
                          </div>
                        {/if}
                      </div>
                    </div>
                  </article>
                {/each}
              {:else}
                {#each tasksForColumn(column.id) as task (task.task_id)}
                  <TaskCard
                    {task}
                    workflowName={workflowName(task.workflow_id)}
                    selected={selectedIds.has(task.task_id)}
                    onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                  />
                {/each}
              {/if}
              {#if (column.id === 'done' ? doneTaskGroups.length : tasksForColumn(column.id).length) === 0}
                <p class="py-6 text-center text-sm text-slate-500">No tasks.</p>
              {/if}
              {#if boardColumns[column.id].hasMore}
                <div use:columnInfiniteScroll={column.id} class="py-2 text-center">
                  <Button size="sm" variant="secondary" disabled={boardColumns[column.id].loading} onclick={() => void loadMoreColumn(column.id)}>
                    {boardColumns[column.id].loading ? 'Loading…' : `Load more (${loadedCountForColumn(column.id)}/${boardColumns[column.id].totalCount})`}
                  </Button>
                </div>
              {/if}
            </div>
          </section>
        {/if}
      {/each}
    </div>

    <!-- Desktop kanban board (lg+) -->
    <div class="hidden lg:block">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <p class="text-sm text-slate-400">Empty columns collapse to keep active work in view.</p>
        <label class="inline-flex items-center gap-2 rounded-full border border-slate-800 bg-slate-900/70 px-3 py-2 text-sm text-slate-300">
          <input type="checkbox" bind:checked={showEmptyDesktopColumns} class="h-4 w-4 rounded border-slate-700 bg-slate-950 text-sky-500 focus:ring-sky-500" />
          Show empty columns
        </label>
      </div>
      <div class="overflow-x-auto">
        <div class="grid gap-4" style={`grid-template-columns: ${desktopBoardGridTemplate()};`}>
        {#each TASK_BOARD_COLUMNS as column}
          {@const columnTaskCount = taskCountForColumn(column.id)}
          {@const columnCollapsed = isDesktopColumnCollapsed(column.id)}
          {#if columnCollapsed}
            <section
              class="flex min-h-[600px] rounded-3xl border border-slate-800/80 bg-slate-900/60 shadow-card transition-colors {dropTargetColumn === column.id && dragState && dragState.column !== column.id ? 'border-sky-500/50 bg-sky-950/20' : ''}"
              ondragover={(event: DragEvent) => {
                if (dragState && isDragTransitionValid(dragState.column, column.id)) {
                  event.preventDefault();
                  dropTargetColumn = column.id;
                }
              }}
              ondragleave={(event: DragEvent) => {
                const target = event.currentTarget as HTMLElement;
                if (!target.contains(event.relatedTarget as Node)) {
                  if (dropTargetColumn === column.id) dropTargetColumn = null;
                }
              }}
              ondrop={() => handleColumnDrop(column.id)}
              aria-label={`${column.label}, empty`}
            >
              <button
                type="button"
                class="flex h-full w-full flex-col items-center gap-3 px-2 py-4 text-slate-400 transition hover:text-white"
                onclick={() => expandDesktopColumn(column.id)}
                aria-label={`Expand ${column.label} column`}
                aria-expanded="false"
              >
                <span class="rounded-full border border-slate-700 bg-slate-950/70 px-2 py-1 text-xs font-semibold text-slate-300">{columnTaskCount}</span>
                <span class="[writing-mode:vertical-rl] rotate-180 text-sm font-semibold tracking-wide">{column.label}</span>
              </button>
            </section>
          {:else}
          <section
            class="flex h-[min(72vh,900px)] min-h-[600px] flex-col rounded-3xl border p-4 shadow-card transition-colors {dropTargetColumn === column.id && dragState && dragState.column !== column.id ? 'border-sky-500/50 bg-sky-950/20' : 'border-slate-800/80 bg-slate-900/70'}"
            ondragover={(event: DragEvent) => {
              if (dragState && isDragTransitionValid(dragState.column, column.id)) {
                event.preventDefault();
                dropTargetColumn = column.id;
              }
            }}
            ondragleave={(event: DragEvent) => {
              const target = event.currentTarget as HTMLElement;
              if (!target.contains(event.relatedTarget as Node)) {
                if (dropTargetColumn === column.id) dropTargetColumn = null;
              }
            }}
            ondrop={() => handleColumnDrop(column.id)}
            aria-label={column.label}
          >
            <div class="mb-3 flex items-center justify-between gap-2">
              <div>
                {#if columnTaskCount === 0}
                  <button
                    type="button"
                    class="text-left text-sm font-semibold text-white transition hover:text-sky-300"
                    onclick={() => collapseDesktopColumn(column.id)}
                    aria-label={`Collapse ${column.label} column`}
                  >
                    {column.label}
                  </button>
                {:else}
                  <p class="text-sm font-semibold text-white">{column.label}</p>
                {/if}
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">{columnTaskCount} items</p>
              </div>
            </div>

            <div class="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1" data-task-column-scroll>
              {#if column.id === 'done'}
                {#each doneTaskGroups as group (group.key)}
                  {@const history = doneGroupHistories[group.key] ?? emptyDoneGroupHistoryState()}
                  {@const expanded = expandedDoneGroups.has(group.key)}
                  {@const olderExecutions = history.items.filter((task) => task.task_id !== group.latest.task_id)}
                  <article class="rounded-2xl border border-slate-800 bg-slate-950/70 p-3">
                    <div class="flex items-start gap-3">
                      <div class="flex h-8 w-8 shrink-0 items-start justify-center">
                        {#if group.task_count > 1}
                          <button
                            type="button"
                            class="mt-1 rounded-full border border-slate-700 bg-slate-900 p-1 text-slate-400 transition hover:border-sky-500/60 hover:text-sky-300"
                            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${group.title} executions`}
                            aria-expanded={expanded}
                            onclick={() => toggleDoneGroup(group)}
                          >
                            <ChevronDown class="h-3.5 w-3.5 transition {expanded ? 'rotate-180' : ''}" />
                          </button>
                        {/if}
                      </div>
                      <div class="min-w-0 flex-1 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2 px-1">
                          <span class="text-xs uppercase tracking-[0.2em] text-slate-500">{group.title}</span>
                          {#if group.task_count > 1}
                            <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">{group.task_count}</span>
                          {/if}
                        </div>
                        <TaskCard
                          task={group.latest}
                          workflowName={workflowName(group.latest.workflow_id)}
                          selected={selectedIds.has(group.latest.task_id)}
                          onclick={(event) => handleCardClick(event, group.latest.task_id, column.id)}
                        />
                        {#if taskActivityLabel(group.latest)}
                          <p class="px-1 text-[11px] text-slate-500" title={formatAbsoluteTime(taskActivityLabel(group.latest)!)}>
                            Last activity {formatRelativeTime(taskActivityLabel(group.latest)!)}
                          </p>
                        {/if}
                        {#if expanded}
                          <div class="space-y-2 border-l border-slate-800 pl-3">
                            {#if history.loading && !history.loaded}
                              <p class="px-1 text-xs text-slate-500">Loading executions…</p>
                            {:else if olderExecutions.length === 0}
                              <p class="px-1 text-xs text-slate-500">No earlier executions loaded.</p>
                            {:else}
                              {#each olderExecutions as task (task.task_id)}
                                <TaskCard
                                  {task}
                                  workflowName={workflowName(task.workflow_id)}
                                  selected={selectedIds.has(task.task_id)}
                                  onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                                />
                              {/each}
                            {/if}
                            {#if history.hasMore}
                              <div class="py-1 text-center">
                                <Button
                                  size="sm"
                                  variant="secondary"
                                  disabled={history.loading}
                                  onclick={() => void loadDoneGroupHistory(group, true)}
                                >
                                  {history.loading ? 'Loading…' : `Load more executions (${history.items.length}/${history.totalCount || group.task_count})`}
                                </Button>
                              </div>
                            {/if}
                          </div>
                        {/if}
                      </div>
                    </div>
                  </article>
                {/each}
              {:else}
                {#each tasksForColumn(column.id) as task (task.task_id)}
                  <div
                    draggable={!filtersActive}
                    role="listitem"
                    ondragstart={() => (dragState = { taskId: task.task_id, column: column.id })}
                    ondragend={() => { dragState = null; dropTargetColumn = null; }}
                    ondragover={(event: DragEvent) => event.preventDefault()}
                    ondrop={(event: DragEvent) => { event.stopPropagation(); handleColumnDrop(column.id, task.task_id); }}
                  >
                    <TaskCard
                      {task}
                      workflowName={workflowName(task.workflow_id)}
                      selected={selectedIds.has(task.task_id)}
                      onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                    />
                  </div>
                {/each}
              {/if}
              {#if boardColumns[column.id].hasMore}
                <div use:columnInfiniteScroll={column.id} class="py-2 text-center">
                  <Button size="sm" variant="secondary" disabled={boardColumns[column.id].loading} onclick={() => void loadMoreColumn(column.id)}>
                    {boardColumns[column.id].loading ? 'Loading…' : `Load more (${loadedCountForColumn(column.id)}/${boardColumns[column.id].totalCount})`}
                  </Button>
                </div>
              {/if}
            </div>
          </section>
          {/if}
        {/each}
        </div>
      </div>
    </div>
  </section>

  <!-- Bulk action bar. Wraps on narrow viewports; sits above the bottom tab bar
       on mobile via safe-area-aware offset. -->
  {#if selectedCount > 0}
    <div
      class="fixed inset-x-2 z-40 flex justify-center lg:inset-x-0 lg:bottom-6"
      style="bottom: calc(var(--app-shell-bottom-offset, 0px) + 0.5rem);"
    >
      <div class="flex w-full max-w-2xl flex-wrap items-center justify-center gap-2 rounded-2xl border border-slate-700 bg-slate-900/95 px-3 py-2.5 shadow-2xl backdrop-blur-sm sm:gap-3 sm:px-5 sm:py-3">
        <span class="text-sm font-medium text-slate-200">{selectedCount} selected</span>
        <div class="hidden h-5 w-px bg-slate-700 sm:block"></div>
        {#if submitCount > 0}
          <Button size="sm" onclick={bulkSubmit}>Submit ({submitCount})</Button>
        {/if}
        {#if cancelCount > 0}
          <Button size="sm" variant="danger" onclick={bulkCancel}>Cancel ({cancelCount})</Button>
        {/if}
        {#if deleteCount > 0}
          <Button size="sm" variant="danger" onclick={bulkDelete}>Delete ({deleteCount})</Button>
        {/if}
        <Button size="sm" variant="secondary" onclick={() => (selectedIds = new Set())}>Clear</Button>
      </div>
    </div>
  {/if}

  <!-- Create task modal -->
  {#if showCreateModal}
    <CreateTaskModal
      {agents}
      {workflows}
      {projects}
      {skills}
      {conversations}
      {creating}
      onclose={() => (showCreateModal = false)}
      oncreate={handleCreateTask}
    />
  {/if}
{/if}
