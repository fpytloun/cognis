<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { AGENT_TEMPLATES, agentFormFromTemplate } from '$lib/agent-templates';
  import { executorReady } from '$lib/onboarding';
  import { isChatModel } from '$lib/providers';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';

  import {
    createEmptyAgentForm,
    defaultSystemPrompt,
    formStateToEffectiveToolsPreviewPayload,
    formStateToPayload,
    type AgentFormState
  } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { createEffectiveToolsPreviewScheduler } from '$lib/effective-tools-preview';
  import { auth } from '$lib/stores/auth';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { addToast } from '$lib/stores/toasts';
  import Button from '$lib/components/ui/Button.svelte';
  import type { Agent, CredentialMetadata, EffectiveToolItem, EffectiveToolsResponse, ExecutorConfig, IntarisMCPServer, KnowledgebaseModel, LLMProvider, MemoryBackendDescriptor, SecretMetadata, Skill, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let tools = $state<EffectiveToolItem[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let memoryBackends = $state<MemoryBackendDescriptor[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let knowledgebases = $state<KnowledgebaseModel[]>([]);
  let skills = $state<Skill[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let agents = $state<Agent[]>([]);
  let form: AgentFormState = $state(createEmptyAgentForm());
  let initialSnapshot = '';
  let selectedTemplate = $state('');
  let advanced = $state(false);
  let avatarOpen = $state(false);
  let managementConfirmed = $state(false);
  const modelOptions = $derived(providers.filter((provider) => provider.status === 'active')
    .flatMap((provider) => provider.models.filter((model) => isChatModel(model) && model.supports_tools)
      .map((model) => ({ provider: provider.provider_id, model: model.model_id, label: `${model.display_name || model.model_id} · ${provider.display_name}` }))));
  let modelChoice = $state('');
  const guided = $derived(page.url.searchParams.get('onboarding') === '1');
  const readyExecutors = $derived(executors.filter(executorReady));

  function applyTemplate(id: string): void {
    const template = AGENT_TEMPLATES.find((entry) => entry.id === id);
    if (!template) return;
    selectedTemplate = id;
    modelChoice = '';
    managementConfirmed = false;
    form = agentFormFromTemplate(template, skills);
    form.executorId = readyExecutors[0]?.executor_id ?? '';
  }
  const preview = createEffectiveToolsPreviewScheduler({
    fetch: (payload: Record<string, unknown>, signal: AbortSignal) =>
      api.agents.previewEffectiveTools(payload, { signal }),
    onResult: (result: EffectiveToolsResponse) => {
      tools = result.configured_state.tools;
    },
  });

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
      try { credentials = await api.credentials.list(); } catch { credentials = []; }
      try { knowledgebases = await api.knowledgebases.list(); } catch { knowledgebases = []; }
      try { skills = await api.skills.list(); } catch { skills = []; }
      try { executors = await api.executor.list(); } catch { executors = []; }
      try { providers = (await api.llmProviders.list()).items; } catch { providers = []; }
      try { memoryBackends = (await api.agents.memoryBackends()).items; } catch { memoryBackends = []; }
      try { intarisMcpServers = await api.tools.intarisMcpServers(); } catch { intarisMcpServers = []; }
      try { agents = await api.agents.listAll(); } catch { agents = []; }

      form.systemPrompt = defaultSystemPrompt('');
      const templateId = page.url.searchParams.get('template');
      if (templateId) applyTemplate(templateId);
      const fullCatalogPayload = formStateToEffectiveToolsPreviewPayload({
        ...form,
        disabledTools: [],
        disabledCategories: [],
        disabledMcpServers: [],
      });
      preview.prime(fullCatalogPayload);
      const fullCatalog = await api.agents.previewEffectiveTools(fullCatalogPayload);
      tools = fullCatalog.configured_state.tools;
      initialSnapshot = JSON.stringify($state.snapshot(form));
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function saveAgent(payload: Record<string, unknown>): Promise<void> {
    if (guided && !readyExecutors.some((executor) => executor.executor_id === form.executorId)) {
      error = 'Connect and select a ready executor before creating your first agent.';
      return;
    }
    if (selectedTemplate === 'xo' && !managementConfirmed) {
      error = 'Confirm owner-only management access before creating the XO.';
      return;
    }
    saving = true;
    error = '';
    try {
      const agent = await api.agents.create({ ...payload, ...(selectedTemplate ? { status: 'active' } : {}) });
      addToast('Agent created.', 'success');
      await goto(guided ? '/getting-started' : `/agents/${agent.agent_id}`);
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
    return () => {
      cleanup();
      preview.dispose();
    };
  });

  $effect(() => {
    if (loading) return;
    // Load the full tool catalog without disabled filters so unchecked
    // tools remain visible and can be re-enabled.
    const catalogPayload = formStateToEffectiveToolsPreviewPayload({
      ...form,
      disabledTools: [],
      disabledCategories: [],
      disabledMcpServers: [],
    });
    preview.schedule(catalogPayload);
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
      <p class="text-sm uppercase tracking-[0.25em] text-muted">Agent creator</p>
      <h1 class="mt-1 text-2xl font-semibold text-strong">Create agent</h1>
    </div>
    <div class="grid gap-3 md:grid-cols-2">
      {#each AGENT_TEMPLATES as template}
        <button type="button" aria-pressed={selectedTemplate === template.id} class="rounded-xl border border-control-border bg-surface p-4 text-left hover:border-accent" onclick={() => applyTemplate(template.id)}>
          <strong class="text-strong">{template.name}</strong>
          <p class="mt-2 text-sm text-muted">{template.description}</p>
          <p class="mt-2 text-xs text-muted">Profiles: {template.profiles.map((profile) => profile.id).join(', ')}</p>
        </button>
      {/each}
    </div>
    {#if guided && !readyExecutors.length}
      <p role="alert" class="text-warning">A connected executor with applied configuration and observed tools is required. <a class="underline" href="/getting-started">Return to executor setup</a>.</p>
    {:else}
      {#if selectedTemplate}
        <p class="text-sm text-muted">Template applied as an independent copy. Choose your model and executor, review the prompt, profiles, skills, and permissions below. XO enables owner-only agent/MCP management; it does not bypass guardrails. Image generation is optional and available in the avatar control.</p>
      {/if}
      {#if selectedTemplate === 'xo'}
        <label class="flex gap-3 text-sm text-foreground"><input type="checkbox" bind:checked={managementConfirmed} />Allow this primary agent to manage my agents and MCP configuration in my owner chat. Intaris and approval requirements remain enabled.</label>
      {/if}
      {#if selectedTemplate && !advanced}
        <div class="space-y-4 rounded-xl border border-control-border p-5">
          <label class="block text-sm">Agent name<input class="mt-1 w-full rounded-lg bg-canvas p-2" bind:value={form.name} /></label>
          <label class="block text-sm">Agent model
            <select class="mt-1 w-full rounded-lg bg-canvas p-2" bind:value={modelChoice} onchange={() => {
              const option = modelOptions.find((entry) => `${entry.provider}/${entry.model}` === modelChoice);
              form.providerId = option?.provider ?? ''; form.model = option?.model ?? '';
            }}>
              <option value="">Choose a tool-capable model</option>
              {#each modelOptions as option}<option value={`${option.provider}/${option.model}`}>{option.label}</option>{/each}
            </select>
          </label>
          <label class="block text-sm">Executor
            <select class="mt-1 w-full rounded-lg bg-canvas p-2" bind:value={form.executorId}>
              <option value="">Choose a connected executor</option>
              {#each readyExecutors as executor}<option value={executor.executor_id}>{executor.name}</option>{/each}
            </select>
          </label>
          <details><summary class="cursor-pointer text-sm">Review included instructions, profiles, and skills</summary>
            <pre class="mt-3 max-h-72 overflow-auto whitespace-pre-wrap text-xs text-secondary">{form.systemPrompt}</pre>
            <p class="mt-2 text-sm">Profiles: {form.agentProfiles.map((profile) => profile.profileId).join(', ')}</p>
            <p class="mt-2 text-sm">Skills: {form.selectedSkillIds.join(', ') || 'No matching system skills available'}</p>
          </details>
          {#if form.avatarUrl}<img src={form.avatarUrl} alt="Generated agent avatar" class="h-20 w-20 rounded-full" />{/if}
          <Button variant="secondary" disabled={!form.name.trim()} onclick={() => { avatarOpen = true; }}>Generate avatar (optional, may incur cost)</Button>
          {#if error}<p role="alert" class="text-danger">{error}</p>{/if}
          <div class="flex flex-wrap gap-3">
            <Button disabled={saving || !form.name.trim() || !form.model || !form.executorId || (selectedTemplate === 'xo' && !managementConfirmed)} onclick={() => saveAgent(formStateToPayload(form))}>Create agent</Button>
            <Button variant="secondary" onclick={() => { advanced = true; }}>Advanced editor</Button>
          </div>
        </div>
      {:else}
        <AgentForm mode="create" bind:form {tools} {workflows} {providers} {memoryBackends} {executors} {secrets} {credentials} {knowledgebases} {skills} {intarisMcpServers} {agents} {saving} {error} onSave={saveAgent} />
      {/if}
    {/if}
  </section>
{/if}
{#if avatarOpen}
  <AvatarGenerateModal name={form.name} description={form.description} onClose={() => { avatarOpen = false; }}
    onAccept={(id, url) => { form.avatarImageId = id; form.avatarUrl = url; avatarOpen = false; }} />
{/if}
