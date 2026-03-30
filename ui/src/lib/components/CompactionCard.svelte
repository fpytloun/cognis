<script lang="ts">
  import type { CompactionTimelineItem } from '$lib/chat';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import Button from '$lib/components/ui/Button.svelte';

  let { item, onViewPreviousSession } = $props<{
    item: CompactionTimelineItem;
    onViewPreviousSession?: ((sessionId: string) => void) | undefined;
  }>();
</script>

<article class="rounded-3xl border border-violet-500/30 bg-violet-500/10 px-4 py-4 text-violet-100 shadow-card">
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] opacity-80">Session compacted</p>
      <p class="mt-1 text-sm leading-6 opacity-90">
        {item.turnsCompacted} {item.turnsCompacted === 1 ? 'turn' : 'turns'} summarized
        <span class="opacity-60">({item.method})</span>
      </p>
      <p class="mt-2 text-xs uppercase tracking-[0.2em] opacity-75" title={formatAbsoluteTime(item.timestamp)}>
        {formatRelativeTime(item.timestamp)}
      </p>
    </div>
    <div class="flex items-center gap-2">
      <span class="rounded-full border border-current/30 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em]">
        compacted
      </span>
    </div>
  </div>

  {#if item.summaryPreview}
    <p class="mt-3 text-sm leading-6 opacity-80 line-clamp-3">{item.summaryPreview}</p>
  {/if}

  {#if onViewPreviousSession && item.previousSessionId}
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
