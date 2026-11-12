<script lang="ts">
  import Star from 'lucide-svelte/icons/star';
  import ConversationProjectBadge from '$lib/components/projects/ConversationProjectBadge.svelte';

  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import ManagedChannelBadge from '$lib/components/ManagedChannelBadge.svelte';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import { isAttentionActionQuickAction } from '$lib/attention/actions';
  import type { ActivityAvatarState } from '$lib/conversation-activity';
  import type {
    AttentionActionSummary,
    Conversation,
    ConversationTodoItem,
  } from '$lib/types/api';

  interface Props {
    conversation: Conversation;
    href: string;
    agentName: string;
    agentAvatarUrl?: string | null;
    avatarState: ActivityAvatarState;
    active: boolean;
    unread: boolean;
    dense: boolean;
    waitingReason?: string | null;
    canStar: boolean;
    starBusy: boolean;
    todoProgress?: Array<ConversationTodoItem & { priority: string }>;
    showTodoProgress?: boolean;
    contextBadge?: string | null;
    onNavigate: () => void;
    onToggleStar: (event: MouseEvent) => void;
    onOpenAttention: (action: AttentionActionSummary, origin: HTMLElement) => void;
    selectedProjectId?: string;
    onToggleProject?: (projectId: string) => void;
  }

  let {
    conversation,
    href,
    agentName,
    agentAvatarUrl = null,
    avatarState,
    active,
    unread,
    dense,
    waitingReason = null,
    canStar,
    starBusy,
    todoProgress = [],
    showTodoProgress = false,
    contextBadge = null,
    onNavigate,
    onToggleStar,
    onOpenAttention,
    selectedProjectId = '',
    onToggleProject,
  }: Props = $props();

  function openAttention(event: MouseEvent, action: AttentionActionSummary): void {
    event.preventDefault();
    event.stopPropagation();
    onOpenAttention(action, event.currentTarget as HTMLElement);
  }
</script>

{#snippet attentionActions(denseMode: boolean)}
  {#each conversation.attention_actions ?? [] as action (action.action_id)}
    {#if isAttentionActionQuickAction(action)}
      <button
        type="button"
        aria-label={`${action.title} for ${conversation.title ?? 'conversation'}`}
        class={`shrink-0 rounded-lg border border-warning-border bg-warning-subtle font-medium text-warning hover:bg-surface-hover ${denseMode ? 'px-1.5 py-1 text-[10px]' : 'px-2 py-1.5 text-xs'}`}
        data-testid={`sidebar-attention-${action.action_id}`}
        onclick={(event) => openAttention(event, action)}
      >{action.title}</button>
    {/if}
  {/each}
{/snippet}

{#snippet todoIndicator()}
  {#if showTodoProgress}
    <TodoProgressPopover
      todos={todoProgress}
      size="sm"
      placement="bottom-right"
      class="text-success"
      label="Conversation todo progress"
    />
  {/if}
{/snippet}

{#snippet starIndicator()}
  {#if canStar && !conversation.is_readonly_for_caller && (active || conversation.starred_at)}
    <button
      aria-label={conversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
      class={`rounded-lg p-1 transition hover:bg-surface-hover ${conversation.starred_at ? 'text-orange-tone' : 'text-quiet hover:text-foreground'}`}
      disabled={starBusy}
      onclick={onToggleStar}
      title={conversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
      type="button"
    >
      <Star class={`h-4 w-4 ${conversation.starred_at ? 'fill-current' : ''}`} />
    </button>
  {:else if canStar && !conversation.is_readonly_for_caller}
    <!-- Reserve the button's geometry without an invisible interactive target. -->
    <span class="pointer-events-none block h-6 w-6 shrink-0" aria-hidden="true"></span>
  {/if}
{/snippet}

<div
  class={`group flex min-w-0 items-center rounded-xl transition ${dense ? 'gap-2 px-2 py-1.5' : 'gap-2 px-2 py-2'} ${active ? 'bg-accent-subtle text-strong' : waitingReason ? 'border border-warning-border bg-warning-subtle text-foreground hover:border-border-strong' : 'text-foreground hover:bg-surface-hover'}`}
  data-testid={`sidebar-conversation-row-${conversation.conversation_id}`}
>
  {#if dense}
    <a class="flex min-w-0 flex-1 items-center gap-2" {href} onclick={onNavigate} title={conversation.title ?? 'Untitled conversation'}>
      <ActivityAvatar
        name={conversation.agent_display_name ?? agentName}
        avatarUrl={conversation.agent_avatar_url ?? agentAvatarUrl}
        state={avatarState}
        class="h-7 w-7 shrink-0"
      />
      <div class="min-w-0 flex-1">
      <p class={`min-w-0 overflow-x-auto whitespace-nowrap text-xs [scrollbar-width:none] [&::-webkit-scrollbar]:hidden ${unread ? 'font-semibold text-strong' : 'font-medium text-strong'}`}>
        {conversation.title ?? 'Untitled conversation'}
      </p>
      <ManagedChannelBadge {conversation} />
      </div>
    </a>
    {@render attentionActions(true)}
    <div class="relative z-10 flex shrink-0 items-center gap-0.5" data-testid="sidebar-dense-indicators">
      <ConversationProjectBadge {conversation} selected={selectedProjectId === conversation.project_id} onToggle={onToggleProject && conversation.project_id ? () => onToggleProject?.(conversation.project_id!) : undefined} />
      {@render todoIndicator()}
      {@render starIndicator()}
    </div>
  {:else}
    <a
      class="flex min-w-0 flex-1 items-center gap-2"
      {href}
      onclick={onNavigate}
      title={conversation.title ?? 'Untitled conversation'}
    >
    <ActivityAvatar
      name={conversation.agent_display_name ?? agentName}
      avatarUrl={conversation.agent_avatar_url ?? agentAvatarUrl}
      state={avatarState}
      class="h-8 w-8 shrink-0"
    />
    <div class="min-w-0 flex-1">
      <p
        class={`line-clamp-2 break-words text-sm leading-5 ${unread ? 'font-semibold text-strong' : 'font-medium text-strong'}`}
      >{conversation.title ?? 'Untitled conversation'}</p>
      <ManagedChannelBadge {conversation} />
      {#if contextBadge || waitingReason}
      <div class="mt-0.5 flex items-center gap-2">
        {#if contextBadge}
          <span class="shrink-0 rounded-full border border-control-border bg-raised/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-widest text-quiet">
            {contextBadge}
          </span>
        {/if}
        {#if waitingReason}
          <span class="truncate text-xs text-warning">{waitingReason}</span>
        {/if}
      </div>
      {/if}
    </div>
    </a>
    {@render attentionActions(false)}
    <div class="relative z-10 grid shrink-0 grid-cols-[auto_minmax(1.5rem,auto)] items-center gap-0.5" data-testid="sidebar-row-indicators">
      <div class="col-start-2 row-start-1 flex justify-center">
        <ConversationProjectBadge {conversation} selected={selectedProjectId === conversation.project_id} onToggle={onToggleProject && conversation.project_id ? () => onToggleProject?.(conversation.project_id!) : undefined} />
      </div>
      <div class="col-start-1 row-start-2 flex justify-center">
        {@render todoIndicator()}
      </div>
      <div class="col-start-2 row-start-2 flex justify-center">
        {@render starIndicator()}
      </div>
    </div>
  {/if}
</div>
