<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { Agent, Conversation, Task, TaskDetail, Workflow } from '$lib/types/api';

  let loading = true;
  let saving = false;
  let error = '';
  let task: TaskDetail | null = null;
  let agents: Agent[] = [];
  let workflows: Workflow[] = [];
  let conversations: Conversation[] = [];
  let allTasks: Task[] = [];
  let dependencyTaskId = '';
  let gateFeedback = '';
  let stepResponse = '';
  let pollTimer: number | null = null;
  let visibilityHandler: (() => void) | null = null;

  let editForm = {
    title: '',
    description: '',
    priority: 0,
    agent_id: '',
    workflow_id: '',
    delivery_mode: 'same_conversation',
    delivery_target: ''
  };

  function taskIdFromRoute(): string {
    return $page.params.taskId ?? '';
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) {
      return 'auto';
    }
    return workflows.find((workflow) => workflow.workflow_id === workflowId)?.name ?? workflowId;
  }

  async function loadTask(): Promise<void> {
    loading = true;
    error = '';
    try {
      [task, agents, workflows, conversations, allTasks] = await Promise.all([
        api.tasks.detail(taskIdFromRoute()),
        api.agents.listAll(),
        api.workflows.listAll(),
        api.conversations.listAll(),
        api.tasks.listAll()
      ]);
      editForm = {
        title: task.title,
        description: task.description,
        priority: task.priority,
        agent_id: task.agent_id,
        workflow_id: task.workflow_id ?? '',
        delivery_mode: task.delivery.mode,
        delivery_target: task.delivery.target ?? ''
      };
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function refreshTaskOnly(): Promise<void> {
    if (document.hidden) {
      return;
    }
    try {
      task = await api.tasks.detail(taskIdFromRoute());
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
      void refreshTaskOnly();
    }, 5000);
  }

  async function saveTask(): Promise<void> {
    if (!task) {
      return;
    }
    saving = true;
    try {
      const updatedTask = await api.tasks.update(task.task_id, {
        title: editForm.title,
        description: editForm.description,
        priority: Number(editForm.priority),
        agent_id: editForm.agent_id,
        workflow_id: editForm.workflow_id || null,
        delivery_mode: editForm.delivery_mode,
        delivery_target: editForm.delivery_mode === 'specific_conversation' ? editForm.delivery_target : null
      });
      task = await api.tasks.detail(updatedTask.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      saving = false;
    }
  }

  async function addDependency(): Promise<void> {
    if (!task || !dependencyTaskId) {
      return;
    }
    try {
      await api.tasks.addDependency(task.task_id, dependencyTaskId, true);
      dependencyTaskId = '';
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function removeDependency(dependsOn: string): Promise<void> {
    if (!task) {
      return;
    }
    try {
      await api.tasks.removeDependency(task.task_id, dependsOn);
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function respondToGate(action: string): Promise<void> {
    if (!task) {
      return;
    }
    try {
      await api.tasks.gateResponse(task.task_id, {
        step_name: task.pending_pause?.step_name,
        action,
        feedback: gateFeedback || undefined
      });
      gateFeedback = '';
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function respondToStepQuestion(response: string): Promise<void> {
    if (!task) {
      return;
    }
    try {
      await api.tasks.stepResponse(task.task_id, {
        step_name: task.pending_pause?.step_name,
        response
      });
      stepResponse = '';
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  onMount(() => {
    visibilityHandler = () => {
      if (document.hidden) {
        stopPolling();
      } else {
        void refreshTaskOnly();
        startPolling();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    void loadTask().then(() => {
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
  <title>{task ? `${task.title} · Task · Cognis` : 'Task · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading task" description="Fetching workflow state, step runs, and dependency information." />
{:else if task}
  <section class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Task detail</p>
        <h1 class="mt-1 text-2xl font-semibold text-white">{task.title}</h1>
      </div>
      <span class="rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-slate-200">{task.status}</span>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div class="space-y-5">
        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Title</span>
              <Input bind:value={editForm.title} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Priority</span>
              <Input bind:value={editForm.priority} type="number" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Agent</span>
              <select bind:value={editForm.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each agents as agent}
                  <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
                {/each}
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Workflow</span>
              <select bind:value={editForm.workflow_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="">Auto</option>
                {#each workflows as workflow}
                  <option value={workflow.workflow_id}>{workflow.name}</option>
                {/each}
              </select>
            </label>
          </div>

          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Description</span>
            <textarea bind:value={editForm.description} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"></textarea>
          </label>

          <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Delivery mode</span>
              <select bind:value={editForm.delivery_mode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="same_conversation">same_conversation</option>
                <option value="specific_conversation">specific_conversation</option>
                <option value="latest_active_for_agent">latest_active_for_agent</option>
                <option value="preferred_channel">preferred_channel</option>
                <option value="silent">silent</option>
              </select>
            </label>
            {#if editForm.delivery_mode === 'specific_conversation'}
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Delivery target</span>
                <select bind:value={editForm.delivery_target} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Select conversation</option>
                  {#each conversations as conversation}
                    <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
                  {/each}
                </select>
              </label>
            {/if}
          </div>

          <div class="mt-5 flex justify-end">
            <Button disabled={saving} onclick={saveTask}>{saving ? 'Saving…' : 'Save task'}</Button>
          </div>
        </Card>

        {#if task.pending_pause}
          <Card class="p-5">
            <div class="space-y-4">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">
                  {task.pending_pause.pause_type === 'gate' ? 'Workflow gate' : 'Step question'}
                </p>
                <h2 class="mt-1 text-lg font-semibold text-white">{task.pending_pause.question}</h2>
              </div>

              {#if task.pending_pause.pause_type === 'gate'}
                <textarea bind:value={gateFeedback} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Optional review feedback"></textarea>
                <div class="flex flex-wrap gap-2">
                  {#each task.pending_pause.options ?? [] as option}
                    <Button size="sm" onclick={() => respondToGate(String(option.action ?? 'continue'))}>{String(option.label ?? option.action ?? 'continue')}</Button>
                  {/each}
                  {#if (task.pending_pause.options ?? []).length === 0}
                    <Button size="sm" onclick={() => respondToGate('continue')}>Continue</Button>
                  {/if}
                </div>
              {:else}
                <textarea bind:value={stepResponse} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Provide the answer that resumes the current step"></textarea>
                <div class="flex flex-wrap gap-2">
                  {#each task.pending_pause.options ?? [] as option}
                    <Button size="sm" onclick={() => respondToStepQuestion(String(option.action ?? option.label ?? ''))}>{String(option.label ?? option.action ?? 'Use option')}</Button>
                  {/each}
                  <Button size="sm" onclick={() => respondToStepQuestion(stepResponse)}>Send response</Button>
                </div>
              {/if}
            </div>
          </Card>
        {/if}

        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Workflow progress</p>
              <h2 class="mt-1 text-lg font-semibold text-white">{workflowName(task.workflow_id)}</h2>
            </div>

            <div class="space-y-3">
              {#each task.step_runs as stepRun}
                <article class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div class="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 class="font-semibold text-white">{stepRun.step_name}</h3>
                      <p class="text-xs uppercase tracking-[0.2em] text-slate-500">{stepRun.step_type} · attempt {stepRun.attempt}</p>
                    </div>
                    <span class="rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-slate-200">{stepRun.status}</span>
                  </div>
                  {#if stepRun.output}
                    <pre class="mt-3 overflow-x-auto rounded-2xl border border-slate-800 bg-slate-950 p-4 text-xs text-slate-300">{JSON.stringify(stepRun.output, null, 2)}</pre>
                  {/if}
                  {#if stepRun.evaluation}
                    <pre class="mt-3 overflow-x-auto rounded-2xl border border-slate-800 bg-slate-950 p-4 text-xs text-slate-300">{JSON.stringify(stepRun.evaluation, null, 2)}</pre>
                  {/if}
                </article>
              {/each}
            </div>
          </div>
        </Card>
      </div>

      <div class="space-y-5">
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Result</p>
          <p class="mt-3 text-sm leading-6 text-slate-300">{task.result_summary ?? 'This task has not produced a final result yet.'}</p>
        </Card>

        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Dependencies</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Dependency graph</h2>
            </div>
            <div class="space-y-3">
              {#each task.dependencies as dependency}
                <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
                  <span class="text-sm text-slate-200">{allTasks.find((candidate) => candidate.task_id === dependency.depends_on)?.title ?? dependency.depends_on}</span>
                  <Button size="sm" variant="danger" onclick={() => removeDependency(dependency.depends_on)}>Remove</Button>
                </div>
              {/each}
              {#if task.dependencies.length === 0}
                <p class="text-sm text-slate-400">No dependencies configured.</p>
              {/if}
            </div>

            <div class="space-y-3 border-t border-slate-800 pt-4">
              <select bind:value={dependencyTaskId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="">Add dependency…</option>
                {#each allTasks.filter((candidate) => candidate.task_id !== taskIdFromRoute()) as candidate}
                  <option value={candidate.task_id}>{candidate.title}</option>
                {/each}
              </select>
              <Button class="w-full justify-center" disabled={!dependencyTaskId} onclick={addDependency}>Add dependency</Button>
            </div>
          </div>
        </Card>
      </div>
    </div>
  </section>
{:else}
  <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">Task not found.</p>
{/if}
