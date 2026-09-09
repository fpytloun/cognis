<script lang="ts">
  import type {
    AttentionActionChoice,
    AttentionActionDetail,
    AttentionActionResolvePayload,
    QuestionSetAnswer,
    QuestionSetQuestion,
  } from '$lib/types/api';
  import { isAttentionActionDetailActionable } from '$lib/attention/actions';

  let {
    action,
    busy = false,
    error = null,
    onSubmit,
    onDismiss,
  } = $props<{
    action: AttentionActionDetail;
    busy?: boolean;
    error?: string | null;
    onSubmit: (payload: Omit<AttentionActionResolvePayload, 'expected_revision' | 'submission_id'>) => void | Promise<void>;
    onDismiss?: () => void;
  }>();

  let note = $state('');
  let feedback = $state('');
  let fields = $state<Record<string, string>>({});
  let answers = $state<Record<string, { selected: string[]; custom: string }>>({});
  let activeActionId = $state('');
  const actionable = $derived(isAttentionActionDetailActionable(action));

  $effect(() => {
    if (activeActionId === action.action_id) return;
    activeActionId = action.action_id;
    note = '';
    feedback = '';
    fields = {};
    answers = {};
  });

  function toggle(question: QuestionSetQuestion, optionId: string): void {
    const current = answers[question.id] ?? { selected: [], custom: '' };
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

  function setCustom(questionId: string, custom: string): void {
    const current = answers[questionId] ?? { selected: [], custom: '' };
    answers = { ...answers, [questionId]: { ...current, custom } };
  }

  function questionAnswers(): QuestionSetAnswer[] {
    return action.display.questions.map((question: QuestionSetQuestion) => {
      const answer = answers[question.id] ?? { selected: [], custom: '' };
      return {
        question_id: question.id,
        selected_option_ids: answer.selected,
        custom_answer: answer.custom.trim() || null,
      };
    });
  }

  function valid(choice: AttentionActionChoice): boolean {
    if (choice.input === 'credential' || choice.input === 'auth_fields') {
      return action.display.required_fields.every((field: string) => Boolean(fields[field]?.trim()));
    }
    if (choice.input === 'structured') {
      return action.display.questions.every((question: QuestionSetQuestion) => {
        if (!question.required) return true;
        const answer = answers[question.id];
        return Boolean(answer?.selected.length || answer?.custom.trim());
      });
    }
    return true;
  }

  async function submit(choice: AttentionActionChoice): Promise<void> {
    const payload: Omit<AttentionActionResolvePayload, 'expected_revision' | 'submission_id'> = {
      action: choice.action,
    };
    if (choice.input === 'note' && note.trim()) payload.note = note.trim();
    if (choice.input === 'feedback' && feedback.trim()) payload.feedback = feedback.trim();
    if (choice.input === 'structured') payload.answers = questionAnswers();
    if (choice.input === 'credential' || choice.input === 'auth_fields') {
      payload.response_fields = Object.fromEntries(
        action.display.required_fields.map((field: string) => [field, fields[field] ?? ''])
      );
    }
    await onSubmit(payload);
  }

  function inputType(field: string): string {
    return /(password|token|secret|seed|code)/i.test(field) ? 'password' : 'text';
  }

  function fieldLabel(field: string): string {
    const value = field.replaceAll('_', ' ');
    return value.charAt(0).toUpperCase() + value.slice(1);
  }

  function preserveInteractivePointer(event: PointerEvent): void {
    if (
      event.target instanceof Element
      && event.target.closest('input, textarea, select, button, a, label')
    ) event.stopPropagation();
  }
</script>

<section
  class="h-full overflow-y-auto overscroll-contain p-4 sm:p-5"
  aria-label={action.title}
  data-testid="attention-action-renderer"
  onpointerdown={preserveInteractivePointer}
>
  <div class="mx-auto max-w-2xl space-y-4">
    <div>
      <p class="text-xs font-semibold uppercase tracking-[0.22em] text-amber-300">Action required</p>
      <h2 class="mt-1 text-lg font-semibold text-white">{action.title}</h2>
      {#if action.display.message}<p class="mt-2 text-sm leading-6 text-slate-300">{action.display.message}</p>{/if}
    </div>

    {#if action.kind === 'escalation'}
      <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 p-4">
        <p class="font-medium text-sky-100">{action.display.tool_name ?? 'Tool request'}</p>
        {#if action.display.arguments_display}
          <pre class="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-950/70 p-3 text-xs text-slate-200">{JSON.stringify(action.display.arguments_display, null, 2)}</pre>
        {/if}
        {#if action.display.reasoning}<p class="mt-3 text-sm text-slate-300">{action.display.reasoning}</p>{/if}
        {#if action.display.risk}<p class="mt-2 text-sm text-amber-200">Risk: {action.display.risk}</p>{/if}
        <label class="mt-3 block text-sm text-slate-200">Optional note
          <textarea bind:value={note} maxlength="500" disabled={busy} class="mt-1 min-h-20 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2"></textarea>
        </label>
      </div>
    {:else if action.kind === 'gate'}
      <label class="block text-sm text-slate-200">Optional instruction
        <textarea bind:value={feedback} maxlength="4000" disabled={busy} class="mt-1 min-h-24 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2"></textarea>
      </label>
    {:else if action.kind === 'step_question'}
      {#each action.display.questions as question (question.id)}
        <fieldset class="space-y-2 rounded-2xl border border-slate-700 p-4">
          <legend class="px-1 text-sm font-medium text-white">{question.question}</legend>
          {#each question.options as option (option.id)}
            <label class="flex min-h-11 items-start gap-3 rounded-xl border border-slate-700 px-3 py-2 text-sm">
              <input
                type={question.multiple ? 'checkbox' : 'radio'}
                name={`${action.action_id}-${question.id}`}
                checked={(answers[question.id]?.selected ?? []).includes(option.id)}
                disabled={busy}
                onchange={() => toggle(question, option.id)}
              />
              <span>{option.label}{#if option.description}<small class="block text-slate-400">{option.description}</small>{/if}</span>
            </label>
          {/each}
          {#if question.allow_custom}
            <textarea
              value={answers[question.id]?.custom ?? ''}
              disabled={busy}
              class="min-h-20 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
              placeholder="Custom answer"
              oninput={(event) => setCustom(question.id, event.currentTarget.value)}
            ></textarea>
          {/if}
        </fieldset>
      {/each}
    {:else if action.kind === 'credential_request' || action.kind === 'auth_challenge'}
      <div class="grid gap-3 sm:grid-cols-2">
        {#each action.display.required_fields as field (field)}
          <label class="text-sm text-slate-200"><span>{fieldLabel(field)}</span>
            <input
              type={inputType(field)}
              autocomplete="off"
              disabled={busy}
              value={fields[field] ?? ''}
              class="mt-1 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2"
              oninput={(event) => fields = { ...fields, [field]: event.currentTarget.value }}
            />
          </label>
        {/each}
      </div>
      {#if action.kind === 'credential_request'}
        <p class="text-xs text-slate-400">Values go directly to encrypted credential storage and are not shown to the model.</p>
      {/if}
    {:else if action.kind === 'oauth_authorization'}
      {#if action.display.authorization_url}
        <a
          class="inline-flex min-h-11 items-center rounded-xl bg-amber-300 px-4 py-2 font-medium text-slate-950"
          href={action.display.authorization_url}
          target="_blank"
          rel="noreferrer"
          onpointerdown={(event) => event.stopPropagation()}
        >Open authorization</a>
      {/if}
      {#if action.display.user_code}<p class="rounded-xl border border-amber-400/30 p-3">Code: <code>{action.display.user_code}</code></p>{/if}
    {:else}
      <p class="rounded-xl border border-slate-700 bg-slate-900 p-3 text-sm text-slate-300">This action is unavailable.</p>
    {/if}

    {#if error}<p class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100" role="alert">{error}</p>{/if}
    {#if busy}<p class="text-sm text-sky-200" role="status" aria-live="polite">Resolving…</p>{/if}

    <div class="flex flex-wrap gap-2">
      {#each actionable ? action.allowed_actions : [] as choice (`${choice.action}-${choice.input}`)}
        <button
          type="button"
          disabled={busy || !valid(choice)}
          class={`min-h-11 rounded-xl px-4 py-2 text-sm font-medium disabled:opacity-50 ${
            choice.intent === 'danger'
              ? 'border border-rose-400/40 text-rose-200'
              : choice.intent === 'primary'
                ? 'bg-sky-400 text-slate-950'
                : 'border border-slate-600 text-slate-200'
           }`}
           onclick={() => void submit(choice)}
           onpointerdown={(event) => event.stopPropagation()}
         >{choice.label}</button>
      {/each}
      {#if !actionable && onDismiss}
        <button
          type="button"
          class="min-h-11 rounded-xl border border-slate-600 px-4 py-2 text-sm font-medium text-slate-200"
          onclick={onDismiss}
        >Close</button>
      {/if}
    </div>
  </div>
</section>
