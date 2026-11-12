<script lang="ts">
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import type { UserInteractionTimelineItem } from '$lib/timeline-render-model';
  import ToolInvocationPreview from '$lib/components/ToolInvocationPreview.svelte';
  import { renderMarkdown } from '$lib/markdown';

  let { item } = $props<{ item: UserInteractionTimelineItem }>();

  const nowDate = $derived(new Date($nowStore));
  let page = $state<{ id: string | null; index: number }>({ id: null, index: 0 });
  const isQuestion = $derived(['step_question', 'question'].includes(item.interactionType));
  const answerIndex = $derived(page.id === item.id ? Math.min(page.index, Math.max(0, item.answers.length - 1)) : 0);
  const currentAnswer = $derived(item.answers[answerIndex]);
  const multipleAnswers = $derived(item.answers.length > 1);
  const title = $derived(isQuestion && item.status === 'complete'
    ? item.answers.length > 1 ? `You answered ${item.answers.length} questions` : 'You answered question'
    : item.title);
  const tone = $derived(
    item.status === 'complete'
      ? 'border-success-border bg-success-subtle text-foreground'
      : item.status === 'denied'
        ? 'border-warning-border bg-warning-subtle text-foreground'
        : item.status === 'failed'
          ? 'border-danger-border bg-danger-subtle text-foreground'
          : item.status === 'cancelled'
            ? 'border-control-border bg-surface/70 text-secondary'
            : 'border-control-border bg-raised/80 text-foreground'
  );
  const approvalDetails = $derived.by(() => {
    if (item.interactionType !== 'escalation') return null;
    const find = (question: string) => item.answers.find(
      (answer: { question?: string | null; answer: string }) => answer.question === question
    )?.answer ?? null;
    const rawArguments = find('Arguments');
    let argumentsDisplay: Record<string, unknown> | null = null;
    if (rawArguments) {
      try {
        const parsed = JSON.parse(rawArguments);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          argumentsDisplay = parsed as Record<string, unknown>;
        }
      } catch {
        // Historical interaction data can be plain text.
      }
    }
    return {
      toolName: find('Action'),
      argumentsDisplay,
      reasoning: find('Reason'),
      risk: find('Risk'),
      feedback: find('Feedback')
    };
  });
</script>

<article class={`ml-auto w-full max-w-2xl overflow-hidden rounded-[1.4rem] border px-4 py-3 shadow-card sm:rounded-3xl ${tone}`}>
  <div class="flex items-start gap-2.5">
    {#if !isQuestion}<span class="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-current opacity-80" aria-hidden="true"></span>{/if}
    <div class="min-w-0 flex-1">
      <p class="flex items-center gap-2 text-sm font-semibold">
        {#if isQuestion && item.status === 'complete'}<span class="text-success" aria-hidden="true">✓</span>{/if}
        {title}
      </p>
      {#if item.summary}
        <p class="mt-1 text-sm leading-6 opacity-90">{item.summary}</p>
      {/if}
      {#if approvalDetails}
        <ToolInvocationPreview
          toolName={approvalDetails.toolName}
          argumentsDisplay={approvalDetails.argumentsDisplay}
          reasoning={approvalDetails.reasoning}
          risk={approvalDetails.risk}
          tone="emerald"
        />
        {#if approvalDetails.feedback}
          <p class="mt-2 text-sm opacity-85">{approvalDetails.feedback}</p>
        {/if}
      {:else if isQuestion && currentAnswer}
        <div class="mt-3 border-t border-current/15 pt-3">
          {#if multipleAnswers}
            <nav class="mb-3 flex items-center justify-between gap-3" aria-label="Question answers">
              <span class="text-xs font-medium text-secondary" aria-live="polite" aria-atomic="true">Answer {answerIndex + 1} / {item.answers.length}</span>
              <div class="flex gap-1">
                <button type="button" aria-label="Previous answer" disabled={answerIndex === 0}
                  onclick={() => page = { id: item.id, index: answerIndex - 1 }}
                  class="flex h-11 w-11 items-center justify-center rounded-xl border border-control-border bg-surface hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-default disabled:opacity-35">
                  <span class="text-xl leading-none" aria-hidden="true">‹</span>
                </button>
                <button type="button" aria-label="Next answer" disabled={answerIndex === item.answers.length - 1}
                  onclick={() => page = { id: item.id, index: answerIndex + 1 }}
                  class="flex h-11 w-11 items-center justify-center rounded-xl border border-control-border bg-surface hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-default disabled:opacity-35">
                  <span class="text-xl leading-none" aria-hidden="true">›</span>
                </button>
              </div>
            </nav>
          {/if}
            <div class="space-y-4">
              <section aria-label="Agent request">
                <h3 class="mb-2 text-xs font-semibold text-secondary">Agent request</h3>
                <div class="grid min-w-0">
                  {#each item.answers as answer, index}
                    <!-- All pages size the grid. Keyboard focus lets users scroll the active request. -->
                    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                    <div tabindex={index === answerIndex ? 0 : -1} role="group" aria-label="Agent request content"
                      inert={index !== answerIndex} aria-hidden={index !== answerIndex}
                      class={`col-start-1 row-start-1 max-h-48 min-w-0 overflow-auto overscroll-contain rounded-xl border border-control-border bg-surface px-3 py-2.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary ${index !== answerIndex ? 'invisible' : ''}`}>
                      <div class="prose prose-sm max-w-none break-words">{@html renderMarkdown(answer.question || 'Question')}</div>
                    </div>
                  {/each}
                </div>
              </section>
              <section aria-label="Your answer">
                <h3 class="mb-2 text-xs font-semibold text-secondary">Your answer</h3>
                <div class="grid min-w-0">
                  {#each item.answers as answer, index}
                    <!-- Keyboard focus is required to scroll an overflowing answer. -->
                    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                    <div tabindex={index === answerIndex ? 0 : -1} role="group" aria-label="Your answer content"
                      inert={index !== answerIndex} aria-hidden={index !== answerIndex}
                      class={`col-start-1 row-start-1 max-h-28 min-w-0 overflow-auto overscroll-contain border-l-2 border-success-border pl-3 pr-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary ${index !== answerIndex ? 'invisible' : ''}`}>
                      <p class="whitespace-pre-wrap break-words text-sm leading-6">{answer.answer}</p>
                    </div>
                  {/each}
                </div>
              </section>
            </div>
        </div>
      {:else if item.answers.length > 0}
        <dl class="mt-3 space-y-2 border-t border-current/15 pt-3">
          {#each item.answers as answer}
            <div class="rounded-2xl bg-inset px-3 py-2">
              {#if answer.question}
                <dt class="text-sm font-semibold leading-5 opacity-85">{answer.question}</dt>
              {/if}
              <dd class="mt-0.5 whitespace-pre-wrap break-words text-sm leading-6">{answer.answer}</dd>
            </div>
          {/each}
        </dl>
      {/if}
    </div>
  </div>
  <div class="mt-2.5 flex justify-end text-[11px] opacity-70" data-copy-exclude>
    <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
  </div>
</article>
