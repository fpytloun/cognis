<script lang="ts">
  import type { Agent, Project } from '$lib/types/api';
  import ProjectSelect from '$lib/components/projects/ProjectSelect.svelte';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';

  let {
    agents,
    selectedAgentId = $bindable(''),
    selectedAgentProfileId = $bindable(''),
    selectedAgentProfileMode = $bindable(null),
    projects = [],
    selectedProjectId = $bindable(''),
    title = 'New chat',
    description = 'Choose the primary agent for the new conversation.',
    confirmLabel = 'Create conversation',
    profileOnly = false,
    busy = false,
    error = '',
    oncancel,
    onconfirm,
  } = $props<{
    agents: Agent[];
    selectedAgentId?: string;
    selectedAgentProfileId?: string;
    selectedAgentProfileMode?: 'auto' | 'default' | null;
    projects?: Project[];
    selectedProjectId?: string;
    title?: string;
    description?: string;
    confirmLabel?: string;
    profileOnly?: boolean;
    busy?: boolean;
    error?: string;
    oncancel: () => void;
    onconfirm: () => void;
  }>();

  const primaryAgents = $derived(
    agents.filter((agent: Agent) => agent.agent_type === 'primary' && agent.status === 'active')
  );
  const selectedAgent = $derived(primaryAgents.find((agent: Agent) => agent.agent_id === selectedAgentId) ?? null);

  $effect(() => {
    if (!profileOnly) selectedAgentProfileId = normalizeSelectedAgentProfileId(selectedAgent, selectedAgentProfileId);
  });

  // Agents shared only through projects can be used only inside those projects.
  const projectOnlyIds = $derived<string[]>(selectedAgent?.shared_via_project_ids ?? []);
  const projectChoices = $derived(
    projectOnlyIds.length > 0
      ? projects.filter((project: Project) => projectOnlyIds.includes(project.project_id))
      : projects
  );

  $effect(() => {
    if (projectOnlyIds.length > 0 && !projectOnlyIds.includes(selectedProjectId)) {
      selectedProjectId = projectOnlyIds[0] ?? '';
    }
  });
</script>

<BlockingDialog label="New chat dialog" onClose={() => !busy && oncancel()} titleId="new-chat-title">
  {#snippet header()}
    <div>
      <p class="text-sm uppercase tracking-[0.25em] text-muted">Conversation setup</p>
      <h2 class="mt-3 text-xl font-semibold text-strong" id="new-chat-title">{title}</h2>
      <p class="mt-3 text-sm leading-6 text-secondary">{description}</p>
    </div>
  {/snippet}

  {#snippet children()}
    {#if error}
      <p class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">{error}</p>
    {/if}

    <div class={error ? 'mt-5' : ''}>
      {#if !profileOnly}
      <label class="block space-y-1">
        <span class="block text-sm font-medium text-foreground">Primary agent</span>
        <select
          bind:value={selectedAgentId}
          class="w-full rounded-xl border border-control-border bg-surface px-3 py-2 text-sm text-foreground focus:border-accent"
          disabled={busy || primaryAgents.length === 0}
        >
          <option value="" disabled>{primaryAgents.length === 0 ? 'No active primary agents' : 'Select an agent'}</option>
          {#each primaryAgents as agent (agent.agent_id)}
            <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
          {/each}
        </select>
      </label>
      {/if}
      <div class={profileOnly ? '' : 'mt-4'}>
        <AgentProfileSelect
          agents={primaryAgents}
          agentId={selectedAgentId}
          bind:value={selectedAgentProfileId}
          bind:mode={selectedAgentProfileMode}
          allowAuto
          disabled={busy}
          help={profileOnly ? '' : 'Auto selects once at startup and keeps the default unless confident. Default skips classification; named profiles are explicit selections.'}
        />
      </div>
      {#if projects.length > 0 && !profileOnly}
        <div class="mt-4" data-testid="new-chat-project">
          <ProjectSelect
            projects={projectChoices}
            bind:value={selectedProjectId}
            disabled={busy}
            id="new-chat-project"
            noneLabel={projectOnlyIds.length > 0 ? 'Select a project' : "No project (agent default applies)"}
          />
          {#if projectOnlyIds.length > 0}
            <p class="mt-2 text-xs text-muted">This agent is shared with you through a project and works only there.</p>
          {/if}
        </div>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="flex flex-wrap justify-end gap-3">
      <Button variant="secondary" disabled={busy} onclick={oncancel}>Cancel</Button>
      <Button disabled={busy || !selectedAgentId} onclick={onconfirm}>{busy ? 'Creating...' : confirmLabel}</Button>
    </div>
  {/snippet}
</BlockingDialog>
