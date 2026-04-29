<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
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
  import { TASK_BOARD_COLUMNS, boardColumnForStatus, matchesTaskFilters, sortTasks, taskFiltersFromSearchParams, taskFiltersToSearchParams, type TaskFilterState, type TaskBoardColumnId } from '$lib/tasks';
  import type { Agent, Conversation, Project, Skill, Task, Workflow } from '$lib/types/api';

  // ---------------------------------------------------------------------------
  // Reactive state
  // ---------------------------------------------------------------------------

  let loading = $state(true);
  let loadingTimedOut = $state(false);
  let creating = $state(false);
  let error = $state('');
  let tasks = $state<Task[]>([]);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let filterWorkflows = $state<Workflow[]>([]);
  let projects = $state<Project[]>([]);
  let skills = $state<Skill[]>([]);
  let conversations = $state<Conversation[]>([]);
  let showCreateModal = $state(false);
  let expandedDoneGroups = $state<Set<string>>(new Set());

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
  let boardLoadRequestId = 0;
  let workflowFilterLoadKey = 0;

  const TASK_BOARD_LOAD_TIMEOUT_MS = 10000;

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

  function hydrateFiltersFromUrl(): void {
    const sp = $page.url.searchParams;
    const next = taskFiltersFromSearchParams(sp);
    if (
      next.search !== filters.search ||
      next.agentId !== filters.agentId ||
      next.workflowId !== filters.workflowId ||
      next.projectId !== filters.projectId ||
      next.status !== filters.status
    ) {
      filters = next;
    }
    const col = sp.get('col') as TaskBoardColumnId | null;
    if (col && col !== mobileActiveColumn && TASK_BOARD_COLUMNS.some((c) => c.id === col)) {
      mobileActiveColumn = col;
    }
  }

  function buildFiltersUrl(): string {
    const sp = taskFiltersToSearchParams(filters);
    if (mobileActiveColumn !== 'running') sp.set('col', mobileActiveColumn);
    const query = sp.toString();
    return query ? `/tasks?${query}` : '/tasks';
  }

  function scheduleFiltersUrlSync(): void {
    if (typeof window === 'undefined') return;
    if (!urlHydrated) return;
    if (urlSyncTimer !== null) window.clearTimeout(urlSyncTimer);
    urlSyncTimer = window.setTimeout(() => {
      urlSyncTimer = null;
      const next = buildFiltersUrl();
      const current = $page.url.pathname + $page.url.search;
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
    void $page.url.search;
    hydrateFiltersFromUrl();
    urlHydrated = true;
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
  let filteredTasks = $derived.by(() => {
    const activeFilters = {
      search: filters.search,
      agentId: filters.agentId,
      workflowId: filters.workflowId,
      projectId: filters.projectId,
      status: filters.status
    };
    return sortTasks(tasks.filter((task) => matchesTaskFilters(task, activeFilters)));
  });
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

  interface TaskDoneGroup {
    key: string;
    title: string;
    latest: Task;
    tasks: Task[];
  }

  function taskGroupKey(task: Task): string {
    const normalizedTitle = task.title.trim().toLowerCase();
    return [
      normalizedTitle || task.task_id,
      task.workflow_id ?? 'auto',
      task.agent_id,
      task.source_type,
      task.source_ref ?? 'none',
    ].join('::');
  }

  function taskActivityLabel(task: Task): string | null {
    return task.updated_at ?? task.completed_at ?? task.started_at ?? task.created_at;
  }

  function taskGroupTitle(task: Task): string {
    return task.title.trim() || task.task_id;
  }

  let doneTaskGroups = $derived.by(() => {
    const groups = new Map<string, TaskDoneGroup>();
    for (const task of tasksForColumn('done')) {
      const key = taskGroupKey(task);
      const existing = groups.get(key);
      if (existing) {
        existing.tasks.push(task);
        continue;
      }
      groups.set(key, {
        key,
        title: taskGroupTitle(task),
        latest: task,
        tasks: [task],
      });
    }
    return [...groups.values()];
  });

  function toggleDoneGroup(groupKey: string): void {
    const next = new Set(expandedDoneGroups);
    if (next.has(groupKey)) {
      next.delete(groupKey);
    } else {
      next.add(groupKey);
    }
    expandedDoneGroups = next;
  }

  function tasksForColumn(columnId: TaskBoardColumnId): Task[] {
    return filteredTasks.filter((task) => boardColumnForStatus(task.status) === columnId);
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  async function loadBoardData(): Promise<void> {
    const requestId = ++boardLoadRequestId;
    loading = true;
    loadingTimedOut = false;
    error = '';
    if (loadTimeoutTimer !== null) {
      window.clearTimeout(loadTimeoutTimer);
    }
    loadTimeoutTimer = window.setTimeout(() => {
      if (requestId === boardLoadRequestId && loading) {
        loadingTimedOut = true;
      }
    }, TASK_BOARD_LOAD_TIMEOUT_MS);
    try {
      const [nextTasks, nextAgents, nextWorkflows, nextProjects, nextSkills, nextConversations] = await Promise.all([
        api.tasks.listAll(),
        api.agents.listAll(),
        api.workflows.listAll(),
        api.projects.list(),
        api.skills.list(),
        api.conversations.listAll()
      ]);
      if (requestId !== boardLoadRequestId) return;
      tasks = nextTasks;
      agents = nextAgents;
      workflows = nextWorkflows;
      filterWorkflows = nextWorkflows;
      projects = nextProjects;
      skills = nextSkills;
      conversations = nextConversations;
    } catch (caughtError) {
      if (requestId !== boardLoadRequestId) return;
      error = asApiError(caughtError).message;
    } finally {
      if (requestId === boardLoadRequestId) {
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

  async function refreshTasksOnly(): Promise<void> {
    if (document.hidden) return;
    try {
      tasks = await api.tasks.listAll();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
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
    const previousTasks = [...tasks];
    const columnTasks = tasksForColumn(columnId);
    const sourceIndex = columnTasks.findIndex((t) => t.task_id === sourceTaskId);
    const targetIndex = columnTasks.findIndex((t) => t.task_id === targetTaskId);
    if (sourceIndex < 0 || targetIndex < 0) return;

    const reordered = [...columnTasks];
    const [moved] = reordered.splice(sourceIndex, 1);
    reordered.splice(targetIndex, 0, moved);
    const updated = reordered.map((task, i) => ({ ...task, priority: reordered.length - i }));
    tasks = tasks.map((task) => updated.find((c) => c.task_id === task.task_id) ?? task);

    try {
      await Promise.all(updated.map((t) => api.tasks.update(t.task_id, { priority: t.priority })));
      await refreshTasksOnly();
    } catch (caughtError) {
      tasks = previousTasks;
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
      expandedDoneGroups = new Set();
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
      <Button onclick={() => (showCreateModal = true)}>Create task</Button>
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
          {@const count = tasksForColumn(column.id).length}
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
                  <article class="rounded-2xl border border-slate-800 bg-slate-950/70 p-3">
                    <div class="flex items-start gap-3">
                      {#if group.tasks.length > 1}
                        <button
                          type="button"
                          class="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-900/70 text-slate-400 transition hover:border-slate-600 hover:text-white"
                          onclick={() => toggleDoneGroup(group.key)}
                          aria-label={expandedDoneGroups.has(group.key) ? 'Collapse task group' : 'Expand task group'}
                        >
                          <ChevronDown class={`h-4 w-4 transition ${expandedDoneGroups.has(group.key) ? 'rotate-180' : ''}`} />
                        </button>
                      {:else}
                        <div class="h-8 w-8 shrink-0"></div>
                      {/if}
                      <div class="min-w-0 flex-1 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2 px-1">
                          <span class="text-xs uppercase tracking-[0.2em] text-slate-500">{group.title}</span>
                          {#if group.tasks.length > 1}
                            <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">{group.tasks.length}</span>
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
                      </div>
                    </div>
                    {#if expandedDoneGroups.has(group.key) && group.tasks.length > 1}
                      <div class="mt-3 space-y-2 border-t border-slate-800 pt-3">
                        {#each group.tasks.slice(1) as task (task.task_id)}
                          <TaskCard
                            {task}
                            workflowName={workflowName(task.workflow_id)}
                            selected={selectedIds.has(task.task_id)}
                            onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                          />
                        {/each}
                      </div>
                    {/if}
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
            </div>
          </section>
        {/if}
      {/each}
    </div>

    <!-- Desktop kanban board (lg+) -->
    <div class="hidden lg:block">
      <div class="overflow-x-auto">
        <div class="grid min-w-[1200px] gap-4 lg:grid-cols-5">
        {#each TASK_BOARD_COLUMNS as column}
          <section
            class="flex min-h-[600px] flex-col rounded-3xl border p-4 shadow-card transition-colors {dropTargetColumn === column.id && dragState && dragState.column !== column.id ? 'border-sky-500/50 bg-sky-950/20' : 'border-slate-800/80 bg-slate-900/70'}"
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
                <p class="text-sm font-semibold text-white">{column.label}</p>
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">{tasksForColumn(column.id).length} items</p>
              </div>
            </div>

            <div class="flex-1 space-y-2 overflow-y-auto">
              {#if column.id === 'done'}
                {#each doneTaskGroups as group (group.key)}
                  <article class="rounded-2xl border border-slate-800 bg-slate-950/70 p-3">
                    <div class="flex items-start gap-3">
                      {#if group.tasks.length > 1}
                        <button
                          type="button"
                          class="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-900/70 text-slate-400 transition hover:border-slate-600 hover:text-white"
                          onclick={() => toggleDoneGroup(group.key)}
                          aria-label={expandedDoneGroups.has(group.key) ? 'Collapse task group' : 'Expand task group'}
                        >
                          <ChevronDown class={`h-4 w-4 transition ${expandedDoneGroups.has(group.key) ? 'rotate-180' : ''}`} />
                        </button>
                      {:else}
                        <div class="h-8 w-8 shrink-0"></div>
                      {/if}
                      <div class="min-w-0 flex-1 space-y-2">
                        <div class="flex flex-wrap items-center justify-between gap-2 px-1">
                          <span class="text-xs uppercase tracking-[0.2em] text-slate-500">{group.title}</span>
                          {#if group.tasks.length > 1}
                            <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-300">{group.tasks.length}</span>
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
                      </div>
                    </div>
                    {#if expandedDoneGroups.has(group.key) && group.tasks.length > 1}
                      <div class="mt-3 space-y-2 border-t border-slate-800 pt-3">
                        {#each group.tasks.slice(1) as task (task.task_id)}
                          <TaskCard
                            {task}
                            workflowName={workflowName(task.workflow_id)}
                            selected={selectedIds.has(task.task_id)}
                            onclick={(event) => handleCardClick(event, task.task_id, column.id)}
                          />
                        {/each}
                      </div>
                    {/if}
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
            </div>
          </section>
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
