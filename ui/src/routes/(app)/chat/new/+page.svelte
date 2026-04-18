<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { api } from '$lib/api/client';
  import { CHAT_STORAGE_KEYS, isRestorableChatConversation } from '$lib/chat-page';
  import type { Agent, HealthResponse } from '$lib/types/api';

  let loading = true;
  let error = '';
  let creating = false;
  let agents: Agent[] = [];
  let selectedAgentId = '';
  let health: HealthResponse | null = null;

  function isLlmUnavailableForSetup(): boolean {
    const llmDetails = JSON.stringify(health?.providers?.llm ?? {}).toLowerCase();
    return llmDetails.includes('no llm model configured') || llmDetails.includes('not configured');
  }

  async function resolveInitialAgentId(allAgents: Agent[]): Promise<string> {
    const primary = allAgents.filter((agent) => agent.agent_type === 'primary' && agent.status === 'active');
    const requestedAgentId = $page.url.searchParams.get('agent_id');
    if (requestedAgentId && primary.some((agent) => agent.agent_id === requestedAgentId)) {
      return requestedAgentId;
    }

    if (typeof window !== 'undefined') {
      const storedAgentId = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
      if (storedAgentId && primary.some((agent) => agent.agent_id === storedAgentId)) {
        return storedAgentId;
      }

      const lastOpenedConversationId = window.localStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
      if (lastOpenedConversationId) {
        try {
          const conversation = await api.conversations.detail(lastOpenedConversationId);
          if (isRestorableChatConversation(conversation) && primary.some((agent) => agent.agent_id === conversation.agent_id)) {
            return conversation.agent_id;
          }
          window.localStorage.removeItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
        } catch {
          window.localStorage.removeItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
        }
      }
    }

    return primary[0]?.agent_id ?? '';
  }

  async function createConversation(): Promise<void> {
    if (!selectedAgentId) {
      error = 'Create an agent first before starting a conversation.';
      return;
    }

    if (isLlmUnavailableForSetup()) {
      error = 'Configure an LLM provider before starting a conversation.';
      return;
    }

    creating = true;
    error = '';
    try {
      const conversation = await api.conversations.create({
        agent_id: selectedAgentId,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      });

      if (typeof window !== 'undefined') {
        window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedAgent, selectedAgentId);
      }

      await goto(`/chat/${conversation.conversation_id}`, { replaceState: true });
    } catch (caughtError) {
      error = caughtError instanceof Error ? caughtError.message : 'Unable to start a new conversation.';
    } finally {
      creating = false;
    }
  }

  async function cancel(): Promise<void> {
    await goto('/chat', { replaceState: true });
  }

  onMount(() => {
    void (async () => {
      try {
        health = await api.system.health();
        agents = await api.agents.listAll();
        selectedAgentId = await resolveInitialAgentId(agents);
      } catch (caughtError) {
        error = caughtError instanceof Error ? caughtError.message : 'Unable to prepare a new conversation.';
      } finally {
        loading = false;
      }
    })();
  });
</script>

<svelte:head>
  <title>New Chat · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Preparing new chat" description="Loading agents and your recent chat context." />
{:else}
  <NewChatModal
    {agents}
    bind:selectedAgentId
    title="Start a new chat"
    description="Pick the primary agent for this new web conversation. The current filter or your last opened conversation is preselected when possible."
    confirmLabel="Create conversation"
    busy={creating}
    {error}
    oncancel={() => void cancel()}
    onconfirm={() => void createConversation()}
  />
{/if}
