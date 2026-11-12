<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount, tick } from 'svelte';
  import Pause from 'lucide-svelte/icons/pause';
  import Play from 'lucide-svelte/icons/play';

  import { agentToFormState, createEmptyAgentForm, formStateToEffectiveToolsPreviewPayload } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { createEffectiveToolsPreviewScheduler } from '$lib/effective-tools-preview';
  import { auth } from '$lib/stores/auth';
  import AgentForm from '$lib/components/agents/AgentForm.svelte';
  import AgentHooks from '$lib/components/AgentHooks.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import type { Agent, AgentGrant, AgentProjectAssignment, AgentSkillUsage, CredentialMetadata, EffectiveToolItem, EffectiveToolsResponse, ExecutorConfig, IntarisMCPServer, KnowledgebaseModel, LLMProvider, MemoryBackendDescriptor, SecretMetadata, Skill, Workflow } from '$lib/types/api';

  let { agentId } = $props<{ agentId: string }>();
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let agent = $state<Agent | null>(null);
  let agentProjects = $state<AgentProjectAssignment[]>([]);
  let tools = $state<EffectiveToolItem[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let memoryBackends = $state<MemoryBackendDescriptor[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let knowledgebases = $state<KnowledgebaseModel[]>([]);
  let skills = $state<Skill[]>([]);
  let skillUsage = $state<AgentSkillUsage[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let agents = $state<Agent[]>([]);
  let secondaryAgents = $state<Agent[]>([]);
  let secondaryBindings = $state<string[]>([]);
  let shares = $state<AgentGrant[]>([]);
  let myShare = $state<AgentGrant | null>(null);
  let shareExecutorId = $state('');
  let shareExecutorSelector = $state('');
  let shareOverrideSaving = $state(false);
  let shareEmail = $state('');
  let shareExecutorScope = $state<'owner_executor' | 'grantee_executor'>('owner_executor');
  let shareNote = $state('');
  let shareSaving = $state(false);
  let shareError = $state('');
  let updatingShareId = $state<string | null>(null);
  let activeSection = $state('identity');
  let bindingsSaving = $state(false);
  let actionBusy = $state(false);
  let collaborationLoaded = $state(false);
  let collaborationLoading = $state(false);
  let projectError = $state('');
  let toolsRequested = $state(false);
  let toolsLoaded = $state(false);
  let toolsLoading = $state(false);
  let toolsError = $state('');
  const preview = createEffectiveToolsPreviewScheduler({
    fetch: (payload: Record<string, unknown>, signal: AbortSignal) =>
      api.agents.previewEffectiveTools(payload, { signal }),
    onResult: (result: EffectiveToolsResponse) => {
      tools = result.configured_state.tools;
    },
  });
  let form = $state(createEmptyAgentForm());
  let initialSnapshot = $state('');

  function isDirty(): boolean {
    return !!initialSnapshot && JSON.stringify($state.snapshot(form)) !== initialSnapshot;
  }

  function canManageShares(): boolean {
    const currentUser = auth.getSnapshot().user;
    return !!agent && !agent.is_system && !agent.is_shared_with_me && agent.owner_email === currentUser?.email;
  }

  function granteeExecution(grant: AgentGrant | null): Record<string, unknown> {
    const overrides = grant?.grantee_overrides;
    const execution = overrides && typeof overrides === 'object' ? overrides.execution : null;
    return execution && typeof execution === 'object' ? execution as Record<string, unknown> : {};
  }

  function selectorText(selector: unknown): string {
    if (!selector || typeof selector !== 'object') return '';
    return Object.entries(selector as Record<string, unknown>)
      .map(([key, value]) => `${key}=${String(value)}`)
      .join('\n');
  }

  function selectorFromText(value: string): Record<string, string> {
    return Object.fromEntries(
      value
        .split(/\n+/)
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [key, ...rest] = line.split('=');
          return [key?.trim(), rest.join('=').trim()] as const;
        })
        .filter(([key, val]) => Boolean(key) && Boolean(val))
    );
  }

  async function loadMyShare(): Promise<void> {
    if (!agent?.is_shared_with_me) {
      myShare = null;
      return;
    }
    try {
      myShare = await api.agents.myShare(agent.agent_id);
      const execution = granteeExecution(myShare);
      shareExecutorId = typeof execution.executor_id === 'string' ? execution.executor_id : '';
      shareExecutorSelector = selectorText(execution.executor_selector);
    } catch {
      myShare = null;
    }
  }

  async function saveShareExecutorOverride(): Promise<void> {
    if (!agent?.is_shared_with_me || myShare?.executor_scope !== 'grantee_executor') return;
    shareOverrideSaving = true;
    try {
      const selector = selectorFromText(shareExecutorSelector);
      const execution: Record<string, unknown> = shareExecutorId
        ? { executor_id: shareExecutorId }
        : Object.keys(selector).length > 0
          ? { executor_selector: selector }
          : {};
      myShare = await api.agents.updateMyShare(agent.agent_id, { execution });
      addToast('Shared-agent executor saved.', 'success');
      agent = await api.agents.detail(agent.agent_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save executor');
    } finally {
      shareOverrideSaving = false;
    }
  }

  async function loadShares(): Promise<void> {
    if (!agent || !canManageShares()) {
      shares = [];
      return;
    }
    try {
      shares = await api.agents.listShares(agent.agent_id);
      shareError = '';
    } catch (caughtError) {
      shareError = asApiError(caughtError).message;
    }
  }

  beforeNavigate((navigation) => {
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function loadAgent(): Promise<void> {
    loading = true;
    optionsLoading = true;
    try {
      agent = await api.agents.detail(agentId);
      Object.assign(form, agentToFormState(agent));
      initialSnapshot = JSON.stringify($state.snapshot(form));
      loading = false;
      [workflows, secrets, credentials, knowledgebases, skills, intarisMcpServers, agents, secondaryBindings, executors, providers, memoryBackends] = await Promise.all([
        api.workflows.listAll(),
        api.secrets.list(),
        api.credentials.list().catch(() => []),
        api.knowledgebases.list().catch(() => []),
        api.skills.list().catch(() => []),
        api.tools.intarisMcpServers().catch(() => []),
        api.agents.listAll().catch(() => []),
        api.agents.listBindings(agentId).catch(() => []),
        api.executor.list().catch(() => []),
        api.llmProviders.list().then(result => result.items).catch(() => []),
        api.agents.memoryBackends().then(result => result.items).catch(() => []),
      ]);
      secondaryAgents = agents.filter((candidate: Agent) => candidate.agent_type === 'secondary');
      skillUsage = agent.is_system || agent.is_readonly_for_caller
        ? []
        : await api.agents.skillUsage(agentId).catch(() => []);
      await loadMyShare();
      optionsLoading = false;
      await tick();
      initialSnapshot = JSON.stringify($state.snapshot(form));
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      loading = false;
    }
  }

  let optionsLoading = $state(true);

  async function loadTools(): Promise<void> {
    if (!agent || toolsLoading) return;
    toolsRequested = true;
    toolsLoading = true;
    toolsError = '';
    try {
      if (agent.is_readonly_for_caller) {
        const effective = await api.agents.effectiveTools(agent.agent_id);
        tools = effective.configured_state.tools;
      } else {
        // Load the full tool catalog without disabled filters so unchecked
        // tools remain visible and can be re-enabled.
        const fullCatalogPayload = formStateToEffectiveToolsPreviewPayload({
          ...form,
          disabledTools: [],
          disabledCategories: [],
          disabledMcpServers: [],
        });
        preview.prime(fullCatalogPayload);
        const fullCatalog = await api.agents.previewEffectiveTools(fullCatalogPayload);
        tools = fullCatalog.configured_state.tools;
      }
      toolsLoaded = true;
    } catch (caughtError) {
      toolsError = asApiError(caughtError).message;
    } finally {
      toolsLoading = false;
    }
  }

  async function saveAgent(payload: Record<string, unknown>): Promise<void> {
    if (saving || actionBusy) return;
    const submittedSnapshot = JSON.stringify($state.snapshot(form));
    saving = true;
    error = '';
    try {
      agent = await api.agents.update(agentId, payload);
      const savedForm = agentToFormState(agent);
      if (JSON.stringify($state.snapshot(form)) === submittedSnapshot) {
        Object.assign(form, savedForm);
      }
      initialSnapshot = JSON.stringify(savedForm);
      addToast('Agent updated.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save agent');
    } finally {
      saving = false;
    }
  }

  async function createShare(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!agent || !canManageShares()) return;
    const granteeEmail = shareEmail.trim();
    if (!granteeEmail) {
      shareError = 'Enter the email address of an existing Cognis user.';
      return;
    }
    shareSaving = true;
    shareError = '';
    try {
      await api.agents.createShare(agent.agent_id, {
        grantee_email: granteeEmail,
        executor_scope: shareExecutorScope,
        note: shareNote.trim() || null,
      });
      shareEmail = '';
      shareNote = '';
      await loadShares();
      addToast('Agent shared.', 'success');
    } catch (caughtError) {
      shareError = asApiError(caughtError).message;
      addToast(shareError, 'error', 4_000, 'Unable to share agent');
    } finally {
      shareSaving = false;
    }
  }

  async function updateShareScope(grant: AgentGrant, event: Event): Promise<void> {
    if (!agent || !canManageShares()) return;
    const executorScope = (event.currentTarget as HTMLSelectElement).value as 'owner_executor' | 'grantee_executor';
    (event.currentTarget as HTMLSelectElement).value = grant.executor_scope;
    if (executorScope === grant.executor_scope) return;
    updatingShareId = grant.grant_id;
    shareError = '';
    try {
      await api.agents.updateShare(agent.agent_id, grant.grant_id, { executor_scope: executorScope, note: grant.note });
      await loadShares();
      addToast('Share updated.', 'success');
    } catch (caughtError) {
      shareError = asApiError(caughtError).message;
      addToast(shareError, 'error', 4_000, 'Unable to update share');
    } finally {
      updatingShareId = null;
    }
  }

  async function revokeShare(grant: AgentGrant): Promise<void> {
    if (!agent || !canManageShares()) return;
    const email = grant.grantee_user_email ?? 'this user';
    const confirmed = await confirmAction({
      title: 'Revoke agent access?',
      message: `This will remove ${email}'s access to this agent and pause their dependent tasks or schedules.`,
      confirmLabel: 'Revoke',
      variant: 'danger',
    });
    if (!confirmed) return;
    updatingShareId = grant.grant_id;
    shareError = '';
    try {
      await api.agents.revokeShare(agent.agent_id, grant.grant_id);
      await loadShares();
      addToast('Share revoked.', 'success');
    } catch (caughtError) {
      shareError = asApiError(caughtError).message;
      addToast(shareError, 'error', 4_000, 'Unable to revoke share');
    } finally {
      updatingShareId = null;
    }
  }

  async function resetOverrides(): Promise<void> {
    if (!agent?.is_system) return;
    const confirmed = await confirmAction({
      title: 'Reset system agent overrides?',
      message: 'This applies immediately and discards saved overrides and unsaved configuration changes.',
      confirmLabel: 'Reset overrides',
      variant: 'danger',
    });
    if (!confirmed) return;
    actionBusy = true;
    try {
      await api.agents.resetOverrides(agentId);
      agent = await api.agents.detail(agentId);
      Object.assign(form, agentToFormState(agent));
      initialSnapshot = JSON.stringify($state.snapshot(form));
      addToast('System agent overrides reset.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to reset overrides');
    } finally {
      actionBusy = false;
    }
  }

  async function toggleSystemDisabled(): Promise<void> {
    if (!agent?.is_system) return;
    actionBusy = true;
    try {
      if (agent.disabled) {
        await api.agents.enableSystem(agent.agent_id);
      } else {
        await api.agents.disableSystem(agent.agent_id);
      }
      agent = await api.agents.detail(agent.agent_id);
      addToast(agent.disabled ? 'System agent disabled.' : 'System agent enabled.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update system agent state');
    } finally {
      actionBusy = false;
    }
  }

  async function duplicateSystemAgent(): Promise<void> {
    if (!agent) return;
    try {
      const duplicated = await api.agents.duplicate(agent.agent_id);
      addToast('Agent duplicated.', 'success');
      await goto(`/agents/${duplicated.agent_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to duplicate agent');
    }
  }

  async function toggleStatus(): Promise<void> {
    if (!agent || agent.is_readonly_for_caller || agent.is_system) return;
    if (agent.status === 'active') {
      const confirmed = await confirmAction({
        title: 'Suspend agent?',
        message: 'Suspended agents cannot be selected for new work until they are activated again.',
        confirmLabel: 'Suspend',
        variant: 'danger',
      });
      if (!confirmed) return;
    }
    error = '';
    actionBusy = true;
    try {
      if (agent.status === 'active') {
        await api.agents.suspend(agent.agent_id);
      } else {
        await api.agents.activate(agent.agent_id);
      }
      const previousStatus = agent.status;
      agent = await api.agents.detail(agent.agent_id);
      addToast(`Agent ${previousStatus === 'active' ? 'suspended' : 'activated'}.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update agent status');
    } finally {
      actionBusy = false;
    }
  }

  async function retrySyncPersonality(): Promise<void> {
    if (!agent || agent.is_readonly_for_caller || isDirty() || saving || actionBusy) return;
    const confirmed = await confirmAction({
      title: 'Sync personality to Mnemory?',
      message: 'This will re-bootstrap the agent personality in Mnemory. If the agent has evolved its identity through conversations, this may override those changes.',
      confirmLabel: 'Sync',
      variant: 'danger',
    });
    if (!confirmed) return;
    error = '';
    actionBusy = true;
    try {
      await api.agents.syncPersonality(agentId);
      agent = await api.agents.detail(agentId);
      addToast('Personality synced.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to sync personality');
    } finally {
      actionBusy = false;
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadAgent();
    return () => {
      cleanup();
      preview.dispose();
    };
  });

  async function loadCollaboration(): Promise<void> {
    if (!agent || collaborationLoading) return;
    collaborationLoading = true;
    projectError = '';
    await Promise.all([
      agent.agent_type === 'primary' && !agent.is_system
        ? api.projects.agentProjects(agent.agent_id).then(items => { agentProjects = items; })
          .catch(cause => { projectError = asApiError(cause).message; })
        : Promise.resolve(),
      loadShares(),
    ]);
    collaborationLoading = false;
    collaborationLoaded = true;
  }

  $effect(() => {
    if (!loading && agent && activeSection === 'collaboration' && !collaborationLoaded && !collaborationLoading) {
      void loadCollaboration();
    }
  });

  $effect(() => {
    if (!loading && agent && activeSection === 'tools' && !toolsRequested) void loadTools();
  });

  $effect(() => {
    if (loading || !toolsLoaded || !agent || agent.is_readonly_for_caller) return;
    // Load the full tool catalog without disabled filters so unchecked
    // tools remain visible and can be re-enabled. Only executor/skills
    // changes should refresh the catalog.
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
  <title>{agent ? `${agent.display_name ?? agent.name} · Agent · Cognis` : 'Agent · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading agent" description="Fetching the agent definition and configuration options." />
{:else}
  <section class="space-y-5">
    <header class="space-y-4 border-b border-separator pb-5">
      <Button size="sm" variant="ghost" onclick={() => goto('/agents')}>Back to agents</Button>
      <div class="flex min-w-0 items-center gap-4">
        <AgentAvatar name={agent?.display_name ?? agent?.name ?? 'Agent'} avatarUrl={form.avatarUrl}
          draggable={false} class="h-16 w-16 text-xl ring-1 ring-separator" />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h1 class="min-w-0 break-words text-2xl font-semibold tracking-tight text-strong">{agent?.display_name ?? agent?.name ?? 'Agent'}</h1>
            <span class="rounded-full border border-control-border px-2 py-0.5 text-xs capitalize text-secondary">{agent?.disabled ? 'Disabled' : agent?.status ?? 'Unavailable'}</span>
          </div>
          <p class="mt-1 text-sm text-muted">{agent?.is_system ? 'System agent' : agent?.is_shared_with_me ? 'Shared agent' : agent?.agent_type === 'secondary' ? 'Secondary agent' : 'Primary agent'}</p>
          <p class="mt-1 truncate font-mono text-xs text-quiet" title={agent?.agent_id}>{agent?.agent_id}</p>
        </div>
      </div>
    </header>
    {#snippet identityNotices()}
    {#if agent && !agent.personality_synced && agent.agent_type === 'primary' && !agent.is_system && !agent.is_readonly_for_caller}
      <div class="rounded-2xl border border-accent/30 bg-accent-subtle px-4 py-4 text-sm text-accent">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="font-medium">Personality was not synced to Mnemory.</p>
            <p class="mt-1 text-accent/90">{agent.personality_sync_error ?? 'Retry the sync to bootstrap this agent into memory.'}</p>
          </div>
          <Button size="sm" variant="secondary" disabled={actionBusy || saving || isDirty()} onclick={retrySyncPersonality}>Retry sync</Button>
        </div>
      </div>
    {/if}
    {#if agent?.is_system}
      <div class="rounded-2xl border border-accent/30 bg-accent-subtle px-4 py-4 text-sm text-accent">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="font-medium">System agent</p>
            <p class="mt-1 text-accent/80">This shipped agent stays immutable. You can only tune selected runtime fields here, or duplicate it for full customization.</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" disabled={actionBusy || saving} onclick={duplicateSystemAgent}>Duplicate</Button>
            <Button size="sm" variant="secondary" onclick={resetOverrides} disabled={!agent.has_overrides || actionBusy || saving}>Reset overrides</Button>
            {#if agent.disableable}
              <Button size="sm" variant="secondary" disabled={actionBusy || saving} onclick={toggleSystemDisabled}>{agent.disabled ? 'Enable' : 'Disable'}</Button>
            {/if}
          </div>
        </div>
      </div>
    {/if}
    {#if agent?.is_shared_with_me}
      <div class="rounded-2xl border border-cyan-border bg-cyan-subtle px-4 py-4 text-sm text-cyan-tone">
        <p class="font-medium">Shared agent</p>
        <p class="mt-1 text-cyan-tone/80">Shared by {agent.shared_by_email ?? agent.owner_email}. You can use this agent, but only the owner can edit or manage sharing.</p>
      </div>
    {/if}
    {/snippet}
    {#snippet projectsPanel()}
    {#if agent && !agent.is_system && agent.agent_type === 'primary'}
      <Card class="p-4 sm:p-5">
        <p class="text-sm uppercase tracking-[0.22em] text-quiet" data-testid="agent-projects">Projects</p>
        <h2 class="mt-1 text-lg font-semibold text-strong">Project assignments</h2>
        <p class="mt-1 text-sm text-muted">Work of this agent counts as project work. Assign agents from a project's Agents tab.</p>
        {#if projectError}<p role="alert" class="mt-3 text-sm text-danger">{projectError} <button type="button" class="underline" onclick={loadCollaboration}>Retry loading projects</button></p>{/if}
        <div class="mt-3 flex flex-wrap gap-2">
          {#each agentProjects as item (item.project_id)}
            <a class="inline-flex items-center gap-2 rounded-full border border-control-border px-3 py-1 text-sm text-foreground hover:border-violet-border" href={`/projects/${item.project_id}?tab=agents`}>
              {item.project_name}
              {#if item.is_default}<span class="text-xs text-violet-tone">default</span>{/if}
              <span class="text-xs text-quiet">{item.executor_scope === 'grantee_executor' ? 'members bring executor' : "owner's executor"}</span>
            </a>
          {/each}
          {#if !projectError && agentProjects.length === 0}<p class="text-sm text-quiet">Not assigned to any project.</p>{/if}
        </div>
      </Card>
    {/if}
    {/snippet}
    {#snippet hooksPanel()}
    {#if agent && !agent.is_system && !agent.is_readonly_for_caller}
      {#key agent.agent_id}<AgentHooks agentId={agent.agent_id} />{/key}
    {/if}
    {/snippet}
    {#snippet identityActions()}
      {@render identityNotices()}
    {#if agent && !agent.is_system && !agent.is_readonly_for_caller}
      <Card class="p-4 sm:p-5">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="text-sm uppercase tracking-[0.22em] text-quiet">Availability</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Agent status</h2>
            <p class="mt-1 text-sm text-muted">Current status: <span class="font-medium text-foreground">{agent.status}</span></p>
          </div>
          <Button size="sm" disabled={actionBusy || saving} variant={agent.status === 'active' ? 'danger' : 'secondary'} onclick={toggleStatus}>
            {#if agent.status === 'active'}
              <Pause class="mr-2 h-4 w-4" />
              Suspend
            {:else}
              <Play class="mr-2 h-4 w-4" />
              Activate
            {/if}
          </Button>
        </div>
      </Card>
    {/if}
    {/snippet}
    {#snippet accessActions()}
    {#if agent?.is_shared_with_me && myShare?.executor_scope === 'grantee_executor'}
      <Card class="p-4 sm:p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-sm uppercase tracking-[0.22em] text-quiet">Your runtime</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Executor for this shared agent</h2>
            <p class="mt-1 text-sm text-muted">Choose your executor for this owner-shared agent. Leave both fields empty to use your default executor.</p>
          </div>
          <Button size="sm" disabled={shareOverrideSaving} onclick={saveShareExecutorOverride}>{shareOverrideSaving ? 'Saving...' : 'Save executor'}</Button>
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-2">
          <label class="space-y-1 text-sm text-secondary">
            <span>Executor</span>
            <select bind:value={shareExecutorId} class="w-full rounded-xl border border-control-border bg-canvas px-3 py-2 text-sm text-foreground outline-none focus:border-accent">
              <option value="">Use default executor</option>
              {#each executors as executor}
                <option value={executor.executor_id}>{executor.name}{executor.is_default ? ' (default)' : ''} ({executor.executor_type})</option>
              {/each}
            </select>
          </label>
          <label class="space-y-1 text-sm text-secondary">
            <span>Executor selector</span>
            <textarea bind:value={shareExecutorSelector} class="min-h-[72px] w-full rounded-2xl border border-control-border bg-canvas px-3 py-2 font-mono text-sm text-foreground outline-none focus:border-accent" disabled={!!shareExecutorId} placeholder="tier=standard&#10;location=local"></textarea>
          </label>
        </div>
      </Card>
    {/if}
    {/snippet}
    {#snippet collaborationPanel()}
    {#if collaborationLoading}
      <p role="status" class="text-sm text-muted">Loading projects and sharing…</p>
    {:else}
    {@render projectsPanel()}
    {#if canManageShares()}
      <Card class="p-4 sm:p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-sm uppercase tracking-[0.22em] text-quiet">Sharing</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Share this agent</h2>
            <p class="mt-1 text-sm text-muted">Grant another Cognis user access by email address. They can use the agent but cannot edit it.</p>
          </div>
        </div>

        <form class="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]" onsubmit={createShare}>
          <label class="space-y-1 text-sm text-secondary">
            <span>Email address</span>
            <input
              bind:value={shareEmail}
              type="email"
              placeholder="user@example.com"
              class="w-full rounded-xl border border-control-border bg-canvas px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
              disabled={shareSaving}
              required
            />
          </label>
          <label class="space-y-1 text-sm text-secondary">
            <span>Executor access</span>
            <select bind:value={shareExecutorScope} class="w-full rounded-xl border border-control-border bg-canvas px-3 py-2 text-sm text-foreground outline-none focus:border-accent" disabled={shareSaving}>
              <option value="owner_executor">Use owner executor setup</option>
              <option value="grantee_executor">Use grantee executor setup</option>
            </select>
          </label>
          <label class="space-y-1 text-sm text-secondary lg:col-span-2">
            <span>Note <span class="text-quiet">optional</span></span>
            <input
              bind:value={shareNote}
              type="text"
              placeholder="Why this user has access"
              class="w-full rounded-xl border border-control-border bg-canvas px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
              disabled={shareSaving}
            />
          </label>
          <div class="lg:col-span-2">
            <Button type="submit" size="sm" disabled={shareSaving}>{shareSaving ? 'Sharing...' : 'Share agent'}</Button>
          </div>
        </form>

        {#if shareError}
          <p role="alert" class="mt-4 rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">{shareError} <button type="button" class="underline" onclick={loadShares}>Retry loading shares</button></p>
        {/if}

        <div class="mt-5 space-y-3">
          <h3 class="text-sm font-medium text-foreground">Current shares</h3>
          {#each shares as grant}
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div class="min-w-0">
                <p class="truncate text-sm font-medium text-strong">{grant.grantee_user_email ?? grant.grantee_group_id ?? 'Unknown grantee'}</p>
                <p class="mt-1 text-xs text-quiet">Permission: {grant.permission}{grant.note ? ` · ${grant.note}` : ''}</p>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <select
                  value={grant.executor_scope}
                  onchange={(event) => updateShareScope(grant, event)}
                  class="rounded-xl border border-control-border bg-canvas px-3 py-1.5 text-xs text-foreground outline-none focus:border-accent"
                  disabled={updatingShareId === grant.grant_id}
                >
                  <option value="owner_executor">Owner executor</option>
                  <option value="grantee_executor">Grantee executor</option>
                </select>
                <Button size="sm" variant="danger" disabled={updatingShareId === grant.grant_id} onclick={() => revokeShare(grant)}>Revoke</Button>
              </div>
            </div>
          {:else}
            <p class="rounded-2xl border border-dashed border-separator px-4 py-3 text-sm text-quiet">This agent is not shared with anyone yet.</p>
          {/each}
        </div>
      </Card>
    {/if}
    {/if}
    {/snippet}
    {#if optionsLoading}
      {#if error}
        <p role="alert">{error}</p>
        <Button onclick={loadAgent}>Retry configuration choices</Button>
      {:else}
        <LoadingState label="Loading configuration choices" />
      {/if}
    {:else}
    <AgentForm
      mode="edit"
      bind:form
      {tools}
      {workflows}
      {providers}
      {memoryBackends}
      {executors}
      {secrets}
      {credentials}
      {knowledgebases}
      {skills}
      {skillUsage}
      {intarisMcpServers}
      {agents}
      {secondaryAgents}
      {secondaryBindings}
      saving={saving || actionBusy}
      {error}
      {toolsLoading}
      {toolsError}
      toolsKnown={toolsLoaded}
      onRetryTools={loadTools}
      bind:activeSection
      {identityActions}
      {accessActions}
      dirty={isDirty()}
      {bindingsSaving}
      extraSections={[
        ...(agent && !agent.is_system && !agent.is_readonly_for_caller ? [{ id: 'hooks', label: 'Hooks', content: hooksPanel }] : []),
        ...(canManageShares() || (agent && !agent.is_system && agent.agent_type === 'primary') ? [{ id: 'collaboration', label: 'Collaboration', content: collaborationPanel }] : []),
      ]}
      readonly={(agent?.is_system ?? false) || (agent?.is_readonly_for_caller ?? false)}
      isSystemAsset={agent?.is_system ?? false}
      editableFields={agent?.editable_fields ?? []}
      onSave={saveAgent}
      onBindingsChange={async (bindings) => {
        bindingsSaving = true;
        try {
          await api.agents.replaceBindings(agentId, bindings);
          secondaryBindings = bindings;
          addToast('Secondary agent bindings saved.', 'success');
        } catch (caughtError) {
          error = asApiError(caughtError).message;
          addToast(error, 'error', 4_000, 'Unable to update bindings');
        } finally {
          bindingsSaving = false;
        }
      }}
    />
    {/if}
  </section>
{/if}
