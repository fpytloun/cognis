<script lang="ts">
  import Pin from 'lucide-svelte/icons/pin';
  import PinOff from 'lucide-svelte/icons/pin-off';
  import type { Conversation } from '$lib/types/api';
  import { conversationWorkspace, retainedConversationId, isAgentHome } from '$lib/dashboard/conversation-workspace';
  import { addToast } from '$lib/stores/toasts';

  let { conversation } = $props<{ conversation: Conversation | null }>();
  const entry = $derived(conversation ? $conversationWorkspace.entries.find((item) =>
    retainedConversationId(item.window) === conversation.conversation_id) : undefined);
  const home = $derived(!!conversation && isAgentHome(conversation));
  const pinned = $derived(home ? $conversationWorkspace.agents.some((agent) => agent.agentId === conversation?.agent_id) : !!entry);
  let busy = $state(false);
  async function toggle(): Promise<void> {
    if (!conversation || busy) return;
    busy = true;
    try {
      const result = home
        ? pinned ? await conversationWorkspace.execute({ type: 'agent', agentId: conversation.agent_id, pinned: false })
          : await conversationWorkspace.home(conversation, true)
        : entry ? await conversationWorkspace.execute({ type: 'unpin', key: entry.window.key })
          : await conversationWorkspace.pinAndSelect(conversation, false);
      if (!result.ok) addToast(result.reason === 'capacity' ? 'Close a pinned conversation first (limit 10).'
        : 'Pin state could not be confirmed. Refresh before trying again.', 'info');
    } finally { busy = false; }
  }
</script>

{#if conversation}
  <button type="button"
    class="touch-target-compact hidden shrink-0 items-center justify-center rounded-lg border border-control-border text-muted transition hover:bg-surface-hover hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent md:inline-flex"
    aria-label={pinned ? 'Unpin conversation' : 'Pin conversation'}
    aria-pressed={pinned}
    title={pinned ? 'Unpin conversation' : 'Pin conversation to the app-wide dock'}
    data-testid="pin-conversation"
    onclick={toggle}
    disabled={busy}
  >
    {#if pinned}<PinOff class="h-4 w-4" />{:else}<Pin class="h-4 w-4" />{/if}
  </button>
{/if}
