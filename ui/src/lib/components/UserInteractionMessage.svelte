<script lang="ts">
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import type { UserInteractionTimelineItem } from '$lib/timeline-render-model';
  import ToolInvocationPreview from '$lib/components/ToolInvocationPreview.svelte';

  let { item } = $props<{ item: UserInteractionTimelineItem }>();

  const nowDate = $derived(new Date($nowStore));
  const tone = $derived(
    item.status === 'complete'
      ? 'border-emerald-400/35 bg-emerald-500/15 text-emerald-50'
      : item.status === 'denied'
        ? 'border-amber-400/35 bg-amber-500/15 text-amber-50'
        : 'border-slate-600 bg-slate-800/80 text-slate-100'
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
    <span class="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-current opacity-80" aria-hidden="true"></span>
    <div class="min-w-0 flex-1">
      <p class="text-sm font-semibold">{item.title}</p>
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
      {:else if item.answers.length > 0}
        <dl class="mt-3 space-y-2 border-t border-current/15 pt-3">
          {#each item.answers as answer}
            <div class="rounded-2xl bg-black/10 px-3 py-2">
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
  <div class="mt-2.5 flex justify-end text-[11px] opacity-70">
    <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
  </div>
</article>
