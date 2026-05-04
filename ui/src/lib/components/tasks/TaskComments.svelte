<script lang="ts">
  import { onMount, untrack } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import { renderMarkdown } from '$lib/markdown';
  import type { TaskComment, TaskDetail } from '$lib/types/api';

  type CommentIntent = 'record_only' | 'context_only' | 'answer_pause' | 'request_revision';

  interface StepOption {
    name: string;
    label: string;
  }

  let {
    task,
    stepOptions = [],
    initialTargetStep = '',
    initialIntent = 'record_only' as CommentIntent,
    onSubmitted = undefined as ((comment: TaskComment) => void | Promise<void>) | undefined
  } = $props<{
    task: TaskDetail | null;
    stepOptions?: StepOption[];
    initialTargetStep?: string;
    initialIntent?: CommentIntent;
    onSubmitted?: (comment: TaskComment) => void | Promise<void>;
  }>();

  let comments = $state<TaskComment[]>([]);
  let loading = $state(true);
  let posting = $state(false);
  let body = $state('');
  let intent = $state<CommentIntent>('record_only');
  let noop = $state(true);
  let targetStep = $state('');
  let lastTaskId = '';
  let initialDefaultsApplied = false;

  // Apply caller-provided defaults once on first render.
  $effect(() => {
    if (initialDefaultsApplied) return;
    initialDefaultsApplied = true;
    untrack(() => {
      if (initialIntent) intent = initialIntent;
      if (initialTargetStep) targetStep = initialTargetStep;
    });
  });

  const taskId = $derived(task?.task_id ?? '');
  const taskStatus = $derived(task?.status ?? null);
  const pause = $derived(task?.pending_pause ?? null);
  const hasWorkflow = $derived(Boolean(task?.workflow_id));
  const isTerminal = $derived(['completed', 'failed', 'cancelled'].includes(taskStatus ?? ''));
  const canAnswerPause = $derived(
    pause !== null && (pause.pause_type === 'gate' || pause.pause_type === 'step_input')
  );
  const canRequestRevision = $derived(hasWorkflow && taskStatus !== 'cancelled');
  const requiresTargetStep = $derived(intent === 'request_revision');
  const submittable = $derived.by(() => {
    if (posting) return false;
    if (!body.trim()) return false;
    if (intent === 'answer_pause' && !canAnswerPause) return false;
    if (intent === 'request_revision' && !canRequestRevision) return false;
    if (requiresTargetStep && !targetStep) return false;
    return true;
  });

  const intentDescription = $derived.by(() => {
    switch (intent) {
      case 'record_only':
        return 'Notes only. Stored on the task and visible to humans. Never injected into the agent.';
      case 'context_only':
        return 'Sent to the agent at the next model boundary. Does not change task state.';
      case 'answer_pause':
        return 'Resolves the current pause (gate or step question) with this body as the answer.';
      case 'request_revision':
        return 'Re-enters the workflow at the chosen step. Previous outputs for that step and downstream steps are superseded.';
    }
  });

  const intentBadge: Record<string, { label: string; cls: string }> = {
    record_only: { label: 'Note', cls: 'border-slate-700 bg-slate-900/80 text-slate-300' },
    context_only: { label: 'Context', cls: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-200' },
    answer_pause: { label: 'Answer', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-200' },
    request_revision: { label: 'Revision', cls: 'border-violet-500/40 bg-violet-500/10 text-violet-200' }
  };

  function intentLabel(value: string): string {
    return intentBadge[value]?.label ?? value;
  }

  function intentClass(value: string): string {
    return intentBadge[value]?.cls ?? 'border-slate-700 bg-slate-900/80 text-slate-300';
  }

  function pickDefaultIntent(): CommentIntent {
    if (canAnswerPause) return 'answer_pause';
    return 'record_only';
  }

  function pickDefaultTargetStep(): string {
    if (initialTargetStep && stepOptions.some((option: StepOption) => option.name === initialTargetStep)) {
      return initialTargetStep;
    }
    const current = task?.workflow_run?.current_step_name ?? task?.pending_pause?.step_name ?? '';
    if (current && stepOptions.some((option: StepOption) => option.name === current)) {
      return current;
    }
    return stepOptions[0]?.name ?? '';
  }

  async function load(): Promise<void> {
    if (!taskId) {
      comments = [];
      loading = false;
      return;
    }
    loading = true;
    try {
      const next = await api.tasks.comments(taskId);
      comments = next;
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    const nextTaskId = task?.task_id ?? '';
    if (nextTaskId === lastTaskId) return;
    lastTaskId = nextTaskId;
    untrack(() => {
      void load();
      intent = pickDefaultIntent();
      noop = true;
      body = '';
      targetStep = pickDefaultTargetStep();
    });
  });

  // Reset target step when stepOptions change to something incompatible.
  $effect(() => {
    if (intent !== 'request_revision') return;
    if (!targetStep) {
      const next = pickDefaultTargetStep();
      if (next) untrack(() => { targetStep = next; });
      return;
    }
    if (!stepOptions.some((option: StepOption) => option.name === targetStep)) {
      const next = pickDefaultTargetStep();
      untrack(() => { targetStep = next; });
    }
  });

  // Auto-disable noop when intent has user-visible side effects.
  $effect(() => {
    if (intent === 'answer_pause' || intent === 'request_revision') {
      untrack(() => { noop = false; });
    }
  });

  export async function refresh(): Promise<void> {
    await load();
  }

  export function setRevisionTarget(stepName: string): void {
    if (!stepOptions.some((option: StepOption) => option.name === stepName)) return;
    intent = 'request_revision';
    targetStep = stepName;
  }

  function selectIntent(next: CommentIntent): void {
    if (next === 'answer_pause' && !canAnswerPause) return;
    if (next === 'request_revision' && !canRequestRevision) return;
    intent = next;
    if (next === 'request_revision' && !targetStep) {
      targetStep = pickDefaultTargetStep();
    }
  }

  const intentOrder: CommentIntent[] = ['record_only', 'context_only', 'answer_pause', 'request_revision'];

  function isIntentEnabled(value: CommentIntent): boolean {
    if (value === 'answer_pause') return canAnswerPause;
    if (value === 'request_revision') return canRequestRevision;
    return true;
  }

  function moveIntentFocus(delta: number): void {
    const enabled = intentOrder.filter(isIntentEnabled);
    if (enabled.length === 0) return;
    const index = enabled.indexOf(intent);
    const nextIndex = index === -1
      ? 0
      : (index + delta + enabled.length) % enabled.length;
    selectIntent(enabled[nextIndex]);
  }

  function handleIntentKeydown(event: KeyboardEvent): void {
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        event.preventDefault();
        moveIntentFocus(1);
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        event.preventDefault();
        moveIntentFocus(-1);
        break;
      case 'Home': {
        const enabled = intentOrder.filter(isIntentEnabled);
        if (enabled.length > 0) {
          event.preventDefault();
          selectIntent(enabled[0]);
        }
        break;
      }
      case 'End': {
        const enabled = intentOrder.filter(isIntentEnabled);
        if (enabled.length > 0) {
          event.preventDefault();
          selectIntent(enabled[enabled.length - 1]);
        }
        break;
      }
    }
  }

  async function submit(): Promise<void> {
    if (!submittable || !task) return;
    posting = true;
    try {
      const payload: Record<string, unknown> = {
        body: body.trim(),
        intent,
        noop
      };
      if (requiresTargetStep && targetStep) {
        payload.target_step = targetStep;
      }
      const created = await api.tasks.addComment(task.task_id, payload);
      comments = [created, ...comments];
      body = '';
      noop = intent !== 'answer_pause' && intent !== 'request_revision';
      if (onSubmitted) await onSubmitted(created);
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error');
    } finally {
      posting = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<Card class="p-4 sm:p-5">
  <div class="flex items-start justify-between gap-3">
    <div>
      <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Comments</p>
      <h3 class="mt-1 text-base font-semibold text-white">Notes, context, answers, and revisions</h3>
      <p class="mt-1 text-xs text-slate-400">{intentDescription}</p>
    </div>
    {#if task}<span class="rounded-full border border-slate-700 bg-slate-950/80 px-2 py-1 text-xs text-slate-300">Attempt #{task.attempt_number}</span>{/if}
  </div>

  {#if task}
    <div class="mt-4 space-y-3">
      <div
        role="radiogroup"
        aria-label="Comment intent"
        tabindex="-1"
        class="grid grid-cols-2 gap-2 sm:grid-cols-4"
        onkeydown={handleIntentKeydown}
      >
        {@render intentButton('record_only', 'Note', true)}
        {@render intentButton('context_only', 'Context', true)}
        {@render intentButton('answer_pause', 'Answer pause', canAnswerPause)}
        {@render intentButton('request_revision', 'Request revision', canRequestRevision)}
      </div>

      {#if intent === 'request_revision'}
        <label class="block space-y-1 text-sm font-medium text-slate-200">
          <span>Target step</span>
          <select bind:value={targetStep} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">Select step…</option>
            {#each stepOptions as option}
              <option value={option.name}>{option.label}</option>
            {/each}
          </select>
          <span class="block text-xs text-slate-500">The chosen step and every step after it will be reopened. Prior outputs are kept as superseded history.</span>
        </label>
      {/if}

      <label class="block">
        <span class="sr-only">Comment body</span>
        <textarea
          bind:value={body}
          aria-label="Comment body"
          class="min-h-[100px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
          placeholder={intent === 'answer_pause'
            ? 'Answer for the current pause…'
            : intent === 'request_revision'
              ? 'What needs to change at this step?'
              : intent === 'context_only'
                ? 'Context for the agent at the next model boundary…'
                : 'Note for the team…'}
        ></textarea>
      </label>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <label class="inline-flex items-center gap-2 text-xs text-slate-400">
          <input
            type="checkbox"
            class="h-4 w-4 rounded border-slate-600 bg-slate-950"
            bind:checked={noop}
            disabled={intent === 'answer_pause' || intent === 'request_revision'}
          />
          <span>Noop (no task state change)</span>
        </label>
        <Button onclick={submit} disabled={!submittable}>
          {posting ? 'Posting…' : intent === 'request_revision' ? 'Request revision' : intent === 'answer_pause' ? 'Submit answer' : 'Add comment'}
        </Button>
      </div>

      {#if intent === 'request_revision' && isTerminal}
        <p class="rounded-xl border border-slate-700/60 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">Task is terminal. Submitting a revision reopens it from the selected step.</p>
      {/if}
    </div>

    <div class="mt-6 space-y-3 border-t border-slate-800/80 pt-4">
      {#if loading}
        <p class="text-sm text-slate-400">Loading comments…</p>
      {:else if comments.length === 0}
        <p class="text-sm text-slate-400">No comments yet.</p>
      {:else}
        {#each comments as comment (comment.comment_id)}
          <article class="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
            <header class="flex flex-wrap items-center gap-2 text-xs">
              <span class={`rounded-full border px-2 py-0.5 font-semibold uppercase tracking-wider ${intentClass(comment.intent)}`}>{intentLabel(comment.intent)}</span>
              {#if comment.applied}
                <span class="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-emerald-200">applied</span>
              {:else if comment.intent === 'answer_pause' || comment.intent === 'request_revision'}
                <span class="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-amber-200">pending</span>
              {/if}
              {#if comment.noop}
                <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-slate-400">noop</span>
              {/if}
              {#if comment.target_step}
                <span class="rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-violet-200">→ {comment.target_step}</span>
              {/if}
              <span class="text-slate-500">attempt #{comment.attempt_number}</span>
              <span class="ml-auto text-slate-500" title={formatAbsoluteTime(comment.created_at)}>{formatRelativeTime(comment.created_at)}</span>
            </header>
            <p class="mt-1 text-xs text-slate-500">{comment.author_email}</p>
            <div class="prose prose-sm prose-invert mt-2 max-w-none text-slate-200">{@html renderMarkdown(comment.body)}</div>
          </article>
        {/each}
      {/if}
    </div>
  {/if}
</Card>

{#snippet intentButton(value: CommentIntent, label: string, enabled: boolean)}
  {@const active = intent === value}
  <button
    type="button"
    role="radio"
    aria-checked={active}
    tabindex={active ? 0 : -1}
    class={`rounded-xl border px-3 py-2 text-sm transition ${active ? 'border-sky-500 bg-sky-500/10 text-sky-100' : 'border-slate-700 bg-slate-950/80 text-slate-300 hover:border-slate-500'} ${enabled ? '' : 'cursor-not-allowed opacity-40'}`}
    onclick={() => selectIntent(value)}
    disabled={!enabled}
  >
    {label}
  </button>
{/snippet}
