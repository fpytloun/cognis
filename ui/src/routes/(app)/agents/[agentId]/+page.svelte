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
  import Card from '$lib/components/ui/Card.svelte';
  import { installBeforeUnloadGuard, blockNavigationIfDirty } from '$lib/navigation/unsaved';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, AgentGrant, CredentialMetadata, EffectiveToolItem, ExecutorConfig, IntarisMCPServer, LLMProvider, SecretMetadata, Skill, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let agent = $state<Agent | null>(null);
  let tools = $state<EffectiveToolItem[]>([]);
  let workflows = $state<Workflow[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let skills = $state<Skill[]>([]);
  let intarisMcpServers = $state<IntarisMCPServer[]>([]);
  let secondaryAgents = $state<Agent[]>([]);
  let secondaryBindings = $state<string[]>([]);
  let shares = $state<AgentGrant[]>([]);
  let shareEmail = $state('');
  let shareExecutorScope = $state<'owner_executor' | 'grantee_executor'>('owner_executor');
  let shareNote = $state('');
  let shareSaving = $state(false);
  let shareError = $state('');
  let updatingShareId = $state<string | null>(null);
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
    editable_fields: [],
    has_overrides: false,
    disabled: false,
    disableable: false,
    sync_metadata: null,
    is_shared_with_me: false,
    shared_by_email: null,
    granted_permission: null,
    executor_scope: null,
    is_readonly_for_caller: false,
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

  function canManageShares(): boolean {
    const currentUser = auth.getSnapshot().user;
    return !!agent && !agent.is_system && !agent.is_shared_with_me && agent.owner_email === currentUser?.email;
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
    if (saving) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function loadAgent(): Promise<void> {
    loading = true;
    try {
      [agent, workflows, secrets, credentials, skills, intarisMcpServers, secondaryAgents, secondaryBindings] = await Promise.all([
        api.agents.detail(agentIdFromRoute()),
        api.workflows.listAll(),
        api.secrets.list(),
        api.credentials.list().catch(() => []),
        api.skills.list().catch(() => []),
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
      // Load the full tool catalog without disabled filters so unchecked
      // tools remain visible and can be re-enabled.
      const fullCatalogPayload = formStateToEffectiveToolsPreviewPayload({
        ...form,
        disabledTools: [],
        disabledCategories: [],
      });
      const fullCatalog = await api.agents.previewEffectiveTools(fullCatalogPayload);
      tools = fullCatalog.configured_state.tools;
      await loadShares();
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
    try {
      await api.agents.resetOverrides(agentIdFromRoute());
      await loadAgent();
      addToast('System agent overrides reset.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to reset overrides');
    }
  }

  async function toggleSystemDisabled(): Promise<void> {
    if (!agent?.is_system) return;
    try {
      if (agent.disabled) {
        await api.agents.enableSystem(agent.agent_id);
      } else {
        await api.agents.disableSystem(agent.agent_id);
      }
      await loadAgent();
      addToast(agent.disabled ? 'System agent enabled.' : 'System agent disabled.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update system agent state');
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
    // Load the full tool catalog without disabled filters so unchecked
    // tools remain visible and can be re-enabled. Only executor/skills
    // changes should refresh the catalog.
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
      <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 text-sm text-sky-100">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="font-medium">Personality was not synced to Mnemory.</p>
            <p class="mt-1 text-sky-50/90">{agent.personality_sync_error ?? 'Retry the sync to bootstrap this agent into memory.'}</p>
          </div>
          <Button size="sm" variant="secondary" onclick={retrySyncPersonality}>Retry sync</Button>
        </div>
      </div>
    {/if}
    {#if agent?.is_system}
      <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 text-sm text-sky-100">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="font-medium">System agent</p>
            <p class="mt-1 text-sky-100/80">This shipped agent stays immutable. You can only tune selected runtime fields here, or duplicate it for full customization.</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" onclick={duplicateSystemAgent}>Duplicate</Button>
            <Button size="sm" variant="secondary" onclick={resetOverrides} disabled={!agent.has_overrides}>Reset overrides</Button>
            {#if agent.disableable}
              <Button size="sm" variant="secondary" onclick={toggleSystemDisabled}>{agent.disabled ? 'Enable' : 'Disable'}</Button>
            {/if}
          </div>
        </div>
      </div>
    {/if}
    {#if agent?.is_shared_with_me}
      <div class="rounded-2xl border border-cyan-500/30 bg-cyan-500/10 px-4 py-4 text-sm text-cyan-100">
        <p class="font-medium">Shared agent</p>
        <p class="mt-1 text-cyan-100/80">Shared by {agent.shared_by_email ?? agent.owner_email}. You can use this agent, but only the owner can edit or manage sharing.</p>
      </div>
    {/if}
    {#if canManageShares()}
      <Card class="p-4 sm:p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-sm uppercase tracking-[0.22em] text-slate-500">Sharing</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Share this agent</h2>
            <p class="mt-1 text-sm text-slate-400">Grant another Cognis user access by email address. They can use the agent but cannot edit it.</p>
          </div>
        </div>

        <form class="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]" onsubmit={createShare}>
          <label class="space-y-1 text-sm text-slate-300">
            <span>Email address</span>
            <input
              bind:value={shareEmail}
              type="email"
              placeholder="user@example.com"
              class="w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-400"
              disabled={shareSaving}
              required
            />
          </label>
          <label class="space-y-1 text-sm text-slate-300">
            <span>Executor access</span>
            <select bind:value={shareExecutorScope} class="w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-400" disabled={shareSaving}>
              <option value="owner_executor">Use owner executor setup</option>
              <option value="grantee_executor">Use grantee executor setup</option>
            </select>
          </label>
          <label class="space-y-1 text-sm text-slate-300 lg:col-span-2">
            <span>Note <span class="text-slate-500">optional</span></span>
            <input
              bind:value={shareNote}
              type="text"
              placeholder="Why this user has access"
              class="w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-400"
              disabled={shareSaving}
            />
          </label>
          <div class="lg:col-span-2">
            <Button type="submit" size="sm" disabled={shareSaving}>{shareSaving ? 'Sharing...' : 'Share agent'}</Button>
          </div>
        </form>

        {#if shareError}
          <p class="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{shareError}</p>
        {/if}

        <div class="mt-5 space-y-3">
          <h3 class="text-sm font-medium text-slate-200">Current shares</h3>
          {#each shares as grant}
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="min-w-0">
                <p class="truncate text-sm font-medium text-white">{grant.grantee_user_email ?? grant.grantee_group_id ?? 'Unknown grantee'}</p>
                <p class="mt-1 text-xs text-slate-500">Permission: {grant.permission}{grant.note ? ` · ${grant.note}` : ''}</p>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <select
                  value={grant.executor_scope}
                  onchange={(event) => updateShareScope(grant, event)}
                  class="rounded-xl border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 outline-none focus:border-sky-400"
                  disabled={updatingShareId === grant.grant_id}
                >
                  <option value="owner_executor">Owner executor</option>
                  <option value="grantee_executor">Grantee executor</option>
                </select>
                <Button size="sm" variant="danger" disabled={updatingShareId === grant.grant_id} onclick={() => revokeShare(grant)}>Revoke</Button>
              </div>
            </div>
          {:else}
            <p class="rounded-2xl border border-dashed border-slate-800 px-4 py-3 text-sm text-slate-500">This agent is not shared with anyone yet.</p>
          {/each}
        </div>
      </Card>
    {/if}
    <AgentForm
      mode="edit"
      {form}
      {tools}
      {workflows}
      {providers}
      {executors}
      {secrets}
      {credentials}
      {skills}
      {intarisMcpServers}
      {secondaryAgents}
      {secondaryBindings}
      {saving}
      {error}
      readonly={(agent?.is_system ?? false) || (agent?.is_readonly_for_caller ?? false)}
      isSystemAsset={agent?.is_system ?? false}
      editableFields={agent?.editable_fields ?? []}
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
