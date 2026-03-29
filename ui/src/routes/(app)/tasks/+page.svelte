<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import TaskCard from '$lib/components/tasks/TaskCard.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { workspaceHealth } from '$lib/system';
  import { TASK_BOARD_COLUMNS, boardColumnForStatus, matchesTaskFilters, sortTasks, type TaskFilterState, type TaskBoardColumnId } from '$lib/tasks';
  import type { Agent, Conversation, Task, Workflow } from '$lib/types/api';

  let loading = true;
  let creating = false;
  let error = '';
  let tasks: Task[] = [];
  let agents: Agent[] = [];
  let workflows: Workflow[] = [];
  let conversations: Conversation[] = [];
  let selectedDraftIds: string[] = [];
  let dragState: { taskId: string; column: TaskBoardColumnId } | null = null;
  let pollTimer: number | null = null;
  let visibilityHandler: (() => void) | null = null;

  let filters: TaskFilterState = {
    search: '',
    agentId: '',
    workflowId: '',
    status: ''
  };

  let draftForm = {
    title: '',
    description: '',
    agent_id: '',
    workflow_id: '',
    priority: 0,
    delivery_mode: 'same_conversation',
    delivery_target: ''
  };

  function isLlmUnavailableForSetup(): boolean {
    const llmDetails = JSON.stringify($workspaceHealth.health?.providers?.llm ?? {}).toLowerCase();
    return llmDetails.includes('no llm model configured') || llmDetails.includes('not configured');
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) {
      return 'auto';
    }
    return workflows.find((workflow) => workflow.workflow_id === workflowId)?.name ?? workflowId;
  }

  function filtersActive(): boolean {
    return Boolean(filters.search || filters.agentId || filters.workflowId || filters.status);
  }

  function filteredTasks(): Task[] {
    return sortTasks(tasks.filter((task) => matchesTaskFilters(task, filters)));
  }

  function tasksForColumn(columnId: TaskBoardColumnId): Task[] {
    return filteredTasks().filter((task) => boardColumnForStatus(task.status) === columnId);
  }

  function defaultAgentId(): string {
    return agents.find((agent) => agent.status === 'active')?.agent_id ?? agents[0]?.agent_id ?? '';
  }

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
      draftForm.agent_id = draftForm.agent_id || defaultAgentId();
      draftForm.workflow_id = draftForm.workflow_id || workflows[0]?.workflow_id || '';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function refreshTasksOnly(): Promise<void> {
    if (document.hidden) {
      return;
    }
    try {
      tasks = await api.tasks.listAll();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling(): void {
    stopPolling();
    if (document.hidden) {
      return;
    }
    pollTimer = window.setInterval(() => {
      void refreshTasksOnly();
    }, 15000);
  }

  async function createDraftTask(): Promise<void> {
    if (!draftForm.title.trim()) {
      error = 'Task title is required.';
      return;
    }
    if (!draftForm.agent_id) {
      error = 'Select an agent for the task.';
      return;
    }
    creating = true;
    error = '';
    try {
      await api.tasks.create({
        agent_id: draftForm.agent_id,
        title: draftForm.title,
        description: draftForm.description,
        workflow_id: draftForm.workflow_id || null,
        priority: Number(draftForm.priority),
        delivery_mode: draftForm.delivery_mode,
        delivery_target: draftForm.delivery_mode === 'specific_conversation' ? draftForm.delivery_target : null,
        status: 'draft'
      });
      draftForm = {
        ...draftForm,
        title: '',
        description: '',
        delivery_target: ''
      };
      await refreshTasksOnly();
      addToast('Draft task created.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create draft');
    } finally {
      creating = false;
    }
  }

  function toggleDraftSelection(taskId: string): void {
    selectedDraftIds = selectedDraftIds.includes(taskId)
      ? selectedDraftIds.filter((value) => value !== taskId)
      : [...selectedDraftIds, taskId];
  }

  async function batchSubmit(): Promise<void> {
    if (selectedDraftIds.length === 0) {
      return;
    }
    try {
      await api.tasks.batchSubmit(selectedDraftIds);
      selectedDraftIds = [];
      await refreshTasksOnly();
      addToast('Selected drafts submitted.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to submit drafts');
    }
  }

  async function changeTaskState(taskId: string, action: 'submit' | 'pause' | 'resume' | 'cancel'): Promise<void> {
    if (action === 'cancel') {
      const confirmed = await confirmAction({
        title: 'Cancel task?',
        message: 'This stops the task and marks it as cancelled.',
        confirmLabel: 'Cancel task'
      });
      if (!confirmed) {
        return;
      }
    }
    try {
      if (action === 'submit') {
        await api.tasks.submit(taskId);
      } else if (action === 'pause') {
        await api.tasks.pause(taskId);
      } else if (action === 'resume') {
        await api.tasks.resume(taskId);
      } else {
        await api.tasks.cancel(taskId);
      }
      await refreshTasksOnly();
      addToast(`Task ${action} completed.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update task');
    }
  }

  async function reorderWithinColumn(targetTaskId: string, columnId: TaskBoardColumnId, sourceTaskId = dragState?.taskId ?? ''): Promise<void> {
    if (filtersActive()) {
      dragState = null;
      error = 'Clear active filters before reordering priorities.';
      return;
    }
    if (!sourceTaskId || (dragState && dragState.column !== columnId) || sourceTaskId === targetTaskId) {
      dragState = null;
      return;
    }

    const previousTasks = [...tasks];
    const columnTasks = tasksForColumn(columnId);
    const sourceIndex = columnTasks.findIndex((task) => task.task_id === sourceTaskId);
    const targetIndex = columnTasks.findIndex((task) => task.task_id === targetTaskId);
    if (sourceIndex < 0 || targetIndex < 0) {
      dragState = null;
      return;
    }

    const reordered = [...columnTasks];
    const [moved] = reordered.splice(sourceIndex, 1);
    reordered.splice(targetIndex, 0, moved);

    const updated = reordered.map((task, index) => ({
      ...task,
      priority: reordered.length - index
    }));

    tasks = tasks.map((task) => updated.find((candidate) => candidate.task_id === task.task_id) ?? task);

    try {
      await Promise.all(
        updated.map((task) => api.tasks.update(task.task_id, { priority: task.priority }))
      );
      await refreshTasksOnly();
    } catch (caughtError) {
      tasks = previousTasks;
      error = asApiError(caughtError).message;
    } finally {
      dragState = null;
    }
  }

  async function moveTaskByOffset(taskId: string, columnId: TaskBoardColumnId, offset: -1 | 1): Promise<void> {
    const columnTasks = tasksForColumn(columnId);
    const currentIndex = columnTasks.findIndex((task) => task.task_id === taskId);
    const target = columnTasks[currentIndex + offset];
    if (currentIndex < 0 || !target) {
      return;
    }
    await reorderWithinColumn(target.task_id, columnId, taskId);
  }

  onMount(() => {
    visibilityHandler = () => {
      if (document.hidden) {
        stopPolling();
      } else {
        void refreshTasksOnly();
        startPolling();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    void loadBoardData().then(() => {
      startPolling();
    });

    return () => {
      stopPolling();
      if (visibilityHandler) {
        document.removeEventListener('visibilitychange', visibilityHandler);
      }
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
      <Button disabled={selectedDraftIds.length === 0} onclick={batchSubmit}>Submit selected drafts</Button>
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
      {#if filtersActive()}
        <p class="mt-3 text-sm text-amber-200">Priority drag reordering is disabled while filters are active.</p>
      {/if}
    </Card>

    <div class="overflow-x-auto">
      <div class="grid min-w-[1200px] gap-4 xl:grid-cols-5">
      {#each TASK_BOARD_COLUMNS as column}
        <section class="flex min-h-[720px] flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 p-4 shadow-card">
          <div class="mb-4 flex items-center justify-between gap-2">
            <div>
              <p class="text-sm font-semibold text-white">{column.label}</p>
              <p class="text-xs uppercase tracking-[0.2em] text-slate-500">{tasksForColumn(column.id).length} items</p>
            </div>
          </div>

          {#if column.id === 'draft'}
            <Card class="mb-4 p-4">
              <div class="space-y-3">
                <Input bind:value={draftForm.title} placeholder="New task title" />
                <textarea bind:value={draftForm.description} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Describe the work item"></textarea>
                <div class="grid gap-3">
                  <select bind:value={draftForm.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    {#each agents as agent}
                      <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
                    {/each}
                  </select>
                  <select bind:value={draftForm.workflow_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="">Auto workflow</option>
                    {#each workflows as workflow}
                      <option value={workflow.workflow_id}>{workflow.name}</option>
                    {/each}
                  </select>
                  <Input bind:value={draftForm.priority} type="number" />
                  <select bind:value={draftForm.delivery_mode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="same_conversation">same_conversation</option>
                    <option value="specific_conversation">specific_conversation</option>
                    <option value="latest_active_for_agent">latest_active_for_agent</option>
                    <option value="preferred_channel">preferred_channel</option>
                    <option value="silent">silent</option>
                  </select>
                  {#if draftForm.delivery_mode === 'specific_conversation'}
                    <select bind:value={draftForm.delivery_target} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      <option value="">Select conversation</option>
                      {#each conversations as conversation}
                        <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
                      {/each}
                    </select>
                  {/if}
                </div>
                <Button class="w-full justify-center" disabled={!draftForm.title.trim() || creating} onclick={createDraftTask}>
                  {creating ? 'Creating…' : 'Create draft'}
                </Button>
              </div>
            </Card>
          {/if}

          <div class="space-y-4 overflow-y-auto">
            {#each tasksForColumn(column.id) as task (task.task_id)}
              <div
                aria-label={`Reorder task ${task.title}`}
                draggable={!filtersActive()}
                ondragstart={() => (dragState = { taskId: task.task_id, column: column.id })}
                ondragover={(event) => event.preventDefault()}
                ondrop={() => reorderWithinColumn(task.task_id, column.id)}
                role="listitem"
              >
                <TaskCard
                  task={task}
                  workflowName={workflowName(task.workflow_id)}
                  selected={selectedDraftIds.includes(task.task_id)}
                  onOpen={() => goto(`/tasks/${task.task_id}`)}
                  onSelect={column.id === 'draft' ? () => toggleDraftSelection(task.task_id) : null}
                  onSubmit={task.status === 'draft' ? () => changeTaskState(task.task_id, 'submit') : null}
                  onPause={task.status === 'running' ? () => changeTaskState(task.task_id, 'pause') : null}
                  onResume={task.status === 'paused' ? () => changeTaskState(task.task_id, 'resume') : null}
                  onCancel={['queued', 'ready', 'running', 'paused', 'draft'].includes(task.status) ? () => changeTaskState(task.task_id, 'cancel') : null}
                  onMoveUp={!filtersActive() ? () => moveTaskByOffset(task.task_id, column.id, -1) : null}
                  onMoveDown={!filtersActive() ? () => moveTaskByOffset(task.task_id, column.id, 1) : null}
                />
              </div>
            {/each}
          </div>
        </section>
      {/each}
      </div>
    </div>
  </section>
{/if}
