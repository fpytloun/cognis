<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api, asApiError } from '$lib/api/client';
  import { AGENT_TEMPLATES, agentFormFromTemplate } from '$lib/agent-templates';
  import { formStateToPayload } from '$lib/agents';
  import { configureInstanceManagement } from '$lib/instance-management';
  import { executorReady, modelReference } from '$lib/onboarding';
  import { isChatModel } from '$lib/providers';
  import { auth } from '$lib/stores/auth';
  import { loadSetupDraft, saveSetupDraft } from '$lib/onboarding-setup';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { Agent, ExecutorConfig, LLMProvider, Skill } from '$lib/types/api';

  let { onBack, selectedExecutorId = '' }: { onBack: () => void; selectedExecutorId?: string } = $props();
  let name = $state('Ari');
  let templateId = $state('xo');
  let model = $state('');
  let selectedProviderId = $state('');
  let executorId = $state('');
  let management = $state(true);
  let avatarId = $state('');
  let avatarUrl = $state('');
  let avatarOpen = $state(false);
  let imageEnabled = $state(false);
  let advanced = $state(false);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let created = $state<Agent | null>(null);
  let creationId = '';
  let providers = $state<LLMProvider[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let skills: Skill[] = [];
  const readyExecutors = $derived(executors.filter(executorReady));
  const template = $derived(AGENT_TEMPLATES.find((entry) => entry.id === templateId) ?? AGENT_TEMPLATES[0]);
  const options = $derived(providers.filter((provider) => provider.status === 'active').flatMap((provider) =>
    provider.models.filter((entry) => isChatModel(entry) && entry.supports_tools).map((entry) => ({
      value: modelReference(provider, entry), label: entry.display_name || entry.model_id
    }))));
  const agentProviders = $derived(providers.filter((provider) => provider.status === 'active' &&
    provider.models.some((entry) => isChatModel(entry) && entry.supports_tools)));
  const providerOptions = $derived(options.filter((option) => option.value.startsWith(`${selectedProviderId}/`)));
  function chooseProvider(id: string) {
    selectedProviderId = id;
    const provider = agentProviders.find((entry) => entry.provider_id === id);
    const candidates = provider?.models.filter((entry) => isChatModel(entry) && entry.supports_tools) ?? [];
    const preferred = candidates.find((entry) => entry.model_id === provider?.config.default_model) ?? candidates[0];
    model = provider && preferred ? modelReference(provider, preferred) : '';
  }
  async function load() {
    const [providerList, list, skillList, routes, agents] = await Promise.all([
      api.llmProviders.list(), api.executor.list(), api.skills.list(), api.modelRouting.get(), api.agents.listAll()
    ]);
    providers = providerList.items;
    executors = list;
    skills = skillList;
    const owner = auth.getSnapshot().user?.email ?? '';
    const draft = loadSetupDraft(owner);
    executorId ||= selectedExecutorId || draft.executorId || list.find(executorReady)?.executor_id || '';
    creationId ||= draft.agentId ?? '';
    model ||= routes.default?.model ?? '';
    const routeProvider = providers.find((entry) => model.startsWith(`${entry.provider_id}/`) ||
      entry.models.some((candidate) => candidate.model_id === model));
    if (routeProvider && !model.startsWith(`${routeProvider.provider_id}/`)) {
      const matched = routeProvider.models.find((candidate) => candidate.model_id === model);
      if (matched) model = modelReference(routeProvider, matched);
    }
    selectedProviderId ||= routeProvider?.provider_id ?? agentProviders[0]?.provider_id ?? '';
    if (!routeProvider && selectedProviderId && !model.startsWith('cognis/')) chooseProvider(selectedProviderId);
    imageEnabled = !!routes.image_generation?.model;
    created = agents.find((agent) => agent.agent_id === creationId && agent.owner_email === owner) ?? null;
  }
  async function start() {
    busy = true; error = '';
    try {
      const owner = auth.getSnapshot().user?.email ?? '';
      const latest = await api.executor.list();
      if (!latest.some((executor) => executor.executor_id === executorId && executorReady(executor))) {
        throw new Error('Your computer disconnected. Go back to executor setup and reconnect it.');
      }
      if (!creationId) {
        creationId = crypto.randomUUID();
      }
      saveSetupDraft(owner, { executorId, agentId: creationId });
      if (!created) {
        const agents = await api.agents.listAll();
        created = agents.find((agent) => agent.agent_id === creationId && agent.owner_email === owner) ?? null;
      }
      if (!created) {
        if (!name.trim() || !model) throw new Error('Enter a name and choose a model in Advanced options.');
        const form = agentFormFromTemplate(template, skills);
        form.agentId = creationId;
        form.name = name.trim();
        const provider = providers.find((entry) => model.startsWith(`${entry.provider_id}/`));
        form.providerId = provider?.provider_id ?? '';
        form.model = provider ? model.slice(provider.provider_id.length + 1) : model;
        form.executorId = executorId;
        form.avatarImageId = avatarId;
        form.avatarUrl = avatarUrl;
        configureInstanceManagement(form, skills, management);
        try {
          created = await api.agents.create({ ...formStateToPayload(form), status: 'active' });
        } catch (caught) {
          // POST may commit before its response is lost. Reconcile the exact
          // owner-scoped identity before retrying; never adopt an unrelated agent.
          const agents = await api.agents.listAll();
          created = agents.find((agent) => agent.agent_id === creationId && agent.owner_email === owner) ?? null;
          if (!created) throw caught;
        }
      }
      if (created.status !== 'active' || created.agent_type !== 'primary' || created.owner_email !== owner ||
          created.execution?.executor_id !== executorId) {
        throw new Error('The saved setup agent no longer matches this configuration. Review it in Agents before continuing.');
      }
      const savedModel = created.llm_config?.model;
      const savedProvider = created.llm_config?.provider_id;
      const usableModel = providers.some((entry) => entry.status === 'active' && (!savedProvider || savedProvider === entry.provider_id) &&
        entry.models.some((candidate) => candidate.model_id === savedModel && isChatModel(candidate) && candidate.supports_tools));
      if (!usableModel && !(typeof savedModel === 'string' && savedModel.startsWith('cognis/') && savedModel === model)) {
        throw new Error('Your agent’s model is no longer available. Review its model in Agents before continuing.');
      }
      // Separate durable steps: retries retain the created agent on failure.
      const conversation = await api.conversations.resolve({
        agent_id: created.agent_id, context_type: 'web', scope: 'agent_direct'
      });
      await api.system.finishOnboarding('completed');
      await goto(`/chat/${conversation.conversation_id}`);
    } catch (caught) { error = asApiError(caught).message; }
    finally { busy = false; }
  }
  onMount(() => {
    void load().catch((caught) => { error = asApiError(caught).message; }).finally(() => { loading = false; });
  });
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-strong">Create agent</h1>
    <p class="mt-3 text-sm leading-6 text-muted">Your agent can control this Cognis instance: manage agents, providers, custom routes, system settings, executors, tools, tasks, and workflows. Tell it what you need in a conversation.</p>
  </div>
  {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
  {#if loading}
    <p role="status" class="text-sm text-muted">Preparing your assistant…</p>
  {:else if !readyExecutors.some((executor) => executor.executor_id === executorId)}
    <p role="alert" class="text-sm text-warning">Your computer needs to be connected before creating an agent.</p>
    <Button onclick={onBack}>Back to executor setup</Button>
  {:else if created}
    <p class="text-sm text-success">{created.name} is ready. Start a conversation to finish setup.</p>
    <Button onclick={start} disabled={busy}>{busy ? 'Opening conversation…' : 'Start conversation'}</Button>
  {:else}
    <label class="block text-sm font-medium text-foreground" for="setup-agent-name">Agent name</label>
    <input id="setup-agent-name" class="!mt-2 w-full rounded-lg border border-control-border bg-canvas px-3 py-3 text-sm" bind:value={name} disabled={busy} maxlength="200" />
    <div class="grid gap-4 sm:grid-cols-2">
      <label class="block text-sm text-foreground" for="setup-agent-provider">Provider
        <select id="setup-agent-provider" class="mt-2 w-full rounded-lg border border-control-border bg-canvas p-3 text-sm" value={selectedProviderId} disabled={busy} onchange={(event) => chooseProvider(event.currentTarget.value)}>
          {#each agentProviders as provider}<option value={provider.provider_id}>{provider.display_name}</option>{/each}
        </select>
      </label>
      <label class="block text-sm text-foreground" for="setup-agent-model">Model
        <select id="setup-agent-model" class="mt-2 w-full rounded-lg border border-control-border bg-canvas p-3 text-sm" bind:value={model} disabled={busy}>
          {#if model && !providerOptions.some((option) => option.value === model)}<option value={model}>Recommended system model</option>{/if}
          {#each providerOptions as option}<option value={option.value}>{option.label}</option>{/each}
        </select>
      </label>
    </div>
    <p class="!mt-2 text-xs text-muted">Defaults are selected for you. Changing the agent’s model does not change Cognis’s system models.</p>
    <div class="flex items-center gap-4">
      {#if avatarUrl}<img src={avatarUrl} alt="Your agent’s avatar" class="h-16 w-16 rounded-full" />{:else}<div class="flex h-16 w-16 items-center justify-center rounded-full bg-raised text-xl text-accent" aria-hidden="true">{name.trim().slice(0, 1).toUpperCase() || 'A'}</div>{/if}
      <div>
        <Button variant="secondary" onclick={() => { avatarOpen = true; }} disabled={busy || !imageEnabled || !name.trim()}>Generate avatar</Button>
        <p class="mt-2 text-xs text-quiet">{imageEnabled ? 'Optional · uses your image model and may incur a charge.' : 'Optional · enable image generation in the provider step.'}</p>
      </div>
    </div>
    <p class="text-xs leading-5 text-muted">Your assistant can use tools on {readyExecutors.find((executor) => executor.executor_id === executorId)?.name ?? 'your connected computer'} and manage agents in Cognis. Safety checks and approvals stay enabled.</p>
    <Button onclick={start} disabled={busy || !name.trim() || !model || !executorId}>{busy ? 'Creating your agent…' : 'Start conversation'}</Button>
    <details bind:open={advanced} class="border-t border-separator pt-4">
      <summary class="cursor-pointer text-sm text-muted">Advanced options</summary>
      <div class="mt-4 space-y-4">
        <label class="block text-sm">Template<select class="mt-2 w-full rounded-lg bg-canvas p-2" bind:value={templateId}>
          {#each AGENT_TEMPLATES as entry}<option value={entry.id}>{entry.name}</option>{/each}
        </select></label>
        <label class="block text-sm">Executor<select class="mt-2 w-full rounded-lg bg-canvas p-2" bind:value={executorId}>
          {#each readyExecutors as executor}<option value={executor.executor_id}>{executor.name}</option>{/each}
        </select></label>
        <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={management} /> Allow full Cognis instance management</label>
        <p class="text-xs text-muted">Profiles: {template.profiles.map((entry) => entry.id).join(', ')}</p>
        <pre class="max-h-52 overflow-auto whitespace-pre-wrap text-xs leading-5 text-muted">{template.prompt}</pre>
      </div>
    </details>
  {/if}
</div>
{#if avatarOpen}
  <AvatarGenerateModal {name} description={template.description} onClose={() => { avatarOpen = false; }}
    onAccept={(id, url) => { avatarId = id; avatarUrl = url; avatarOpen = false; }} />
{/if}
