<script lang="ts">
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';

  import { policyOptions, type ChannelEditorDraft, type ChannelEditorMode, type SetupGuide } from '$lib/channels';
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
  export let onClose: () => void;
  export let onSelectType: (meta: ChannelMeta) => void;
  export let onSave: () => void;

  function isSignalDirectMode(): boolean {
    return selectedType?.channel_type === 'signal' && draft.settingValues.transport === 'direct_jsonrpc';
  }

  function primaryAgents(): Agent[] {
    return agents.filter((agent) => agent.agent_type === 'primary');
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
        <h2 class="text-lg font-semibold text-white">{mode === 'edit' ? 'Edit channel account' : 'Create channel account'}</h2>
        <p class="mt-1 text-sm text-slate-400">
          {mode === 'edit'
            ? 'Update settings, policies, executor placement, or rotate stored credentials.'
            : 'Choose a platform, then follow the adapter-specific setup steps.'}
        </p>
      </div>
      {#if mobile || mode !== 'closed'}
        <!-- On mobile the editor is a full-screen overlay; the Close button
             needs a generous tap target. On desktop it stays compact. -->
        <Button aria-label="Close channel editor" variant="secondary" size={mobile ? 'default' : 'sm'} onclick={onClose}>
          <ArrowLeft class="mr-2 h-4 w-4" /> Close
        </Button>
      {/if}
    </div>

    {#if mode !== 'edit'}
      <div class="mt-5">
        <ChannelTypePicker {channelTypes} {selectedType} onSelect={onSelectType} />
      </div>
    {/if}
  </Card>

  {#if selectedType}
    <ChannelSetupGuide {guide} docsUrl={selectedType.docs_url} />

    <Card class="p-5">
      <div class="grid gap-4">
        <label class="grid gap-2 text-sm text-slate-300">
          Display name
          <Input bind:value={draft.display_name} placeholder={`${selectedType.label} Account`} />
        </label>

        <div class="grid gap-2 text-sm text-slate-300">
          <span>Agent</span>
          <AgentSelect
            agents={primaryAgents()}
            value={draft.agent_id}
            onchange={(next) => { draft.agent_id = next; }}
            placeholder="Select an agent"
            emptyLabel="No primary agents"
          />
          <span class="text-xs text-slate-500">Only primary agents can own channel accounts.</span>
        </div>

        <label class="grid gap-2 text-sm text-slate-300">
          Adapter location
          <select bind:value={draft.adapter_location} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="controller">Controller (default)</option>
            <option value="executor">Executor (remote)</option>
          </select>
        </label>

        {#if draft.adapter_location === 'executor'}
          <label class="grid gap-2 text-sm text-slate-300">
            Executor
            <select bind:value={draft.executor_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Any compatible executor</option>
              {#each compatibleExecutors() as executor}
                <option value={executor.executor_id}>{executor.name} ({executor.status})</option>
              {/each}
            </select>
            {#if isSignalDirectMode()}
              <span class="text-xs text-slate-500">
                Only executors with Signal direct mode enabled are shown here.
              </span>
            {/if}
          </label>
        {/if}

        <ChannelDynamicFields meta={selectedType} {draft} editing={mode === 'edit'} {credentialOverrides} />

        <div class="grid gap-4 md:grid-cols-2">
          <label class="grid gap-2 text-sm text-slate-300">
            DM policy
            <select bind:value={draft.dm_policy} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              {#each policyOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </label>

          <label class="grid gap-2 text-sm text-slate-300">
            Group policy
            <select bind:value={draft.group_policy} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              {#each policyOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </label>
        </div>

        <label class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-300">
          <input bind:checked={draft.allow_new_conversations} type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900" />
          Allow this adapter to create new conversations automatically when a new chat appears.
        </label>

        <label class="flex items-start gap-3 rounded-2xl border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
          <input bind:checked={draft.preferred_for_task_delivery} type="checkbox" class="mt-0.5 h-4 w-4 rounded border-slate-600 bg-slate-900" />
          <span>
            Use this account as the preferred task delivery channel.
            <span class="mt-1 block text-xs text-sky-200/75">Only one channel account per agent is preferred; saving this will replace the previous preference.</span>
          </span>
        </label>

        <div class="flex items-center justify-between gap-3">
          <p class="text-xs text-slate-500">{isDirty ? 'You have unsaved changes.' : 'No pending changes.'}</p>
          <div class="flex justify-end gap-2">
            <Button variant="secondary" onclick={onClose} disabled={busy}>Cancel</Button>
            <Button variant="primary" onclick={onSave} disabled={busy}>{mode === 'edit' ? 'Save changes' : 'Save account'}</Button>
          </div>
        </div>
      </div>
    </Card>
  {/if}
</div>
