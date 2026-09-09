<script lang="ts">
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import CompactionCard from '$lib/components/CompactionCard.svelte';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import ThinkingBlock from '$lib/components/ThinkingBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import UserInteractionMessage from '$lib/components/UserInteractionMessage.svelte';
  import WorkflowComposedCard from '$lib/components/WorkflowComposedCard.svelte';
  import TimelineActivityNotice from '$lib/components/timeline/TimelineActivityNotice.svelte';
  import type { AssistantDeliverableTimelineItem, MessageTimelineItem, SystemMessageTimelineItem, TimelineItem, ToolCallTimelineItem, UserInteractionTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope } from '$lib/chat-v2/types';
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
    pinnedTransient = false,
    scope
  } = $props<{
    item: TimelineItem;
    agent?: Agent | null;
    compact?: boolean;
    searchQuery?: string;
    searchMatched?: boolean;
    searchSelected?: boolean;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    pinnedTransient?: boolean;
    scope?: TimelineScope | undefined;
  }>();

  // A message is "unsettled" (and thus animates on entry) when it is actively
  // streaming or is an optimistic client-local row. A canonical row painted on
  // history load is settled and must not replay the entry animation. The
  // optimistic->canonical user echo remounts under a new key, so this keeps the
  // canonical echo from re-flashing.
  const messageUnsettled = $derived(
    item.kind === 'message'
    && (item.streaming === true || (typeof item.id === 'string' && item.id.startsWith('local')))
  );
  const systemItem = $derived(
    item.kind === 'system_message' ? item as SystemMessageTimelineItem : null
  );
  let retryCountdownNowMs = $state(Date.now());
  const retryRemainingSeconds = $derived.by(() => {
    if (!systemItem?.retryAt) return null;
    const retryAtMs = Date.parse(systemItem.retryAt);
    if (!Number.isFinite(retryAtMs)) return null;
    return Math.max(0, Math.ceil((retryAtMs - retryCountdownNowMs) / 1000));
  });
  const retryNoticeActive = $derived(
    systemItem?.noticeKind === 'model_recovery'
    && systemItem.noticeScope === 'retry'
    && retryRemainingSeconds !== null
    && retryRemainingSeconds > 0
  );
  const retryAttemptLabel = $derived.by(() => {
    if (!systemItem || systemItem.attempt == null || systemItem.maxAttempts == null) return null;
    return `Attempt ${systemItem.attempt}/${systemItem.maxAttempts}`;
  });
  const retryCountdownLabel = $derived.by(() => {
    if (retryRemainingSeconds == null) return null;
    return retryRemainingSeconds > 0
      ? `Retrying in ${formatDuration(retryRemainingSeconds)}`
      : 'Retrying now';
  });
  const retrySummary = $derived.by(() => {
    const parts = [
      retryCountdownLabel,
      retryAttemptLabel,
      [systemItem?.providerId, systemItem?.model].filter(Boolean).join(' · ')
    ].filter(Boolean);
    return parts.join(' · ') || 'Retrying the model call.';
  });

  $effect(() => {
    if (systemItem?.noticeKind !== 'model_recovery' || systemItem.noticeScope !== 'retry' || !systemItem.retryAt) return;
    retryCountdownNowMs = Date.now();
    const timer = window.setInterval(() => {
      retryCountdownNowMs = Date.now();
    }, 1000);
    return () => window.clearInterval(timer);
  });

  function formatDuration(seconds: number): string {
    const bounded = Math.max(0, Math.ceil(seconds));
    if (bounded < 60) return `${bounded}s`;
    const minutes = Math.floor(bounded / 60);
    const remainingSeconds = bounded % 60;
    return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  }
</script>

{#if item.kind === 'message'}
  <div
    data-message-id={item.id}
    data-kind="message"
    data-role={item.role}
    data-streaming={item.streaming === true ? 'true' : 'false'}
    data-stable={messageUnsettled ? 'false' : 'true'}
    class={`flex min-w-0 ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}
  >
    <ChatMessage
      item={item as MessageTimelineItem}
      {agent}
      {compact}
      {searchQuery}
      searchActive={searchMatched}
      searchSelected={searchSelected}
      live={item.streaming === true}
    />
  </div>
{:else if item.kind === 'thinking'}
  <div data-kind="thinking" data-streaming={item.streaming === true ? 'true' : 'false'}>
    <ThinkingBlock {item} live={item.streaming === true} />
  </div>
{:else if item.kind === 'tool_call'}
  <div data-kind="tool_call" data-tool-status={item.status}>
    <ToolCallBlock {item} {getToolCall} {onViewSession} {scope} />
  </div>
{:else if item.kind === 'assistant_deliverable'}
  <div data-kind="assistant_deliverable" class="flex min-w-0 w-full max-w-full justify-start">
    <AssistantDeliverableBlock item={item as AssistantDeliverableTimelineItem} />
  </div>
{:else if item.kind === 'user_interaction'}
  <div data-kind="user_interaction" class="flex min-w-0 justify-end">
    <UserInteractionMessage item={item as UserInteractionTimelineItem} />
  </div>
{:else if item.kind === 'delegation'}
  <div>
    <DelegationCard {item} {onViewSession} />
  </div>
{:else if item.kind === 'workflow_composed'}
  <div>
    <WorkflowComposedCard {item} />
  </div>
{:else if item.kind === 'compaction'}
  <div>
    <CompactionCard {item} onViewPreviousSession={onViewSession} />
  </div>
{:else if item.kind === 'system_message'}
  {#if item.noticeKind === 'managed_takeover'}
    <div class="mx-auto max-w-xl rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-center text-xs text-sky-100 shadow-card">
      <p class="font-medium">{item.text}</p>
      {#if item.followUpConversationId}
        <a
          class="mt-2 inline-flex items-center justify-center rounded-full border border-sky-300/30 bg-slate-950/30 px-3 py-1 font-medium text-sky-100 transition hover:border-sky-200/60 hover:text-white"
          href={`/chat/${item.followUpConversationId}`}
        >
          Open follow-up conversation
        </a>
      {/if}
    </div>
  {:else if item.noticeKind === 'model_recovery' && item.noticeScope === 'retry'}
    {#if pinnedTransient || retryNoticeActive}
      <TimelineActivityNotice
        title="Connection interrupted"
        text={retrySummary}
        details={item.text}
        active={retryNoticeActive}
      />
    {/if}
  {:else if item.noticeKind === 'model_error'}
    <TimelineActivityNotice
      title="Model request failed"
      text={item.recoverable ? 'The request can be retried.' : 'The request did not complete.'}
      details={item.text}
      tone="error"
      code={item.reasonClass}
    />
  {:else if item.noticeKind === 'model_recovery'}
    <TimelineActivityNotice
      title={item.noticeScope === 'continuation' ? 'Continuing from saved work' : 'Model recovered'}
      text={item.text}
      details={item.text}
    />
  {:else if item.noticeKind === 'turn_initiated'}
    <TimelineActivityNotice title="Turn initiated" text={item.text} details={item.text} />
  {:else}
    <TimelineActivityNotice title="System notice" text={item.text} details={item.text} />
  {/if}
{:else}
  <TimelineActivityNotice
    title={item.title}
    text={item.description}
    details={item.details ?? item.description}
    tone={item.tone}
    actionRequired={item.actionRequired ?? false}
    code={item.code}
    active={item.status === 'running' || item.status === 'pending' || item.status === 'waiting' || (item.actionRequired ?? false)}
  />
{/if}
