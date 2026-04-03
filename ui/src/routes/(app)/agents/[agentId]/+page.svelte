<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { agentToFormState } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, IntarisMCPServer, LLMProvider, MCPServerTestResponse, SecretMetadata, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let agent = $state<Agent | null>(null);
  let tools = $state<ToolDefinitionSummary[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let secondaryAgents = $state<Agent[]>([]);
  let secondaryBindings = $state<string[]>([]);
  let mcpTesting = $state(false);
  let mcpTestResult = $state<MCPServerTestResponse | null>(null);
  let form = $state(agentToFormState({
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
    personality_synced: true,
    personality_sync_error: null,
    personality_sync_checked_at: null,
    avatar_url: null,
    agent_type: 'primary',
    is_system: false,
    hidden: false,
    status: 'draft',
    created_at: null,
    updated_at: null
  }));
  let initialSnapshot = '';

  function agentIdFromRoute(): string {
    return $page.params.agentId ?? '';
  }

  function isDirty(): boolean {
    return JSON.stringify($state.snapshot(form)) !== initialSnapshot;
  }

  beforeNavigate((navigation) => {
    if (saving) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function loadAgent(): Promise<void> {
    loading = true;
    try {
      [agent, tools, workflows, secrets, intarisMcpServers, secondaryAgents, secondaryBindings] = await Promise.all([
        api.agents.detail(agentIdFromRoute()),
        api.tools.list(),
        api.workflows.listAll(),
        api.secrets.list(),
        api.tools.intarisMcpServers().catch(() => []),
        api.agents.listAll({ agent_type: 'secondary' }),
        api.agents.listBindings(agentIdFromRoute()).catch(() => []),
      ]);
      try {
        providers = (await api.llmProviders.list()).items;
      } catch {
        providers = [];
      }
      Object.assign(form, agentToFormState(agent));
      initialSnapshot = JSON.stringify($state.snapshot(form));
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
      addToast('Agent updated.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save agent');
    } finally {
      saving = false;
    }
  }

  async function testMcp(): Promise<void> {
    mcpTesting = true;
    error = '';
    try {
      mcpTestResult = await api.tools.testAgentMcp(agentIdFromRoute());
      addToast(mcpTestResult.ok ? 'MCP discovery succeeded.' : 'MCP discovery finished with issues.', mcpTestResult.ok ? 'success' : 'warning');
      if (mcpTestResult.ok) {
        const discoveredTools = mcpTestResult.items.flatMap((item) =>
          item.tools.map((toolName) => ({
            name: toolName,
            description: 'Discovered MCP tool',
            parameters: {},
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
      addToast(error, 'error', 4_000, 'Unable to test MCP');
    } finally {
      mcpTesting = false;
    }
  }

  async function retrySyncPersonality(): Promise<void> {
    error = '';
    try {
      await api.agents.syncPersonality(agentIdFromRoute());
      await loadAgent();
      addToast('Personality synced.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to sync personality');
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadAgent();
    return cleanup;
  });
</script>

<svelte:head>
  <title>{agent ? `${agent.display_name ?? agent.name} · Agent · Cognis` : 'Agent · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading agent" description="Fetching the agent definition, tools, workflows, and LLM options." />
{:else}
  <section class="space-y-5">
    <div class="space-y-3">
      <Button size="sm" variant="secondary" onclick={() => goto('/agents')}>Back to agents</Button>
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Agent editor</p>
      <h1 class="mt-1 text-2xl font-semibold text-white">{agent?.name ?? 'Agent'}</h1>
    </div>
    {#if agent && !agent.personality_synced && agent.agent_type === 'primary' && !agent.is_system}
      <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-4 text-sm text-amber-100">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="font-medium">Personality was not synced to Mnemory.</p>
            <p class="mt-1 text-amber-50/90">{agent.personality_sync_error ?? 'Retry the sync to bootstrap this agent into memory.'}</p>
          </div>
          <Button size="sm" variant="secondary" onclick={retrySyncPersonality}>Retry sync</Button>
        </div>
      </div>
    {/if}
    <AgentForm
      mode="edit"
      {form}
      {tools}
      {workflows}
      {providers}
      {secrets}
      {intarisMcpServers}
      {secondaryAgents}
      {secondaryBindings}
      {saving}
      {error}
      readonly={agent?.is_system ?? false}
      onSave={saveAgent}
      onTestMcp={testMcp}
      onBindingsChange={async (bindings) => {
        try {
          await api.agents.replaceBindings(agentIdFromRoute(), bindings);
          secondaryBindings = bindings;
        } catch (caughtError) {
          error = asApiError(caughtError).message;
          addToast(error, 'error', 4_000, 'Unable to update bindings');
        }
      }}
      {mcpTesting}
      {mcpTestResult}
    />
  </section>
{/if}
