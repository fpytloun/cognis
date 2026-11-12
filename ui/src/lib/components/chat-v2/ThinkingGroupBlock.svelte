<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ActivityLabel from './ActivityLabel.svelte';
  import { chatDisclosure } from '$lib/chat-v2/motion';
  import { getContext, untrack } from 'svelte';
  import { timelineDisclosureContext, TimelineDisclosureState } from '$lib/chat-v2/presentation-state';

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

  const disclosures = getContext<TimelineDisclosureState | undefined>(timelineDisclosureContext);
  const memberIds = $derived(row.items.map((item: ThinkingGroupRow['items'][number]) => item.id));
  const restored = untrack(() => disclosures?.read('thinking_group', memberIds));
  let instantDisclosure = $state(true);
  let expanded = $state(restored ?? untrack(() => row.defaultExpanded));
  let userChoice = $state(restored);
  let initializedGroupId = $state<string | null>(null);

  const selectedChildVisible = $derived.by(() =>
    searchSelectedId !== null && row.items.some((item: ThinkingGroupRow['items'][number]) => item.id === searchSelectedId)
  );

  $effect(() => {
    if (initializedGroupId !== row.id) {
      const remembered = untrack(() => disclosures?.read('thinking_group', memberIds));
      userChoice = remembered;
      expanded = remembered ?? row.defaultExpanded;
      if (remembered !== undefined) disclosures?.remember('thinking_group', memberIds, remembered);
      initializedGroupId = row.id;
    }
    if (userChoice !== undefined) disclosures?.remember('thinking_group', memberIds, userChoice);
    if (selectedChildVisible || row.defaultExpanded) {
      expanded = true;
    }
  });

  function toggleExpanded(): void {
    instantDisclosure = false;
    expanded = !expanded;
    userChoice = expanded;
    disclosures?.remember('thinking_group', memberIds, expanded);
  }

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
    onclick={toggleExpanded}
    class={`chat-state-handoff flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left text-sm hover:bg-surface-hover focus-visible:outline-accent ${row.summary.accentClass}`}
    data-motion-state={row.summary.status}
    aria-expanded={expanded}
  >
    <ActivityGroupIcon
      icon={row.summary.icon}
      class="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-70"
    />
    <span class="min-w-0 flex-1 truncate font-medium">
      <ActivityLabel label={row.summary.label} active={row.summary.status === 'running'} />
    </span>
    <span class="shrink-0 text-xs tabular-nums text-quiet">
      {row.summary.detailLabel}
    </span>
    {#if durationLabel}
      <span class="shrink-0 text-xs tabular-nums text-quiet">{durationLabel}</span>
    {/if}
    {#if statusLabel}
      <span class="shrink-0 text-xs text-danger">{statusLabel}</span>
    {/if}
    <ChevronDown
      class="chat-disclosure-chevron h-3.5 w-3.5 shrink-0 text-quiet {expanded ? 'rotate-180' : ''}"
    />
  </button>

  {#if expanded}
    <div transition:chatDisclosure={{ instant: instantDisclosure }}
      onoutrostart={(event) => { event.currentTarget.inert = true; }}
      onintrostart={(event) => { event.currentTarget.inert = false; }}
      class="mx-3 mb-2 space-y-2 overflow-hidden rounded-lg border border-control-border bg-inset/20 px-2 py-2">
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
