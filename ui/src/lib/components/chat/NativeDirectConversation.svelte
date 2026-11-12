<script lang="ts">
  import { auth } from '$lib/stores/auth';
  import { api, asApiError } from '$lib/api/client';
  import { postNative, nativeVisibility, nativeComposerFocus } from '$lib/native/host';
  import CompactConversationChat from '$lib/components/chat-v2/CompactConversationChat.svelte';
  import type { Agent, Conversation } from '$lib/types/api';

  let { agentId }: { agentId: string } = $props();
  let conversation = $state<Conversation | null>(null), agent = $state<Agent | null>(null), error = $state('');
  let owner = $derived($auth.status === 'authenticated' ? $auth.user?.email : null);
  let selectedAgent = $derived(agentId);
  $effect(() => {
    const account = owner;
    const id = selectedAgent;
    let active = true;
    conversation = null; agent = null; error = '';
    if (account) void (async () => {
      try {
        const available = await api.agents.detail(id);
        if (!active) return;
        if (available.is_system || available.disabled) { error = 'This agent is unavailable.'; return; }
        const direct = await api.conversations.resolve({ agent_id: id, scope: 'agent_direct' });
        if (!active) return;
        agent = available; conversation = direct;
        postNative('panel-state', { agentId: id, conversationId: direct.conversation_id });
      } catch (failure) { if (active) error = asApiError(failure).message; }
    })();
    return () => { active = false; };
  });
  $effect(() => {
    if (conversation && $nativeVisibility.visible && $nativeVisibility.focused) {
      void api.conversations.markRead(conversation.conversation_id);
    }
  });
</script>

<section class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-control-border bg-surface text-foreground" aria-label="Agent conversation">
  <header class="flex items-center gap-2 border-b border-separator bg-chrome p-3 text-foreground">
    <span class="min-w-0 flex-1 truncate">{agent?.display_name ?? agent?.name ?? 'Conversation'}</span>
    {#if conversation}<button type="button" class="text-sm text-accent hover:text-accent-hover" onclick={() => postNative('open-window', { conversationId: conversation!.conversation_id })}>Open conversation</button>{/if}
    <button type="button" aria-label="Close chat" onclick={() => postNative('dismiss', {})}>Close</button>
  </header>
  {#if error}<p role="alert" class="p-4 text-danger">{error}</p>
  {:else if conversation && agent}
    {#key conversation.conversation_id}<CompactConversationChat conversationId={conversation.conversation_id} {agent} {conversation}
      composerFocusRequest={$nativeVisibility.visible && $nativeVisibility.focused && $nativeComposerFocus?.agentId === agentId
        ? $nativeComposerFocus?.request : null}
      embedded readOnly={Boolean(conversation.is_readonly_for_caller)} />{/key}
  {:else}<p class="p-4 text-muted">Opening conversation…</p>{/if}
</section>
