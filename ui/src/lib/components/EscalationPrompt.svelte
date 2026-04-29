<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { Escalation } from '$lib/types/api';

  let { item, secondsRemaining, onApprove, onDeny, pending = false, queuedCount = 0 } = $props<{
    item: Escalation;
    secondsRemaining: number;
    onApprove: () => void;
    onDeny: () => void;
    pending?: boolean;
    queuedCount?: number;
  }>();

  const expired = $derived(secondsRemaining <= 0);
</script>

<article class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 shadow-card">
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] text-sky-200">Approval required</p>
      <h3 class="mt-1 text-base font-semibold text-white">{item.tool_name ?? 'Escalated action'}</h3>
    </div>
    <div class="flex items-center gap-2">
      {#if queuedCount > 0}
        <span class="rounded-full border border-sky-300/40 px-2.5 py-0.5 text-xs font-medium text-sky-200">
          +{queuedCount} queued
        </span>
      {/if}
      <span class="rounded-full border border-sky-300/40 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-sky-100">
        {expired ? 'Expired' : `${Math.max(secondsRemaining, 0)}s left`}
      </span>
    </div>
  </div>

  {#if item.session_id}
    <p class="mt-1 text-xs text-sky-300/60">Session: {item.session_id.slice(0, 12)}...</p>
  {/if}

  <div class="mt-3 space-y-2 text-sm leading-6 text-sky-50">
    {#if item.risk}
      <p><span class="font-semibold">Risk:</span> {item.risk}</p>
    {/if}
    {#if item.reasoning}
      <p><span class="font-semibold">Reasoning:</span> {item.reasoning}</p>
    {/if}
  </div>

  <p class="mt-3 text-xs text-sky-200/70">
    You can also type <code class="rounded bg-sky-900/40 px-1 py-0.5">/approve</code> or <code class="rounded bg-sky-900/40 px-1 py-0.5">/deny</code> in the chat input.
  </p>

  <div class="mt-4 flex flex-wrap gap-2">
    <Button disabled={pending || expired} size="sm" onclick={onApprove}>Approve</Button>
    <Button disabled={pending || expired} size="sm" variant="danger" onclick={onDeny}>Deny</Button>
  </div>
</article>
