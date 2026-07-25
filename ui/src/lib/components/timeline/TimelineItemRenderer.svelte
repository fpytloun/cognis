<script lang="ts">
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import CompactionCard from '$lib/components/CompactionCard.svelte';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import ThinkingBlock from '$lib/components/ThinkingBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import WorkflowComposedCard from '$lib/components/WorkflowComposedCard.svelte';
  import type { AssistantDeliverableTimelineItem, MessageTimelineItem, SystemMessageTimelineItem, TimelineItem, ToolCallTimelineItem } from '$lib/timeline-render-model';
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
    <div class="mx-auto max-w-2xl rounded-2xl border border-slate-700/80 bg-slate-900/80 px-4 py-3 text-xs text-slate-300 shadow-card">
      <div class="flex items-start gap-3">
        <span class={`mt-1 h-2 w-2 shrink-0 rounded-full ${retryNoticeActive ? 'animate-pulse bg-sky-300' : 'bg-slate-500'}`}></span>
        <div class="min-w-0 flex-1">
          <p class="whitespace-pre-line leading-5">{item.text}</p>
          <div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] not-italic">
            {#if retryCountdownLabel}
              <span class="rounded-full border border-sky-400/30 bg-sky-500/10 px-2 py-0.5 font-medium text-sky-100">
                {retryCountdownLabel}
              </span>
            {/if}
            {#if retryAttemptLabel}
              <span class="rounded-full border border-slate-600 bg-slate-800 px-2 py-0.5 text-slate-300">
                {retryAttemptLabel}
              </span>
            {/if}
            {#if item.providerId || item.model}
              <span class="rounded-full border border-slate-700 bg-slate-950/40 px-2 py-0.5 text-slate-400">
                {[item.providerId, item.model].filter(Boolean).join(' · ')}
              </span>
            {/if}
          </div>
        </div>
      </div>
    </div>
  {:else}
    <p class="py-1 text-center text-xs italic text-slate-500 whitespace-pre-line">{item.text}</p>
  {/if}
{:else}
  <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-sky-500/30 bg-sky-500/10 text-sky-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
    <h3 class="font-semibold">{item.title}</h3>
    {#if item.description}
      <p class="mt-2 leading-6">{item.description}</p>
    {/if}
  </article>
{/if}
