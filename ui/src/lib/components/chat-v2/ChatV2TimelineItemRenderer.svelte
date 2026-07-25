<script lang="ts">
  import { onDestroy } from 'svelte';
  import TimelineItemRenderer from '$lib/components/timeline/TimelineItemRenderer.svelte';
  import { createMarkdownStreamer, renderMarkdown, type MarkdownStreamer } from '$lib/markdown';
  import { toRenderItem, type RenderMarkdownContent } from '$lib/chat-v2/render-adapter';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineItem as ChatV2TimelineItem, TimelineScope } from '$lib/chat-v2/types';
  import type { Agent } from '$lib/types/api';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatched = false,
    searchSelected = false,
    getToolCall = () => null,
    onViewSession,
    scope
  } = $props<{
    item: ChatV2TimelineItem;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatched?: boolean;
    searchSelected?: boolean;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  const markdownStreamers = new Map<string, MarkdownStreamer>();
  let activeScopeKey = '';

  const renderMarkdownContent: RenderMarkdownContent = (content, itemKey, streaming) => {
    const key = `${scope?.key ?? 'unscoped'}:${itemKey}`;
    if (!streaming) {
      const streamer = markdownStreamers.get(key);
      markdownStreamers.delete(key);
      return streamer ? streamer.finalize(content) : renderMarkdown(content);
    }
    let streamer = markdownStreamers.get(key);
    if (!streamer) {
      streamer = createMarkdownStreamer();
      markdownStreamers.set(key, streamer);
    }
    return streamer.render(content) || (content ? renderMarkdown(content) : '');
  };

  $effect(() => {
    const scopeKey = scope?.key ?? '';
    if (activeScopeKey && activeScopeKey !== scopeKey) markdownStreamers.clear();
    activeScopeKey = scopeKey;
  });

  onDestroy(() => markdownStreamers.clear());

  // Per-item conversion to the leaf renderer shape. Pure, stateless, and
  // recomputed from the canonical Chat v2 item on every change — no merge
  // store, no reconcile, no identity churn.
  const renderItem = $derived(toRenderItem(item, renderMarkdownContent));
</script>

{#if renderItem}
  <TimelineItemRenderer
    item={renderItem}
    {agent}
    {compact}
    {searchQuery}
    {searchMatched}
    {searchSelected}
    {getToolCall}
    {onViewSession}
    {scope}
  />
{/if}
