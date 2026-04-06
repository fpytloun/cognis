<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import {
    createEmptyAgentForm,
    defaultSystemPrompt,
    formStateToEffectiveToolsPreviewPayload,
    type AgentFormState
  } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { addToast } from '$lib/stores/toasts';
  import Button from '$lib/components/ui/Button.svelte';
  import type { EffectiveToolItem, ExecutorConfig, IntarisMCPServer, LLMProvider, SecretMetadata, Skill, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let tools = $state<EffectiveToolItem[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let skills = $state<Skill[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let form: AgentFormState = $state(createEmptyAgentForm());
  let initialSnapshot = '';
  let previewTimer: ReturnType<typeof setTimeout> | null = null;

  function isDirty(): boolean {
    return JSON.stringify($state.snapshot(form)) !== initialSnapshot;
  }

  beforeNavigate((navigation) => {
    if (saving) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function loadOptions(): Promise<void> {
    loading = true;
    try {
      const [, loadedWorkflows] = await Promise.all([
        api.tools.list(),
        api.workflows.listAll(),
      ]);
      workflows = loadedWorkflows;

      // These are non-critical — load gracefully
      try { secrets = await api.secrets.list(); } catch { secrets = []; }
      try { skills = await api.skills.list(); } catch { skills = []; }
      try { executors = await api.executor.list(); } catch { executors = []; }
      if (auth.getSnapshot().user?.role === 'admin') {
        try { providers = (await api.llmProviders.list()).items; } catch { providers = []; }
      }
      try { intarisMcpServers = await api.tools.intarisMcpServers(); } catch { intarisMcpServers = []; }

      // Update form with system workflows pre-selected
      const systemWorkflowIds = workflows.filter((w) => w.is_system).map((w) => w.workflow_id);
      form.availableWorkflowIds = systemWorkflowIds;
      form.defaultWorkflowId = 'system:direct';
      form.systemPrompt = defaultSystemPrompt('');
      const fullCatalogPayload = formStateToEffectiveToolsPreviewPayload({
        ...form,
        disabledTools: [],
        disabledCategories: [],
      });
      const preview = await api.agents.previewEffectiveTools(fullCatalogPayload);
      tools = preview.configured_state.tools;
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
      const agent = await api.agents.create(payload);
      addToast('Agent created.', 'success');
      await goto(`/agents/${agent.agent_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create agent');
    } finally {
      saving = false;
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadOptions();
    return cleanup;
  });

  $effect(() => {
    if (loading) return;
    // Load the full tool catalog without disabled filters so unchecked
    // tools remain visible and can be re-enabled.
    const catalogPayload = formStateToEffectiveToolsPreviewPayload({
      ...form,
      disabledTools: [],
      disabledCategories: [],
    });
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const preview = await api.agents.previewEffectiveTools(catalogPayload);
        tools = preview.configured_state.tools;
      } catch {
        // best-effort preview only
      }
    }, 200);
  });
</script>

<svelte:head>
  <title>New Agent · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Preparing agent editor" description="Loading tools, workflows, and available providers." />
{:else}
  <section class="space-y-5">
    <div class="space-y-3">
      <Button size="sm" variant="secondary" onclick={() => goto('/agents')}>Back to agents</Button>
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Agent creator</p>
      <h1 class="mt-1 text-2xl font-semibold text-white">Create agent</h1>
    </div>
    <AgentForm mode="create" {form} {tools} {workflows} {providers} {executors} {secrets} {skills} {intarisMcpServers} {saving} {error} onSave={saveAgent} />
  </section>
{/if}
