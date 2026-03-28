<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { createEmptyAgentForm } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import type { LLMProvider, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  const form = createEmptyAgentForm();
  let loading = true;
  let saving = false;
  let error = '';
  let tools: ToolDefinitionSummary[] = [];
  let workflows: Workflow[] = [];
  let providers: LLMProvider[] = [];

  async function loadOptions(): Promise<void> {
    loading = true;
    try {
      [tools, workflows] = await Promise.all([api.tools.list(), api.workflows.listAll()]);
      try {
        providers = (await api.llmProviders.list()).items;
      } catch {
        providers = [];
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function saveAgent(payload: Record<string, unknown>): Promise<void> {
    saving = true;
    error = '';
    try {
      const agent = await api.agents.create(payload);
      await goto(`/agents/${agent.agent_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      saving = false;
    }
  }

  onMount(() => {
    void loadOptions();
  });
</script>

<svelte:head>
  <title>New Agent · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Preparing agent editor" description="Loading tools, workflows, and available providers." />
{:else}
  <section class="space-y-5">
    <div>
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Agent creator</p>
      <h1 class="mt-1 text-2xl font-semibold text-white">Create agent</h1>
    </div>
    <AgentForm mode="create" {form} {tools} {workflows} {providers} {saving} {error} onSave={saveAgent} />
  </section>
{/if}
