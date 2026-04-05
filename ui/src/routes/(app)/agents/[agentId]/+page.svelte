<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount, tick } from 'svelte';

  import { agentToFormState, formStateToEffectiveToolsPreviewPayload } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, EffectiveToolItem, ExecutorConfig, IntarisMCPServer, LLMProvider, SecretMetadata, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let agent = $state<Agent | null>(null);
  let tools = $state<EffectiveToolItem[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let secondaryAgents = $state<Agent[]>([]);
  let secondaryBindings = $state<string[]>([]);
  let previewTimer: ReturnType<typeof setTimeout> | null = null;
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
    avatar_image_id: null,
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
      [agent, workflows, secrets, intarisMcpServers, secondaryAgents, secondaryBindings] = await Promise.all([
        api.agents.detail(agentIdFromRoute()),
        api.workflows.listAll(),
        api.secrets.list(),
        api.tools.intarisMcpServers().catch(() => []),
        api.agents.listAll({ agent_type: 'secondary' }),
        api.agents.listBindings(agentIdFromRoute()).catch(() => []),
      ]);
      try {
        executors = await api.executor.list();
      } catch {
        executors = [];
      }
      if (auth.getSnapshot().user?.role === 'admin') {
        try {
          providers = (await api.llmProviders.list()).items;
        } catch {
          providers = [];
        }
      }
      Object.assign(form, agentToFormState(agent));
      const preview = await api.agents.effectiveTools(agentIdFromRoute());
      tools = preview.configured_state.tools;
      loading = false;
      await tick(); // Let AgentForm mount and settle select bindings before capturing snapshot
      initialSnapshot = JSON.stringify($state.snapshot(form));
    } catch (caughtError) {
      error = asApiError(caughtError).message;
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

  async function retrySyncPersonality(): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Sync personality to Mnemory?',
      message: 'This will re-bootstrap the agent personality in Mnemory. If the agent has evolved its identity through conversations, this may override those changes.',
      confirmLabel: 'Sync',
      variant: 'danger',
    });
    if (!confirmed) return;
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

  $effect(() => {
    if (loading || !agent) return;
    const payload = formStateToEffectiveToolsPreviewPayload(form);
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const preview = await api.agents.previewEffectiveTools(payload);
        tools = preview.configured_state.tools;
      } catch {
        // best-effort preview only
      }
    }, 200);
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
      {executors}
      {secrets}
      {intarisMcpServers}
      {secondaryAgents}
      {secondaryBindings}
      {saving}
      {error}
      readonly={agent?.is_system ?? false}
      onSave={saveAgent}
      onBindingsChange={async (bindings) => {
        try {
          await api.agents.replaceBindings(agentIdFromRoute(), bindings);
          secondaryBindings = bindings;
        } catch (caughtError) {
          error = asApiError(caughtError).message;
          addToast(error, 'error', 4_000, 'Unable to update bindings');
        }
      }}
    />
  </section>
{/if}
