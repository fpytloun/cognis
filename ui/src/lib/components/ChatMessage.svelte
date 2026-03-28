<script lang="ts">
  import type { MessageTimelineItem } from '$lib/chat';

  let { item } = $props<{ item: MessageTimelineItem }>();

  function bubbleClass(): string {
    return item.role === 'user'
      ? 'ml-auto bg-sky-500 text-slate-950'
      : item.role === 'assistant'
        ? 'bg-slate-900 text-slate-100 border border-slate-800'
        : 'bg-slate-800/70 text-slate-200 border border-slate-700';
  }
</script>

<article class={`max-w-[80%] rounded-3xl px-4 py-3 shadow-card ${bubbleClass()}`}>
  {#if item.role === 'assistant'}
    <div class="prose prose-invert max-w-none prose-pre:overflow-x-auto prose-code:text-sky-200">{@html item.html}</div>
  {:else}
    <p class="whitespace-pre-wrap text-sm leading-6">{item.content}</p>
  {/if}

  <div class="mt-3 flex items-center justify-between gap-4 text-[11px] uppercase tracking-[0.2em] opacity-70">
    <span>{item.role}</span>
    {#if item.streaming}
      <span>streaming</span>
    {/if}
  </div>
</article>
