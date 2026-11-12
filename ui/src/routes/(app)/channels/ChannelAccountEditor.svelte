<script lang="ts">
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';

  import { policyOptions, type ChannelEditorDraft, type ChannelEditorMode, type SetupGuide } from '$lib/channels';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { Agent, ChannelMeta, ExecutorConfig } from '$lib/types/api';

  import ChannelDynamicFields from './ChannelDynamicFields.svelte';
  import ChannelSetupGuide from './ChannelSetupGuide.svelte';
  import ChannelTypePicker from './ChannelTypePicker.svelte';

  export let mode: ChannelEditorMode = 'closed';
  export let selectedType: ChannelMeta | null = null;
  export let channelTypes: ChannelMeta[] = [];
  export let draft: ChannelEditorDraft;
  export let credentialOverrides: Record<string, string> = {};
  export let agents: Agent[] = [];
  export let executors: ExecutorConfig[] = [];
  export let guide: SetupGuide | null = null;
  export let busy = false;
  export let mobile = false;
  export let isDirty = false;
  export let onlyChannel = false;
  export let onClose: () => void;
  export let onSelectType: (meta: ChannelMeta) => void;
  export let onSave: () => void;

  function isSignalDirectMode(): boolean {
    return selectedType?.channel_type === 'signal' && draft.settingValues.transport === 'direct_jsonrpc';
  }

  function primaryAgents(): Agent[] {
    return agents.filter((agent) => agent.agent_type === 'primary');
  }

  function selectedAgent(): Agent | undefined {
    return primaryAgents().find((agent) => agent.agent_id === draft.agent_id);
  }

  function selectAgent(agentId: string): void {
    draft.agent_id = agentId;
    draft.default_agent_profile_id = normalizeSelectedAgentProfileId(
      selectedAgent(),
      draft.default_agent_profile_id,
    );
  }

  function compatibleExecutors(): ExecutorConfig[] {
    return executors.filter((executor) => {
      if (executor.status !== 'active') return false;
      if (!isSignalDirectMode()) return true;
      const signalConfig = (executor.config?.signal ?? {}) as Record<string, unknown>;
      return signalConfig.direct_enabled === true;
    });
  }
</script>

