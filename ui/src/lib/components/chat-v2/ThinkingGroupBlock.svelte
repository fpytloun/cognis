<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';

  import ActivityGroupIcon from '$lib/components/chat-v2/ActivityGroupIcon.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import type { ThinkingGroupRow } from '$lib/chat-v2/tool-groups';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import type { Agent } from '$lib/types/api';

  let {
    row,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatchedIds = new Set<string>(),
    searchSelectedId = null,
    getToolCall = () => null,
    onViewSession,
    scope
  } = $props<{
    row: ThinkingGroupRow;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  let expanded = $state(false);
  let initializedGroupId = $state<string | null>(null);

  const selectedChildVisible = $derived.by(() =>
    searchSelectedId !== null && row.items.some((item: ThinkingGroupRow['items'][number]) => item.id === searchSelectedId)
  );

  $effect(() => {
    if (initializedGroupId !== row.id) {
      expanded = row.defaultExpanded;
      initializedGroupId = row.id;
    }
    if (selectedChildVisible || row.defaultExpanded) {
      expanded = true;
    }
  });

  const durationLabel = $derived.by(() => {
    const ms = row.summary.durationMs;
    if (ms === null) return '';
    if (ms < 1000) return `${Math.max(1, Math.round(ms))}ms`;
    return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  });

  const statusLabel = $derived.by(() => {
    if (row.summary.status === 'failed') return 'failed';
    return '';
  });
</script>

<div class="group relative my-0.5" data-kind="thinking_group" data-thinking-group-status={row.summary.status}>
  <button
    type="button"
    onclick={() => (expanded = !expanded)}
    class={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm transition hover:bg-slate-500/5 focus:outline-none ${row.summary.accentClass}`}
    aria-expanded={expanded}
  >
    <ActivityGroupIcon
      icon={row.summary.icon}
      class="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-70 {row.summary.status === 'running' ? 'animate-pulse' : ''}"
    />
    <span class="min-w-0 flex-1 truncate font-medium">
      {row.summary.label}
    </span>
    <span class="shrink-0 text-xs tabular-nums text-slate-500">
      {row.summary.detailLabel}
    </span>
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-slate-500">{durationLabel}</span>
    {/if}
    {#if statusLabel}
      <span class="shrink-0 text-xs text-slate-500">{statusLabel}</span>
    {/if}
    <ChevronDown
      class="h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform duration-150 ease-out {expanded ? 'rotate-180' : ''}"
    />
  </button>

  {#if expanded}
    <div class="mx-3 mb-2 space-y-2 overflow-hidden rounded-lg border border-slate-700/60 bg-slate-950/20 px-2 py-2">
      {#each row.items as item (item.id)}
        {@const searchMatched = searchMatchedIds.has(item.id)}
        <ChatV2TimelineItemRenderer
          {item}
          {agent}
          {compact}
          {searchQuery}
          {searchMatched}
          searchSelected={searchMatched && searchSelectedId === item.id}
          {getToolCall}
          {onViewSession}
          {scope}
        />
      {/each}
    </div>
  {/if}
</div>
