<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import Button from '$lib/components/ui/Button.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { api } from '$lib/api/client';
  import type { Agent, HealthResponse } from '$lib/types/api';

  let loading = true;
  let error = '';
  let agents: Agent[] = [];
  let health: HealthResponse | null = null;

  function isLlmUnavailableForSetup(): boolean {
    const llmDetails = JSON.stringify(health?.providers?.llm ?? {}).toLowerCase();
    return llmDetails.includes('no llm model configured') || llmDetails.includes('not configured');
  }

  function getSelectedAgentId(): string {
    const primary = agents.filter((a) => a.agent_type === 'primary');
    const requestedAgentId = $page.url.searchParams.get('agent_id');
    if (requestedAgentId && primary.some((a) => a.agent_id === requestedAgentId)) {
      return requestedAgentId;
    }
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem('cognis-chat-selected-agent');
      if (stored && primary.some((a) => a.agent_id === stored && a.status === 'active')) {
        return stored;
      }
    }
    return primary.find((a) => a.status === 'active')?.agent_id ?? primary[0]?.agent_id ?? '';
  }

  async function createConversation(): Promise<void> {
    const agentId = getSelectedAgentId();
    if (!agentId) {
      error = 'Create an agent first before starting a conversation.';
      loading = false;
      return;
    }

    if (isLlmUnavailableForSetup()) {
      error = 'Configure an LLM provider before starting a conversation.';
      loading = false;
      return;
    }

    const conversation = await api.conversations.create({
      agent_id: agentId,
      context: {
        type: 'web',
        ref: null,
        platform_data: {},
        memory_labels: {}
      }
    });

    // Persist the agent choice
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('cognis-chat-selected-agent', agentId);
    }

    await goto(`/chat/${conversation.conversation_id}`, { replaceState: true });
  }

  onMount(() => {
    void (async () => {
      try {
        health = await api.system.health();
        agents = await api.agents.listAll();
        await createConversation();
      } catch (caughtError) {
        error = caughtError instanceof Error ? caughtError.message : 'Unable to start a new conversation.';
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
  <LoadingState label="Starting conversation" description="Creating a new web conversation for your selected agent." />
{:else if error}
  <section class="rounded-3xl border border-amber-500/30 bg-amber-500/10 px-6 py-10 text-center text-sm text-amber-100">
    <p>{error}</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
      <Button variant="secondary" onclick={() => goto('/chat')}>Back to chat</Button>
    </div>
  </section>
{/if}
