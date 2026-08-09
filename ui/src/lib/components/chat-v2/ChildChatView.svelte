<script lang="ts">
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
  import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
  import X from 'lucide-svelte/icons/x';
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

  let { view, node, agent = null, preferences, work = [], workTruncated = false, managedConversation = null, managedBusy = null, managedError = '', inspectorOpen, userScrolledUp = $bindable(false), todos = $bindable([]), timelineApi, timelineRealtime, onBack, onClose, onToggleInspector, onViewSession, onManagedStop, onManagedSend, onManagedTakeControl } = $props<{
    view: ChildView; node: WorkstreamRef; agent?: Agent | null; preferences: UserPreferences;
    work?: BackgroundWorkItem[]; workTruncated?: boolean;
    managedConversation?: Conversation | null; managedBusy?: string | null; managedError?: string;
    timelineApi?: Pick<ChatV2ApiClient, 'snapshot' | 'sync' | 'timeline'>;
    timelineRealtime?: ScopedChatV2Realtime;
    inspectorOpen: boolean; userScrolledUp?: boolean; todos?: TodoSnapshotItem[]; onBack: () => void; onClose: () => void;
    onToggleInspector: (trigger: HTMLElement) => void;
    onViewSession: (sessionId: string, node?: WorkstreamRef) => void;
    onManagedStop?: () => void; onManagedSend?: (message: string) => void; onManagedTakeControl?: () => void;
  }>();
  let copied = $state(false);
  let ongoingWorkOpen = $state(false);
  const scope = $derived(childViewScope(view));
  const displayName = $derived(node.agent_display_name ?? agent?.display_name ?? agent?.name ?? node.agent_id);
  const avatarUrl = $derived(node.agent_avatar_url ?? agent?.avatar_url ?? null);
  const normalizedStatus = $derived(node.activity_state === 'ongoing' ? 'Running' : node.activity_state === 'active' ? 'Active' : node.status === 'failed' ? 'Failed' : node.status === 'cancelled' ? 'Cancelled' : 'Closed');
  async function copySessionId(): Promise<void> {
    await navigator.clipboard?.writeText(view.sessionId);
    copied = true;
    window.setTimeout(() => { copied = false; }, 1200);
  }
</script>

<section class="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-slate-950/95" data-testid="child-chat-view">
  <header class="flex shrink-0 items-center gap-3 border-b border-slate-800/80 px-4 py-3">
    <button class="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white" type="button" aria-label="Back to parent conversation" onclick={onBack}><ArrowLeft class="h-4 w-4" /></button>
    <AgentAvatar name={displayName} {avatarUrl} class="h-8 w-8 shrink-0" />
    <div class="min-w-0 flex-1">
      <h2 class="truncate text-sm font-semibold text-slate-100">{node.title}</h2>
      <div class="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-[10px] text-slate-400">
        <span class="truncate text-slate-200">{displayName}</span>
        {#if node.agent_profile_id}<span>profile {node.agent_profile_id}</span>{/if}
        {#if node.model}<span>{node.model}</span>{/if}
        {#if node.reasoning_effort}<span>thinking {node.reasoning_effort}</span>{/if}
        <span>{view.kind}</span><span>{normalizedStatus}</span>
        {#if node.activity_state === 'ongoing'}<LiveDots inline size="sm" tone={userScrolledUp ? 'slate' : 'sky'} label={userScrolledUp ? 'Live follow paused' : 'Following latest'} />{/if}
      </div>
    </div>
    <button class="flex shrink-0 items-center gap-1 rounded px-1.5 py-1 font-mono text-[10px] text-slate-500 hover:bg-slate-800 hover:text-slate-200" type="button" title="Copy canonical session ID" onclick={() => void copySessionId()}>{view.sessionId.slice(0, 12)}{#if copied}<Check class="h-3 w-3 text-emerald-400" />{:else}<Copy class="h-3 w-3" />{/if}</button>
    <button class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-100" type="button" aria-label={inspectorOpen ? 'Collapse conversation inspector' : 'Open conversation inspector'} aria-expanded={inspectorOpen} aria-controls="conversation-info-drawer" data-testid="child-header-inspector" onclick={(event) => onToggleInspector(event.currentTarget as HTMLElement)}>{#if inspectorOpen}<ChevronsRight class="h-4 w-4" />{:else}<ChevronsLeft class="h-4 w-4" />{/if}</button>
    <button class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-white" type="button" aria-label="Close child conversation" onclick={onClose}><X class="h-4 w-4" /></button>
  </header>
  {#if work.length || todos.length}
    <div class="shrink-0 px-4 pt-3">
      <TimelineOngoingWorkDrawer {todos} {work} truncated={workTruncated} bind:open={ongoingWorkOpen} {onViewSession} />
    </div>
  {/if}
  <ScopedChatV2Timeline {scope} {agent} {preferences} bind:userScrolledUp {onViewSession} onTodosChange={(next) => { todos = next; }} api={timelineApi} realtime={timelineRealtime} activityStatus={node.activity_state === 'ongoing' ? 'running' : node.status} emptyLabel="No events recorded yet." />
  {#if view.kind === 'managed' && managedConversation && onManagedStop && onManagedSend && onManagedTakeControl}
    <div class="shrink-0 p-3">
      <ManagedConversationControls conversation={managedConversation} busy={managedBusy} error={managedError} onStop={onManagedStop} onSend={onManagedSend} onTakeControl={onManagedTakeControl} />
    </div>
  {/if}
</section>
