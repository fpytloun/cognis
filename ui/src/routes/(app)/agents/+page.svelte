<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { clearPersistedScroll } from '$lib/actions/scrollPersist';
  import { api, asApiError } from '$lib/api/client';
  import { confirmAction } from '$lib/stores/confirm';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Workflow } from '$lib/types/api';

  // Expanded-group state survives tab switches via sessionStorage.
  // Falls back to expanded-by-default on first visit and when storage is
  // unavailable (e.g. Safari private mode).
  const AGENTS_PRIMARY_EXPANDED_KEY = 'cognis-agents:primaryExpanded';
  const AGENTS_SECONDARY_EXPANDED_KEY = 'cognis-agents:secondaryExpanded';

  function readExpanded(key: string, fallback: boolean): boolean {
    if (typeof sessionStorage === 'undefined') return fallback;
    try {
      const raw = sessionStorage.getItem(key);
      if (raw === null) return fallback;
      return raw === '1';
    } catch {
      return fallback;
    }
  }

  function writeExpanded(key: string, value: boolean): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem(key, value ? '1' : '0');
    } catch {
      // non-fatal
    }
  }

  let loading = true;
  let error = '';
  let agents: Agent[] = [];
  let workflows: Workflow[] = [];
  let primaryExpanded = readExpanded(AGENTS_PRIMARY_EXPANDED_KEY, true);
  let secondaryExpanded = readExpanded(AGENTS_SECONDARY_EXPANDED_KEY, true);
  let lightboxUrl: string | null = null;
  let lightboxAlt = '';

  // Persist expanded state whenever it changes so the next mount picks
  // it up. `$:` blocks run on every reactive update.
  $: writeExpanded(AGENTS_PRIMARY_EXPANDED_KEY, primaryExpanded);
  $: writeExpanded(AGENTS_SECONDARY_EXPANDED_KEY, secondaryExpanded);

  $: primaryAgents = agents.filter((a) => a.agent_type === 'primary');
  $: secondaryAgents = agents.filter((a) => a.agent_type === 'secondary');

  async function loadAgents(): Promise<void> {
    loading = true;
    error = '';
    try {
      [agents, workflows] = await Promise.all([
        api.agents.listAll({ include_disabled: true }),
        api.workflows.listAll({ include_disabled: true })
      ]);
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
    const confirmed = await confirmAction({
      title: 'Sync personality to Mnemory?',
      message: 'This will re-bootstrap the agent personality in Mnemory. If the agent has evolved its identity through conversations, this may override those changes.',
      confirmLabel: 'Sync',
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await api.agents.syncPersonality(agent.agent_id);
      addToast('Personality synced.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to sync personality');
    }
  }

  async function duplicateAgent(agent: Agent): Promise<void> {
    try {
      const duplicated = await api.agents.duplicate(agent.agent_id);
      addToast('Agent duplicated.', 'success');
      await goto(`/agents/${duplicated.agent_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to duplicate agent');
    }
  }

  onMount(() => {
    void loadAgents();

    // Same-tab tap resets the accordion and scrolls to the top.
    const unsubTabReset = onTabReset('/agents', () => {
      primaryExpanded = true;
      secondaryExpanded = true;
      clearPersistedScroll('/agents');
      const el = document.querySelector<HTMLElement>('[data-app-content="true"]');
      if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
    });

    return () => {
      unsubTabReset();
    };
  });
</script>

<svelte:head>
  <title>Agents · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading agents" description="Fetching your agent definitions and workflow defaults." />
{:else}
  <section class="space-y-5 overflow-x-hidden">
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
        <div class="mt-3 grid gap-4 lg:grid-cols-2">
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
        <div class="mt-3 grid gap-4 lg:grid-cols-2">
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
  <Card class="p-4 sm:p-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div class="flex min-w-0 flex-1 items-start gap-4">
        {#if agent.avatar_url}
          <button type="button" class="shrink-0 cursor-pointer" onclick={() => { lightboxUrl = agent.avatar_url; lightboxAlt = agent.display_name ?? agent.name; }}>
            <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} />
          </button>
        {:else}
          <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={null} />
        {/if}
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <h2 class="truncate text-lg font-semibold text-white">{agent.display_name ?? agent.name}</h2>
            {#if agent.is_system}
              <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.15em] text-sky-300">System</span>
            {/if}
            {#if agent.is_shared_with_me}
              <span class="rounded-full border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.15em] text-cyan-300">Shared</span>
            {/if}
          </div>
          <p class="break-all text-sm text-slate-400">{agent.agent_id}</p>
          {#if agent.is_shared_with_me}
            <p class="mt-1 text-xs text-cyan-300/80">Shared by {agent.shared_by_email ?? agent.owner_email}</p>
          {/if}
          <p class="mt-3 text-sm leading-6 text-slate-300">{agent.description ?? 'No description yet.'}</p>
        </div>
      </div>
      <span class="shrink-0 rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-medium uppercase tracking-[0.2em] text-slate-200">
        {agent.disabled ? 'disabled' : agent.status}
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
      <div class="mt-4 rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
        <p class="font-medium">Personality was not synced to Mnemory.</p>
        <p class="mt-1 text-sky-50/90">{agent.personality_sync_error ?? 'Retry the sync to bootstrap this agent into memory.'}</p>
      </div>
    {/if}

    <div class="mt-5 flex flex-wrap gap-2">
      {#if agent.is_shared_with_me}
        <Button size="sm" variant="secondary" onclick={() => goto(`/agents/${agent.agent_id}`)}>View</Button>
      {:else if !agent.is_system}
        <Button size="sm" variant="secondary" onclick={() => goto(`/agents/${agent.agent_id}`)}>Edit</Button>
        <Button size="sm" variant="secondary" onclick={() => toggleStatus(agent)}>{agent.status === 'active' ? 'Suspend' : 'Activate'}</Button>
        {#if agent.agent_type === 'primary'}
          <Button size="sm" variant="secondary" onclick={() => syncPersonality(agent)}>Sync personality</Button>
        {/if}
      {:else}
        <Button size="sm" variant="secondary" onclick={() => goto(`/agents/${agent.agent_id}`)}>{agent.has_overrides || agent.disabled ? 'Configure' : 'View'}</Button>
        <Button size="sm" variant="secondary" onclick={() => duplicateAgent(agent)}>Duplicate</Button>
      {/if}
    </div>
  </Card>
{/snippet}

{#if lightboxUrl}
  <ImageLightbox src={lightboxUrl} alt={lightboxAlt} onClose={() => { lightboxUrl = null; }} />
{/if}
