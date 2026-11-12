<script lang="ts">
  import type { UsageAvailability } from './model';
  /** One-line state when part of the range has no request detail. */
  let { value }: { value?: UsageAvailability } = $props();
  const cutoff = $derived(value?.cutoff ? new Date(value.cutoff).toLocaleDateString('en', { month: 'short', day: 'numeric' }) : null);
  const text = $derived(!value ? null
    : value.resolution === 'detail_expired' || value.resolution === 'aggregate_only' ? `Request details expired${cutoff ? ` before ${cutoff}` : ''} · totals only`
    : value.resolution === 'mixed' ? `Request details kept${cutoff ? ` since ${cutoff}` : ''} · older days are totals only`
    : null);
</script>
{#if text}<p class="mb-3 text-xs text-secondary" aria-label="Usage retention">{text}</p>{/if}
