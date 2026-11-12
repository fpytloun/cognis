<script lang="ts">
  import { tick, type Snippet } from 'svelte';
  import X from 'lucide-svelte/icons/x';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import Home from 'lucide-svelte/icons/house';
  import Plus from 'lucide-svelte/icons/plus';
  import Search from 'lucide-svelte/icons/search';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import ChatWorkspace from '$lib/components/chat-v2/ChatWorkspace.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import PinnedConversationPicker from './PinnedConversationPicker.svelte';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';
  import type { PinnedPanel } from './pinned-panel.svelte';

  let { panel, controls, onMinimize, onOpenFull, composerFocusRequest = null }:
    { panel: PinnedPanel; controls?: Snippet; onMinimize: () => void; onOpenFull?: (path: string) => void;
      composerFocusRequest?: string | null } = $props();
  const net = useSectionConnectivity({ hasError: () => Boolean(panel.error),
    retry: () => panel.selected ? panel.select(panel.selected) : undefined });
  let workspace: ChatWorkspace | undefined = $state();
  let inspectorToggleContainer: HTMLSpanElement | undefined = $state();
  const inspectorState = $derived(workspace?.getInspectorState());

  function inspectorStateChanged(open: boolean): void {
    if (!open && document.activeElement?.closest('[data-dashboard-conversation-inspector-overlay]')) {
      void tick().then(() => inspectorToggleContainer?.querySelector('button')?.focus({ preventScroll: true }));
    }
  }
</script>

