<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { Conversation } from '$lib/types/api';
  import { managedConversationTurnState } from '$lib/chat-page';

  let {
    conversation,
    busy = null,
    error = '',
    onStop,
    onSend,
    onTakeControl,
  } = $props<{
    conversation: Conversation;
    busy?: string | null;
    error?: string;
    onStop: () => void;
    onSend: (message: string) => void;
    onTakeControl: () => void;
  }>();

  let instructionOpen = $state(false);
  let instruction = $state('');
  const conversationState = $derived(conversation.managed_agent?.conversation_state ?? 'open');
  const turnState = $derived(managedConversationTurnState(conversation));
  const active = $derived(turnState === 'running' || turnState === 'queued');
</script>

<section class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100" data-testid="managed-conversation-controls">
  <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
    <div class="min-w-0 sm:flex-1">
      <p class="font-medium">Agent work</p>
      <p class="mt-1 text-sky-100/80">Read-only target conversation · state {conversationState} · turn {turnState}</p>
      {#if conversation.managed_agent?.last_error && !active}
        <p class="mt-2 break-words text-xs text-rose-100">Last error: {conversation.managed_agent.last_error}</p>
      {/if}
    </div>
    <div class="grid w-full grid-cols-2 gap-2 sm:flex sm:w-auto sm:shrink-0">
      {#if active}
        <Button size="sm" variant="danger" disabled={busy !== null} onclick={onStop}>{busy === 'stop' ? 'Stopping…' : 'Stop'}</Button>
      {/if}
      <Button size="sm" variant="secondary" disabled={busy !== null || conversationState === 'closed' || active} onclick={() => { instructionOpen = !instructionOpen; }}>Send instruction</Button>
      <Button size="sm" variant="secondary" disabled={busy !== null || conversationState === 'closed' || active} onclick={() => onSend('Continue')}>Continue</Button>
      <Button size="sm" variant="secondary" disabled={busy !== null || conversationState === 'closed' || active} onclick={onTakeControl}>{busy === 'take-control' ? 'Forking…' : 'Take control'}</Button>
    </div>
  </div>
  {#if active}<p class="mt-2 text-xs text-sky-100/70">Stop the current turn before sending an instruction or taking control.</p>{/if}
  {#if instructionOpen && conversationState !== 'closed'}
    <form class="mt-3 space-y-2" onsubmit={(event) => {
      event.preventDefault();
      const message = instruction.trim();
      if (!message) return;
      onSend(message);
      instruction = '';
      instructionOpen = false;
    }}>
      <textarea class="min-h-24 w-full rounded-2xl border border-sky-300/25 bg-slate-950/40 px-3 py-2 text-sm" aria-label="Managed instruction" bind:value={instruction} disabled={busy !== null || active}></textarea>
      <div class="flex justify-end gap-2">
        <Button size="sm" variant="ghost" type="button" onclick={() => { instructionOpen = false; }}>Cancel</Button>
        <Button size="sm" type="submit" disabled={busy !== null || active || !instruction.trim()}>{busy === 'send' ? 'Sending…' : 'Send'}</Button>
      </div>
    </form>
  {/if}
  {#if error}<p class="mt-2 break-words text-xs text-rose-100">{error}</p>{/if}
</section>
