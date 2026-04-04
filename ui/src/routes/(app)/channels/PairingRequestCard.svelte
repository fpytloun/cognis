<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import XCircle from 'lucide-svelte/icons/x-circle';

  import { formatRemaining } from '$lib/channels';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { PairingRequest } from '$lib/types/api';

  export let request: PairingRequest;
  export let busy = false;
  export let onApprove: () => void;
  export let onReject: () => void;
</script>

<Card class="p-5">
  <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between" data-testid="pairing-request-card">
    <div>
      <div class="flex flex-wrap items-center gap-2">
        <h2 class="text-lg font-semibold text-white">{request.sender_name || request.sender_id}</h2>
        <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{request.channel_type}</span>
        <span class="rounded-full border border-amber-500/20 bg-amber-500/10 px-3 py-1 text-xs text-amber-300">{formatRemaining(request.expires_at)}</span>
      </div>
      <div class="mt-3 space-y-1 text-sm text-slate-400">
        <p>Account: {request.account_display_name || request.account_id}</p>
        <p>Agent: {request.agent_name || request.agent_id || 'Unknown agent'}</p>
        <p>Sender ID: {request.sender_id}</p>
        <p>Chat: {request.chat_name || request.chat_id}</p>
      </div>
      <code class="mt-3 inline-block rounded-xl bg-slate-950/80 px-3 py-2 text-sm text-slate-100">{request.code}</code>
    </div>
    <div class="flex gap-2">
      <Button variant="secondary" size="sm" onclick={onApprove} disabled={busy}>
        <CheckCircle2 class="mr-2 h-4 w-4" /> Approve
      </Button>
      <Button variant="danger" size="sm" onclick={onReject} disabled={busy}>
        <XCircle class="mr-2 h-4 w-4" /> Reject
      </Button>
    </div>
  </div>
</Card>
