<script lang="ts">
  import ParentChatLink from './ParentChatLink.svelte';
  import Search from 'lucide-svelte/icons/search';
  import { findTarget } from '$lib/find';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import SessionLogHeader from './SessionLogHeader.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import ScopedChatV2Timeline, { type ScopedChatV2Realtime } from './ScopedChatV2Timeline.svelte';
  import type { ChatV2ApiClient } from '$lib/chat-v2/api';
  import TimelineOngoingWorkDrawer from '$lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  import ManagedConversationControls from '$lib/components/ManagedConversationControls.svelte';
  import { childViewScope, type ChildView } from '$lib/childView';
  import type { WorkstreamRef } from '$lib/chat-v2/types';
  import type { Agent, BackgroundWorkItem, Conversation, UserPreferences } from '$lib/types/api';
  import type { TodoSnapshotItem } from '$lib/todos';
  import { onDestroy } from 'svelte';
  import {
    displaySessionStatus,
    isTerminalSessionStatus,
  } from '$lib/session-status';

  let { view, node, agent = null, preferences, work = [], workTruncated = false, managedConversation = null, managedBusy = null, managedError = '', inspectorOpen, userScrolledUp = $bindable(false), todos = $bindable([]), timelineApi, timelineRealtime, onBack, onClose, onToggleInspector, onViewSession, onRuntimeActiveChange, onManagedStop, onManagedSend, onManagedTakeControl, onManagedTerminate } = $props<{
    view: ChildView; node: WorkstreamRef; agent?: Agent | null; preferences: UserPreferences;
    work?: BackgroundWorkItem[]; workTruncated?: boolean;
    managedConversation?: Conversation | null; managedBusy?: string | null; managedError?: string;
    timelineApi?: Pick<ChatV2ApiClient, 'snapshot' | 'sync' | 'timeline'>;
    timelineRealtime?: ScopedChatV2Realtime;
    inspectorOpen: boolean; userScrolledUp?: boolean; todos?: TodoSnapshotItem[]; onBack: () => void; onClose: () => void;
    onToggleInspector: (trigger: HTMLElement) => void;
    onViewSession: (sessionId: string, node?: WorkstreamRef) => void;
    onRuntimeActiveChange?: (active: boolean) => void;
    onManagedStop?: () => void; onManagedSend?: (message: string) => void; onManagedTakeControl?: () => void;
    onManagedTerminate?: () => void;
  }>();
  let ongoingWorkOpen = $state(false);
  let timeline = $state<{ openSearch: (opener?: HTMLElement) => Promise<HTMLInputElement | null> } | null>(null);
  let searchButton = $state<HTMLButtonElement | undefined>(undefined);
  const scope = $derived(childViewScope(view));
  const displayName = $derived(node.agent_display_name ?? agent?.display_name ?? agent?.name ?? node.agent_id);
  const avatarUrl = $derived(node.agent_avatar_url ?? agent?.avatar_url ?? null);
  const title = $derived(managedConversation?.title || node.title);
  const showManagedControls = $derived(view.kind === 'managed' && managedConversation && onManagedStop && onManagedSend && onManagedTakeControl);
  const normalizedStatus = $derived(displaySessionStatus(node.status, node.activity_state, false, node.execution_state));
  function handleTodosChange(next: TodoSnapshotItem[]): void {
    todos = next;
  }
  onDestroy(() => onRuntimeActiveChange?.(false));
</script>

<section
  class="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-transparent"
  style="padding-bottom: var(--app-bottom-control-inset);"
  data-testid="child-chat-view" data-find-container
>
  <SessionLogHeader {title} testId="child-header">
    {#snippet leading()}
      <ParentChatLink conversationId={view.controllerRootConversationId} {onBack} />
    {/snippet}
    {#snippet metadata()}
      <div class="flex min-w-0 items-center gap-1 text-xs text-muted">
        <AgentAvatar name={displayName} {avatarUrl} class="h-4 w-4 shrink-0" />
        <span class="truncate text-foreground">{displayName}</span>
        <span aria-hidden="true">·</span><span class="shrink-0">{normalizedStatus}</span>
      </div>
    {/snippet}
    {#snippet actions()}
      <button class="touch-target-compact inline-flex items-center justify-center rounded-lg border border-control-border text-muted hover:bg-surface-hover hover:text-foreground"
        bind:this={searchButton} type="button" aria-label="Search in conversation" title="Search in conversation"
        use:findTarget={{ primary: true, identity: () => scope.key, open: () => timeline?.openSearch(searchButton) ?? Promise.resolve(null) }}
        onclick={() => { void timeline?.openSearch(searchButton); }}>
        <Search class="h-4 w-4" />
      </button>
      <SessionDetailsButton open={inspectorOpen} ariaControls="conversation-info-drawer" testId="child-header-inspector" onclick={(event) => onToggleInspector(event.currentTarget as HTMLElement)} />
    {/snippet}
  </SessionLogHeader>
  <ScopedChatV2Timeline bind:this={timeline} {scope} {agent} {preferences} bind:userScrolledUp {onViewSession} {onRuntimeActiveChange} onTodosChange={handleTodosChange} showTodoDrawer={false} showLiveIndicator={view.kind !== 'managed'} api={timelineApi} realtime={timelineRealtime} activityStatus={node.execution_state ?? (isTerminalSessionStatus(node.status) ? node.status : node.activity_state === 'ongoing' ? 'running' : node.status)} emptyLabel="No events recorded yet." />
  {#if work.length || todos.length || showManagedControls}
    <footer class="flex max-h-[75%] shrink-0 flex-col gap-3 px-3 py-3" data-testid="child-footer">
      {#if work.length || todos.length}
        <TimelineOngoingWorkDrawer {todos} {work} truncated={workTruncated} bind:open={ongoingWorkOpen} {onViewSession} />
      {/if}
      {#if showManagedControls && managedConversation && onManagedStop && onManagedSend && onManagedTakeControl}
        <ManagedConversationControls conversation={managedConversation} busy={managedBusy} error={managedError} onStop={onManagedStop} onSend={onManagedSend} onTakeControl={onManagedTakeControl}
          onTerminate={onManagedTerminate}
          onNavigateParent={(parentId) => {
            if (parentId !== view.controllerRootConversationId) return false;
            onClose();
            return true;
          }} />
      {/if}
    </footer>
  {/if}
</section>
