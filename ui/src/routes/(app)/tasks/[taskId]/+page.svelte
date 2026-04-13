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
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import WorkflowDiagram from '$lib/components/workflows/WorkflowDiagram.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { renderMarkdown } from '$lib/markdown';
  import { formatAbsoluteTime, formatDuration, formatRelativeTime } from '$lib/time';
  import { workflowToFormState } from '$lib/workflows';
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
  let tickNow = $state(Date.now());
  let durationTimer: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;

  // Session logs drawer
  let sessionDrawer = $state<{ conversationId: string; sessionId: string; stepName: string } | null>(null);

  let editForm = $state({
    title: '',
    description: '',
    priority: 0,
    expected_output: '',
    agent_id: '',
    workflow_id: '',
    delivery_mode: 'same_conversation',
    delivery_target: ''
  });

  const statusColors: Record<string, string> = {
    pending: 'border-slate-600 text-slate-400',
    running: 'border-sky-600 text-sky-300',
    evaluating: 'border-violet-600 text-violet-300',
    approved: 'border-emerald-700 text-emerald-300',
    completed: 'border-emerald-700 text-emerald-300',
    failed: 'border-rose-700 text-rose-300',
    cancelled: 'border-slate-600 text-slate-500',
    paused: 'border-yellow-700 text-yellow-300',
    rejected: 'border-amber-700 text-amber-300',
  };

  const statusHints: Record<string, string> = {
    pending: 'Step is queued and waiting to start',
    running: 'Agent is actively working on this step',
    evaluating: 'Evaluator LLM is checking if the step objective was met',
    approved: 'Evaluator approved the step output',
    completed: 'Step finished (no evaluation or evaluation skipped)',
    failed: 'Step failed after exhausting all attempts',
    cancelled: 'Step was cancelled',
    paused: 'Step is paused waiting for human input',
    rejected: 'Evaluator rejected the output — agent will revise',
  };

  const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];
  const CANCELLABLE_STATUSES = ['queued', 'ready', 'running', 'paused', 'draft'];
  let isEditable = $derived(task != null && !TERMINAL_STATUSES.includes(task.status));
  let isCancellable = $derived(task != null && CANCELLABLE_STATUSES.includes(task.status));

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function taskIdFromRoute(): string {
    return $page.params.taskId ?? '';
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) return 'Auto';
    return workflows.find((w) => w.workflow_id === workflowId)?.name ?? workflowId;
  }

  function agentName(agentId: string | null): string {
    if (!agentId) return 'Unknown';
    return agents.find((a) => a.agent_id === agentId)?.name ?? agentId;
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

  function stepOutputError(stepRun: StepRun): string {
    const val = stepRun.output?.error;
    return typeof val === 'string' ? val : '';
  }

  function stepOutcomeStatus(stepRun: StepRun): string {
    const outcome = stepRun.output?.outcome;
    if (!outcome || typeof outcome !== 'object') return 'success';
    const status = (outcome as Record<string, unknown>).status;
    return typeof status === 'string' ? status : 'success';
  }

  function stepOutcomeReason(stepRun: StepRun): string {
    const outcome = stepRun.output?.outcome;
    if (!outcome || typeof outcome !== 'object') return '';
    const reason = (outcome as Record<string, unknown>).reason;
    return typeof reason === 'string' ? reason : '';
  }

  function displayStepStatus(stepRun: StepRun): string {
    const outcomeStatus = stepOutcomeStatus(stepRun);
    if (stepRun.status === 'approved' && outcomeStatus === 'rejected') {
      return 'rejected';
    }
    if (stepRun.status === 'approved' && outcomeStatus === 'failed') {
      return 'failed';
    }
    return stepRun.status;
  }

  function displayStepStatusHint(stepRun: StepRun): string {
    const outcomeStatus = stepOutcomeStatus(stepRun);
    if (stepRun.status === 'approved' && outcomeStatus === 'rejected') {
      return 'Step output was evaluator-approved, but the completed step rejected prior work';
    }
    if (stepRun.status === 'approved' && outcomeStatus === 'failed') {
      return 'Step output was evaluator-approved, but the completed step reported an operational failure';
    }
    return statusHints[stepRun.status] ?? stepRun.status;
  }

  function stepEvalFeedback(stepRun: StepRun): string {
    const val = stepRun.evaluation?.feedback;
    return typeof val === 'string' ? val : '';
  }

  function openSessionLogs(stepRun: StepRun): void {
    const sessionId = String(stepRun.output?.session_id ?? stepRun.session_id ?? '');
    if (!sessionId || !task) return;
    let conversationId = stepRun.conversation_id;
    if (!conversationId) {
      const conv = conversations.find((c) =>
        c.context?.ref === task!.task_id && c.title?.includes(stepRun.step_name)
      ) ?? conversations.find((c) => c.context?.ref === task!.task_id);
      conversationId = conv?.conversation_id ?? sessionId;
    }
    sessionDrawer = {
      conversationId,
      sessionId,
      stepName: `${stepRun.step_name} (attempt ${stepRun.attempt})`
    };
  }

  // ---------------------------------------------------------------------------
  // Diagram helpers
  // ---------------------------------------------------------------------------

  /** Resolve the workflow definition for the diagram */
  let workflowDef = $derived.by(() => {
    if (!task?.workflow_id) return null;
    return workflows.find((w) => w.workflow_id === task!.workflow_id) ?? null;
  });

  let diagramSteps = $derived.by(() => {
    if (!workflowDef) return [];
    return workflowToFormState(workflowDef).steps;
  });

  let diagramActiveStep = $derived(task?.workflow_run?.current_step_name ?? '');

  /** Build step status map from step_runs (latest attempt per step) */
  let diagramStepStatuses = $derived.by(() => {
    if (!task) return {};
    const map: Record<string, string> = {};
    const latestAttempts: Record<string, number> = {};
    for (const sr of task.step_runs) {
      const nextStatus = displayStepStatus(sr);
      if (!(sr.step_name in latestAttempts) || sr.attempt >= latestAttempts[sr.step_name]) {
        latestAttempts[sr.step_name] = sr.attempt;
        map[sr.step_name] = nextStatus;
      }
    }
    return map;
  });

  /** Build step duration map (latest attempt per step) */
  let diagramStepDurations = $derived.by(() => {
    if (!task) return {};
    const map: Record<string, string> = {};
    // Group by step_name, take the latest attempt
    const latestByStep = new Map<string, StepRun>();
    for (const sr of task.step_runs) {
      const existing = latestByStep.get(sr.step_name);
      if (!existing || sr.attempt > existing.attempt) {
        latestByStep.set(sr.step_name, sr);
      }
    }
    for (const [name, sr] of latestByStep) {
      const dur = formatDuration(sr.started_at, sr.completed_at, tickNow);
      if (dur) map[name] = dur;
    }
    return map;
  });

  let diagramSkippedSteps = $derived(task?.workflow_state?.skipped_steps ?? []);

  // ---------------------------------------------------------------------------
  // Statistics
  // ---------------------------------------------------------------------------

  let stats = $derived.by(() => {
    if (!task) return null;
    const runs = task.step_runs;
    const totalAttempts = runs.length;
    const completedSteps = new Set(
      runs
        .filter((r) => ['approved', 'completed'].includes(r.status) && stepOutcomeStatus(r) === 'success')
        .map((r) => r.step_name)
    ).size;
    const evalRevisions = runs.filter((r) => r.evaluation && String(r.evaluation.decision) === 'revise').length;
    const evalFailures = runs.filter((r) => r.evaluation && String(r.evaluation.decision) === 'failed').length;
    const multiAttemptSteps = new Set(
      runs.filter((r) => r.attempt > 1).map((r) => r.step_name)
    ).size;
    const skipped = diagramSkippedSteps.length;

    // Loop iterations from workflow state
    const loopIters = task.workflow_state?.loop_iterations;
    const totalLoops = loopIters ? Object.values(loopIters).reduce((a, b) => a + b, 0) : 0;

    // Unique step names that have run
    const uniqueSteps = new Set(runs.map((r) => r.step_name)).size;

    return {
      uniqueSteps,
      completedSteps,
      totalAttempts,
      evalRevisions,
      evalFailures,
      multiAttemptSteps,
      skipped,
      totalLoops,
    };
  });

  // ---------------------------------------------------------------------------
  // Origin / initiator
  // ---------------------------------------------------------------------------

  let sourceLabel = $derived.by(() => {
    if (!task) return '';
    const labels: Record<string, string> = {
      chat: 'Chat conversation',
      agent: 'Agent delegation',
      api: 'API request',
      scheduler: 'Scheduled',
      webhook: 'Webhook',
    };
    return labels[task.source_type] ?? task.source_type;
  });

  let sourceConversation = $derived.by(() => {
    if (!task?.source_ref) return null;
    if (task.source_type !== 'chat' && task.source_type !== 'agent') return null;
    return conversations.find((c) => c.conversation_id === task!.source_ref) ?? null;
  });

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
        expected_output: task.expected_output ?? '',
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
        expected_output: editForm.expected_output || null,
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

  async function cancelTask(): Promise<void> {
    if (!task) return;
    const confirmed = await confirmAction({
      title: 'Cancel task?',
      message: 'This stops the task and marks it as cancelled. This action cannot be undone.',
      confirmLabel: 'Cancel task'
    });
    if (!confirmed) return;
    try {
      await api.tasks.cancel(task.task_id);
      task = await api.tasks.detail(task.task_id);
      addToast('Task cancelled.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to cancel task');
    }
  }

  function startDurationTimer(): void {
    if (durationTimer) return;
    durationTimer = setInterval(() => { tickNow = Date.now(); }, 1000);
  }

  function stopDurationTimer(): void {
    if (durationTimer) { clearInterval(durationTimer); durationTimer = null; }
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
    void loadTask().then(() => { startPolling(); startDurationTimer(); });
    return () => {
      stopPolling();
      stopDurationTimer();
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
      <div class="flex items-center gap-3">
        {#if isCancellable}
          <Button size="sm" variant="danger" onclick={cancelTask}>Cancel task</Button>
        {/if}
        <span class="rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] {statusColors[task.status] ?? 'border-slate-700 text-slate-200'}">
          {task.status}
        </span>
      </div>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div class="space-y-5">
        <!-- Pipeline diagram -->
        {#if diagramSteps.length > 0}
          <Card class="p-5">
            <p class="mb-3 text-xs uppercase tracking-[0.25em] text-slate-400">Pipeline</p>
            <WorkflowDiagram
              steps={diagramSteps}
              interactionMode={workflowDef?.interaction?.mode?.toString() ?? 'explicit_gates'}
              activeStepName={diagramActiveStep}
              stepStatuses={diagramStepStatuses}
              stepDurations={diagramStepDurations}
              skippedSteps={diagramSkippedSteps}
            />
          </Card>
        {/if}

        <!-- Edit form -->
        <Card class="p-5">
          <p class="mb-3 text-xs uppercase tracking-[0.25em] text-slate-400">Task configuration</p>
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
                {#each agents.filter((a) => a.agent_type === 'primary') as agent}
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

          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Expected output</span>
            <textarea bind:value={editForm.expected_output} disabled={!isEditable} class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50" placeholder="Describe the expected format or content of the result (optional)"></textarea>
          </label>

          <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span class="inline-flex items-center gap-2">
                Delivery mode
                <Tooltip text="How the task result is delivered back. 'Same conversation' posts to the originating chat. 'Silent' stores the result without notifying.">
                  <span class="cursor-help text-slate-500">(?)</span>
                </Tooltip>
              </span>
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

        <!-- Workflow progress / step runs -->
        <Card class="p-5">
          <div class="space-y-4">
            <div class="flex items-center justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Step runs</p>
                <h2 class="mt-1 text-lg font-semibold text-white">{workflowName(task.workflow_id)}</h2>
              </div>
              {#if task.step_runs.length > 0}
                <span class="text-xs text-slate-500">{task.step_runs.length} run{task.step_runs.length !== 1 ? 's' : ''}</span>
              {/if}
            </div>

            <div class="space-y-3">
              {#each task.step_runs as stepRun (stepRun.step_run_id)}
                {@const summary = stepOutputSummary(stepRun)}
                {@const content = stepOutputContent(stepRun)}
                {@const claims = stepOutputClaims(stepRun)}
                {@const stepError = stepOutputError(stepRun)}
                {@const outcomeStatus = stepOutcomeStatus(stepRun)}
                {@const outcomeReason = stepOutcomeReason(stepRun)}
                {@const visibleStatus = displayStepStatus(stepRun)}
                {@const feedback = stepEvalFeedback(stepRun)}
                {@const isExpanded = expandedSteps.has(stepRun.step_run_id)}

                <article class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <!-- Header -->
                  <div class="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <div class="flex items-center gap-2">
                        <h3 class="font-semibold text-white">{stepRun.step_name}</h3>
                        <span class="text-xs text-slate-600">#{stepRun.attempt}</span>
                      </div>
                      <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                        <span>{stepRun.step_type === 'gate' ? 'Gate' : 'Run'}</span>
                        {#if stepRun.agent_id}
                          <span class="text-slate-400">{agentName(stepRun.agent_id)}</span>
                        {/if}
                        {#if stepRun.started_at}
                          <Tooltip text={formatAbsoluteTime(stepRun.started_at)}>
                            <span class="cursor-help">started {formatRelativeTime(stepRun.started_at)}</span>
                          </Tooltip>
                        {/if}
                        {#if stepRun.started_at}
                          <span class="font-mono text-slate-300" title="{formatAbsoluteTime(stepRun.started_at)} — {stepRun.completed_at ? formatAbsoluteTime(stepRun.completed_at) : 'running'}">
                            {formatDuration(stepRun.started_at, stepRun.completed_at, tickNow)}
                          </span>
                        {/if}
                      </div>
                    </div>
                    <div class="flex items-center gap-2">
                      {#if stepRun.output?.session_id || stepRun.session_id}
                        <Button size="sm" variant="ghost" onclick={() => openSessionLogs(stepRun)}>Logs</Button>
                      {/if}
                      <Tooltip text={displayStepStatusHint(stepRun)}>
                        <span class="cursor-help rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">
                          {visibleStatus}
                        </span>
                      </Tooltip>
                    </div>
                  </div>

                  {#if outcomeStatus !== 'success'}
                    <div class="mt-3 rounded-xl border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
                      <p class="font-medium uppercase tracking-wide text-[11px] text-amber-300">Outcome</p>
                      <p class="mt-1">
                        This step finished properly but reported
                        <span class="font-semibold uppercase"> {outcomeStatus}</span>
                        {#if outcomeReason}
                          : {outcomeReason}
                        {/if}
                      </p>
                    </div>
                  {/if}

                  <!-- Error -->
                  {#if stepError}
                    <div class="mt-3 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
                      <p class="font-medium">Error</p>
                      <pre class="mt-1 whitespace-pre-wrap text-xs text-rose-300">{stepError}</pre>
                    </div>
                  {/if}

                  <!-- Summary -->
                  {#if summary && !stepError}
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
                    {@const evalColor = evalDecision === 'approved' || evalDecision === 'approve'
                      ? 'text-emerald-400'
                      : evalDecision === 'revise'
                        ? 'text-blue-400'
                        : evalDecision === 'failed' || evalDecision === 'reject'
                          ? 'text-rose-400'
                          : 'text-amber-400'}
                    <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/80 px-3 py-2">
                      <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Evaluation</p>
                      <p class="mt-1 text-sm text-slate-300">
                        <Tooltip text={evalDecision === 'approved' ? 'Step objective was met' : evalDecision === 'revise' ? 'Agent needs to revise and retry' : evalDecision === 'failed' ? 'Step cannot succeed — will not retry' : evalDecision}>
                          <span class="cursor-help font-medium {evalColor}">{evalDecision}</span>
                        </Tooltip>
                        {#if evalReasoning}
                          — {evalReasoning}
                        {/if}
                      </p>
                      {#if feedback}
                        <p class="mt-2 rounded-lg border border-slate-700/50 bg-slate-900/50 px-2 py-1.5 text-xs text-slate-400">
                          <span class="font-medium text-slate-500">Feedback:</span> {feedback}
                        </p>
                      {/if}
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
        <!-- Origin -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Origin</p>
          <dl class="mt-3 space-y-2 text-sm">
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Source</dt>
              <dd class="text-slate-300">{sourceLabel}</dd>
            </div>
            {#if sourceConversation}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Conversation</dt>
                <dd>
                  <a href="/chat?conversation={sourceConversation.conversation_id}" class="text-sky-400 hover:text-sky-300 hover:underline">
                    {sourceConversation.title ?? 'Untitled'}
                  </a>
                </dd>
              </div>
            {:else if task.source_ref}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Reference</dt>
                <dd class="truncate text-slate-400" title={task.source_ref}>{task.source_ref}</dd>
              </div>
            {/if}
            {#if task.created_by}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Created by</dt>
                <dd class="truncate text-slate-300" title={task.created_by}>{task.created_by}</dd>
              </div>
            {/if}
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Agent</dt>
              <dd class="text-slate-300">{agentName(task.agent_id)}</dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Workflow</dt>
              <dd class="text-slate-300">{workflowName(task.workflow_id)}</dd>
            </div>
          </dl>
        </Card>

        <!-- Timing -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Timing</p>
          <dl class="mt-3 space-y-2 text-sm">
            {#if task.created_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Created</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.created_at)}>{formatRelativeTime(task.created_at)}</dd>
              </div>
            {/if}
            {#if task.started_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Started</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.started_at)}>{formatRelativeTime(task.started_at)}</dd>
              </div>
            {/if}
            {#if task.completed_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Completed</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.completed_at)}>{formatRelativeTime(task.completed_at)}</dd>
              </div>
            {/if}
            {#if task.started_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Duration</dt>
                <dd class="font-mono text-slate-200">{formatDuration(task.started_at, task.completed_at, tickNow)}</dd>
              </div>
            {/if}
          </dl>
        </Card>

        <!-- Statistics -->
        {#if stats && stats.totalAttempts > 0}
          <Card class="p-5">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Statistics</p>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex justify-between">
                <dt class="text-slate-500">Steps completed</dt>
                <dd class="font-mono text-slate-200">{stats.completedSteps} / {stats.uniqueSteps}</dd>
              </div>
              <div class="flex justify-between">
                <dt class="inline-flex items-center gap-1 text-slate-500">
                  Total step runs
                  <Tooltip text="Total number of step execution attempts, including retries. Higher than step count when steps are retried after evaluation rejection.">
                    <span class="cursor-help text-slate-600">(?)</span>
                  </Tooltip>
                </dt>
                <dd class="font-mono text-slate-200">{stats.totalAttempts}</dd>
              </div>
              {#if stats.evalRevisions > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Eval revisions
                    <Tooltip text="Times the evaluator sent a step back for revision. The agent retries within the same step run.">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-amber-300">{stats.evalRevisions}</dd>
                </div>
              {/if}
              {#if stats.evalFailures > 0}
                <div class="flex justify-between">
                  <dt class="text-slate-500">Eval failures</dt>
                  <dd class="font-mono text-rose-300">{stats.evalFailures}</dd>
                </div>
              {/if}
              {#if stats.totalLoops > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Review loops
                    <Tooltip text="Times a review loop sent execution back to an earlier step (e.g. code review rejecting implementation back to the plan step).">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-amber-300">{stats.totalLoops}</dd>
                </div>
              {/if}
              {#if stats.multiAttemptSteps > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Re-executed steps
                    <Tooltip text="Steps that were executed more than once (full re-execution, not just in-place revision). Happens when a review loop sends execution back to an earlier step.">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-slate-200">{stats.multiAttemptSteps}</dd>
                </div>
              {/if}
              {#if stats.skipped > 0}
                <div class="flex justify-between">
                  <dt class="text-slate-500">Skipped steps</dt>
                  <dd class="font-mono text-slate-400">{stats.skipped}</dd>
                </div>
              {/if}
            </dl>
          </Card>
        {/if}

        <!-- Result -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Result</p>
          <p class="mt-3 text-sm leading-6 text-slate-300">{task.result_summary ?? 'This task has not produced a final result yet.'}</p>
        </Card>

        <!-- Dependencies -->
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Dependencies</p>
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
