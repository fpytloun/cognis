<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Link2 from 'lucide-svelte/icons/link-2';
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

<Card class={`p-5 ${selected ? 'border-sky-400/40 bg-sky-500/10' : ''}`}>
  <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
    <div class="min-w-0 space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <div class="inline-flex items-center gap-2 rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-200">
          <MessagesSquare class="h-3.5 w-3.5 text-sky-300" />
          <span>{meta?.label ?? account.channel_type}</span>
        </div>
        <span class={`rounded-full px-3 py-1 text-xs font-medium ${statusClass(account)}`}>{statusText(account)}</span>
        {#if pendingCount > 0}
          <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-3 py-1 text-xs text-sky-300">{pendingCount} pending pairing</span>
        {/if}
        {#if account.preferred_for_task_delivery}
          <span class="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-xs text-emerald-300">Preferred task delivery</span>
        {/if}
        {#if meta?.connection_mode}
          <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{meta.connection_mode}</span>
        {/if}
      </div>
      <div>
        <h2 class="text-lg font-semibold text-white">{account.display_name}</h2>
        <p class="mt-1 text-sm text-slate-300">Agent: {agentName}</p>
      </div>
      <div class="flex flex-wrap gap-2 text-xs text-slate-400">
        <span class="rounded-full border border-slate-700 px-3 py-1">DM: {account.dm_policy}</span>
        <span class="rounded-full border border-slate-700 px-3 py-1">Groups: {account.group_policy}</span>
        <span class="rounded-full border border-slate-700 px-3 py-1">New conversations: {account.allow_new_conversations ? 'yes' : 'no'}</span>
        {#if account.adapter_location === 'executor'}
          <span class="rounded-full border border-cyan-500/20 bg-cyan-500/10 px-3 py-1 text-cyan-300">executor</span>
        {/if}
      </div>
      {#if account.status && 'last_error' in account.status && account.status.last_error}
        <p class="text-sm text-rose-300">Last error: {account.status.last_error}</p>
      {/if}
    </div>
    <div class="flex flex-wrap gap-2">
      <Button variant="secondary" size="sm" onclick={onToggle} disabled={busy}>
        {#if statusText(account) === 'connected'}
          <XCircle class="mr-2 h-4 w-4" /> Stop
        {:else}
          <CheckCircle2 class="mr-2 h-4 w-4" /> Start
        {/if}
      </Button>
      <Button variant="secondary" size="sm" onclick={onEdit}>
        <Link2 class="mr-2 h-4 w-4" /> Open
      </Button>
      <Button variant="danger" size="sm" onclick={onDelete} disabled={busy}>
        <Trash2 class="mr-2 h-4 w-4" /> Delete
      </Button>
    </div>
  </div>
</Card>
