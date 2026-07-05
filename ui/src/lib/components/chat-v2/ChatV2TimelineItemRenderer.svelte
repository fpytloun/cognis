<script lang="ts">
  import TimelineItemRenderer from '$lib/components/timeline/TimelineItemRenderer.svelte';
  import { toRenderItem } from '$lib/chat-v2/render-adapter';
  import type { ToolCallTimelineItem } from '$lib/chat';
  import type { TimelineItem as ChatV2TimelineItem } from '$lib/chat-v2/types';
  import type { Agent } from '$lib/types/api';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatched = false,
    searchSelected = false,
    toolCallsByCallId = new Map<string, ToolCallTimelineItem>(),
    onViewSession
  } = $props<{
    item: ChatV2TimelineItem;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatched?: boolean;
    searchSelected?: boolean;
    toolCallsByCallId?: Map<string, ToolCallTimelineItem>;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
  }>();

  // Per-item conversion to the leaf renderer shape. Pure, stateless, and
  // recomputed from the canonical Chat v2 item on every change — no merge
  // store, no reconcile, no identity churn.
  const renderItem = $derived(toRenderItem(item));
</script>

{#if renderItem}
  <TimelineItemRenderer
    item={renderItem}
    {agent}
    {compact}
    {searchQuery}
    {searchMatched}
    {searchSelected}
    {toolCallsByCallId}
    {onViewSession}
  />
{/if}
