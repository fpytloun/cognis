<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { AgentCommentReply } from '$lib/feedback/agent-replies';
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import FeedbackWorkspace from '$lib/components/feedback/FeedbackWorkspace.svelte';
  import ChatFeedbackIntent from './ChatFeedbackIntent.svelte';
  import type { FeedbackSourceRef } from '$lib/feedback/types';
  import { chatTranscriptSource } from '$lib/feedback/chat-transcript-source';
  import TimelineItemRenderer from '$lib/components/timeline/TimelineItemRenderer.svelte';
  import { createMarkdownStreamer, renderMarkdown, type MarkdownStreamer } from '$lib/markdown';
  import { toRenderItem, type RenderMarkdownContent } from '$lib/chat-v2/render-adapter';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineItem as ChatV2TimelineItem, TimelineScope } from '$lib/chat-v2/types';
  import type { Agent, TaskDeliverableFreshness } from '$lib/types/api';
  import type { CommandV2Response } from '$lib/chat-v2/types';
  import TaskOutcomeLink from './TaskOutcomeLink.svelte';
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { taskOutcomeCard } from '$lib/components/ui/private-view-definitions';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatched = false,
    searchSelected = false,
    getToolCall = () => null,
    onViewSession,
    pinnedTransient = false,
    scope,
    providerRecovery,
    commentReplies = [],
    taskFreshness = null,
  } = $props<{
    item: ChatV2TimelineItem;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatched?: boolean;
    searchSelected?: boolean;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    pinnedTransient?: boolean;
    scope?: TimelineScope | undefined;
    providerRecovery?: { conversationId: string; turnId: string; onResponse: (response: CommandV2Response) => Promise<void> };
    commentReplies?: AgentCommentReply[];
    taskFreshness?: TaskDeliverableFreshness | null;
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
    return streamer.render(content);
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
  const renderItem = $derived.by(() => {
    const rendered = toRenderItem(item, renderMarkdownContent);
    return rendered?.kind === 'message' ? { ...rendered, commentReplies } : rendered;
  });
  const assistantFeedbackSource = $derived(chatTranscriptSource(item, scope));
  const feedbackSource = $derived.by((): FeedbackSourceRef | null => {
    if (item.kind === 'message') return assistantFeedbackSource;
    const event = item.source_refs.find((ref: import('$lib/chat-v2/types').SourceRef) => ref.event_type === 'tool_result') ?? item.source_refs[0];
    if (!item.stable || !event || !scope?.conversation_id || item.kind !== 'tool_call') return null;
    return { kind: 'transcript', conversation_id: scope.conversation_id,
      source_event: { ...event, event_id: event.event_id ?? undefined }, source_item_id: item.id,
      field_path: ['result'] };
  });
  const transcriptFeedback = $derived(Boolean(assistantFeedbackSource));
</script>

{#if item.kind === 'task'}
  <DeferredPrivateView definition={taskOutcomeCard} viewProps={{ item }} label="task outcome" />
{:else if item.kind === 'message' && item.role === 'system' && item.task_outcome}
  <TaskOutcomeLink task={item.task_outcome} handling />
{:else if renderItem && !(item.kind === 'message' && item.notice_resolved)}
  <FeedbackHost source={feedbackSource}>
  <FeedbackWorkspace>
  <ChatFeedbackIntent sourceKey={JSON.stringify(feedbackSource)} transcript={transcriptFeedback}>
  <div data-timeline-item-id={item.id}>
  <TimelineItemRenderer
    item={renderItem}
    {agent}
    {compact}
    {searchQuery}
    {searchMatched}
    {searchSelected}
    {getToolCall}
    {onViewSession}
    {pinnedTransient}
    {scope}
    {providerRecovery}
    {taskFreshness}
  />
  {#if (item.kind === 'message' || item.kind === 'assistant_deliverable') && item.task_outcome}
    <TaskOutcomeLink task={item.task_outcome} />
  {/if}
  </div>
  </ChatFeedbackIntent>
  </FeedbackWorkspace>
  </FeedbackHost>
{/if}
