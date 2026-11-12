<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ActivityLabel from './ActivityLabel.svelte';
  import { chatDisclosure } from '$lib/chat-v2/motion';
  import { getContext, untrack } from 'svelte';
  import { timelineDisclosureContext, TimelineDisclosureState } from '$lib/chat-v2/presentation-state';

  import ActivityGroupIcon from '$lib/components/chat-v2/ActivityGroupIcon.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import type { ToolGroupRow } from '$lib/chat-v2/tool-groups';
  import type { Agent } from '$lib/types/api';

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
    row: ToolGroupRow;
    active?: boolean;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  const disclosures = getContext<TimelineDisclosureState | undefined>(timelineDisclosureContext);
  const memberIds = $derived(row.items.map((item: ToolGroupRow['items'][number]) => item.id));
  const restored = untrack(() => disclosures?.read('tool_group', memberIds));
  let instantDisclosure = $state(true);
  let expanded = $state(restored ?? untrack(() => row.defaultExpanded));
  let userChoice = $state(restored);
  let initializedGroupId = $state<string | null>(null);
  let liveNow = $state(Date.now());

  const selectedChildVisible = $derived.by(() =>
    searchSelectedId !== null && row.items.some((item: ToolGroupRow['items'][number]) => item.id === searchSelectedId)
  );

  $effect(() => {
    if (initializedGroupId !== row.id) {
      const remembered = untrack(() => disclosures?.read('tool_group', memberIds));
      userChoice = remembered;
      expanded = remembered ?? row.defaultExpanded;
      if (remembered !== undefined) disclosures?.remember('tool_group', memberIds, remembered);
      initializedGroupId = row.id;
    }
    if (userChoice !== undefined) disclosures?.remember('tool_group', memberIds, userChoice);
    if (selectedChildVisible || (row.defaultExpanded && userChoice === undefined)) {
      expanded = true;
    }
  });

  function toggleExpanded(): void {
    instantDisclosure = false;
    expanded = !expanded;
    userChoice = expanded;
    disclosures?.remember('tool_group', memberIds, expanded);
  }

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
    const ms = row.summary.durationMs;
    return formatDurationMs(ms);
  });

</script>

<div class="group relative my-0.5" data-kind="tool_group" data-tool-group-kind={row.summary.kind} data-tool-group-status={row.summary.status}>
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
    <span class="tool-group-label min-w-0 flex-1 truncate font-medium">
      <ActivityLabel label={row.summary.label} {active} />
    </span>
    {#if row.summary.editStats && (row.summary.editStats.additions > 0 || row.summary.editStats.deletions > 0)}
      <span class="shrink-0 text-xs tabular-nums text-quiet">
        {row.summary.editStats.fileCount} {row.summary.editStats.fileCount === 1 ? 'file' : 'files'}
        <span class="text-quiet">(</span><span class="text-success">+{row.summary.editStats.additions}</span><span class="text-quiet">/</span><span class="text-danger">-{row.summary.editStats.deletions}</span><span class="text-quiet">)</span>
      </span>
    {:else}
      <span class="shrink-0 text-xs tabular-nums text-quiet">
        {row.summary.detailLabel}
      </span>
    {/if}
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-quiet">{durationLabel}</span>
    {/if}
    <ChevronDown
      class="chat-disclosure-chevron h-3.5 w-3.5 shrink-0 text-quiet {expanded ? 'rotate-180' : ''}"
    />
  </button>

  {#if expanded}
    <div transition:chatDisclosure={{ instant: instantDisclosure }}
      onoutrostart={(event) => { event.currentTarget.inert = true; }}
      onintrostart={(event) => { event.currentTarget.inert = false; }}
      class="tool-group-content mx-3 mb-2 space-y-2 overflow-hidden rounded-lg border border-separator/50 px-2 py-2">
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

<style>
  .tool-group-label { color: var(--tool-group-label-fg, inherit); }
  .tool-group-content {
    border-width: var(--tool-group-border, 1px);
    background: var(--tool-group-background, rgb(var(--color-inset) / .2));
  }
</style>
