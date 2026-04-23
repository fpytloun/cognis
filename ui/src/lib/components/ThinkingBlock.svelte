<script lang="ts">
  import type { ThinkingTimelineItem } from '$lib/chat';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import Brain from 'lucide-svelte/icons/brain';

  let { item, compact = false } = $props<{ item: ThinkingTimelineItem; compact?: boolean }>();

  let expanded = $state(false);

  function toggle(): void {
    expanded = !expanded;
  }

  function displayTitle(raw: string | null | undefined): string {
    const cleaned = String(raw ?? '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/[*_`~>#]+/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    return cleaned || 'Thinking';
  }

  /**
   * Derive the display title shown in the collapsed header.
   * While streaming: use the activeTitle (mutates as the model thinks).
   * After completion: use the title of the last block.
   */
  const headerTitle = $derived.by(() => {
    if (item.streaming && item.activeTitle) {
      return displayTitle(item.activeTitle);
    }
    if (item.blocks.length > 0) {
      return displayTitle(item.blocks[item.blocks.length - 1].title);
    }
    return 'Thinking';
  });
</script>

<div class="group relative my-0.5">
  <!-- Collapsed / header row -->
  <button
    type="button"
    onclick={toggle}
    class="flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm transition hover:bg-violet-500/5 focus:outline-none"
    aria-expanded={expanded}
  >
    <Brain
      class="mt-0.5 h-3.5 w-3.5 shrink-0 {item.streaming
        ? 'text-violet-400 animate-pulse'
        : 'text-violet-500/70'}"
    />
    <span class="min-w-0 flex-1 truncate font-medium text-violet-300/80">
      {#if item.streaming && item.blocks.length === 0}
        Thinking…
      {:else}
        Thinking: {headerTitle}
      {/if}
    </span>
    {#if item.streaming}
      <LiveDots inline={true} size="sm" tone="sky" label="Thinking" />
    {:else}
      {#if expanded}
        <ChevronUp class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {:else}
        <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {/if}
    {/if}
  </button>

  <!-- Expanded body -->
  {#if expanded || (compact && item.blocks.length > 0)}
    <div class="mx-3 mb-2 overflow-hidden rounded-lg border border-violet-500/20 bg-violet-950/20">
      {#each item.blocks as block, i (block.block_id)}
        {#if i > 0}
          <hr class="border-violet-500/10" />
        {/if}
        <div class="px-3 py-2.5">
          {#if item.blocks.length > 1 || block.title !== headerTitle}
            <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-violet-400/60">
              Thinking: {displayTitle(block.title)}
            </p>
          {/if}
          <!-- Markdown-rendered thinking content -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="prose prose-sm prose-invert max-w-none text-slate-300/80 prose-p:my-1 prose-ul:my-1 prose-li:my-0 prose-code:text-violet-200 prose-code:bg-violet-900/30"
          >
            {@html block.html}
            {#if !block.complete}
              <span class="inline-block h-3 w-0.5 animate-pulse bg-violet-400/60 align-middle"></span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>
