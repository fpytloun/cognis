<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { renderMarkdown } from '$lib/markdown';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import type { Agent, Conversation, StepRun, Task, TaskDetail, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let task = $state<TaskDetail | null>(null);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let conversations = $state<Conversation[]>([]);
  let allTasks = $state<Task[]>([]);
  let dependencyTaskId = $state('');
  let gateFeedback = $state('');
  let stepResponse = $state('');
  let expandedSteps = $state<Set<string>>(new Set());
  let pollTimer: number | null = null;
  let visibilityHandler: (() => void) | null = null;

  // Session logs drawer
  let sessionDrawer = $state<{ conversationId: string; sessionId: string; stepName: string } | null>(null);

  let editForm = $state({
    title: '',
    description: '',
    priority: 0,
    agent_id: '',
    workflow_id: '',
    delivery_mode: 'same_conversation',
    delivery_target: ''
  });

  const statusColors: Record<string, string> = {
    pending: 'border-slate-600 text-slate-400',
    running: 'border-amber-700 text-amber-300',
    approved: 'border-emerald-700 text-emerald-300',
    completed: 'border-emerald-700 text-emerald-300',
    failed: 'border-rose-700 text-rose-300',
    cancelled: 'border-slate-600 text-slate-500',
    paused: 'border-yellow-700 text-yellow-300',
  };

  const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];
  let isEditable = $derived(task != null && !TERMINAL_STATUSES.includes(task.status));

  function taskIdFromRoute(): string {
    return $page.params.taskId ?? '';
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) return 'auto';
    return workflows.find((w) => w.workflow_id === workflowId)?.name ?? workflowId;
  }

  function toggleStepExpand(stepRunId: string): void {
    const next = new Set(expandedSteps);
    if (next.has(stepRunId)) next.delete(stepRunId);
    else next.add(stepRunId);
    expandedSteps = next;
  }

  function stepOutputSummary(stepRun: StepRun): string {
    const val = stepRun.output?.summary;
    return typeof val === 'string' ? val : '';
  }

  function stepOutputContent(stepRun: StepRun): string {
    const val = stepRun.output?.content;
    return typeof val === 'string' ? val : '';
  }

  function stepOutputClaims(stepRun: StepRun): string[] {
    const claims = stepRun.output?.claims;
    return Array.isArray(claims) ? claims.filter((c): c is string => typeof c === 'string') : [];
  }

  function openSessionLogs(stepRun: StepRun): void {
    const sessionId = String(stepRun.output?.session_id ?? stepRun.session_id ?? '');
    if (!sessionId || !task) return;
    // Step sessions each get their own conversation (title: "Task: X / Step: Y").
    // Find it by matching the session_id in the conversation's sessions.
    // Fall back to searching by task context ref, then to the session ID itself
    // (the API will return 404 if invalid — the drawer handles this gracefully).
    const conv = conversations.find((c) =>
      c.context?.ref === task!.task_id && c.title?.includes(stepRun.step_name)
    ) ?? conversations.find((c) => c.context?.ref === task!.task_id);
    const conversationId = conv?.conversation_id ?? sessionId;
    sessionDrawer = {
      conversationId,
      sessionId,
      stepName: `${stepRun.step_name} (attempt ${stepRun.attempt})`
    };
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

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
    if (document.hidden) return;
    try {
      task = await api.tasks.detail(taskIdFromRoute());
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
    pollTimer = window.setInterval(() => { void refreshTaskOnly(); }, 5000);
  }

  // ---------------------------------------------------------------------------
  // Task actions
  // ---------------------------------------------------------------------------

  async function saveTask(): Promise<void> {
    if (!task) return;
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
      addToast('Task updated.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update task');
    } finally {
      saving = false;
    }
  }

  async function addDependency(): Promise<void> {
    if (!task || !dependencyTaskId) return;
    try {
      await api.tasks.addDependency(task.task_id, dependencyTaskId, true);
      dependencyTaskId = '';
      task = await api.tasks.detail(task.task_id);
      addToast('Dependency added.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to add dependency');
    }
  }

  async function removeDependency(dependsOn: string): Promise<void> {
    if (!task) return;
    const confirmed = await confirmAction({
      title: 'Remove dependency?',
      message: 'The task will no longer wait for this dependency before running.',
      confirmLabel: 'Remove dependency'
    });
    if (!confirmed) return;
    try {
      await api.tasks.removeDependency(task.task_id, dependsOn);
      task = await api.tasks.detail(task.task_id);
      addToast('Dependency removed.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to remove dependency');
    }
  }

  async function respondToGate(action: string): Promise<void> {
    if (!task) return;
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
    if (!task) return;
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

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  onMount(() => {
    visibilityHandler = () => {
      if (document.hidden) stopPolling();
      else { void refreshTaskOnly(); startPolling(); }
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    void loadTask().then(() => startPolling());
    return () => {
      stopPolling();
      if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
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
        <div class="mb-3">
          <Button size="sm" variant="secondary" onclick={() => goto('/tasks')}>Back to task board</Button>
        </div>
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
        <!-- Edit form -->
        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Title</span>
              <Input bind:value={editForm.title} disabled={!isEditable} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Priority</span>
              <Input bind:value={editForm.priority} type="number" disabled={!isEditable} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Agent</span>
              <select bind:value={editForm.agent_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
                {#each agents as agent}
                  <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
                {/each}
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Workflow</span>
              <select bind:value={editForm.workflow_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
                <option value="">Auto</option>
                {#each workflows as workflow}
                  <option value={workflow.workflow_id}>{workflow.name}</option>
                {/each}
              </select>
            </label>
          </div>

          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Description</span>
            <textarea bind:value={editForm.description} disabled={!isEditable} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50"></textarea>
          </label>

          <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Delivery mode</span>
              <select bind:value={editForm.delivery_mode} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
                <option value="same_conversation">Same conversation</option>
                <option value="specific_conversation">Specific conversation</option>
                <option value="latest_active_for_agent">Latest active</option>
                <option value="preferred_channel">Preferred channel</option>
                <option value="silent">Silent</option>
              </select>
            </label>
            {#if editForm.delivery_mode === 'specific_conversation'}
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Delivery target</span>
                <select bind:value={editForm.delivery_target} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
                  <option value="">Select conversation</option>
                  {#each conversations as conversation}
                    <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
                  {/each}
                </select>
              </label>
            {/if}
          </div>

          <div class="mt-5 flex justify-end">
            <Button disabled={saving || !isEditable} onclick={saveTask}>{saving ? 'Saving...' : 'Save task'}</Button>
          </div>
        </Card>

        <!-- Pending pause -->
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

        <!-- Workflow progress -->
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Workflow progress</p>
              <h2 class="mt-1 text-lg font-semibold text-white">{workflowName(task.workflow_id)}</h2>
            </div>

            <div class="space-y-3">
              {#each task.step_runs as stepRun (stepRun.step_run_id)}
                {@const summary = stepOutputSummary(stepRun)}
                {@const content = stepOutputContent(stepRun)}
                {@const claims = stepOutputClaims(stepRun)}
                {@const isExpanded = expandedSteps.has(stepRun.step_run_id)}

                <article class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <!-- Header -->
                  <div class="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 class="font-semibold text-white">{stepRun.step_name}</h3>
                      <p class="text-xs text-slate-500">
                        {stepRun.step_type} · attempt {stepRun.attempt}
                        {#if stepRun.completed_at}
                          <span title={formatAbsoluteTime(stepRun.completed_at)}> · {formatRelativeTime(stepRun.completed_at)}</span>
                        {/if}
                      </p>
                    </div>
                    <div class="flex items-center gap-2">
                      {#if stepRun.output?.session_id || stepRun.session_id}
                        <Button size="sm" variant="ghost" onclick={() => openSessionLogs(stepRun)}>Logs</Button>
                      {/if}
                      <span class="rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[stepRun.status] ?? 'border-slate-600 text-slate-400'}">
                        {stepRun.status}
                      </span>
                    </div>
                  </div>

                  <!-- Summary -->
                  {#if summary}
                    <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-300">
                      {@html renderMarkdown(summary)}
                    </div>
                  {/if}

                  <!-- Claims -->
                  {#if claims.length > 0}
                    <ul class="mt-3 space-y-1 text-sm text-slate-400">
                      {#each claims as claim}
                        <li class="flex items-start gap-2">
                          <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600"></span>
                          <span>{claim}</span>
                        </li>
                      {/each}
                    </ul>
                  {/if}

                  <!-- Content (collapsible) -->
                  {#if content}
                    {#if content.length > 300 && !isExpanded}
                      <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-400">
                        {@html renderMarkdown(content.slice(0, 300) + '...')}
                      </div>
                      <button class="mt-2 text-xs text-blue-400 hover:text-blue-300" onclick={() => toggleStepExpand(stepRun.step_run_id)}>
                        Show full output
                      </button>
                    {:else if content.length > 300}
                      <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-400">
                        {@html renderMarkdown(content)}
                      </div>
                      <button class="mt-2 text-xs text-blue-400 hover:text-blue-300" onclick={() => toggleStepExpand(stepRun.step_run_id)}>
                        Collapse
                      </button>
                    {:else}
                      <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-400">
                        {@html renderMarkdown(content)}
                      </div>
                    {/if}
                  {/if}

                  <!-- Evaluation -->
                  {#if stepRun.evaluation}
                    {@const evalDecision = String(stepRun.evaluation.decision ?? '')}
                    {@const evalReasoning = String(stepRun.evaluation.reasoning ?? '')}
                    <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/80 px-3 py-2">
                      <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Evaluation</p>
                      <p class="mt-1 text-sm text-slate-300">
                        <span class="font-medium {evalDecision === 'approve' ? 'text-emerald-400' : 'text-amber-400'}">
                          {evalDecision}
                        </span>
                        {#if evalReasoning}
                          — {evalReasoning}
                        {/if}
                      </p>
                    </div>
                  {/if}
                </article>
              {/each}

              {#if task.step_runs.length === 0}
                <p class="text-sm text-slate-400">No steps have been executed yet.</p>
              {/if}
            </div>
          </div>
        </Card>
      </div>

      <!-- Sidebar -->
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
                  <span class="text-sm text-slate-200">{allTasks.find((c) => c.task_id === dependency.depends_on)?.title ?? dependency.depends_on}</span>
                  <Button size="sm" variant="danger" onclick={() => removeDependency(dependency.depends_on)}>Remove</Button>
                </div>
              {/each}
              {#if task.dependencies.length === 0}
                <p class="text-sm text-slate-400">No dependencies configured.</p>
              {/if}
            </div>

            {#if isEditable}
              <div class="space-y-3 border-t border-slate-800 pt-4">
                <select bind:value={dependencyTaskId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Add dependency...</option>
                  {#each allTasks.filter((c) => c.task_id !== taskIdFromRoute()) as candidate}
                    <option value={candidate.task_id}>{candidate.title}</option>
                  {/each}
                </select>
                <Button class="w-full justify-center" disabled={!dependencyTaskId} onclick={addDependency}>Add dependency</Button>
              </div>
            {/if}
          </div>
        </Card>
      </div>
    </div>
  </section>

  <!-- Session logs drawer -->
  {#if sessionDrawer}
    <SessionLogsDrawer
      conversationId={sessionDrawer.conversationId}
      sessionId={sessionDrawer.sessionId}
      stepName={sessionDrawer.stepName}
      onclose={() => (sessionDrawer = null)}
    />
  {/if}
{:else}
  <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">Task not found.</p>
{/if}
