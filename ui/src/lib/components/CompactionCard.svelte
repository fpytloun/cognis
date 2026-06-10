<script lang="ts">
  import type { CompactionTimelineItem } from '$lib/chat';
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
  const summaryText = $derived(item.summary || item.summaryPreview);
  const summaryHtml = $derived(summaryText ? renderMarkdown(summaryText) : '');
</script>

<article class="rounded-3xl border border-cyan-500/30 bg-cyan-500/10 px-4 py-4 text-cyan-100 shadow-card">
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] opacity-80">
        {item.status === 'running' ? 'Compacting session' : 'Session compacted'}
      </p>
      <p class="mt-1 text-sm leading-6 opacity-90">
        {#if item.status === 'running'}
          Reducing conversation history before the next response.
        {:else}
          {item.turnsCompacted} {item.turnsCompacted === 1 ? 'turn' : 'turns'} summarized
          <span class="opacity-60">({item.method})</span>
        {/if}
      </p>
      {#if usageLabel || item.usedTimeoutFallback || item.hardPressureExceeded}
        <p class="mt-1 text-xs leading-5 opacity-75">
          {#if usageLabel}
            Context reached {usageLabel}
          {/if}
          {#if item.hardPressureExceeded}
            {usageLabel ? ' · ' : ''}hard pressure exceeded
          {/if}
          {#if item.usedTimeoutFallback}
            {usageLabel || item.hardPressureExceeded ? ' · ' : ''}fallback used
          {/if}
        </p>
      {/if}
      <p class="mt-2 text-xs uppercase tracking-[0.2em] opacity-75" title={formatAbsoluteTime(item.timestamp)}>
        {formatRelativeTime(item.timestamp)}
      </p>
    </div>
    <div class="flex items-center gap-2">
      {#if item.status === 'running'}
        <LiveDots inline={true} size="sm" tone="sky" label="Compacting" />
      {:else}
        <span class="rounded-full border border-current/30 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em]">
          compacted
        </span>
      {/if}
    </div>
  </div>

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
