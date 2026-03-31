<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import TaskCard from '$lib/components/tasks/TaskCard.svelte';
  import CreateTaskModal from '$lib/components/tasks/CreateTaskModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { workspaceHealth } from '$lib/system';
  import { TASK_BOARD_COLUMNS, boardColumnForStatus, matchesTaskFilters, sortTasks, type TaskFilterState, type TaskBoardColumnId } from '$lib/tasks';
  import type { Agent, Conversation, Task, Workflow } from '$lib/types/api';

  // ---------------------------------------------------------------------------
  // Reactive state
  // ---------------------------------------------------------------------------

  let loading = $state(true);
  let creating = $state(false);
  let error = $state('');
  let tasks = $state<Task[]>([]);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let conversations = $state<Conversation[]>([]);
  let showCreateModal = $state(false);

  // Multi-select
  let selectedIds = $state<Set<string>>(new Set());
  let lastClickedId = $state<string | null>(null);

  // Drag state
  let dragState = $state<{ taskId: string; column: TaskBoardColumnId } | null>(null);
  let dropTargetColumn = $state<TaskBoardColumnId | null>(null);

  let pollTimer: number | null = null;
  let visibilityHandler: (() => void) | null = null;

  let filters = $state<TaskFilterState>({
    search: '',
    agentId: '',
    workflowId: '',
    status: ''
  });

  // ---------------------------------------------------------------------------
  // Derived state
  // ---------------------------------------------------------------------------

  let filtersActive = $derived(Boolean(filters.search || filters.agentId || filters.workflowId || filters.status));
  let filteredTasks = $derived(sortTasks(tasks.filter((task) => matchesTaskFilters(task, filters))));
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

  function tasksForColumn(columnId: TaskBoardColumnId): Task[] {
    return filteredTasks.filter((task) => boardColumnForStatus(task.status) === columnId);
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  async function loadBoardData(): Promise<void> {
    loading = true;
    error = '';
    try {
      [tasks, agents, workflows, conversations] = await Promise.all([
        api.tasks.listAll(),
        api.agents.listAll(),
        api.workflows.listAll(),
        api.conversations.listAll()
      ]);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

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
      await refreshTasksOnly();
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
    return () => {
      stopPolling();
      if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    };
  });
</script>

<svelte:head>
  <title>Tasks · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading task board" description="Fetching draft, queued, running, paused, and completed work items." />
{:else}
  <section class="space-y-5">
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

    <Card class="p-5">
      <div class="grid gap-4 md:grid-cols-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Search</span>
          <Input bind:value={filters.search} placeholder="title or description" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Agent</span>
          <select bind:value={filters.agentId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">All</option>
            {#each agents as agent}
              <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
            {/each}
          </select>
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Workflow</span>
          <select bind:value={filters.workflowId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">All</option>
            {#each workflows as workflow}
              <option value={workflow.workflow_id}>{workflow.name}</option>
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
        <p class="mt-3 text-sm text-amber-200">Priority drag reordering is disabled while filters are active.</p>
      {/if}
    </Card>

    <div class="overflow-x-auto">
      <div class="grid min-w-[1200px] gap-4 xl:grid-cols-5">
      {#each TASK_BOARD_COLUMNS as column}
        <section
          class="flex min-h-[600px] flex-col rounded-3xl border p-4 shadow-card transition-colors {dropTargetColumn === column.id && dragState && dragState.column !== column.id ? 'border-blue-500/50 bg-blue-950/20' : 'border-slate-800/80 bg-slate-900/70'}"
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
            {#each tasksForColumn(column.id) as task (task.task_id)}
              <div
                draggable={!filtersActive}
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
          </div>
        </section>
      {/each}
      </div>
    </div>
  </section>

  <!-- Bulk action bar -->
  {#if selectedCount > 0}
    <div class="fixed inset-x-0 bottom-6 z-40 flex justify-center">
      <div class="flex items-center gap-3 rounded-2xl border border-slate-700 bg-slate-900/95 px-5 py-3 shadow-2xl backdrop-blur-sm">
        <span class="text-sm font-medium text-slate-200">{selectedCount} selected</span>
        <div class="h-5 w-px bg-slate-700"></div>
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
      {conversations}
      {creating}
      onclose={() => (showCreateModal = false)}
      oncreate={handleCreateTask}
    />
  {/if}
{/if}
