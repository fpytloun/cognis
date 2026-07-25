<script lang="ts">
  import type { ThinkingTimelineItem } from '$lib/timeline-render-model';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import Brain from 'lucide-svelte/icons/brain';

  let { item, live = false } = $props<{ item: ThinkingTimelineItem; live?: boolean }>();

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

  function hasRenderedBody(block: ThinkingTimelineItem['blocks'][number]): boolean {
    return Boolean(block.html?.trim() || block.content?.trim());
  }

  function parseTime(value: string | null | undefined): number | null {
    if (!value) return null;
    const parsed = new Date(value).getTime();
    return Number.isNaN(parsed) ? null : parsed;
  }

  function formatDurationMs(durationMs: number | null | undefined): string {
    if (durationMs == null) return '';
    if (durationMs < 1000) return `${durationMs}ms`;
    if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)}s`;
    const totalSeconds = Math.floor(durationMs / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }

  function blockDurationMs(block: ThinkingTimelineItem['blocks'][number]): number | null {
    if (typeof block.durationMs === 'number') return block.durationMs;
    const start = parseTime(block.startedAt);
    if (start == null) return null;
    const end = parseTime(block.completedAt) ?? Date.now();
    return Math.max(0, end - start);
  }

  const durationLabel = $derived.by(() => {
    const durations: number[] = item.blocks
      .map((block: ThinkingTimelineItem['blocks'][number]) => blockDurationMs(block))
      .filter((value: number | null): value is number => typeof value === 'number');
    if (durations.length === 0) return '';
    return formatDurationMs(durations.reduce((total: number, value: number) => total + value, 0));
  });

  /**
   * Derive the display title shown in the collapsed header.
   * While streaming: use the activeTitle (mutates as the model thinks).
   * After completion: use the title of the last block.
   */
  const headerTitle = $derived.by(() => {
    if (item.streaming && live && item.activeTitle) {
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
    class="flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm transition hover:bg-cyan-500/5 focus:outline-none"
    aria-expanded={expanded}
  >
    <Brain
      class="mt-0.5 h-3.5 w-3.5 shrink-0 {item.streaming && live
        ? 'text-cyan-400 animate-pulse'
        : 'text-cyan-500/70'}"
    />
    <span class="min-w-0 flex-1 truncate font-medium text-cyan-300/80">
      {#if item.streaming && live && item.blocks.length === 0}
        Thinking…
      {:else}
        Thinking: {headerTitle}
      {/if}
    </span>
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-slate-500">{durationLabel}</span>
    {/if}
    {#if item.streaming && live}
      <LiveDots inline={true} size="sm" tone="sky" />
    {:else}
      {#if expanded}
        <ChevronUp class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {:else}
        <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {/if}
    {/if}
  </button>

  <!-- Expanded body -->
  {#if expanded}
    <div class="mx-3 mb-2 overflow-hidden rounded-lg border border-cyan-500/20 bg-cyan-950/20">
      {#each item.blocks as block, i (block.block_id)}
        {#if i > 0}
          <hr class="border-cyan-500/10" />
        {/if}
        <div class="px-3 py-2.5">
          {#if !(i === 0 && displayTitle(block.title) === headerTitle)}
            {@const duration = formatDurationMs(blockDurationMs(block))}
            <p class="mb-1.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-cyan-400/60">
              <span>Thinking: {displayTitle(block.title)}</span>
              {#if duration}
                <span class="normal-case tracking-normal text-slate-500">{duration}</span>
              {/if}
            </p>
          {/if}
          <!-- Markdown-rendered thinking content -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="prose prose-sm prose-invert max-w-none text-slate-300/80 prose-p:my-1 prose-ul:my-1 prose-li:my-0 prose-code:text-cyan-200 prose-code:bg-cyan-900/30"
          >
            {#if hasRenderedBody(block)}
              {@html block.html}
            {:else}
              <p class="my-1 text-sm text-slate-400/80">Thinking details unavailable.</p>
            {/if}
            {#if !block.complete}
              <span class="inline-block h-3 w-0.5 animate-pulse bg-cyan-400/60 align-middle"></span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>
