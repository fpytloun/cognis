<script lang="ts">
  import { goto } from '$app/navigation';
  import '$lib/dashboard/bounded-sections.css';
  import DashboardRowIndicators from './DashboardRowIndicators.svelte';
  import DashboardRowTitle from './DashboardRowTitle.svelte';

  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import ManagedChannelBadge from '$lib/components/ManagedChannelBadge.svelte';
  import WaitingRefreshStatus from '$lib/components/WaitingRefreshStatus.svelte';
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
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import { wsClient } from '$lib/ws/client';
  import { dashboardRealtimeLoads } from '$lib/dashboard/realtime';
  import { RefreshCooldown, SidebarRefreshController } from '$lib/sidebar-refresh';
  import { backgroundWorkItemIsRunning } from '$lib/ongoing-work';
  import { isSidebarConversationWaiting } from '$lib/conversation-sidebar';
  import { isDashboardConversationActive, isDashboardTopicConversation } from '$lib/dashboard/dashboard';
  import {
    DASHBOARD_MOBILE_PAGE_SIZE,
    DASHBOARD_MOBILE_QUERY,
    canLoadMoreRecent,
    loadMoreRecent,
    visibleRecentItems,
  } from '$lib/dashboard/bounded-list';
  import type { Agent, AttentionActionSummary, BackgroundWorkItem, Conversation, Project } from '$lib/types/api';

  const EVIDENCE_LIMIT = 300;

  let {
    conversations,
    waitingConversations,
    waitingError = null,
    waitingScope = '',
    agents,
    projects = [], selectedProjectId = '', onToggleProject, onToggleStar,
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
    onSeeAll = () => goto('/chat'),
    showAgents = true,
    stripAgents,
    metadataForItem,
    onRetry = () => undefined,
    onAgentsRetry = () => undefined,
    recentHasMore = false,
    recentLoading = false,
    recentError = null,
    onLoadMoreRecent = () => undefined,
  } = $props<{
    conversations: Conversation[] | null;
    waitingConversations?: Conversation[];
    waitingError?: string | null;
    waitingScope?: string;
    agents: Agent[];
    projects?: Project[];
    selectedProjectId?: string;
    onToggleProject?: (id: string) => void;
    onToggleStar?: (conversation: Conversation) => void;
    loading?: boolean;
    error?: string | null;
    agentsLoading?: boolean;
    agentsError?: string | null;
    openConversationId?: string | null;
    openConversationIds?: string[];
    onOpenConversation: (conversationId: string) => void;
    onOpenAttention?: (action: AttentionActionSummary, origin: HTMLElement) => void;
    onSelectAgent?: (agent: Agent) => void;
    onNewChat?: () => void;
    onSeeAll?: (() => void) | null;
    showAgents?: boolean;
    stripAgents?: Agent[];
    metadataForItem?: (item: Conversation) => string;
    onRetry?: () => void;
    onAgentsRetry?: () => void;
    recentHasMore?: boolean;
    recentLoading?: boolean;
    recentError?: string | null;
    onLoadMoreRecent?: () => void;
  }>();

  let backgroundWork = $state<BackgroundWorkItem[]>([]);
  $effect(() => {
    const owner = $auth.user?.email;
    backgroundWork = [];
    if (!owner) return;
    const controller = new SidebarRefreshController(async ({ signal }) => {
      const result = await api.conversations.backgroundWork({ signal });
      if (!signal.aborted && result.background_work_changed) backgroundWork = result.background_work.items;
    }, new RefreshCooldown());
    controller.setLifecycle({ scope: owner, eligible: true });
    const refresh = () => { void controller.request(owner); };
    refresh();
    const stop = wsClient.subscribe((event) => {
      if (event.type === 'work_invalidated'
        || dashboardRealtimeLoads(event.type, 'reason' in event ? event.reason : undefined).includes('conversations')) refresh();
    });
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('focus', visible);
    document.addEventListener('visibilitychange', visible);
    return () => {
      controller.dispose();
      stop();
      window.removeEventListener('focus', visible);
      document.removeEventListener('visibilitychange', visible);
    };
  });
  const backgroundIds = $derived(new Set(backgroundWork.filter(backgroundWorkItemIsRunning)
    .map((item) => item.controller_conversation_id)));
  const lanes = $derived.by(() => {
    const history = partitionConversationLanes(conversations ?? [], {
      recent: Number.MAX_SAFE_INTEGER,
    }, backgroundWork);
    if (waitingConversations === undefined) return history;
    const waiting = waitingConversations.filter((item: Conversation) =>
      isDashboardTopicConversation(item)
      && isSidebarConversationWaiting(item, isDashboardConversationActive(item)
        || backgroundIds.has(item.conversation_id)));
    const ids = new Set(waiting.map((item: Conversation) => item.conversation_id));
    return {
      waiting,
      active: history.active.filter((item) => !ids.has(item.conversation_id)),
      recent: history.recent.filter((item) => !ids.has(item.conversation_id)),
    };
  });

  // Mobile: bounded Recent list with explicit Load more (see bounded-list.ts).
  let mobile = $state(false);
  let recentLimit = $state(DASHBOARD_MOBILE_PAGE_SIZE);
  $effect(() => {
    const query = window.matchMedia?.(DASHBOARD_MOBILE_QUERY);
    if (!query) return;
    mobile = query.matches;
    const change = (event: MediaQueryListEvent) => { mobile = event.matches; };
    query.addEventListener('change', change);
    return () => query.removeEventListener('change', change);
  });
  const recentItems = $derived(visibleRecentItems(lanes.recent, mobile, recentLimit));
  const showLoadMore = $derived(canLoadMoreRecent(lanes.recent.length, recentHasMore, mobile, recentLimit));

  function onLoadMoreClick(): void {
    recentLimit = loadMoreRecent({
      loadedCount: lanes.recent.length,
      serverHasMore: recentHasMore,
      mobile,
      limit: recentLimit,
      fetchMore: onLoadMoreRecent,
    });
  }

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
    if (backgroundIds.has(conversation.conversation_id)) return 'Background work active';
    return 'Idle';
  }

  function avatarState(conversation: Conversation) {
    return conversationActivityState(conversation, {
      backgroundWork,
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
      if (entries.some((entry) => entry.isIntersecting)) onLoadMoreClick();
    }, { root, rootMargin: '160px 0px' });
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }
</script>

