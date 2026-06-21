<script lang="ts">
  import TimelineItemRenderer from '$lib/components/timeline/TimelineItemRenderer.svelte';
  import type { TimelineItem } from '$lib/chat';
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
    {onViewSession}
  />
{/each}
