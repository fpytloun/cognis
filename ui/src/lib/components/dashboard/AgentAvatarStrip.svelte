<script lang="ts">
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import MessageCircle from 'lucide-svelte/icons/message-circle';
  import type { Agent } from '$lib/types/api';

  let {
    agents,
    onSelectAgent
  } = $props<{
    agents: Agent[];
    onSelectAgent: (agent: Agent) => void;
  }>();

  const strip = $derived(
    agents
      .filter((agent: Agent) => !agent.hidden && !agent.disabled && !agent.is_system)
      .sort((left: Agent, right: Agent) => (left.display_name ?? left.name).localeCompare(right.display_name ?? right.name))
  );

  function statusDotClass(agent: Agent): string {
    if (agent.status === 'active') return 'bg-success';
    if (agent.status === 'suspended') return 'bg-warning';
    return 'bg-muted';
  }
</script>

{#if strip.length > 0}
<div class="flex min-w-0 max-w-full items-center gap-3 overflow-x-auto pb-1" data-testid="dashboard-agent-strip" aria-label="Agents">
  {#each strip as agent (agent.agent_id)}
    <button
      type="button"
      class="group flex shrink-0 flex-col items-center gap-1"
      data-testid={`dashboard-agent-avatar-${agent.agent_id}`}
      aria-label={`Chat with ${agent.display_name ?? agent.name}`}
      onclick={() => onSelectAgent(agent)}
    >
      <span class="relative">
        <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} class="h-12 w-12 transition group-hover:ring-2 group-hover:ring-accent" />
        {#if agent.is_contact_only}
          <span class="absolute -bottom-0.5 -right-0.5 rounded-full bg-surface p-0.5 text-accent" title="Incoming channel"><MessageCircle class="h-3.5 w-3.5" /></span>
        {:else}
          <span class={`absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-separator ${statusDotClass(agent)}`} aria-hidden="true"></span>
        {/if}
      </span>
      <span class="max-w-[4.5rem] truncate text-[11px] text-secondary">{agent.display_name ?? agent.name}</span>
    </button>
  {/each}
</div>
{/if}
