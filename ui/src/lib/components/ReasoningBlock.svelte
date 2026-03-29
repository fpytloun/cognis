<script lang="ts">
  import type { ReasoningTimelineItem } from '$lib/chat';

  let { item } = $props<{ item: ReasoningTimelineItem }>();

  let userToggled = $state(false);
  let expanded = $derived(userToggled || item.streaming);

  function toggle(): void {
    userToggled = !expanded;
  }
</script>

<article class="rounded-2xl border border-slate-800/60 bg-slate-900/50 text-sm shadow-card">
  <button
    class="flex w-full items-center gap-2 px-4 py-2.5 text-left transition hover:bg-slate-800/30"
    onclick={toggle}
    type="button"
  >
    <span class="text-xs text-slate-500">{expanded ? '\u25BC' : '\u25B6'}</span>
    <span class="text-xs font-medium italic text-slate-400">
      {#if item.streaming}
        Thinking
        <span class="inline-flex items-center gap-0.5 pl-1">
          <span class="h-1 w-1 animate-pulse rounded-full bg-slate-400"></span>
          <span class="h-1 w-1 animate-pulse rounded-full bg-slate-400 [animation-delay:120ms]"></span>
          <span class="h-1 w-1 animate-pulse rounded-full bg-slate-400 [animation-delay:240ms]"></span>
        </span>
      {:else}
        Thinking...
      {/if}
    </span>
  </button>

  {#if expanded}
    <div class="max-h-[30vh] overflow-y-auto border-t border-slate-800/40 px-4 py-3">
      <p class="whitespace-pre-wrap text-xs italic leading-5 text-slate-400">{item.content}</p>
    </div>
  {/if}
</article>
