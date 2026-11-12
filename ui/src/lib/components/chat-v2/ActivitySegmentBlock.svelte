<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ActivityLabel from './ActivityLabel.svelte';
  import { chatDisclosure } from '$lib/chat-v2/motion';
  import { getContext, untrack } from 'svelte';
  import { timelineDisclosureContext, timelineRowItemIds, TimelineDisclosureState } from '$lib/chat-v2/presentation-state';

  import ActivityGroupIcon from '$lib/components/chat-v2/ActivityGroupIcon.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import type { ToolCallTimelineItem as RenderToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';
  import type { ActivitySegmentEntry, ActivitySegmentRow, ToolGroupRow } from '$lib/chat-v2/tool-groups';
  import type { Agent, AttachmentRef } from '$lib/types/api';

  let {
    row,
    active = row.summary.status === 'running',
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
    active?: boolean;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    getToolCall?: (callId: string) => RenderToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  const disclosures = getContext<TimelineDisclosureState | undefined>(timelineDisclosureContext);
  const memberIds = $derived(timelineRowItemIds(row));
  const restored = untrack(() => disclosures?.read('activity_segment', memberIds));
  let instantDisclosure = $state(true);
  let expanded = $state(restored ?? untrack(() => row.defaultExpanded));
  let initializedGroupId = $state<string | null>(null);
  // Tracks whether the user has manually toggled this segment. Until they do,
  // expansion follows `row.defaultExpanded` — expanded while the assistant text
  // is live (so streamed content stays visible through the fold), then collapsed
  // when it completes. A manual toggle pins the state and stops auto-follow, so
  // the tidy-up never overrides a deliberate user choice.
  let userToggled = $state(restored !== undefined);
  let lastDefaultExpanded = $state<boolean | null>(null);
  let liveNow = $state(Date.now());

  function toggleExpanded(): void {
    instantDisclosure = false;
    expanded = !expanded;
    userToggled = true;
    disclosures?.remember('activity_segment', memberIds, expanded);
  }

  function groupItems(group: ToolGroupRow): ToolCallTimelineItem[] {
    return group.items;
  }

  function entryMessageMatchesSelection(entry: ActivitySegmentEntry): boolean {
    return entry.kind !== 'tool_group' && entry.item.id === searchSelectedId;
  }

  function toolMatchesSelection(item: ToolCallTimelineItem): boolean {
    return item.id === searchSelectedId;
  }

  const tools = $derived(row.toolGroups.flatMap(groupItems));
  const assistantAttachments = $derived([...new Map<string, AttachmentRef>(
    row.entries.flatMap((entry: ActivitySegmentEntry) => entry.kind === 'assistant' ? entry.item.attachments : [])
      .map((attachment: AttachmentRef) => [attachment.artifact_id, attachment] as const)
  ).values()]);
  const selectedChildVisible = $derived.by(() =>
      searchSelectedId !== null
      && (
       row.entries.some(entryMessageMatchesSelection)
      || tools.some(toolMatchesSelection)
    )
  );

  $effect(() => {
    if (initializedGroupId !== row.id) {
      const remembered = untrack(() => disclosures?.read('activity_segment', memberIds));
      expanded = remembered ?? row.defaultExpanded;
      if (remembered !== undefined) disclosures?.remember('activity_segment', memberIds, remembered);
      initializedGroupId = row.id;
      userToggled = remembered !== undefined;
      lastDefaultExpanded = row.defaultExpanded;
    } else if (!userToggled && row.defaultExpanded !== lastDefaultExpanded) {
      // Auto-follow defaultExpanded transitions while the user hasn't taken
      // control. This is what collapses a segment when its live assistant text
      // completes (defaultExpanded true -> false) even though the segment id is
      // now stable across that transition.
      instantDisclosure = false;
      expanded = row.defaultExpanded;
      lastDefaultExpanded = row.defaultExpanded;
    }
    if (userToggled) disclosures?.remember('activity_segment', memberIds, expanded);
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
    class={`chat-state-handoff flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm hover:bg-surface-hover focus-visible:outline-accent ${row.summary.accentClass}`}
    data-motion-state={row.summary.status}
    aria-expanded={expanded}
  >
    <ActivityGroupIcon
      icon={row.summary.icon}
      class="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-75"
    />
    <span class="min-w-0 flex-1">
      <span class="block truncate font-medium">
        <ActivityLabel label={row.summary.label} {active} />
      </span>
      {#if !expanded && row.assistantPreview}
        <span class="mt-0.5 block truncate text-xs font-normal text-muted">
          {row.assistantPreview}
        </span>
      {/if}
    </span>
    <span class="shrink-0 text-xs tabular-nums text-quiet">
      {row.summary.detailLabel}
    </span>
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-quiet">{durationLabel}</span>
    {/if}
    <ChevronDown
      class="chat-disclosure-chevron h-3.5 w-3.5 shrink-0 text-quiet {expanded ? 'rotate-180' : ''}"
    />
  </button>

  {#if !expanded && assistantAttachments.length}
    <div class="mx-3 mb-2" data-testid="collapsed-assistant-attachments">
      <MessageAttachments attachments={assistantAttachments} />
    </div>
  {/if}

  {#if expanded}
    <div transition:chatDisclosure={{ instant: instantDisclosure }}
      onoutrostart={(event) => { event.currentTarget.inert = true; }}
      onintrostart={(event) => { event.currentTarget.inert = false; }}
      class="mx-3 mb-2 space-y-2 overflow-hidden rounded-lg border border-control-border bg-inset/20 px-2 py-2">
      {#each row.entries as entry (entry.kind === 'tool_group' ? entry.group.id : `message:${entry.kind}:${entry.item.id}`)}
        {#if entry.kind !== 'tool_group'}
          {@const assistantSearchMatched = searchMatchedIds.has(entry.item.id)}
          <div
            data-kind={entry.kind === 'assistant' ? 'activity_segment_assistant' : 'activity_segment_recovery_notice'}
            data-message-id={entry.item.id}
          >
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
