<script lang="ts">
  import { goto } from '$app/navigation';

  import AttentionPanel from '$lib/components/task-cockpit/AttentionPanel.svelte';
  import TaskComments from '$lib/components/tasks/TaskComments.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { confirmCancel, confirmNewRun, confirmPause } from '$lib/task-actions';
  import type { QuestionSetAnswer, TaskComment, TaskDetail } from '$lib/types/api';

  type ActivePause = NonNullable<TaskDetail['pending_pause']>;
  type RefreshResult = 'refreshed' | 'superseded' | 'not-found' | 'failed';

  let {
    task,
    canMutate,
    onRefresh,
    onMutationSettled,
  } = $props<{
    task: TaskDetail;
    canMutate: boolean;
    onRefresh: () => RefreshResult | Promise<RefreshResult>;
    onMutationSettled?: (taskId: string, changed: boolean) => void | Promise<void>;
  }>();

  let busyAction = $state<string | null>(null);
  let error = $state<string | null>(null);
  let success = $state<string | null>(null);
  let unavailable = $state(false);
  let actionGeneration = 0;

  const cancellable = $derived(['queued', 'ready', 'running', 'paused', 'draft'].includes(task.status));
  const rerunnable = $derived(['completed', 'failed', 'cancelled'].includes(task.status));
  const currentStep = $derived(
    task.workflow_run?.current_step_name
      ?? task.workflow_projection?.current_step_name
      ?? null
  );
  const activePause = $derived.by<ActivePause | null>(() => {
    if (task.status !== 'paused' || !task.pending_pause) return null;
    const pause = task.pending_pause;
    if (pause.pause_type === 'gate') return pause;
    if (
      ['step_input', 'step_question'].includes(pause.pause_type)
      && (pause.questions?.length ?? 0) > 0
    ) return pause;
    return null;
  });
  const blockerReason = $derived(
    task.pending_pause?.question
      ?? task.pending_pause?.questions?.[0]?.question
      ?? task.result_summary
      ?? task.applied_completion_reason
      ?? null
  );

  function begin(action: string): void {
    actionGeneration += 1;
    busyAction = action;
    error = null;
    success = null;
  }

  async function refreshAfter(message: string): Promise<void> {
    const refreshed = await onRefresh();
    if (refreshed === 'refreshed') {
      success = message;
    } else if (refreshed === 'failed') {
      error = `${message} Current task details could not be refreshed.`;
    } else if (refreshed === 'not-found') {
      unavailable = true;
    }
  }

  function taskRevision(): string {
    return `${task.status}:${task.updated_at ?? ''}:${task.attempt_number}`;
  }

  async function recoverLifecycleFailure(
    caught: unknown,
    startedTask: TaskDetail,
    startedRevision: string
  ): Promise<void> {
    const apiError = asApiError(caught);
    const originalError = apiError.message || 'The task lifecycle action failed.';
    const generation = actionGeneration;
    error = originalError;
    if (
      ![404, 409].includes(apiError.status)
      || task !== startedTask
      || taskRevision() !== startedRevision
    ) return;
    const refreshed = await onRefresh();
    if (generation !== actionGeneration) return;
    error = originalError;
    if (refreshed === 'not-found') unavailable = true;
  }

  async function pause(): Promise<void> {
    if (!canMutate || busyAction || !['running', 'evaluating'].includes(task.status)) return;
    if (!(await confirmPause())) return;
    const startedTask = task;
    const startedRevision = taskRevision();
    begin('pause');
    let changed = false;
    try {
      await api.tasks.pause(task.task_id);
      changed = true;
      await refreshAfter('Task paused.');
    } catch (caught) {
      await recoverLifecycleFailure(caught, startedTask, startedRevision);
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function resume(): Promise<void> {
    if (!canMutate || busyAction || task.status !== 'paused' || activePause) return;
    const startedTask = task;
    const startedRevision = taskRevision();
    begin('resume');
    let changed = false;
    try {
      await api.tasks.resume(task.task_id);
      changed = true;
      await refreshAfter('Task resumed.');
    } catch (caught) {
      await recoverLifecycleFailure(caught, startedTask, startedRevision);
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function cancel(): Promise<void> {
    if (!canMutate || busyAction || !cancellable) return;
    if (!(await confirmCancel())) return;
    const startedTask = task;
    const startedRevision = taskRevision();
    begin('cancel');
    let changed = false;
    try {
      await api.tasks.cancel(task.task_id);
      changed = true;
      await refreshAfter('Task cancelled.');
    } catch (caught) {
      await recoverLifecycleFailure(caught, startedTask, startedRevision);
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function rerun(): Promise<void> {
    if (!canMutate || busyAction || !rerunnable) return;
    if (task.status !== 'paused' && !(await confirmNewRun(task.status))) return;
    const startedTask = task;
    const startedRevision = taskRevision();
    begin('rerun');
    let changed = false;
    try {
      const result = await api.tasks.rerun(task.task_id);
      changed = !result.created_new;
      if (result.created_new && result.task_id !== task.task_id) {
        await goto(`/tasks/${result.task_id}`);
        return;
      }
      await refreshAfter('Task re-run started.');
    } catch (caught) {
      await recoverLifecycleFailure(caught, startedTask, startedRevision);
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function respondToGate(action: string, instruction: string): Promise<void> {
    if (!canMutate || busyAction || !activePause) return;
    begin('response');
    let changed = false;
    try {
      await api.tasks.gateResponse(task.task_id, {
        pause_id: activePause.pause_id,
        step_name: activePause.step_name,
        action,
        feedback: instruction.trim() || null
      });
      changed = true;
      await refreshAfter('Decision submitted.');
    } catch (caught) {
      error = asApiError(caught).message || 'Could not submit the decision.';
      await onRefresh();
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function respondToQuestion(answers: QuestionSetAnswer[]): Promise<void> {
    if (!canMutate || busyAction || !activePause) return;
    begin('response');
    let changed = false;
    try {
      await api.tasks.stepResponse(task.task_id, { mode: 'structured', answers });
      changed = true;
      await refreshAfter('Response submitted.');
    } catch (caught) {
      error = asApiError(caught).message || 'Could not submit the response.';
      await onRefresh();
    } finally {
      await onMutationSettled?.(task.task_id, changed);
      busyAction = null;
    }
  }

  async function commentSubmitted(_comment: TaskComment): Promise<void> {
    await refreshAfter('Task context saved.');
  }
</script>

<div class="space-y-4" data-testid="dashboard-task-control">
  {#if unavailable}
    <div data-testid="dashboard-task-control-unavailable">
      <Card class="border-warning-border bg-warning-subtle p-4">
        <p class="text-sm font-semibold text-warning">Task unavailable</p>
        <p class="mt-1 text-sm text-secondary">This task no longer exists or is no longer available. Lifecycle actions are disabled.</p>
      </Card>
    </div>
    {#if error}
      <p class="rounded-xl border border-danger-border bg-danger-subtle p-3 text-sm text-danger" role="alert">{error}</p>
    {/if}
  {:else}
  <Card class="p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-semibold uppercase tracking-[0.22em] text-quiet">Task control</p>
        <p class="mt-1 text-lg font-semibold capitalize text-strong">{task.status}</p>
        <p class="mt-1 text-xs text-muted">
          {currentStep ? `Current step: ${currentStep}` : 'No active workflow step'}
        </p>
      </div>
      <span class="rounded-full border border-control-border bg-canvas/70 px-2.5 py-1 text-xs text-secondary">
        Attempt {task.attempt_number}
      </span>
    </div>
    {#if blockerReason}
      <div class="mt-3 rounded-xl border border-warning-border bg-warning-subtle px-3 py-2">
        <p class="text-xs font-medium text-warning">{task.pending_pause ? 'Pause / blocker' : 'Latest status detail'}</p>
        <p class="mt-1 text-sm text-foreground">{blockerReason}</p>
      </div>
    {/if}
  </Card>

  {#if !canMutate}
    <p class="rounded-xl border border-control-border bg-surface/70 p-3 text-sm text-secondary" data-testid="dashboard-task-control-readonly">
      View-only access. Task lifecycle actions and responses are available only to task operators.
    </p>
  {/if}

  {#if activePause}
    {#if canMutate}
      <AttentionPanel
        pause={activePause}
        compact
        busy={busyAction !== null}
        onGate={respondToGate}
        onQuestion={respondToQuestion}
      />
    {:else}
      <Card class="border-warning-border bg-warning-subtle p-4">
        <p class="text-xs font-semibold uppercase tracking-[0.22em] text-warning">Pending request</p>
        <p class="mt-2 text-sm text-foreground">
          {activePause.question ?? activePause.questions?.[0]?.question ?? 'This task needs an operator response.'}
        </p>
      </Card>
    {/if}
  {:else if task.pending_pause}
    <div data-testid="dashboard-task-control-protected-pause">
      <Card class="border-warning-border bg-warning-subtle p-4">
        <p class="text-xs font-semibold uppercase tracking-[0.22em] text-warning">Protected request</p>
        <p class="mt-2 text-sm text-foreground">
          {task.pending_pause.question ?? 'This request must be completed in the full task view.'}
        </p>
        <p class="mt-1 text-xs text-muted">Open the full task to use its protected resolver.</p>
      </Card>
    </div>
  {/if}

  {#if canMutate}
    <Card class="p-4">
      <h3 class="text-sm font-semibold text-strong">Lifecycle</h3>
      <div class="mt-3 flex flex-wrap gap-2">
        {#if ['running', 'evaluating'].includes(task.status)}
          <Button size="sm" variant="secondary" disabled={busyAction !== null} onclick={() => void pause()}>
            {busyAction === 'pause' ? 'Pausing…' : 'Pause task'}
          </Button>
        {:else if task.status === 'paused' && !task.pending_pause}
          <Button size="sm" disabled={busyAction !== null} onclick={() => void resume()}>
            {busyAction === 'resume' ? 'Resuming…' : 'Resume task'}
          </Button>
        {:else if task.status === 'paused' && task.pending_pause}
          <Button size="sm" disabled>Resolve the pending request to resume</Button>
        {/if}
        {#if rerunnable && task.status !== 'paused'}
          <Button size="sm" variant="secondary" disabled={busyAction !== null} onclick={() => void rerun()}>
            {busyAction === 'rerun' ? 'Starting…' : 'Start a new run'}
          </Button>
        {/if}
        {#if cancellable}
          <Button class="ml-auto" size="sm" variant="ghost" disabled={busyAction !== null} onclick={() => void cancel()}>
            <span class="text-danger">{busyAction === 'cancel' ? 'Cancelling…' : 'Cancel task'}</span>
          </Button>
        {/if}
      </div>
    </Card>

    <TaskComments
      {task}
      initialIntent="context_only"
      onSubmitted={commentSubmitted}
    />
  {/if}

  {#if error}
    <p class="rounded-xl border border-danger-border bg-danger-subtle p-3 text-sm text-danger" role="alert">{error}</p>
  {/if}
  {#if success}
    <p class="rounded-xl border border-success-border bg-success-subtle p-3 text-sm text-success" role="status">{success}</p>
  {/if}
  {/if}
</div>
