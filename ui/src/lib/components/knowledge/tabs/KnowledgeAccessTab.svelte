<script lang="ts">
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import KnowledgePeopleAccess from '$lib/components/knowledge/KnowledgePeopleAccess.svelte';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, KnowledgebaseModel } from '$lib/types/api';

  let { kb }: { kb: KnowledgebaseModel } = $props();

  let loading = $state(true);
  let error = $state('');
  let agents = $state<Agent[]>([]);
  let assignedAgentIds = $state<string[]>([]);
  let pendingAgentIds = $state(new Set<string>());

  async function load(): Promise<void> {
    loading = true;
    error = '';
    try {
      const [agentList, assignments] = await Promise.all([
        api.agents.listAll(),
        api.knowledgebases.agentAssignments(kb.knowledgebase_id)
      ]);
      agents = agentList.filter((agent) => !agent.is_system);
      assignedAgentIds = assignments;
    } catch (err) {
      error = asApiError(err).message;
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function toggleAgent(agentId: string, assign: boolean): Promise<void> {
    const next = new Set(pendingAgentIds);
    next.add(agentId);
    pendingAgentIds = next;
    try {
      if (assign) {
        await api.knowledgebases.assignAgent(kb.knowledgebase_id, agentId);
        assignedAgentIds = [...assignedAgentIds, agentId];
      } else {
        await api.knowledgebases.unassignAgent(kb.knowledgebase_id, agentId);
        assignedAgentIds = assignedAgentIds.filter((id) => id !== agentId);
      }
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      const cleared = new Set(pendingAgentIds);
      cleared.delete(agentId);
      pendingAgentIds = cleared;
    }
  }
</script>

<div class="flex flex-col gap-4">
  <div>
    <h2 class="text-base font-semibold text-white">Access</h2>
    <p class="mt-1 text-sm text-slate-400">
      Manage direct people access separately from agent-context assignments.
    </p>
  </div>

  <KnowledgePeopleAccess {kb} disabled={kb.status === 'archived'} />

  <div class="border-t border-slate-800 pt-4">
    <h3 class="text-sm font-semibold text-white">Agents</h3>
    <p class="mt-1 text-sm text-slate-400">
      Agent assignments only make this knowledgebase available inside those agents' runtime context. They do not grant a person access.
    </p>
  </div>

  {#if loading}
    <LoadingState label="Loading agents…" />
  {:else if error}
    <div class="rounded-2xl border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-300" role="alert">{error}</div>
  {:else if agents.length === 0}
    <p class="rounded-2xl border border-dashed border-slate-800/80 px-4 py-8 text-center text-sm text-slate-400">
      No eligible agents found.
    </p>
  {:else}
    <ul class="flex flex-col gap-2">
      {#each agents as agent (agent.agent_id)}
        {@const assigned = assignedAgentIds.includes(agent.agent_id)}
        <li class="flex items-center justify-between gap-3 rounded-xl border border-slate-800/80 bg-slate-900/60 px-4 py-3">
          <div class="min-w-0">
            <p class="truncate text-sm font-medium text-white">{agent.name}</p>
            <p class="text-xs text-slate-500">{assigned ? 'Shared with this agent' : 'Not shared'}</p>
          </div>
          <label class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center">
            <input
              type="checkbox"
              class="peer sr-only"
              checked={assigned}
               disabled={kb.status === 'archived' || pendingAgentIds.has(agent.agent_id)}
              onchange={(event) => toggleAgent(agent.agent_id, (event.currentTarget as HTMLInputElement).checked)}
              aria-label={`Share this knowledgebase with ${agent.name}`}
            />
            <span class="absolute inset-0 rounded-full bg-slate-700 transition peer-checked:bg-sky-500"></span>
            <span class="absolute left-1 h-4 w-4 rounded-full bg-white transition peer-checked:translate-x-5"></span>
          </label>
        </li>
      {/each}
    </ul>
  {/if}
</div>
