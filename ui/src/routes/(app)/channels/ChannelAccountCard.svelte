<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Pencil from 'lucide-svelte/icons/pencil';
  import MessagesSquare from 'lucide-svelte/icons/messages-square';
  import Trash2 from 'lucide-svelte/icons/trash-2';
  import XCircle from 'lucide-svelte/icons/x-circle';

  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { statusClass, statusText } from '$lib/channels';
  import type { ChannelAccount, ChannelMeta } from '$lib/types/api';

  export let account: ChannelAccount;
  export let meta: ChannelMeta | null = null;
  export let agentName = '';
  export let pendingCount = 0;
  export let busy = false;
  export let selected = false;
  export let onEdit: () => void;
  export let onToggle: () => void;
  export let onDelete: () => void;
</script>

<Card class={`p-5 ${selected ? 'border-accent/40 bg-accent-subtle' : ''}`}>
  <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
    <div class="min-w-0 space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <div class="inline-flex items-center gap-2 rounded-full border border-control-border px-3 py-1 text-xs text-foreground">
          <MessagesSquare class="h-3.5 w-3.5 text-accent" />
          <span>{meta?.label ?? account.channel_type}</span>
        </div>
        <span class={`rounded-full px-3 py-1 text-xs font-medium ${statusClass(account)}`}>{statusText(account)}</span>
        {#if pendingCount > 0}
          <span class="rounded-full border border-accent/30 bg-accent-subtle px-3 py-1 text-xs text-accent">{pendingCount} pending pairing</span>
        {/if}
        {#if account.preferred_for_task_delivery}
          <span class="rounded-full border border-success-border bg-success-subtle px-3 py-1 text-xs text-success">Preferred</span>
        {/if}
        {#if account.channel_type !== 'web' && meta?.connection_mode}
          <span class="rounded-full border border-control-border px-3 py-1 text-xs text-secondary">{meta.connection_mode}</span>
        {/if}
      </div>
      <div>
        <h2 class="text-lg font-semibold text-strong">{account.display_name}</h2>
        <p class="mt-1 text-sm text-secondary">Agent: {agentName}</p>
      </div>
      {#if account.channel_type === 'web'}
        <p class="text-xs text-muted">Managed by Cognis. Always available in this agent's main app chat.</p>
      {:else}
      <div class="flex flex-wrap gap-2 text-xs text-muted">
        <span class="rounded-full border border-control-border px-3 py-1">DM: {account.dm_policy}</span>
        <span class="rounded-full border border-control-border px-3 py-1">Groups: {account.group_policy}</span>
        <span class="rounded-full border border-control-border px-3 py-1">New conversations: {account.allow_new_conversations ? 'yes' : 'no'}</span>
        {#if account.adapter_location === 'executor'}
          <span class="rounded-full border border-cyan-border bg-cyan-subtle px-3 py-1 text-cyan-tone">executor</span>
        {/if}
      </div>
      {/if}
      {#if account.status && 'last_error' in account.status && account.status.last_error}
        <p class="text-sm text-danger">Last error: {account.status.last_error}</p>
      {/if}
    </div>
    <div class="flex flex-wrap gap-2">
      {#if account.channel_type !== 'web'}
      <Button variant="secondary" size="sm" onclick={onToggle} disabled={busy}>
        {#if statusText(account) === 'connected'}
          <XCircle class="mr-2 h-4 w-4" /> Stop
        {:else}
          <CheckCircle2 class="mr-2 h-4 w-4" /> Start
        {/if}
      </Button>
      {/if}
      <Button variant="secondary" size="sm" onclick={onEdit}>
        <Pencil class="mr-2 h-4 w-4" /> Edit
      </Button>
      {#if account.channel_type !== 'web'}
      <Button variant="danger" size="sm" onclick={onDelete} disabled={busy}>
        <Trash2 class="mr-2 h-4 w-4" /> Delete
      </Button>
      {/if}
    </div>
  </div>
</Card>
