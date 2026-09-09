<script lang="ts">
  import type { CompactionTimelineItem } from '$lib/timeline-render-model';
  import { renderMarkdown } from '$lib/markdown';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import Button from '$lib/components/ui/Button.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';

  let { item, onViewPreviousSession } = $props<{
    item: CompactionTimelineItem;
    onViewPreviousSession?: ((sessionId: string) => void) | undefined;
  }>();

  const usageLabel = $derived(
    item.effectiveUsagePercentage != null
      ? `${item.effectiveUsagePercentage.toFixed(1)}% effective`
      : item.previousUsagePercentage != null
        ? `${item.previousUsagePercentage.toFixed(1)}% window`
        : null
  );
  const summaryText = $derived(
    item.status === 'compacted' ? (item.summary || item.summaryPreview) : ''
  );
  const summaryHtml = $derived(summaryText ? renderMarkdown(summaryText) : '');
  const detailsEntries = $derived.by(() => {
    return [
      item.trigger ? ['Trigger', item.trigger] : null,
      item.reason ? ['Reason', item.reason] : null,
      usageLabel ? ['Context usage', usageLabel] : null,
      item.hardPressureExceeded ? ['Pressure', 'hard pressure exceeded'] : null,
      item.usedTimeoutFallback ? ['Fallback', 'mechanical fallback used'] : null
    ].filter((entry): entry is [string, string] => entry !== null);
  });
  const title = $derived(
    item.status === 'running'
      ? 'Compacting session'
      : item.status === 'compacted'
        ? 'Session compacted'
        : item.status === 'skipped'
          ? 'Compaction skipped'
          : 'Compaction failed'
  );
  const badge = $derived(item.status === 'compacted' ? 'compacted' : item.status);
  const tone = $derived(
    item.status === 'failed'
      ? 'border-rose-500/30 bg-rose-500/10 text-rose-100'
      : item.status === 'skipped'
        ? 'border-slate-700 bg-slate-900/70 text-slate-300'
        : 'border-cyan-500/30 bg-cyan-500/10 text-cyan-100'
  );
</script>

<article class={`rounded-3xl border px-4 py-4 shadow-card ${tone}`}>
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] opacity-80">
        {title}
      </p>
      <p class="mt-1 text-sm leading-6 opacity-90">
        {#if item.status === 'running'}
          Reducing conversation history before the next response.
        {:else if item.status === 'compacted'}
          {item.turnsCompacted} {item.turnsCompacted === 1 ? 'turn' : 'turns'} summarized
          <span class="opacity-60">({item.method})</span>
        {:else}
          {item.reason || item.summaryPreview}
        {/if}
      </p>
      <p class="mt-2 text-xs uppercase tracking-[0.2em] opacity-75" title={formatAbsoluteTime(item.timestamp)}>
        {formatRelativeTime(item.timestamp)}
      </p>
    </div>
    <div class="flex items-center gap-2">
      {#if item.status === 'running'}
        <LiveDots inline={true} size="sm" tone="sky" label="Compacting" />
      {:else}
        <span class="rounded-full border border-current/30 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em]">
          {badge}
        </span>
      {/if}
    </div>
  </div>

  {#if detailsEntries.length > 0}
    <details class="mt-3 text-sm leading-6 opacity-80">
      <summary class="cursor-pointer select-none text-xs font-semibold uppercase tracking-[0.2em] opacity-80">
        Details
      </summary>
      <dl class="mt-2 grid gap-1.5 text-xs">
        {#each detailsEntries as [label, value]}
          <div class="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
            <dt class="opacity-60">{label}</dt>
            <dd class="break-words font-mono">{value}</dd>
          </div>
        {/each}
      </dl>
    </details>
  {/if}

  {#if summaryText}
    <details class="mt-3 text-sm leading-6 opacity-80">
      <summary class="cursor-pointer select-none text-xs font-semibold uppercase tracking-[0.2em] opacity-80">
        Show compaction summary
      </summary>
      <div
        class="chat-markdown prose prose-invert prose-sm mt-2 max-w-none overflow-x-auto break-words [overflow-wrap:anywhere] prose-code:text-sky-100 prose-code:before:content-none prose-code:after:content-none prose-pre:overflow-x-auto"
      >
        {@html summaryHtml}
      </div>
    </details>
  {/if}

  {#if item.status === 'compacted' && onViewPreviousSession && item.previousSessionId}
    <div class="mt-3 flex justify-end">
      <Button
        size="sm"
        variant="ghost"
        onclick={() => onViewPreviousSession?.(item.previousSessionId)}
      >
        View previous session
      </Button>
    </div>
  {/if}
</article>
