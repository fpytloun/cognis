<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { Escalation } from '$lib/types/api';

  let { item, secondsRemaining, onApprove, onDeny, pending = false } = $props<{
    item: Escalation;
    secondsRemaining: number;
    onApprove: () => void;
    onDeny: () => void;
    pending?: boolean;
  }>();
</script>

<article class="rounded-3xl border border-amber-500/30 bg-amber-500/10 px-4 py-4 shadow-card">
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] text-amber-200">Approval required</p>
      <h3 class="mt-1 text-base font-semibold text-white">{item.tool_name ?? 'Escalated action'}</h3>
    </div>
    <span class="rounded-full border border-amber-300/40 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-amber-100">
      {Math.max(secondsRemaining, 0)}s left
    </span>
  </div>

  <div class="mt-3 space-y-2 text-sm leading-6 text-amber-50">
    {#if item.risk}
      <p><span class="font-semibold">Risk:</span> {item.risk}</p>
    {/if}
    {#if item.reasoning}
      <p><span class="font-semibold">Reasoning:</span> {item.reasoning}</p>
    {/if}
  </div>

  <div class="mt-4 flex flex-wrap gap-2">
    <Button disabled={pending} size="sm" onclick={onApprove}>Approve</Button>
    <Button disabled={pending} size="sm" variant="danger" onclick={onDeny}>Deny</Button>
  </div>
</article>
