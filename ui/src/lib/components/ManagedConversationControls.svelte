<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import type { Conversation } from '$lib/types/api';
  import { managedConversationTurnState } from '$lib/chat-page';
  import { isPlainNavigation } from '$lib/chat-v2/navigation';
  import { managedChannelIdentity } from '$lib/managed-channel';
  import ManagedChannelBadge from '$lib/components/ManagedChannelBadge.svelte';

  let {
    conversation,
    busy = null,
    error = '',
    onStop,
    onSend,
    onTakeControl,
    onTerminate,
    onNavigateParent,
  } = $props<{
    conversation: Conversation;
    busy?: string | null;
    error?: string;
    onStop: () => void;
    onSend: (message: string) => void;
    onTakeControl: () => void;
    onTerminate?: () => void;
    onNavigateParent?: (conversationId: string) => boolean;
  }>();

  let instructionOpen = $state(false);
  let instruction = $state('');
  let actionsOpen = $state(false);
  // Expanded content must not contribute to the panel's minimum height.
  let summaryHeight = $state<number | undefined>(undefined);
  const componentId = $props.id();
  const actionsId = `managed-actions-${componentId}`;
  const conversationId = $derived(conversation.conversation_id);
  $effect(() => {
    if (!conversationId) return;
    actionsOpen = false;
    instructionOpen = false;
    instruction = '';
  });
  const conversationState = $derived(conversation.managed_agent?.conversation_state ?? 'open');
  const turnState = $derived(managedConversationTurnState(conversation));
  const active = $derived(turnState === 'running' || turnState === 'queued');
  const channel = $derived(managedChannelIdentity(conversation));
  const closed = $derived(channel ? conversationState !== 'open' : conversationState === 'closed');
  const parentConversationId = $derived(
    conversation.managed_agent?.controller_conversation_id
      ?? conversation.root_controller_conversation_id
      ?? null
  );
  const showParentConversation = $derived(
    parentConversationId !== null && parentConversationId !== conversation.conversation_id
  );
</script>

<section class="grid grid-rows-[auto_minmax(0,1fr)] rounded-2xl border border-blue-border bg-blue-subtle text-sm text-foreground"
  style:min-height={summaryHeight === undefined ? 'auto' : `${summaryHeight + 2}px`}
  data-testid="managed-conversation-controls">
  <div bind:clientHeight={summaryHeight} class="min-w-0 px-4 py-3">
      <p class="font-medium">{channel ? 'Managed channel conversation' : 'Agent work'}</p>
      <ManagedChannelBadge {conversation} />
      <div class="mt-1 flex items-end justify-between gap-3">
        <p class="min-w-0 text-secondary">{channel ? 'Sender workspace' : 'Read-only target conversation'} · state {conversationState} · turn {turnState}</p>
        {#if turnState === 'running'}
          <span role="status" class="ml-auto inline-flex shrink-0 items-center gap-1.5 text-secondary" data-testid="managed-running-indicator">
            <span aria-hidden="true" class="h-3.5 w-3.5 animate-spin rounded-full border-2 border-control-border border-t-accent motion-reduce:animate-none"></span>
            running
          </span>
        {/if}
      </div>
      <div class="mt-1 flex items-center justify-between gap-3">
      {#if showParentConversation && parentConversationId}
        <a
          class="inline-flex min-h-11 items-center text-xs text-accent underline underline-offset-2 hover:text-accent-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          href={`/chat/${encodeURIComponent(parentConversationId)}`}
          onclick={(event) => {
            if (isPlainNavigation(event) && onNavigateParent?.(parentConversationId) === true) event.preventDefault();
          }}
        >Parent conversation</a>
      {/if}
        <button type="button" class="ml-auto inline-flex min-h-11 shrink-0 items-center gap-1 rounded-lg px-2 text-xs text-muted hover:bg-surface-hover hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          aria-expanded={actionsOpen} aria-controls={actionsId} onclick={() => { actionsOpen = !actionsOpen; }}>
          Actions
          {#if actionsOpen}<ChevronUp class="h-3.5 w-3.5" />{:else}<ChevronDown class="h-3.5 w-3.5" />{/if}
        </button>
      </div>
      {#if conversation.managed_agent?.last_error && !active}
        <p class="mt-2 break-words text-xs text-danger">Last error: {conversation.managed_agent.last_error}</p>
      {/if}
      {#if error}<p role="alert" class="mt-2 break-words text-xs text-danger">{error}</p>{/if}
  </div>
  {#if actionsOpen}
  <div id={actionsId} class="min-h-0 overflow-y-auto overscroll-contain px-4 pb-3" data-testid="managed-conversation-actions">
    <div class="grid w-full grid-cols-2 gap-2 sm:flex sm:flex-wrap">
      {#if active && !channel}
        <Button size="sm" variant="danger" disabled={busy !== null} onclick={onStop}>{busy === 'stop' ? 'Stopping…' : 'Stop'}</Button>
      {/if}
      <Button size="sm" variant="secondary" disabled={busy !== null || closed || active} onclick={() => { instructionOpen = !instructionOpen; }}>{channel ? 'Send instructions' : 'Send instruction'}</Button>
      {#if channel}
      <Button size="sm" variant="danger" disabled={busy !== null || closed || !onTerminate} onclick={onTerminate}>{busy === 'close' ? 'Terminating…' : 'Terminate conversation'}</Button>
      {:else}
      <Button size="sm" variant="secondary" disabled={busy !== null || conversationState === 'closed' || active} onclick={() => onSend('Continue')}>Continue</Button>
      <Button size="sm" variant="secondary" disabled={busy !== null || active || Boolean(conversation.managed_agent?.follow_up_conversation_id)} onclick={onTakeControl}>{busy === 'take-control' ? 'Forking…' : 'Take control'}</Button>
      {/if}
    </div>
   {#if active}<p class="mt-2 text-xs text-secondary">{channel ? 'Wait for the current turn to finish before sending private instructions. Termination stops the conversation and its reply route.' : 'Stop the current turn before sending an instruction or taking control.'}</p>{/if}
   {#if channel}<p class="mt-2 text-xs text-secondary">Instructions are private to the agent. The participant only receives delivered assistant messages.</p>{/if}
   {#if instructionOpen && !closed}
    <form class="mt-3 space-y-2" onsubmit={(event) => {
      event.preventDefault();
      const message = instruction.trim();
      if (!message) return;
      onSend(message);
      instruction = '';
      instructionOpen = false;
    }}>
      <textarea class="min-h-24 w-full rounded-2xl border border-control-border bg-surface px-3 py-2 text-sm text-foreground focus:border-accent" aria-label="Managed instruction" bind:value={instruction} disabled={busy !== null || active}></textarea>
      <div class="flex justify-end gap-2">
        <Button size="sm" variant="ghost" type="button" onclick={() => { instructionOpen = false; }}>Cancel</Button>
        <Button size="sm" type="submit" disabled={busy !== null || active || !instruction.trim()}>{busy === 'send' ? 'Sending…' : 'Send'}</Button>
      </div>
    </form>
  {/if}
  </div>
  {/if}
</section>
