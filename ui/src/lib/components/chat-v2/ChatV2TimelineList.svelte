<script lang="ts">
  import ActivitySegmentBlock from '$lib/components/chat-v2/ActivitySegmentBlock.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import ThinkingGroupBlock from '$lib/components/chat-v2/ThinkingGroupBlock.svelte';
  import ToolCallGroupBlock from '$lib/components/chat-v2/ToolCallGroupBlock.svelte';
  import { prepareTimelineRows } from '$lib/chat-v2/tool-groups';
  import { toRenderItem } from '$lib/chat-v2/render-adapter';
  import { selectRenderableTimeline } from '$lib/chat-v2/selectors';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import type { ToolCallTimelineItem as RenderToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineItem as ChatV2TimelineItem, TimelineScope, TurnCycleState } from '$lib/chat-v2/types';
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
    onViewSession,
    scope
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
    scope?: TimelineScope | undefined;
  }>();

  // Stable canonical tool-call lookup for tool-output-helper cards that
  // reference an original call by id.
  function getToolCall(callId: string): RenderToolCallTimelineItem | null {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (!item) continue;
      if (item.kind !== 'tool_call') continue;
      if (item.call_id !== callId && item.recovery_call_id !== callId) continue;
      const renderItem = toRenderItem(item);
      return renderItem?.kind === 'tool_call' ? renderItem : null;
    }
    return null;
  }

  const renderableItems = $derived(selectRenderableTimeline(items));
  const rows = $derived(prepareTimelineRows(renderableItems, preferences, cycleStates));

  function itemRenderKey(item: ChatV2TimelineItem): string {
    return item.kind === 'message' ? `${item.kind}:${item.role}:${item.id}` : `${item.kind}:${item.id}`;
  }

  function rowRenderKey(row: ReturnType<typeof prepareTimelineRows>[number]): string {
    return row.kind === 'item' ? itemRenderKey(row.item) : row.id;
  }
</script>

{#each rows as row (rowRenderKey(row))}
  <div class="mb-8 last:mb-0" data-timeline-row-key={rowRenderKey(row)}>
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
        {getToolCall}
        {onViewSession}
        {scope}
      />
    {:else if row.kind === 'activity_segment'}
      <ActivitySegmentBlock
        {row}
        {agent}
        {compact}
        {searchQuery}
        {searchMatchedIds}
        {searchSelectedId}
        {getToolCall}
        {onViewSession}
        {scope}
      />
    {:else if row.kind === 'tool_group'}
      <ToolCallGroupBlock
        {row}
        {agent}
        {compact}
        {searchQuery}
        {searchMatchedIds}
        {searchSelectedId}
        {getToolCall}
        {onViewSession}
        {scope}
      />
    {:else}
      <ThinkingGroupBlock
        {row}
        {agent}
        {compact}
        {searchQuery}
        {searchMatchedIds}
        {searchSelectedId}
        {getToolCall}
        {onViewSession}
        {scope}
      />
    {/if}
  </div>
{/each}
