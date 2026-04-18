<script lang="ts">
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';

  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { PairingRequest } from '$lib/types/api';

  import PairingRequestCard from './PairingRequestCard.svelte';

  export let pairingRequests: PairingRequest[] = [];
  export let redeemCode = '';
  export let busy = false;
  export let onRedeem: () => void;
  export let onRedeemCodeChange: (value: string) => void;
  export let onApprove: (request: PairingRequest) => void;
  export let onReject: (requestId: string) => void;
</script>

<div class="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
  <div class="space-y-4">
    <div>
      <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Pairing inbox</p>
      <h2 class="mt-1 text-lg font-semibold text-white">Pending remote sender approvals</h2>
    </div>
    {#if pairingRequests.length === 0}
      <Card class="p-6 text-sm text-slate-300">No pending pairing requests. New remote senders will appear here when they ask to connect.</Card>
    {/if}
    {#each pairingRequests as request (request.request_id)}
      <PairingRequestCard {request} {busy} onApprove={() => onApprove(request)} onReject={() => onReject(request.request_id)} />
    {/each}
  </div>

  <Card class="p-5">
    <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Manual redeem</p>
    <h3 class="mt-1 text-lg font-semibold text-white">Redeem pairing code</h3>
    <p class="mt-1 text-sm text-slate-400">Use this only when you already have the short-lived code from the remote chat.</p>
    <div class="mt-5 grid gap-4">
      <label class="grid gap-2 text-sm text-slate-300">
        Pairing code
        <Input value={redeemCode} placeholder="ABC-123" oninput={(event) => onRedeemCodeChange((event.currentTarget as HTMLInputElement).value)} />
      </label>
      <div class="flex justify-end">
        <Button variant="primary" onclick={onRedeem} disabled={busy}>
          <CheckCircle2 class="mr-2 h-4 w-4" /> Redeem code
        </Button>
      </div>
    </div>
  </Card>
</div>
