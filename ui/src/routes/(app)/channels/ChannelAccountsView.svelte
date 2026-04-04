<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { ChannelAccount, ChannelMeta } from '$lib/types/api';

  import ChannelAccountCard from './ChannelAccountCard.svelte';

  export let accounts: ChannelAccount[] = [];
  export let metas: Record<string, ChannelMeta> = {};
  export let busy = false;
  export let selectedAccountId: string | null = null;
  export let agentName: (agentId: string) => string;
  export let pendingCount: (accountId: string) => number;
  export let onCreate: () => void;
  export let onEdit: (account: ChannelAccount) => void;
  export let onToggle: (account: ChannelAccount) => void;
  export let onDelete: (account: ChannelAccount) => void;
</script>

<div class="space-y-4">
  <div class="flex items-center justify-between gap-3">
    <div>
      <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Accounts</p>
      <h2 class="mt-1 text-lg font-semibold text-white">Messaging accounts</h2>
    </div>
    <Button size="sm" onclick={onCreate}>New account</Button>
  </div>

  {#if accounts.length === 0}
    <Card class="p-6 text-sm text-slate-300">
      No channel accounts yet. Start by adding a platform and follow the setup guide.
    </Card>
  {/if}

  {#each accounts as account (account.account_id)}
    <ChannelAccountCard
      {account}
      meta={metas[account.channel_type] ?? null}
      agentName={agentName(account.agent_id)}
      pendingCount={pendingCount(account.account_id)}
      {busy}
      selected={selectedAccountId === account.account_id}
      onEdit={() => onEdit(account)}
      onToggle={() => onToggle(account)}
      onDelete={() => onDelete(account)}
    />
  {/each}
</div>