<div class="space-y-4" data-testid="channels-editor">
  <Card class="p-5">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h2 id="channel-editor-title" class="text-lg font-semibold text-strong">{mode === 'edit' ? 'Edit channel account' : 'Create channel account'}</h2>
        <p class="mt-1 text-sm text-muted">
          {selectedType?.channel_type === 'web'
            ? 'Built into Cognis and always available for this agent.'
            : mode === 'edit'
            ? 'Update settings, policies, executor placement, or rotate stored credentials.'
            : 'Choose a platform, then follow the adapter-specific setup steps.'}
        </p>
      </div>
      {#if mobile || mode !== 'closed'}
        <!-- On mobile the editor is a full-screen overlay; the Close button
             needs a generous tap target. On desktop it stays compact. -->
        <Button aria-label="Close channel editor" variant="secondary" size={mobile ? 'default' : 'sm'} onclick={onClose} disabled={busy}>
          <ArrowLeft class="mr-2 h-4 w-4" /> Close
        </Button>
      {/if}
    </div>

    {#if mode !== 'edit'}
      <div class="mt-5">
        <ChannelTypePicker channelTypes={channelTypes.filter((meta) => meta.channel_type !== 'web')} {selectedType} onSelect={onSelectType} />
      </div>
    {/if}
  </Card>

  {#if selectedType?.channel_type === 'web'}
    <Card class="space-y-4 p-5">
      <p class="text-sm text-secondary">Cognis app is always enabled. This channel uses the agent's main app chat.</p>
      <label class="flex items-start gap-3 text-sm text-secondary">
        <input bind:checked={draft.preferred_for_task_delivery} disabled={onlyChannel || busy} type="checkbox" class="mt-0.5 h-4 w-4" />
        Use Cognis app as this agent's preferred channel.
      </label>
      <p class="text-sm text-muted">Managed by Cognis; it cannot be added, removed, or disabled.</p>
      {#if onlyChannel}
        <p class="text-sm text-muted">Selected automatically because this agent has no other channels.</p>
      {/if}
      <Button onclick={onSave} disabled={busy || !isDirty}>Save preference</Button>
    </Card>
  {:else if selectedType}
    <ChannelSetupGuide {guide} docsUrl={selectedType.docs_url} />

    <Card class="p-5">
      <div class="grid gap-4">
        <label class="grid gap-2 text-sm text-secondary">
          Display name
          <Input bind:value={draft.display_name} placeholder={`${selectedType.label} Account`} />
        </label>

        <div class="grid gap-2 text-sm text-secondary">
          <span>Agent</span>
          <AgentSelect
            agents={primaryAgents()}
            value={draft.agent_id}
            onchange={selectAgent}
            placeholder="Select an agent"
            emptyLabel="No primary agents"
          />
          <span class="text-xs text-quiet">Only primary agents can own channel accounts.</span>
        </div>

        <AgentProfileSelect
          agents={primaryAgents()}
          agentId={draft.agent_id}
          bind:value={draft.default_agent_profile_id}
          label="Default runtime profile"
          help="Used by interactive conversations on this channel unless the conversation or session selects an explicit profile."
        />

        <label class="grid gap-2 text-sm text-secondary">
          Adapter location
          <select bind:value={draft.adapter_location} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="controller">Controller (default)</option>
            <option value="executor">Executor (remote)</option>
          </select>
        </label>

        {#if draft.adapter_location === 'executor'}
          <label class="grid gap-2 text-sm text-secondary">
            Executor
            <select bind:value={draft.executor_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">Any compatible executor</option>
              {#each compatibleExecutors() as executor}
                <option value={executor.executor_id}>{executor.name} ({executor.status})</option>
              {/each}
            </select>
            {#if isSignalDirectMode()}
              <span class="text-xs text-quiet">
                Only executors with Signal direct mode enabled are shown here.
              </span>
            {/if}
          </label>
        {/if}

        <ChannelDynamicFields meta={selectedType} {draft} editing={mode === 'edit'} {credentialOverrides} />

        <div class="grid gap-4 md:grid-cols-2">
          <label class="grid gap-2 text-sm text-secondary">
            DM policy
            <select bind:value={draft.dm_policy} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              {#each policyOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="text-xs text-quiet">
              Controls direct Matrix chats. Use Disabled to prevent DMs entirely.
            </span>
          </label>

          <label class="grid gap-2 text-sm text-secondary">
            Group policy
            <select bind:value={draft.group_policy} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              {#each policyOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="text-xs text-quiet">
              Controls group rooms after any Matrix room allowlist and mention gate.
            </span>
          </label>
        </div>

        <label class="grid gap-2 text-sm text-secondary">
          Allowed Matrix senders
          <textarea
            bind:value={draft.allowed_senders}
            rows="3"
            placeholder="@filip:fpy.cz, @teammate:matrix.org"
            class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
          ></textarea>
          <span class="text-xs text-quiet">
            One Matrix user ID per line or comma-separated. Used only when DM policy or group policy is Allowlist.
          </span>
        </label>

        <label class="flex items-center gap-3 rounded-2xl border border-separator bg-canvas/60 px-4 py-3 text-sm text-secondary">
          <input bind:checked={draft.allow_new_conversations} type="checkbox" class="h-4 w-4 rounded border-control-border bg-surface" />
          Allow this adapter to create new conversations automatically when a new chat appears.
        </label>

        <label class="flex items-start gap-3 rounded-2xl border border-accent/20 bg-accent-subtle px-4 py-3 text-sm text-accent">
          <input bind:checked={draft.preferred_for_task_delivery} type="checkbox" class="mt-0.5 h-4 w-4 rounded border-control-border bg-surface" />
          <span>
            Use this account as the preferred channel for this agent.
            <span class="mt-1 block text-xs text-accent/75">Only one channel account per agent is preferred; saving this will replace the previous preference.</span>
          </span>
        </label>

        <div class="flex items-center justify-between gap-3">
          <p class="text-xs text-quiet">{isDirty ? 'You have unsaved changes.' : 'No pending changes.'}</p>
          <div class="flex justify-end gap-2">
            <Button variant="secondary" onclick={onClose} disabled={busy}>Cancel</Button>
            <Button variant="primary" onclick={onSave} disabled={busy}>{mode === 'edit' ? 'Save changes' : 'Save account'}</Button>
          </div>
        </div>
      </div>
    </Card>
  {/if}
</div>
