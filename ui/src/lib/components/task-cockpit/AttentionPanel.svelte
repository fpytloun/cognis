<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { QuestionSetAnswer, QuestionSetQuestion } from '$lib/types/api';

  type PauseOption = { action?: string | null; label?: string | null };
  type Pause = {
    pause_type: string;
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
  let answers = $state<Record<string, { selected: string[]; custom: string }>>({});
  const questions = $derived(pause.questions ?? []);
  const gateOptions = $derived(pause.options ?? []);

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
  <Card class={`overflow-hidden border-amber-500/30 bg-amber-500/5 p-0 ${compact ? 'rounded-2xl' : ''}`}>
    <div class={compact ? 'px-4 py-3' : 'px-5 py-4'}>
      <div class="flex flex-wrap items-center gap-2">
        <p class="text-xs font-semibold uppercase tracking-[0.22em] text-amber-300">Attention</p>
        {#if pause.step_name}
          <span class="rounded-full border border-amber-500/30 px-2 py-0.5 text-[10px] text-amber-100">{pause.step_name}</span>
        {/if}
      </div>
      <h2 class={`mt-2 font-semibold text-white ${compact ? 'text-sm' : 'text-lg'}`}>
        {pause.question ?? questions[0]?.question ?? 'The task needs your decision.'}
      </h2>
      <p class="mt-1 text-xs text-slate-400">
        {pause.pause_type === 'gate' ? 'Review the evidence, then approve or request changes.' : 'Answer the question to continue the task.'}
      </p>
    </div>

    <div class={`border-t border-amber-500/20 ${compact ? 'space-y-3 px-4 py-3' : 'space-y-4 px-5 py-5'}`}>
      {#if pause.pause_type === 'gate'}
        <label class="block space-y-2 text-sm text-slate-200">
          <span>Optional instruction</span>
          <textarea bind:value={instruction} class={`w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm ${compact ? 'min-h-20' : 'min-h-28'}`} placeholder="Add guidance for the next attempt."></textarea>
        </label>
        <div class="flex flex-wrap gap-2">
          {#if gateOptions.length > 0}
            {#each gateOptions as option, index}
              <Button size="sm" variant={index === 0 ? 'primary' : 'secondary'} disabled={busy} onclick={() => onGate(String(option.action ?? 'continue'), instruction)}>
                {String(option.label ?? option.action ?? 'Continue')}
              </Button>
            {/each}
          {:else}
            <Button size="sm" disabled={busy} onclick={() => onGate('continue', instruction)}>Approve & continue</Button>
            <Button size="sm" variant="secondary" disabled={busy} onclick={() => onGate('reject', instruction)}>Reject / revise</Button>
          {/if}
        </div>
      {:else}
        {#each questions as question (question.id)}
          <fieldset class="space-y-2">
            <legend class="text-sm font-medium text-slate-100">{question.question}</legend>
            {#each question.options as option (option.id)}
              <label class="flex min-h-11 cursor-pointer items-start gap-3 rounded-xl border border-slate-700 px-3 py-2 text-sm text-slate-200">
                <input type={question.multiple ? 'checkbox' : 'radio'} name={question.id} checked={answerFor(question).selected.includes(option.id)} onchange={() => toggle(question, option.id)} />
                <span><span class="block">{option.label}</span>{#if option.description}<span class="text-xs text-slate-400">{option.description}</span>{/if}</span>
              </label>
            {/each}
            {#if question.allow_custom}
              <textarea value={answerFor(question).custom} oninput={(event) => setCustom(question, event.currentTarget.value)} class="min-h-20 w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm" placeholder="Custom answer"></textarea>
            {/if}
          </fieldset>
        {/each}
        <Button size="sm" disabled={busy || !questionsValid} onclick={() => onQuestion(questionAnswers())}>Send response</Button>
      {/if}
    </div>
  </Card>
</section>
