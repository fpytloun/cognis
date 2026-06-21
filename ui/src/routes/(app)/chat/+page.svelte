<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { api } from '$lib/api/client';
  import {
    CHAT_STORAGE_KEYS,
    lastOpenedConversationStorageKey
  } from '$lib/chat-page';
  import type { Agent } from '$lib/types/api';

  let loading = true;
  let error = '';
  let noAgents = false;

  function getSelectedAgentId(agents: Agent[]): string {
    const primary = agents.filter((a) => a.agent_type === 'primary');
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
      if (stored && primary.some((a) => a.agent_id === stored && a.status === 'active')) {
        return stored;
      }
    }
    return primary.find((a) => a.status === 'active')?.agent_id ?? primary[0]?.agent_id ?? '';
  }

  function lastOpenedConversationCandidates(selectedAgentId: string): string[] {
    if (typeof window === 'undefined') return [];
    const agentKey = lastOpenedConversationStorageKey(selectedAgentId);
    return [
      window.sessionStorage.getItem(agentKey),
      window.localStorage.getItem(agentKey),
      window.sessionStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation),
      window.localStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation),
    ].filter((value, index, values): value is string => (
      typeof value === 'string'
      && value.length > 0
      && values.indexOf(value) === index
    ));
  }

  function rememberOpenedConversation(agentId: string, conversationId: string): void {
    if (typeof window === 'undefined') return;
    window.sessionStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, conversationId);
    window.sessionStorage.setItem(lastOpenedConversationStorageKey(agentId), conversationId);
    window.localStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, conversationId);
    window.localStorage.setItem(lastOpenedConversationStorageKey(agentId), conversationId);
  }

  onMount(() => {
    void (async () => {
      try {
        const agents = await api.agents.listAll();
        const agentId = getSelectedAgentId(agents);
        if (!agentId) {
          noAgents = true;
          loading = false;
          return;
        }

        const conversation = await api.conversations.open({
          agent_id: agentId,
          context_type: 'web',
          candidate_conversation_ids: lastOpenedConversationCandidates(agentId),
        });
        rememberOpenedConversation(conversation.agent_id, conversation.conversation_id);
        await goto(`/chat/${conversation.conversation_id}`, { replaceState: true });
      } catch (caughtError) {
        error = caughtError instanceof Error ? caughtError.message : 'Unable to load conversations.';
      } finally {
        loading = false;
      }
    })();
  });
</script>

<svelte:head>
  <title>Chat · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Opening chat" description="Resolving your default conversation." />
{:else if noAgents}
  <section class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-6 py-10 text-center text-sm text-sky-100">
    <p>Create an agent first before starting a conversation.</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
    </div>
  </section>
{:else if error}
  <section class="rounded-3xl border border-rose-500/30 bg-rose-500/10 px-6 py-10 text-center text-sm text-rose-100">
    <p>{error}</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
      <Button variant="secondary" onclick={() => goto('/chat/new')}>Start a new conversation</Button>
    </div>
  </section>
{/if}
