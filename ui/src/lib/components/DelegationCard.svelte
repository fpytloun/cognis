<script lang="ts">
  import type { DelegationTimelineItem } from '$lib/chat';

  let { item } = $props<{ item: DelegationTimelineItem }>();

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
    </div>
    <span class="rounded-full border border-current/30 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em]">
      {item.status}
    </span>
  </div>

  {#if item.result}
    <p class="mt-3 text-sm leading-6 opacity-90">{item.result}</p>
  {/if}
</article>
