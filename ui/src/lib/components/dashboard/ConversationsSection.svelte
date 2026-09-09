<script lang="ts">
  import { goto } from '$app/navigation';
  import ExternalLink from 'lucide-svelte/icons/external-link';

  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import AgentAvatarStrip from '$lib/components/dashboard/AgentAvatarStrip.svelte';
  import WorkstreamExecutionStatus from '$lib/components/WorkstreamExecutionStatus.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { isAttentionActionQuickAction } from '$lib/attention/actions';
  import {
    dashboardConversationWaitingReason,
    partitionConversationLanes
  } from '$lib/dashboard/dashboard';
  import { formatRelativeTime } from '$lib/time';
  import { conversationActivityState } from '$lib/conversation-activity';
  import type { Agent, AttentionActionSummary, Conversation } from '$lib/types/api';

  const EVIDENCE_LIMIT = 300;

  let {
    conversations,
    agents,
    loading = false,
    error = null,
    agentsLoading = false,
    agentsError = null,
    openConversationId = null,
    openConversationIds = [],
    onOpenConversation,
    onOpenAttention,
    onSelectAgent = () => undefined,
    onNewChat,
    onRetry = () => undefined,
    onAgentsRetry = () => undefined,
    recentHasMore = false,
    recentLoading = false,
    recentError = null,
    onLoadMoreRecent = () => undefined,
  } = $props<{
    conversations: Conversation[] | null;
    agents: Agent[];
    loading?: boolean;
    error?: string | null;
    agentsLoading?: boolean;
    agentsError?: string | null;
    openConversationId?: string | null;
    openConversationIds?: string[];
    onOpenConversation: (conversationId: string) => void;
    onOpenAttention?: (action: AttentionActionSummary, origin: HTMLElement) => void;
    onSelectAgent?: (agent: Agent) => void;
    onNewChat: () => void;
    onRetry?: () => void;
    onAgentsRetry?: () => void;
    recentHasMore?: boolean;
    recentLoading?: boolean;
    recentError?: string | null;
    onLoadMoreRecent?: () => void;
  }>();

  const lanes = $derived(partitionConversationLanes(conversations ?? [], {
    recent: Number.MAX_SAFE_INTEGER,
  }));

  function agentFor(agentId: string): Agent | undefined {
    return agents.find((agent: Agent) => agent.agent_id === agentId);
  }

  function openRow(conversationId: string): void {
    onOpenConversation(conversationId);
  }

  function handleRowKeydown(event: KeyboardEvent, conversationId: string): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    openRow(conversationId);
  }

  function openLink(event: MouseEvent, conversationId: string): void {
    event.stopPropagation();
    void goto(`/chat/${conversationId}`);
  }

  function stopRowKeyboardActivation(event: KeyboardEvent): void {
    event.stopPropagation();
  }

  function openAttention(event: MouseEvent, action: AttentionActionSummary): void {
    event.stopPropagation();
    onOpenAttention?.(action, event.currentTarget as HTMLElement);
  }

  function executionLabel(conversation: Conversation): string {
    if (conversation.managed_agent?.turn_state) return conversation.managed_agent.turn_state;
    if (conversation.has_active_turn) return conversation.active_session_status ?? 'Active';
    return 'Idle';
  }

  function avatarState(conversation: Conversation) {
    return conversationActivityState(conversation, {
      open: conversation.conversation_id === openConversationId
        || openConversationIds.includes(conversation.conversation_id),
    });
  }

  function evidence(conversation: Conversation): string | null {
    const summary = conversation.managed_agent?.last_result_summary?.trim();
    if (!summary) return null;
    return summary.length <= EVIDENCE_LIMIT
      ? summary
      : `${summary.slice(0, EVIDENCE_LIMIT - 1).trimEnd()}…`;
  }

  function observeRecentSentinel(node: HTMLElement): { destroy: () => void } {
    const root = node.closest('[data-testid="dashboard-conversations-recent-list"]');
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) onLoadMoreRecent();
    }, { root, rootMargin: '160px 0px' });
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }
</script>

