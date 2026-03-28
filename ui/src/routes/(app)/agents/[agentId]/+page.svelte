<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { agentToFormState } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import type { Agent, LLMProvider, MCPServerTestResponse, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  let loading = true;
  let saving = false;
  let error = '';
  let agent: Agent | null = null;
  let tools: ToolDefinitionSummary[] = [];
  let workflows: Workflow[] = [];
  let providers: LLMProvider[] = [];
  let mcpTesting = false;
  let mcpTestResult: MCPServerTestResponse | null = null;
  let form = agentToFormState({
    agent_id: '',
    owner_email: '',
    name: '',
    display_name: null,
    description: null,
    system_prompt: null,
    personality: null,
    skills: null,
    tools: null,
    permissions: null,
    llm_config: null,
    execution: null,
    avatar_url: null,
    status: 'draft',
    created_at: null,
    updated_at: null
  });

  function agentIdFromRoute(): string {
    return $page.params.agentId ?? '';
  }

  async function loadAgent(): Promise<void> {
    loading = true;
    try {
      [agent, tools, workflows] = await Promise.all([
        api.agents.detail(agentIdFromRoute()),
        api.tools.list(),
        api.workflows.listAll()
      ]);
      try {
        providers = (await api.llmProviders.list()).items;
      } catch {
        providers = [];
      }
      form = agentToFormState(agent);
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
      await api.agents.update(agentIdFromRoute(), payload);
      await loadAgent();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      saving = false;
    }
  }

  async function testMcp(): Promise<void> {
    mcpTesting = true;
    error = '';
    try {
      mcpTestResult = await api.tools.testAgentMcp(agentIdFromRoute());
      if (mcpTestResult.ok) {
        const discoveredTools = mcpTestResult.items.flatMap((item) =>
          item.tools.map((toolName) => ({
            name: toolName,
            description: 'Discovered MCP tool',
            category: 'mcp',
            read_only: false,
            source: { type: 'local_mcp', server_name: item.name },
            timeout_seconds: 30,
            non_bypassable: false
          }))
        );
        const merged = new Map(tools.map((tool) => [tool.name, tool]));
        for (const tool of discoveredTools) {
          merged.set(tool.name, tool);
        }
        tools = Array.from(merged.values());
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      mcpTesting = false;
    }
  }

  onMount(() => {
    void loadAgent();
  });
</script>

<svelte:head>
  <title>{agent ? `${agent.display_name ?? agent.name} · Agent · Cognis` : 'Agent · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading agent" description="Fetching the agent definition, tools, workflows, and LLM options." />
{:else}
  <section class="space-y-5">
    <div>
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Agent editor</p>
      <h1 class="mt-1 text-2xl font-semibold text-white">{agent?.display_name ?? agent?.name ?? 'Agent'}</h1>
    </div>
    <AgentForm mode="edit" {form} {tools} {workflows} {providers} {saving} {error} onSave={saveAgent} onTestMcp={testMcp} {mcpTesting} {mcpTestResult} />
  </section>
{/if}
