<script lang="ts">
  import { onDestroy } from 'svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { confirmCancel } from '$lib/task-actions';
  import type { QuestionSetAnswer, QuestionSetQuestion } from '$lib/types/api';

  type PauseOption = { action?: string | null; label?: string | null };
  type Pause = {
    pause_type: string;
    pause_id?: string | null;
    step_name?: string | null;
    question?: string | null;
    questions?: QuestionSetQuestion[] | null;
    options?: PauseOption[] | null;
  };

  let {
    pause,
    compact = false,
    busy = false,
    onGate,
    onQuestion
  } = $props<{
    pause: Pause;
    compact?: boolean;
    busy?: boolean;
    onGate: (action: string, instruction: string) => unknown | Promise<unknown>;
    onQuestion: (answers: QuestionSetAnswer[]) => unknown | Promise<unknown>;
  }>();

  let instruction = $state('');
  let confirmingCancel = $state(false);
  let alive = true;
  let answers = $state<Record<string, { selected: string[]; custom: string }>>({});
  const questions = $derived(pause.questions ?? []);
  const gateOptions = $derived(pause.options ?? []);

  onDestroy(() => { alive = false; });

  async function submitGate(action: string): Promise<void> {
    if (busy || confirmingCancel) return;
    const requestedPauseId = pause.pause_id;
    const requestedStep = pause.step_name;
    const requestedInstruction = instruction;
    if (action === 'cancel') {
      confirmingCancel = true;
      try {
        if (!(await confirmCancel())) return;
        if (
          !alive || busy || pause.pause_type !== 'gate'
          || pause.pause_id !== requestedPauseId || pause.step_name !== requestedStep
          || !gateOptions.some((option: PauseOption) => option.action === action)
        ) return;
        await onGate(action, requestedInstruction);
      } finally {
        confirmingCancel = false;
      }
      return;
    }
    await onGate(action, requestedInstruction);
  }

  function answerFor(question: QuestionSetQuestion) {
    return answers[question.id] ?? { selected: [], custom: '' };
  }

  function toggle(question: QuestionSetQuestion, optionId: string): void {
    const current = answerFor(question);
    const selected = new Set(current.selected);
    if (question.multiple) {
      if (selected.has(optionId)) selected.delete(optionId);
      else selected.add(optionId);
    } else {
      selected.clear();
      selected.add(optionId);
    }
    answers = { ...answers, [question.id]: { ...current, selected: [...selected] } };
  }

  function setCustom(question: QuestionSetQuestion, custom: string): void {
    answers = { ...answers, [question.id]: { ...answerFor(question), custom } };
  }

  function questionAnswers(): QuestionSetAnswer[] {
    return questions.map((question: QuestionSetQuestion) => ({
      question_id: question.id,
      selected_option_ids: answerFor(question).selected,
      custom_answer: answerFor(question).custom.trim() || null
    }));
  }

  const questionsValid = $derived(questions.length > 0 && questions.every((question: QuestionSetQuestion) => {
    if (!question.required) return true;
    const answer = answerFor(question);
    return answer.selected.length > 0 || Boolean(answer.custom.trim());
  }));
</script>

<section id="attention" class="scroll-mt-20" aria-label="Action required" aria-live="polite" data-testid="task-attention">
  <Card class={`overflow-hidden border-warning-border bg-warning-subtle p-0 ${compact ? 'rounded-2xl' : ''}`}>
    <div class={compact ? 'px-4 py-3' : 'px-5 py-4'}>
      <div class="flex flex-wrap items-center gap-2">
        <p class="text-xs font-semibold uppercase tracking-[0.22em] text-warning">Attention</p>
        {#if pause.step_name}
          <span class="rounded-full border border-warning-border px-2 py-0.5 text-[10px] text-warning">{pause.step_name}</span>
        {/if}
      </div>
      <h2 class={`mt-2 font-semibold text-strong ${compact ? 'text-sm' : 'text-lg'}`}>
        {pause.pause_type === 'gate'
          ? (pause.question ?? 'The task needs your approval.')
          : questions.length > 0
            ? (questions.length === 1 ? 'The agent has a question' : `The agent has ${questions.length} questions`)
            : (pause.question ?? 'The task needs your decision.')}
      </h2>
      <p class="mt-1 text-xs text-muted">
        {pause.pause_type === 'gate' ? 'Review the evidence, then approve or request changes.' : 'The task continues after you answer.'}
      </p>
    </div>

    <div class={`border-t border-warning-border ${compact ? 'space-y-3 px-4 py-3' : 'space-y-4 px-5 py-5'}`}>
      {#if pause.pause_type === 'gate'}
        <label class="block space-y-2 text-sm text-foreground">
          <span>Optional instruction</span>
          <textarea bind:value={instruction} class={`w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm ${compact ? 'min-h-20' : 'min-h-28'}`} placeholder="Add guidance for the next attempt."></textarea>
        </label>
        <div class="flex flex-wrap gap-2">
          {#each gateOptions as option, index}
            {@const action = option.action}
            {#if action}
              <Button size="sm" variant={index === 0 ? 'primary' : 'secondary'} disabled={busy || confirmingCancel} onclick={() => submitGate(action)}>
                {option.label ?? action}
              </Button>
            {/if}
          {/each}
        </div>
      {:else}
        {#each questions as question (question.id)}
          <fieldset class="min-w-0 space-y-2">
            <legend class="request-prose mb-3 w-full whitespace-normal font-medium text-foreground">{question.question}</legend>
            {#each question.options as option (option.id)}
              <label class="flex min-h-11 cursor-pointer items-start gap-3 rounded-xl border border-separator bg-inset px-3 py-2 text-sm text-foreground">
                <input type={question.multiple ? 'checkbox' : 'radio'} name={question.id} checked={answerFor(question).selected.includes(option.id)} onchange={() => toggle(question, option.id)} />
                <span><span class="block">{option.label}</span>{#if option.description}<span class="text-xs text-muted">{option.description}</span>{/if}</span>
              </label>
            {/each}
            {#if question.allow_custom}
              <textarea value={answerFor(question).custom} oninput={(event) => setCustom(question, event.currentTarget.value)} class="min-h-20 w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm" placeholder="Custom answer"></textarea>
            {/if}
          </fieldset>
        {/each}
        <Button size="sm" disabled={busy || !questionsValid} onclick={() => onQuestion(questionAnswers())}>Send response</Button>
      {/if}
    </div>
  </Card>
</section>