{#snippet indicators(conversation: Conversation)}
  {@const todos = conversation.conversation_state?.active_session?.todos ?? []}
  <DashboardRowIndicators item={conversation} {projects} {selectedProjectId} {onToggleProject}
    starred={Boolean(conversation.starred_at)}
    active={openConversationId === conversation.conversation_id || openConversationIds.includes(conversation.conversation_id)}
    onToggleStar={onToggleStar && !conversation.is_readonly_for_caller ? () => onToggleStar?.(conversation) : undefined}
    progress={{ total: todos.length, completed: todos.filter((todo) => todo.status === 'completed').length, in_progress: todos.filter((todo) => todo.status === 'in_progress').length }} />
{/snippet}

{#snippet rowTitle(conversation: Conversation)}
  <DashboardRowTitle title={conversation.title ?? 'Untitled conversation'}
    linkLabel="Open conversation in full page" linkTestId={`dashboard-conversation-row-link-${conversation.conversation_id}`}
    onOpenLink={(event) => openLink(event, conversation.conversation_id)} />
  <ManagedChannelBadge {conversation} />
{/snippet}

<div class="dashboard-bounded-section min-w-0 md:flex md:h-full md:min-h-0 md:flex-col" data-testid="dashboard-conversations-section">
<Card class="min-w-0 p-4 md:flex md:h-full md:min-h-0 md:flex-col md:overflow-hidden">
  {#if showAgents}<div class="mb-4 shrink-0" data-testid="dashboard-conversations-agents">
    {#if agentsError}
      <p class="rounded-xl border border-warning-border bg-warning-subtle p-3 text-xs text-warning" role="alert">
        Could not load agents: {agentsError}
        <Button class="ml-2" size="sm" variant="secondary" onclick={onAgentsRetry}>Try again</Button>
      </p>
    {:else if agentsLoading && agents.length === 0}
      <p class="text-xs text-quiet">Loading agents…</p>
    {:else}
      <AgentAvatarStrip agents={stripAgents ?? agents} {onSelectAgent} />
    {/if}
  </div>{/if}
  <div class="flex min-w-0 shrink-0 items-center justify-between gap-3">
    <h2 class="text-sm font-semibold uppercase tracking-[0.2em] text-secondary">Conversations</h2>
    <div class="flex items-center gap-2">
      {#if onNewChat}<Button size="sm" variant="secondary" data-testid="dashboard-conversations-new" onclick={onNewChat}>New chat</Button>{/if}
      {#if onSeeAll}<Button size="sm" variant="ghost" data-testid="dashboard-conversations-see-all" onclick={onSeeAll}>See all</Button>{/if}
    </div>
  </div>

  <WaitingRefreshStatus error={waitingError} scope={waitingScope} onRetry={onRetry} />

  {#if error}
    <p class="mt-3 shrink-0 rounded-xl border border-warning-border bg-warning-subtle p-3 text-xs text-warning" role="alert" data-testid="dashboard-conversations-error">
      Could not load conversations: {error}
      <Button class="ml-2" size="sm" variant="secondary" onclick={onRetry}>Try again</Button>
    </p>
  {/if}
  {#if loading && !conversations && (waitingConversations?.length ?? 0) === 0}
    <p class="mt-3 shrink-0 text-xs text-quiet">Loading conversations…</p>
  {:else}
    <div class="dashboard-section-lanes mt-4 space-y-5 md:flex md:min-h-0 md:flex-1 md:flex-col md:space-y-0 md:gap-2 md:overflow-hidden">
      <section class="md:flex md:min-h-16 md:flex-col" data-testid="dashboard-conversations-active">
        <div class="flex shrink-0 items-center gap-2">
          <p class="text-xs font-medium uppercase tracking-widest text-accent">Active now</p>
          <span class="rounded-full bg-raised/70 px-1.5 py-0.5 text-[10px] tabular-nums text-muted"
            aria-label={`${lanes.active.length} active conversations`} data-testid="active-conversation-count">{lanes.active.length}</span>
        </div>
        {#if lanes.active.length === 0}
          <p class="mt-2 shrink-0 text-xs text-quiet">No active conversations.</p>
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
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-separator bg-canvas/40 px-3 py-2 text-left hover:border-accent/40"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                  <ActivityAvatar
                    name={conversation.agent_display_name ?? conversationAgent?.display_name ?? conversation.agent_id}
                    avatarUrl={conversation.agent_avatar_url ?? conversationAgent?.avatar_url ?? null}
                    state={avatarState(conversation)}
                    class="h-8 w-8"
                  />
                   <div class="min-w-0 flex-1">
                    {@render rowTitle(conversation)}
                    <WorkstreamExecutionStatus label={executionLabel(conversation)} active />
                     {#if metadataForItem?.(conversation)}<p class="truncate text-xs text-quiet">{metadataForItem(conversation)}</p>{/if}
                    <p class="truncate text-xs text-muted">
                      {formatRelativeTime(conversation.last_message_at)}
                      {#if evidence(conversation)} · {evidence(conversation)}{/if}
                    </p>
                  </div>
                  {#each conversation.attention_actions ?? [] as action (action.action_id)}
                    {#if isAttentionActionQuickAction(action)}
                      <button
                        type="button"
                        aria-label={`${action.title} for ${conversation.title ?? 'conversation'}`}
                        class="shrink-0 rounded-lg border border-warning-border bg-warning-subtle px-2 py-1.5 text-xs font-medium text-warning hover:bg-warning-subtle"
                        data-testid={`dashboard-attention-${action.action_id}`}
                        onclick={(event) => openAttention(event, action)}
                        onkeydown={stopRowKeyboardActivation}
                      >{action.title}</button>
                    {/if}
                  {/each}
                  {@render indicators(conversation)}
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      {#if lanes.waiting.length > 0}
        <section class="md:flex md:min-h-16 md:flex-col" data-testid="dashboard-conversations-waiting">
          <p class="shrink-0 text-xs font-medium uppercase tracking-widest text-warning">Waiting for you</p>
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
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-left hover:border-warning-border"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                   <ActivityAvatar
                    name={conversation.agent_display_name ?? conversationAgent?.display_name ?? conversation.agent_id}
                    avatarUrl={conversation.agent_avatar_url ?? conversationAgent?.avatar_url ?? null}
                     state={avatarState(conversation)}
                     class="h-8 w-8"
                   />
                  <div class="min-w-0 flex-1">
                    {@render rowTitle(conversation)}
                    {#if metadataForItem?.(conversation)}<p class="truncate text-xs text-quiet">{metadataForItem(conversation)}</p>{/if}
                    <div class="mt-0.5 flex min-w-0 items-center gap-2 text-xs">
                      <WorkstreamExecutionStatus label={reason ? 'Pending' : 'Unread'} />
                      <span class={`min-w-0 flex-1 truncate ${reason ? 'text-warning' : 'text-accent'}`} data-testid={`dashboard-conversation-waiting-reason-${conversation.conversation_id}`}>{reason ?? 'Unread messages'}</span>
                      <span class="shrink-0 text-quiet">{formatRelativeTime(conversation.last_message_at)}</span>
                    </div>
                   </div>
                  {#each conversation.attention_actions ?? [] as action (action.action_id)}
                    {#if isAttentionActionQuickAction(action)}
                      <button
                        type="button"
                        aria-label={`${action.title} for ${conversation.title ?? 'conversation'}`}
                        class="shrink-0 rounded-lg border border-warning-border bg-warning-subtle px-2 py-1.5 text-xs font-medium text-warning hover:bg-warning-subtle"
                        data-testid={`dashboard-attention-${action.action_id}`}
                        onclick={(event) => openAttention(event, action)}
                        onkeydown={stopRowKeyboardActivation}
                      >{action.title}</button>
                    {/if}
                  {/each}
                  {@render indicators(conversation)}
                </div>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <section class="dashboard-section-recent flex min-h-0 flex-col md:min-h-20 md:flex-1" data-testid="dashboard-conversations-recent">
        <p class="shrink-0 text-xs font-medium uppercase tracking-widest text-muted">Recent</p>
        {#if lanes.recent.length === 0}
          <p class="mt-2 shrink-0 text-xs text-quiet">No recent conversations.</p>
        {:else}
          <ul
            class="mt-2 min-h-0 space-y-1.5 md:flex-1 md:overflow-y-auto"
            data-testid="dashboard-conversations-recent-list"
            aria-label="Recent conversations"
          >
            {#each recentItems as conversation (conversation.conversation_id)}
              {@const conversationAgent = agentFor(conversation.agent_id)}
              <li>
                <div
                  role="button"
                  tabindex="0"
                  class="flex min-w-0 w-full items-center gap-3 rounded-xl border border-separator bg-canvas/40 px-3 py-2 text-left hover:border-accent/40"
                  data-testid={`dashboard-conversation-row-${conversation.conversation_id}`}
                  onclick={() => openRow(conversation.conversation_id)}
                  onkeydown={(event) => handleRowKeydown(event, conversation.conversation_id)}
                >
                   <ActivityAvatar
                    name={conversation.agent_display_name ?? conversationAgent?.display_name ?? conversation.agent_id}
                    avatarUrl={conversation.agent_avatar_url ?? conversationAgent?.avatar_url ?? null}
                     state={avatarState(conversation)}
                     class="h-8 w-8"
                   />
                  <div class="min-w-0 flex-1">
                     {@render rowTitle(conversation)}
                     {#if metadataForItem?.(conversation)}<p class="truncate text-xs text-quiet">{metadataForItem(conversation)}</p>{/if}
                    <p class="truncate text-xs text-muted">
                      {formatRelativeTime(conversation.last_message_at)}
                      {#if evidence(conversation)} · {evidence(conversation)}{/if}
                    </p>
                  </div>
                  {@render indicators(conversation)}
                </div>
              </li>
            {/each}
            {#if showLoadMore && !recentLoading && !recentError}
              {#key recentItems.length}
                <li use:observeRecentSentinel aria-hidden="true" data-testid="dashboard-conversations-recent-sentinel" class="h-px"></li>
              {/key}
            {/if}
            {#if recentLoading}<li class="py-2 text-center text-xs text-quiet">Loading…</li>{/if}
            {#if recentError}<li class="py-2 text-xs text-warning" role="alert">{recentError}</li>{/if}
          </ul>
        {/if}
        {#if recentHasMore && !recentLoading && !recentError && lanes.recent.length === 0}
          <div use:observeRecentSentinel aria-hidden="true" data-testid="dashboard-conversations-recent-sentinel" class="h-px"></div>
        {/if}
        {#if showLoadMore}<Button size="sm" variant="ghost" disabled={recentLoading} onclick={onLoadMoreClick} data-testid="dashboard-conversations-load-more">{recentError ? 'Retry loading conversations' : 'Load more conversations'}</Button>{/if}
      </section>
    </div>
  {/if}
</Card>
</div>