<div class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-control-border bg-surface text-foreground shadow-2xl"
  role="dialog" aria-label={`Pinned chat with ${panel.group?.agent?.display_name ?? panel.group?.agent?.name ?? 'agent'}`}
  aria-modal="false" data-testid="pinned-chat-panel">
  <header class="flex shrink-0 items-center gap-2 border-b border-separator bg-chrome px-3 py-2">
    {#if controls}{@render controls()}{/if}
    <span class="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{panel.group?.agent?.display_name ?? panel.group?.agent?.name ?? 'Pinned conversation'}</span>
    {#if panel.selectedId}
      <a href={`/chat/${encodeURIComponent(panel.selectedId)}`} class="rounded-lg p-2 text-muted hover:bg-surface-hover hover:text-foreground"
        aria-label="Open full conversation" title="Open conversation"
        onclick={(event) => { if (onOpenFull) { event.preventDefault(); onOpenFull(event.currentTarget.getAttribute('href')!); } }}>
        <ExternalLink class="h-4 w-4" />
      </a>
    {/if}
    <button type="button" class="rounded-lg p-2 text-muted hover:bg-surface-hover hover:text-foreground"
      aria-label="Minimize pinned chat" data-testid="pinned-chat-minimize" onclick={onMinimize}><ChevronDown class="h-4 w-4" /></button>
    <button type="button" class="rounded-lg p-2 text-muted hover:bg-danger-subtle hover:text-danger"
      aria-label="Unpin agent" title="Unpin agent and all its chats" onclick={panel.unpinAgent}><X class="h-4 w-4" /></button>
  </header>
  <div class="chat-header-shell flex shrink-0 border-b border-separator">
    <div class="flex min-w-0 gap-1 overflow-x-auto p-2" role="tablist" aria-label="Pinned conversations">
      {#each panel.group?.entries ?? [] as entry (entry.window.key)}
        {@const home = entry.window.key.startsWith('home:')}
        <div class="flex max-w-[220px] shrink-0 items-center rounded-lg border border-transparent" class:bg-accent-subtle={entry.window.key === panel.selectedKey && !panel.picker}>
          <button type="button" role="tab" aria-selected={entry.window.key === panel.selectedKey && !panel.picker}
            aria-controls="pinned-chat-content" aria-label={home ? 'Home' : undefined}
            title={home ? 'Main conversation' : entry.window.title} tabindex={entry.window.key === panel.selectedKey ? 0 : -1}
            id={`pinned-tab-${entry.window.key}`} class="flex min-w-0 items-center gap-2 px-2 py-1.5 text-xs text-secondary hover:text-strong"
            onclick={() => void panel.select(entry)}
            onkeydown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !panel.group) return;
              event.preventDefault();
              const tabs = panel.group.entries, index = tabs.indexOf(entry);
              const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
                : (index + (event.key === 'ArrowLeft' ? -1 : 1) + tabs.length) % tabs.length;
              void panel.select(tabs[next]); void tick().then(() => document.getElementById(`pinned-tab-${tabs[next].window.key}`)?.focus());
            }}>
            {#if home}<ActivityAvatar name="Home" state={panel.activity(entry)} class="h-5 w-5">
              {#snippet icon()}<Home class="h-4 w-4" />{/snippet}
            </ActivityAvatar>
            {:else}<ActivityAvatar name={entry.window.title} avatarUrl={panel.group?.agent?.avatar_url} state={panel.activity(entry)} class="h-5 w-5" /><span class="truncate">{entry.window.title}</span>{/if}
          </button>
          {#if !home}<button type="button" aria-label={`Unpin ${entry.window.title}`} title="Unpin conversation"
            class="mr-1 rounded p-1 text-quiet hover:bg-surface-hover hover:text-foreground" onclick={() => panel.unpin(entry)}><X class="h-3 w-3" /></button>{/if}
        </div>
      {/each}
    </div>
    <div class="flex shrink-0 items-center gap-0.5 pr-2">
      <button type="button" aria-label="New conversation" title="New conversation" disabled={panel.creating || !!panel.conversation?.is_readonly_for_caller}
        class="rounded-lg p-2 text-muted hover:bg-surface-hover hover:text-strong disabled:opacity-40" onclick={() => void panel.newConversation()}><Plus class="h-4 w-4" /></button>
      <button type="button" id="pinned-search-action" aria-controls="pinned-chat-content" aria-label="Find conversation" title="Recent conversations" aria-pressed={panel.picker}
        class="rounded-lg p-2 text-muted hover:bg-surface-hover hover:text-strong" class:bg-accent-subtle={panel.picker} onclick={() => { panel.picker = !panel.picker; }}><Search class="h-4 w-4" /></button>
    </div>
    {#if panel.selectedId && panel.conversation && !panel.picker}
      <span bind:this={inspectorToggleContainer} class="ml-auto mr-2 inline-flex shrink-0 items-center">
        <SessionDetailsButton open={inspectorState?.open ?? false}
          ariaControls={inspectorState?.controlsId ?? ''} testId="pinned-chat-inspector-toggle"
          onclick={() => { void workspace?.toggleInspector(); }} />
      </span>
    {/if}
  </div>
  <div id="pinned-chat-content" role="tabpanel" aria-labelledby={panel.picker ? 'pinned-search-action' : `pinned-tab-${panel.selectedKey}`}
    class="flex min-h-0 flex-1 flex-col">
    {#if panel.picker && panel.group}
      {#key panel.group.id}<PinnedConversationPicker agentId={panel.group.id} agent={panel.group.agent} onOpen={panel.openConversation} />{/key}
    {:else if panel.resolving || (panel.error && net.hideErrors)}<p class="m-auto text-sm text-muted">Opening conversation…</p>
    {:else if panel.error}<div class="m-auto p-4 text-sm text-danger" role="alert">{panel.error}<button class="ml-2 text-accent hover:text-accent-hover" onclick={() => { if (panel.selected) void panel.select(panel.selected); }}>Retry</button></div>
    {:else if panel.selectedId && panel.conversation}
      {#key panel.selectedId}
        <ChatWorkspace bind:this={workspace} conversationId={panel.selectedId} agent={panel.group?.agent ?? null} inspectorOpen={false}
          inspectorControlInHeader {composerFocusRequest}
          onInspectorStateChange={inspectorStateChanged}
          sessionId={panel.conversation.active_session_id ?? ''} agents={panel.group?.agent ? [panel.group.agent] : []}
          conversation={panel.conversation} readOnly={Boolean(panel.conversation.is_readonly_for_caller)}
          onInitialLoaded={panel.loaded} onSent={() => { if (panel.selectedId) void panel.refresh(panel.selectedId); }} onCommandResponse={panel.command} />
      {/key}
    {:else}<div class="m-auto flex flex-col items-center gap-3 p-4 text-sm text-muted"><span>Loading conversation…</span>
      <button type="button" class="text-accent hover:text-accent-hover" onclick={() => { if (panel.selectedId) void panel.refresh(panel.selectedId); }}>Retry</button></div>
    {/if}
  </div>
</div>
