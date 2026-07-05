<script lang="ts">
  import ActivitySegmentBlock from '$lib/components/chat-v2/ActivitySegmentBlock.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import ThinkingGroupBlock from '$lib/components/chat-v2/ThinkingGroupBlock.svelte';
  import ToolCallGroupBlock from '$lib/components/chat-v2/ToolCallGroupBlock.svelte';
  import { toRenderItem } from '$lib/chat-v2/render-adapter';
  import { prepareTimelineRows } from '$lib/chat-v2/tool-groups';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import type { ToolCallTimelineItem } from '$lib/chat';
  import type { TimelineItem as ChatV2TimelineItem, TurnCycleState } from '$lib/chat-v2/types';
  import type { Agent, UserPreferences } from '$lib/types/api';

  let {
    items,
    cycleStates = [],
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatchedIds = new Set<string>(),
    searchSelectedId = null,
    preferences = DEFAULT_USER_PREFERENCES,
    onViewSession
  } = $props<{
    items: ChatV2TimelineItem[];
    cycleStates?: TurnCycleState[];
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    preferences?: UserPreferences;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
  }>();

  // Tool-call lookup for tool-output-helper cards that reference an original
  // call by id (built from the converted leaf shape so callId/recoveryCallId
  // match what ToolCallBlock expects).
  const toolCallsByCallId = $derived.by(() => {
    const lookup = new Map<string, ToolCallTimelineItem>();
    for (const item of items) {
      if (item.kind !== 'tool_call') continue;
      const converted = toRenderItem(item);
      if (!converted || converted.kind !== 'tool_call') continue;
      lookup.set(converted.callId, converted);
      if (converted.recoveryCallId) {
        lookup.set(converted.recoveryCallId, converted);
      }
    }
    return lookup;
  });

  const rows = $derived(prepareTimelineRows(items, preferences, cycleStates));

  function itemRenderKey(item: ChatV2TimelineItem): string {
    return item.kind === 'message' ? `${item.kind}:${item.role}:${item.id}` : `${item.kind}:${item.id}`;
  }

  function rowRenderKey(row: ReturnType<typeof prepareTimelineRows>[number]): string {
    return row.kind === 'item' ? itemRenderKey(row.item) : row.id;
  }
</script>

{#each rows as row (rowRenderKey(row))}
  {#if row.kind === 'item'}
    {@const item = row.item}
    {@const searchMatched = searchMatchedIds.has(item.id)}
    <ChatV2TimelineItemRenderer
      {item}
      {agent}
      {compact}
      {searchQuery}
      {searchMatched}
      searchSelected={searchMatched && searchSelectedId === item.id}
      {toolCallsByCallId}
      {onViewSession}
    />
  {:else if row.kind === 'activity_segment'}
    <ActivitySegmentBlock
      {row}
      {agent}
      {compact}
      {searchQuery}
      {searchMatchedIds}
      {searchSelectedId}
      {toolCallsByCallId}
      {onViewSession}
    />
  {:else if row.kind === 'tool_group'}
    <ToolCallGroupBlock
      {row}
      {agent}
      {compact}
      {searchQuery}
      {searchMatchedIds}
      {searchSelectedId}
      {toolCallsByCallId}
      {onViewSession}
    />
  {:else}
    <ThinkingGroupBlock
      {row}
      {agent}
      {compact}
      {searchQuery}
      {searchMatchedIds}
      {searchSelectedId}
      {toolCallsByCallId}
      {onViewSession}
    />
  {/if}
{/each}
