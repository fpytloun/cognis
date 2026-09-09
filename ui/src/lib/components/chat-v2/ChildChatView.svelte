<script lang="ts">
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
  import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
  import X from 'lucide-svelte/icons/x';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
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
    isEffectiveSessionRunning,
    isTerminalSessionStatus,
  } from '$lib/session-status';

  let { view, node, agent = null, preferences, work = [], workTruncated = false, managedConversation = null, managedBusy = null, managedError = '', inspectorOpen, userScrolledUp = $bindable(false), todos = $bindable([]), timelineApi, timelineRealtime, onBack, onClose, onToggleInspector, onViewSession, onRuntimeActiveChange, onManagedStop, onManagedSend, onManagedTakeControl } = $props<{
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
  }>();
  let copied = $state(false);
  let ongoingWorkOpen = $state(false);
  const scope = $derived(childViewScope(view));
  const displayName = $derived(node.agent_display_name ?? agent?.display_name ?? agent?.name ?? node.agent_id);
  const avatarUrl = $derived(node.agent_avatar_url ?? agent?.avatar_url ?? null);
  const normalizedStatus = $derived(displaySessionStatus(node.status, node.activity_state, false, node.execution_state));
  const executionActive = $derived(isEffectiveSessionRunning(node.status, node.activity_state, false, node.execution_state));
  async function copySessionId(): Promise<void> {
    await navigator.clipboard?.writeText(view.sessionId);
    copied = true;
    window.setTimeout(() => { copied = false; }, 1200);
  }
  function handleTodosChange(next: TodoSnapshotItem[]): void {
    todos = next;
  }
  onDestroy(() => onRuntimeActiveChange?.(false));
</script>

<section
  class="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-transparent"
  style="padding-bottom: var(--app-bottom-control-inset);"
  data-testid="child-chat-view"
>
  <header
    class="flex shrink-0 items-center gap-2 border-b border-slate-800/80 px-3 pt-[calc(0.625rem+env(safe-area-inset-top))] pb-2.5 sm:gap-3 sm:px-4 sm:pt-[calc(0.75rem+env(safe-area-inset-top))] sm:pb-3"
    style="padding-left: max(0.75rem, env(safe-area-inset-left)); padding-right: max(0.75rem, env(safe-area-inset-right));"
    data-testid="child-header"
  >
    <button class="flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-white sm:h-8 sm:w-8" type="button" aria-label="Back to parent conversation" onclick={onBack}><ArrowLeft class="h-4 w-4" /></button>
    <AgentAvatar name={displayName} {avatarUrl} class="h-8 w-8 shrink-0" />
    <div class="min-w-0 flex-1">
      <h2 class="truncate text-sm font-semibold text-slate-100">{node.title}</h2>
      <div class="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-[10px] text-slate-400">
        <span class="truncate text-slate-200">{displayName}</span>
        {#if node.agent_profile_id}<span class="hidden sm:inline">profile {node.agent_profile_id}</span>{/if}
        {#if node.model}<span class="hidden sm:inline">{node.model}</span>{/if}
        {#if node.reasoning_effort}<span class="hidden sm:inline">thinking {node.reasoning_effort}</span>{/if}
        <span>{view.kind}</span><span>{normalizedStatus}</span>
        {#if executionActive}<LiveDots inline size="sm" tone={userScrolledUp ? 'slate' : 'sky'} label={userScrolledUp ? 'Live follow paused' : 'Following latest'} />{/if}
      </div>
    </div>
    <div class="flex shrink-0 items-center gap-0.5 sm:gap-1.5" data-testid="child-header-actions">
      <button class="flex h-[44px] min-w-[44px] shrink-0 items-center justify-center gap-1 rounded-lg px-2 font-mono text-[10px] text-slate-500 hover:bg-slate-800 hover:text-slate-200 sm:h-8 sm:min-w-0 sm:px-1.5 sm:py-1" type="button" aria-label="Copy canonical session ID" title="Copy canonical session ID" onclick={() => void copySessionId()}>
        <span class="hidden sm:inline">{view.sessionId.slice(0, 12)}</span>
        {#if copied}<Check class="h-3.5 w-3.5 text-emerald-400 sm:h-3 sm:w-3" />{:else}<Copy class="h-3.5 w-3.5 sm:h-3 sm:w-3" />{/if}
      </button>
      {#if view.kind === 'managed'}
        <a
          class="inline-flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-sky-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/60 sm:h-8 sm:w-8"
          href={`/chat/${encodeURIComponent(view.conversationId)}`}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${node.title} in new window`}
        ><ExternalLink class="h-4 w-4" /></a>
      {/if}
      <button class="inline-flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-100 sm:h-8 sm:w-8" type="button" aria-label={inspectorOpen ? 'Collapse conversation inspector' : 'Open conversation inspector'} aria-expanded={inspectorOpen} aria-controls="conversation-info-drawer" data-testid="child-header-inspector" onclick={(event) => onToggleInspector(event.currentTarget as HTMLElement)}>{#if inspectorOpen}<ChevronsRight class="h-4 w-4" />{:else}<ChevronsLeft class="h-4 w-4" />{/if}</button>
      <button class="inline-flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-white sm:h-8 sm:w-8" type="button" aria-label="Close child conversation" onclick={onClose}><X class="h-4 w-4" /></button>
    </div>
  </header>
  {#if work.length || todos.length}
    <div class="shrink-0 px-4 pt-3">
      <TimelineOngoingWorkDrawer {todos} {work} truncated={workTruncated} bind:open={ongoingWorkOpen} {onViewSession} />
    </div>
  {/if}
  <ScopedChatV2Timeline {scope} {agent} {preferences} bind:userScrolledUp {onViewSession} {onRuntimeActiveChange} onTodosChange={handleTodosChange} api={timelineApi} realtime={timelineRealtime} activityStatus={node.execution_state ?? (isTerminalSessionStatus(node.status) ? node.status : node.activity_state === 'ongoing' ? 'running' : node.status)} emptyLabel="No events recorded yet." />
  {#if view.kind === 'managed' && managedConversation && onManagedStop && onManagedSend && onManagedTakeControl}
    <div class="shrink-0 p-3">
      <ManagedConversationControls conversation={managedConversation} busy={managedBusy} error={managedError} onStop={onManagedStop} onSend={onManagedSend} onTakeControl={onManagedTakeControl} />
    </div>
  {/if}
</section>
