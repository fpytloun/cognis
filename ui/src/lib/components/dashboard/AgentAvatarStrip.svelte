<script lang="ts">
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
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
    if (agent.status === 'active') return 'bg-emerald-400';
    if (agent.status === 'suspended') return 'bg-amber-400';
    return 'bg-slate-500';
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
        <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} class="h-12 w-12 transition group-hover:ring-2 group-hover:ring-sky-400" />
        <span class={`absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-slate-950 ${statusDotClass(agent)}`} aria-hidden="true"></span>
      </span>
      <span class="max-w-[4.5rem] truncate text-[11px] text-slate-300">{agent.display_name ?? agent.name}</span>
    </button>
  {/each}
</div>
{/if}
