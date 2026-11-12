<script lang="ts">
  import ActivitySegmentBlock from '$lib/components/chat-v2/ActivitySegmentBlock.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import RecapCard from '$lib/components/chat-v2/RecapCard.svelte';
  import HandoffCard from '$lib/components/chat-v2/HandoffCard.svelte';
  import HookCard from '$lib/components/chat-v2/HookCard.svelte';
  import ThinkingGroupBlock from '$lib/components/chat-v2/ThinkingGroupBlock.svelte';
  import ToolCallGroupBlock from '$lib/components/chat-v2/ToolCallGroupBlock.svelte';
  import { prepareTimelineRows } from '$lib/chat-v2/tool-groups';
  import { activeActivityGroupId } from '$lib/chat-v2/activity-progress';
  import { TimelineDisclosureState, timelineDisclosureContext, timelineRowItemIds } from '$lib/chat-v2/presentation-state';
  import type { RuntimeOverlaySnapshot } from '$lib/chat-v2/types';
  import type { CommandV2Response } from '$lib/chat-v2/types';
  import { toRenderItem } from '$lib/chat-v2/render-adapter';
  import { selectRenderableTimeline } from '$lib/chat-v2/selectors';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import type { ToolCallTimelineItem as RenderToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineItem as ChatV2TimelineItem, TimelineScope, TurnCycleState, WorkCategory } from '$lib/chat-v2/types';
  import type { Agent, TaskDeliverableFreshness, UserPreferences } from '$lib/types/api';
  import { setContext, untrack } from 'svelte';
  import { chatArrivalContext, ChatArrivalRegistry } from '$lib/chat-v2/motion';
  import { commentReplyPresentation } from '$lib/feedback/agent-replies';

  let {
    items,
    cycleStates = [],
    runtime,
    agent = null,
    compact = false,
    searchQuery = '',
    searchMatchedIds = new Set<string>(),
    searchSelectedId = null,
    preferences = DEFAULT_USER_PREFERENCES,
    onViewSession,
    onOpenWork,
    onHandoffConversation,
    scope,
    onProviderSwitchResponse,
    deliverableFreshness,
  } = $props<{
    items: ChatV2TimelineItem[];
    deliverableFreshness?: Record<string, TaskDeliverableFreshness>;
    cycleStates?: TurnCycleState[];
    runtime?: RuntimeOverlaySnapshot | null;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatchedIds?: Set<string>;
    searchSelectedId?: string | null;
    preferences?: UserPreferences;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    onOpenWork?: ((category: WorkCategory, sessionId: string, path?: string, sourceItemId?: string) => void) | undefined;
    onHandoffConversation?: (item: import('$lib/chat-v2/types').HandoffTimelineItem) => void;
    scope?: TimelineScope | undefined;
    onProviderSwitchResponse?: (response: CommandV2Response) => Promise<void>;
  }>();

  // Initial/restored rows are a baseline, not new-message arrivals.
  const arrivals = new ChatArrivalRegistry(untrack(() => items.map((item: ChatV2TimelineItem) => item.id)));
  setContext(chatArrivalContext, arrivals);
  const disclosures = new TimelineDisclosureState(() => scope?.key ?? '');
  setContext(timelineDisclosureContext, disclosures);
  let arrivalConversation = untrack(() => scope?.conversation_id);
  $effect.pre(() => {
    if (scope?.conversation_id !== arrivalConversation) {
      arrivals.reset(items.map((item: ChatV2TimelineItem) => item.id));
      arrivalConversation = scope?.conversation_id;
    }
  });
  $effect(() => {
    // Folded history is already known. A genuinely new live message may mount
    // one effect later when its activity segment auto-expands; let that first
    // visible arrival claim its entry, then never replay it on subsequent folds.
    arrivals.remember(items.filter((item: ChatV2TimelineItem) => item.stable !== false).map((item: ChatV2TimelineItem) => item.id));
  });

  // Stable canonical tool-call lookup for tool-output-helper cards that
  // reference an original call by id.
  // Indexed once per items change instead of scanning the whole history for
  // every helper card render. Later items win, matching the former reverse scan.
  const toolCallIndex = $derived.by(() => {
    const index = new Map<string, ChatV2TimelineItem>();
    for (const item of items) {
      if (!item || item.kind !== 'tool_call') continue;
      if (item.call_id) index.set(item.call_id, item);
      if (item.recovery_call_id) index.set(item.recovery_call_id, item);
    }
    return index;
  });

  function getToolCall(callId: string): RenderToolCallTimelineItem | null {
    const item = toolCallIndex.get(callId);
    if (!item) return null;
    const renderItem = toRenderItem(item);
    return renderItem?.kind === 'tool_call' ? renderItem : null;
  }

  const renderableItems = $derived(selectRenderableTimeline(items));
  const commentReplies = $derived(commentReplyPresentation(renderableItems));
  const commentCompletionKey = $derived(commentReplies.completionKey);
  $effect(() => {
    // Tool completion is canonical on every connected client, unlike browser-local mutations.
    if (commentCompletionKey !== '[]') window.dispatchEvent(new Event('feedback-changed'));
  });
  const rows = $derived(prepareTimelineRows(renderableItems, preferences, cycleStates));
  const activeGroupId = $derived(activeActivityGroupId(rows, runtime));

  function itemRenderKey(item: ChatV2TimelineItem): string {
    return item.kind === 'message' ? `${item.kind}:${item.role}:${item.id}` : `${item.kind}:${item.id}`;
  }

  function rowRenderKey(row: ReturnType<typeof prepareTimelineRows>[number]): string {
    return row.kind === 'item' ? itemRenderKey(row.item) : row.id;
  }
</script>

{#each rows as row (`${scope?.key ?? ''}:${rowRenderKey(row)}`)}
  <!-- Rows always render at their real size. Do not defer off-screen rows
       with placeholder heights: they resolve while scrolling up and shift the
        content being read. Window changes preserve canonical item anchors. -->
  <div class="mb-8 last:mb-0" data-timeline-row-key={rowRenderKey(row)}
    data-timeline-item-ids={JSON.stringify(timelineRowItemIds(row))}>
    {#if row.kind === 'item'}
      {@const item = row.item}
      {@const searchMatched = searchMatchedIds.has(item.id)}
      {#if item.kind === 'recap'}
        <RecapCard {item} {onOpenWork} />
      {:else if item.kind === 'handoff'}
        <HandoffCard {item} onNewConversation={onHandoffConversation} />
      {:else if item.kind === 'notice' && item.hook}
        <HookCard {item} {onViewSession} />
      {:else}
        <ChatV2TimelineItemRenderer
        {item}
        taskFreshness={item.kind === 'assistant_deliverable' ? deliverableFreshness?.[item.deliverable_id] : null}
        commentReplies={commentReplies.byMessage.get(item.id)}
        {agent}
        {compact}
        {searchQuery}
        {searchMatched}
        searchSelected={searchMatched && searchSelectedId === item.id}
        {getToolCall}
        {onViewSession}
        {scope}
        providerRecovery={onProviderSwitchResponse && scope?.conversation_id && runtime?.runtime_authority &&
          (!runtime.has_active_turn || runtime.active_turn?.status === 'waiting') ? {
            conversationId: scope.conversation_id,
            turnId: runtime.runtime_authority.turn_id,
            onResponse: onProviderSwitchResponse
          } : undefined}
        />
      {/if}
    {:else if row.kind === 'activity_segment'}
      <ActivitySegmentBlock
        {row}
        active={runtime === undefined ? undefined : activeGroupId === row.id}
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
        active={runtime === undefined ? undefined : activeGroupId === row.id}
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