<div class="min-w-0 md:flex md:h-full md:min-h-0 md:flex-col" data-testid="dashboard-conversations-section">
<Card class="min-w-0 p-4 md:flex md:h-full md:min-h-0 md:flex-col md:overflow-hidden">
  <div class="mb-4 shrink-0" data-testid="dashboard-conversations-agents">
    {#if agentsError}
      <p class="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100" role="alert">
        Could not load agents: {agentsError}
        <Button class="ml-2" size="sm" variant="secondary" onclick={onAgentsRetry}>Try again</Button>
      </p>
    {:else if agentsLoading && agents.length === 0}
      <p class="text-xs text-slate-500">Loading agents…</p>
    {:else}
      <AgentAvatarStrip {agents} {onSelectAgent} />
    {/if}
  </div>
  <div class="flex min-w-0 shrink-0 items-center justify-between gap-3">
    <h2 class="text-sm font-semibold uppercase tracking-[0.2em] text-slate-300">Conversations</h2>
    <div class="flex items-center gap-2">
      <Button size="sm" variant="secondary" data-testid="dashboard-conversations-new" onclick={onNewChat}>New chat</Button>
      <Button size="sm" variant="ghost" data-testid="dashboard-conversations-see-all" onclick={() => goto('/chat')}>See all</Button>
    </div>
  </div>

  {#if error}
    <p class="mt-3 shrink-0 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100" role="alert" data-testid="dashboard-conversations-error">
      Could not load conversations: {error}
      <Button class="ml-2" size="sm" variant="secondary" onclick={onRetry}>Try again</Button>
    </p>
  {:else if loading && !conversations}
    <p class="mt-3 shrink-0 text-xs text-slate-500">Loading conversations…</p>
  {:else}
    <div class="mt-4 space-y-5 md:flex md:min-h-0 md:flex-1 md:flex-col md:space-y-0 md:gap-2 md:overflow-hidden">
      <section class="md:flex md:min-h-16 md:flex-col" data-testid="dashboard-conversations-active">
        <p class="shrink-0 text-xs font-medium uppercase tracking-widest text-sky-300">Active now</p>
        {#if lanes.active.length === 0}
          <p class="mt-2 shrink-0 text-xs text-slate-500">No active conversations.</p>
        {:else}
          <ul
            class="mt-2 min-h-0 space-y-1.5 md:min-h-8 md:max-h-56 md:overflow-y-auto"
            data-testid="dashboard-conversations-active-list"
            aria-label="Active conversations"
          >
            {#each lanes.active as conversation (conversation.conversation_id)}
              {@const conversationAgent = agentFor(conversation.agent_id)}
              <li>
                <div
                  role="button"
                  tabindex="0"
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/40 px-3 py-2 text-left hover:border-sky-500/40"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                  <ActivityAvatar
                    name={conversationAgent?.display_name ?? conversation.agent_id}
                    avatarUrl={conversationAgent?.avatar_url ?? null}
                    state={avatarState(conversation)}
                    class="h-8 w-8"
                  />
                   <div class="min-w-0 flex-1">
                    <div class="flex min-w-0 items-center gap-2">
                      <p class="min-w-0 flex-1 truncate text-sm text-white">{conversation.title ?? 'Untitled conversation'}</p>
                       <WorkstreamExecutionStatus label={executionLabel(conversation)} active />
                    </div>
                    <p class="truncate text-xs text-slate-400">
                      {formatRelativeTime(conversation.last_message_at)}
                      {#if evidence(conversation)} · {evidence(conversation)}{/if}
                    </p>
                  </div>
                  {#each conversation.attention_actions ?? [] as action (action.action_id)}
                    {#if isAttentionActionQuickAction(action)}
                      <button
                        type="button"
                        aria-label={`${action.title} for ${conversation.title ?? 'conversation'}`}
                        class="shrink-0 rounded-lg border border-amber-400/40 bg-amber-400/10 px-2 py-1.5 text-xs font-medium text-amber-100 hover:bg-amber-400/20"
                        data-testid={`dashboard-attention-${action.action_id}`}
                        onclick={(event) => openAttention(event, action)}
                        onkeydown={stopRowKeyboardActivation}
                      >{action.title}</button>
                    {/if}
                  {/each}
                  <button
                    type="button"
                    aria-label="Open conversation in full page"
                    class="shrink-0 rounded-lg p-1.5 text-slate-500 hover:bg-slate-800 hover:text-white"
                    data-testid={`dashboard-conversation-row-link-${conversation.conversation_id}`}
                    onclick={(event) => openLink(event, conversation.conversation_id)}
                    onkeydown={stopRowKeyboardActivation}
                  >
                    <ExternalLink class="h-3.5 w-3.5" />
                  </button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      {#if lanes.waiting.length > 0}
        <section class="md:flex md:min-h-16 md:flex-col" data-testid="dashboard-conversations-waiting">
          <p class="shrink-0 text-xs font-medium uppercase tracking-widest text-amber-300">Waiting for you</p>
          <ul
            class="mt-2 min-h-0 space-y-1.5 md:min-h-8 md:max-h-56 md:overflow-y-auto"
            data-testid="dashboard-conversations-waiting-list"
            aria-label="Conversations waiting for you"
          >
            {#each lanes.waiting as conversation (conversation.conversation_id)}
              {@const conversationAgent = agentFor(conversation.agent_id)}
              {@const reason = dashboardConversationWaitingReason(conversation)}
              <li>
                <div
                  role="button"
                  tabindex="0"
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-left hover:border-amber-400/50"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                   <ActivityAvatar
                     name={conversationAgent?.display_name ?? conversation.agent_id}
                     avatarUrl={conversationAgent?.avatar_url ?? null}
                     state={avatarState(conversation)}
                     class="h-8 w-8"
                   />
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-medium text-white">{conversation.title ?? 'Untitled conversation'}</p>
                    <div class="mt-0.5 flex min-w-0 items-center gap-2 text-xs">
                      <WorkstreamExecutionStatus label="Pending" />
                      <span class="min-w-0 flex-1 truncate text-amber-200" data-testid={`dashboard-conversation-waiting-reason-${conversation.conversation_id}`}>{reason}</span>
                      <span class="shrink-0 text-slate-500">{formatRelativeTime(conversation.last_message_at)}</span>
                    </div>
                   </div>
                  {#each conversation.attention_actions ?? [] as action (action.action_id)}
                    {#if isAttentionActionQuickAction(action)}
                      <button
                        type="button"
                        aria-label={`${action.title} for ${conversation.title ?? 'conversation'}`}
                        class="shrink-0 rounded-lg border border-amber-400/40 bg-amber-400/10 px-2 py-1.5 text-xs font-medium text-amber-100 hover:bg-amber-400/20"
                        data-testid={`dashboard-attention-${action.action_id}`}
                        onclick={(event) => openAttention(event, action)}
                        onkeydown={stopRowKeyboardActivation}
                      >{action.title}</button>
                    {/if}
                  {/each}
                  <button
                    type="button"
                    aria-label="Open conversation in full page"
                    class="shrink-0 rounded-lg p-1.5 text-slate-500 hover:bg-slate-800 hover:text-white"
                    data-testid={`dashboard-conversation-row-link-${conversation.conversation_id}`}
                    onclick={(event) => openLink(event, conversation.conversation_id)}
                    onkeydown={stopRowKeyboardActivation}
                  >
                    <ExternalLink class="h-3.5 w-3.5" />
                  </button>
                </div>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <section class="flex min-h-0 flex-col md:min-h-20 md:flex-1" data-testid="dashboard-conversations-recent">
        <p class="shrink-0 text-xs font-medium uppercase tracking-widest text-slate-400">Recent</p>
        {#if lanes.recent.length === 0}
          <p class="mt-2 shrink-0 text-xs text-slate-500">No recent conversations.</p>
        {:else}
          <ul
            class="mt-2 min-h-0 space-y-1.5 overflow-y-auto md:flex-1"
            data-testid="dashboard-conversations-recent-list"
            aria-label="Recent conversations"
          >
            {#each lanes.recent as conversation (conversation.conversation_id)}
              {@const conversationAgent = agentFor(conversation.agent_id)}
              <li>
                <div
                  role="button"
                  tabindex="0"
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/40 px-3 py-2 text-left hover:border-sky-500/40"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                   <ActivityAvatar
                     name={conversationAgent?.display_name ?? conversation.agent_id}
                     avatarUrl={conversationAgent?.avatar_url ?? null}
                     state={avatarState(conversation)}
                     class="h-8 w-8"
                   />
                  <div class="min-w-0 flex-1">
                     <p class="truncate text-sm text-white">{conversation.title ?? 'Untitled conversation'}</p>
                    <p class="truncate text-xs text-slate-400">
                      {formatRelativeTime(conversation.last_message_at)}
                      {#if evidence(conversation)} · {evidence(conversation)}{/if}
                    </p>
                  </div>
                  <button
                    type="button"
                    aria-label="Open conversation in full page"
                    class="shrink-0 rounded-lg p-1.5 text-slate-500 hover:bg-slate-800 hover:text-white"
                    data-testid={`dashboard-conversation-row-link-${conversation.conversation_id}`}
                    onclick={(event) => openLink(event, conversation.conversation_id)}
                    onkeydown={stopRowKeyboardActivation}
                  >
                    <ExternalLink class="h-3.5 w-3.5" />
                  </button>
                </div>
              </li>
            {/each}
            {#if recentHasMore}
              <li use:observeRecentSentinel aria-hidden="true" data-testid="dashboard-conversations-recent-sentinel" class="h-px"></li>
            {/if}
            {#if recentLoading}<li class="py-2 text-center text-xs text-slate-500">Loading…</li>{/if}
            {#if recentError}<li class="py-2 text-xs text-amber-200" role="alert">{recentError}</li>{/if}
          </ul>
        {/if}
        {#if recentHasMore && lanes.recent.length === 0}
          <div use:observeRecentSentinel aria-hidden="true" data-testid="dashboard-conversations-recent-sentinel" class="h-px"></div>
        {/if}
      </section>
    </div>
  {/if}
</Card>
</div>
