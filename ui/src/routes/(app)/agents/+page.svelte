<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Workflow } from '$lib/types/api';

  let loading = true;
  let error = '';
  let agents: Agent[] = [];
  let workflows: Workflow[] = [];
  let primaryExpanded = true;
  let secondaryExpanded = true;

  $: primaryAgents = agents.filter((a) => a.agent_type === 'primary');
  $: secondaryAgents = agents.filter((a) => a.agent_type === 'secondary');

  async function loadAgents(): Promise<void> {
    loading = true;
    error = '';
    try {
      [agents, workflows] = await Promise.all([api.agents.listAll(), api.workflows.listAll()]);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  function defaultWorkflowLabel(agent: Agent): string {
    const workflowId = typeof agent.execution?.default_workflow_id === 'string' ? agent.execution.default_workflow_id : null;
    return workflows.find((workflow) => workflow.workflow_id === workflowId)?.name ?? workflowId ?? 'automatic';
  }

  async function toggleStatus(agent: Agent): Promise<void> {
    try {
      if (agent.status === 'active') {
        await api.agents.suspend(agent.agent_id);
      } else {
        await api.agents.activate(agent.agent_id);
      }
      await loadAgents();
      addToast(`Agent ${agent.status === 'active' ? 'suspended' : 'activated'}.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update agent status');
    }
  }

  async function syncPersonality(agent: Agent): Promise<void> {
    try {
      await api.agents.syncPersonality(agent.agent_id);
      addToast('Personality sync requested.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to sync personality');
    }
  }

  onMount(() => {
    void loadAgents();
  });
</script>

<svelte:head>
  <title>Agents · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading agents" description="Fetching your agent definitions and workflow defaults." />
{:else}
  <section class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Agent management</p>
        <h1 class="mt-1 text-2xl font-semibold text-white">Agents</h1>
      </div>
      <Button onclick={() => goto('/agents/new')}>Create agent</Button>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <!-- Primary Agents -->
    <div>
      <button
        class="flex w-full items-center gap-2 text-left"
        onclick={() => (primaryExpanded = !primaryExpanded)}
      >
        <svg
          class="h-4 w-4 text-slate-400 transition-transform {primaryExpanded ? 'rotate-0' : '-rotate-90'}"
          fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
        <h2 class="text-lg font-semibold text-white">Primary Agents</h2>
        <span class="rounded-full bg-slate-700/60 px-2 py-0.5 text-xs text-slate-300">{primaryAgents.length}</span>
      </button>

      {#if primaryExpanded}
        <div class="mt-3 grid gap-4 xl:grid-cols-2">
          {#each primaryAgents as agent}
            {@render agentCard(agent)}
          {:else}
            <p class="text-sm text-slate-500">No primary agents yet. Create one to get started.</p>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Secondary Agents -->
    <div>
      <button
        class="flex w-full items-center gap-2 text-left"
        onclick={() => (secondaryExpanded = !secondaryExpanded)}
      >
        <svg
          class="h-4 w-4 text-slate-400 transition-transform {secondaryExpanded ? 'rotate-0' : '-rotate-90'}"
          fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
        <h2 class="text-lg font-semibold text-white">Secondary Agents</h2>
        <span class="rounded-full bg-slate-700/60 px-2 py-0.5 text-xs text-slate-300">{secondaryAgents.length}</span>
      </button>

      {#if secondaryExpanded}
        <div class="mt-3 grid gap-4 xl:grid-cols-2">
          {#each secondaryAgents as agent}
            {@render agentCard(agent)}
          {:else}
            <p class="text-sm text-slate-500">No secondary agents.</p>
          {/each}
        </div>
      {/if}
    </div>
  </section>
{/if}

{#snippet agentCard(agent: Agent)}
  <Card class="p-5">
    <div class="flex items-start justify-between gap-4">
      <div class="flex items-start gap-4">
        <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} />
        <div>
          <div class="flex items-center gap-2">
            <h2 class="text-lg font-semibold text-white">{agent.display_name ?? agent.name}</h2>
            {#if agent.is_system}
              <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.15em] text-sky-300">System</span>
            {/if}
          </div>
          <p class="text-sm text-slate-400">{agent.agent_id}</p>
          <p class="mt-3 text-sm leading-6 text-slate-300">{agent.description ?? 'No description yet.'}</p>
        </div>
      </div>
      <span class="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-medium uppercase tracking-[0.2em] text-slate-200">
        {agent.status}
      </span>
    </div>

    {#if agent.agent_type === 'primary'}
      <dl class="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-2">
        <div>
          <dt class="text-xs uppercase tracking-[0.2em] text-slate-500">Default workflow</dt>
          <dd class="mt-1">{defaultWorkflowLabel(agent)}</dd>
        </div>
        <div>
          <dt class="text-xs uppercase tracking-[0.2em] text-slate-500">Model</dt>
          <dd class="mt-1">{typeof agent.llm_config?.model === 'string' ? agent.llm_config.model : 'inherit'}</dd>
        </div>
      </dl>
    {:else}
      <dl class="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-2">
        <div>
          <dt class="text-xs uppercase tracking-[0.2em] text-slate-500">Model</dt>
          <dd class="mt-1">{typeof agent.llm_config?.model === 'string' ? agent.llm_config.model : 'inherit from caller'}</dd>
        </div>
        <div>
          <dt class="text-xs uppercase tracking-[0.2em] text-slate-500">Tools</dt>
          <dd class="mt-1">{Array.isArray((agent.tools as Record<string, unknown>)?.builtin_tools) ? ((agent.tools as Record<string, unknown>).builtin_tools as string[]).join(', ') : 'default'}</dd>
        </div>
      </dl>
    {/if}

    {#if !agent.personality_synced && agent.agent_type === 'primary'}
      <div class="mt-4 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
        <p class="font-medium">Personality was not synced to Mnemory.</p>
        <p class="mt-1 text-amber-50/90">{agent.personality_sync_error ?? 'Retry the sync to bootstrap this agent into memory.'}</p>
      </div>
    {/if}

    <div class="mt-5 flex flex-wrap gap-2">
      {#if !agent.is_system}
        <Button size="sm" variant="secondary" onclick={() => goto(`/agents/${agent.agent_id}`)}>Open</Button>
        <Button size="sm" variant="secondary" onclick={() => toggleStatus(agent)}>{agent.status === 'active' ? 'Suspend' : 'Activate'}</Button>
        {#if agent.agent_type === 'primary'}
          <Button size="sm" variant="secondary" onclick={() => syncPersonality(agent)}>Sync personality</Button>
        {/if}
      {:else}
        <Button size="sm" variant="secondary" onclick={() => goto(`/agents/${agent.agent_id}`)}>View</Button>
      {/if}
    </div>
  </Card>
{/snippet}
