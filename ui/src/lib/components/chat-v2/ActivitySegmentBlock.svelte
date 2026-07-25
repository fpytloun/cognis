<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';

  import ActivityGroupIcon from '$lib/components/chat-v2/ActivityGroupIcon.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import type { ToolCallTimelineItem as RenderToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';
  import type { ActivitySegmentEntry, ActivitySegmentRow, ToolGroupRow } from '$lib/chat-v2/tool-groups';
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
    row: ActivitySegmentRow;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    getToolCall?: (callId: string) => RenderToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  let expanded = $state(false);
  let initializedGroupId = $state<string | null>(null);
  // Tracks whether the user has manually toggled this segment. Until they do,
  // expansion follows `row.defaultExpanded` — expanded while the assistant text
  // is live (so streamed content stays visible through the fold), then collapsed
  // when it completes. A manual toggle pins the state and stops auto-follow, so
  // the tidy-up never overrides a deliberate user choice.
  let userToggled = $state(false);
  let lastDefaultExpanded = $state<boolean | null>(null);
  let liveNow = $state(Date.now());

  function toggleExpanded(): void {
    expanded = !expanded;
    userToggled = true;
  }

  function groupItems(group: ToolGroupRow): ToolCallTimelineItem[] {
    return group.items;
  }

  function entryAssistantMatchesSelection(entry: ActivitySegmentEntry): boolean {
    return entry.kind === 'assistant' && entry.item.id === searchSelectedId;
  }

  function toolMatchesSelection(item: ToolCallTimelineItem): boolean {
    return item.id === searchSelectedId;
  }

  const tools = $derived(row.toolGroups.flatMap(groupItems));
  const selectedChildVisible = $derived.by(() =>
      searchSelectedId !== null
      && (
      row.entries.some(entryAssistantMatchesSelection)
      || tools.some(toolMatchesSelection)
    )
  );

  $effect(() => {
    if (initializedGroupId !== row.id) {
      expanded = row.defaultExpanded;
      initializedGroupId = row.id;
      userToggled = false;
      lastDefaultExpanded = row.defaultExpanded;
    } else if (!userToggled && row.defaultExpanded !== lastDefaultExpanded) {
      // Auto-follow defaultExpanded transitions while the user hasn't taken
      // control. This is what collapses a segment when its live assistant text
      // completes (defaultExpanded true -> false) even though the segment id is
      // now stable across that transition.
      expanded = row.defaultExpanded;
      lastDefaultExpanded = row.defaultExpanded;
    }
    // A selected search hit always forces the segment open regardless of state.
    if (selectedChildVisible) {
      expanded = true;
    }
  });

  $effect(() => {
    if (row.summary.status !== 'running') return;
    liveNow = Date.now();
    const timer = window.setInterval(() => {
      liveNow = Date.now();
    }, 250);
    return () => window.clearInterval(timer);
  });

  function parseTime(value: string | null | undefined): number | null {
    if (!value) return null;
    const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
    const parsed = new Date(normalized).getTime();
    return Number.isNaN(parsed) ? null : parsed;
  }

  function formatDurationMs(durationMs: number | null | undefined): string {
    if (durationMs == null) return '';
    if (durationMs < 1000) return `${Math.max(1, Math.round(durationMs))}ms`;
    if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`;
    const totalSeconds = Math.floor(durationMs / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }

  const durationLabel = $derived.by(() => {
    if (row.summary.status === 'running') {
      const startedAt = parseTime(row.summary.startedAt);
      if (startedAt !== null) {
        return formatDurationMs(Math.max(row.summary.durationMs ?? 0, liveNow - startedAt));
      }
    }
    return formatDurationMs(row.summary.durationMs);
  });

  const displayLabel = $derived(row.summary.status === 'running' ? row.summary.label.replace(/…$/, '') : row.summary.label);
  const animatedDots = $derived(['.', '..', '...'][Math.floor(liveNow / 450) % 3]);

</script>

<div
  class="group relative my-0.5"
  data-kind="activity_segment"
  data-tool-group-kind={row.summary.kind}
  data-tool-group-status={row.summary.status}
>
  <button
    type="button"
    onclick={toggleExpanded}
    class={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm transition hover:bg-slate-500/5 focus:outline-none ${row.summary.accentClass}`}
    aria-expanded={expanded}
  >
    <ActivityGroupIcon
      icon={row.summary.icon}
      class="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-75 {row.summary.status === 'running' ? 'animate-pulse' : ''}"
    />
    <span class="min-w-0 flex-1">
      <span class="block truncate font-medium">
        {displayLabel}{#if row.summary.status === 'running'}<span class="inline-block min-w-[1.5em] text-left">{animatedDots}</span>{/if}
      </span>
      {#if !expanded && row.assistantPreview}
        <span class="mt-0.5 block truncate text-xs font-normal text-slate-400">
          {row.assistantPreview}
        </span>
      {/if}
    </span>
    <span class="shrink-0 text-xs tabular-nums text-slate-500">
      {row.summary.detailLabel}
    </span>
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-slate-500">{durationLabel}</span>
    {/if}
    <ChevronDown
      class="h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform duration-150 ease-out {expanded ? 'rotate-180' : ''}"
    />
  </button>

  {#if expanded}
    <div class="mx-3 mb-2 space-y-10 overflow-hidden rounded-lg border border-slate-700/60 bg-slate-950/20 px-2 py-2">
      {#each row.entries as entry (entry.kind === 'assistant' ? `message:assistant:${entry.item.id}` : entry.group.id)}
        {#if entry.kind === 'assistant'}
          {@const assistantSearchMatched = searchMatchedIds.has(entry.item.id)}
          <div data-kind="activity_segment_assistant" data-message-id={entry.item.id}>
            <ChatV2TimelineItemRenderer
              item={entry.item}
              {agent}
              {compact}
              {searchQuery}
              searchMatched={assistantSearchMatched}
              searchSelected={assistantSearchMatched && searchSelectedId === entry.item.id}
              {getToolCall}
              {onViewSession}
              {scope}
            />
          </div>
        {:else}
          {#each groupItems(entry.group) as item (item.id)}
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
        {/if}
      {/each}
    </div>
  {/if}
</div>
