<script lang="ts">
  import TimelineItemRenderer from '$lib/components/timeline/TimelineItemRenderer.svelte';
  import type { TimelineItem, ToolCallTimelineItem } from '$lib/chat';
  import type { Agent } from '$lib/types/api';

  let {
    items,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatchedIds = new Set<string>(),
    searchSelectedId = null,
    onViewSession
  } = $props<{
    items: TimelineItem[];
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
  }>();

  const toolCallsByCallId = $derived.by(() => {
    const lookup = new Map<string, ToolCallTimelineItem>();
    for (const timelineItem of items) {
      if (timelineItem.kind !== 'tool_call') continue;
      lookup.set(timelineItem.callId, timelineItem);
      if (timelineItem.recoveryCallId) {
        lookup.set(timelineItem.recoveryCallId, timelineItem);
      }
    }
    return lookup;
  });
</script>

{#each items as item (item.id)}
  {@const searchMatched = searchMatchedIds.has(item.id)}
  <TimelineItemRenderer
    {item}
    {agent}
    {compact}
    {searchQuery}
    {searchMatched}
    searchSelected={searchMatched && searchSelectedId === item.id}
    {toolCallsByCallId}
    {onViewSession}
  />
{/each}
