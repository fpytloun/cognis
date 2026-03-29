<script lang="ts">
  import type { DelegationTimelineItem } from '$lib/chat';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import Button from '$lib/components/ui/Button.svelte';

  let { item, onViewSession } = $props<{
    item: DelegationTimelineItem;
    onViewSession?: ((taskId: string) => void) | undefined;
  }>();

  function toneClass(): string {
    return item.status === 'completed'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100'
      : item.status === 'failed' || item.status === 'cancelled'
        ? 'border-rose-500/40 bg-rose-500/10 text-rose-100'
        : 'border-sky-500/30 bg-sky-500/10 text-sky-100';
  }
</script>

<article class={`rounded-3xl border px-4 py-4 shadow-card ${toneClass()}`}>
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] opacity-80">Delegation</p>
      <h3 class="mt-1 text-base font-semibold">{item.taskLabel}</h3>
      <p class="mt-2 text-xs uppercase tracking-[0.2em] opacity-75" title={formatAbsoluteTime(item.timestamp)}>
        {formatRelativeTime(item.timestamp)}
      </p>
    </div>
    <div class="flex items-center gap-2">
      <span class="rounded-full border border-current/30 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em]">
        {item.status}
      </span>
    </div>
  </div>

  {#if item.result}
    <p class="mt-3 text-sm leading-6 opacity-90">{item.result}</p>
  {/if}

  <div class="mt-3 flex justify-end">
    <Button
      size="sm"
      variant="ghost"
      onclick={() => onViewSession?.(item.taskId)}
    >
      View session
    </Button>
  </div>
</article>
