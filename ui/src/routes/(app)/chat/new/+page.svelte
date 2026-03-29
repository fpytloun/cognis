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

  async function createConversation(): Promise<void> {
    const requestedAgentId = $page.url.searchParams.get('agent_id');
    const selectedAgent =
      agents.find((agent) => agent.agent_id === requestedAgentId) ??
      agents.find((agent) => agent.status === 'active') ??
      agents[0];

    if (!selectedAgent) {
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
      agent_id: selectedAgent.agent_id,
      context: {
        type: 'web',
        ref: null,
        platform_data: {},
        memory_labels: {}
      }
    });
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
  <LoadingState label="Starting conversation" description="Creating a new root conversation and session for your selected agent." />
{:else if error}
  <section class="rounded-3xl border border-amber-500/30 bg-amber-500/10 px-6 py-10 text-center text-sm text-amber-100">
    <p>{error}</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
      <Button variant="secondary" onclick={() => goto('/chat')}>Back to chat</Button>
    </div>
  </section>
{/if}
