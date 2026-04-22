<script lang="ts">
  import type { Agent } from '$lib/types/api';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';

  let {
    agents,
    selectedAgentId = $bindable(''),
    title = 'New chat',
    description = 'Choose the primary agent for the new conversation.',
    confirmLabel = 'Create conversation',
    busy = false,
    error = '',
    oncancel,
    onconfirm,
  } = $props<{
    agents: Agent[];
    selectedAgentId?: string;
    title?: string;
    description?: string;
    confirmLabel?: string;
    busy?: boolean;
    error?: string;
    oncancel: () => void;
    onconfirm: () => void;
  }>();

  const primaryAgents = $derived(
    agents.filter((agent: Agent) => agent.agent_type === 'primary' && agent.status === 'active')
  );
</script>

<BlockingDialog label="New chat dialog" onClose={() => !busy && oncancel()} titleId="new-chat-title">
  {#snippet header()}
    <div>
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Conversation setup</p>
      <h2 class="mt-3 text-xl font-semibold text-white" id="new-chat-title">{title}</h2>
      <p class="mt-3 text-sm leading-6 text-slate-300">{description}</p>
    </div>
  {/snippet}

  {#snippet children()}
    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class={error ? 'mt-5' : ''}>
      <AgentSelect
        label="Primary agent"
        agents={primaryAgents}
        value={selectedAgentId}
        onchange={(next) => { selectedAgentId = next; }}
        disabled={busy || primaryAgents.length === 0}
        emptyLabel="No active primary agents"
        placeholder="Select an agent"
      />
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="flex flex-wrap justify-end gap-3">
      <Button variant="secondary" disabled={busy} onclick={oncancel}>Cancel</Button>
      <Button disabled={busy || !selectedAgentId} onclick={onconfirm}>{busy ? 'Creating...' : confirmLabel}</Button>
    </div>
  {/snippet}
</BlockingDialog>
