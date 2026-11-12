<script lang="ts">
  import type { TargetSummary } from '$lib/feedback/target-summary';
  let { summary, onLocate }: { summary: TargetSummary; onLocate?: () => void } = $props();
  // A quotation identifies plain text targets by itself; structural targets keep their scope label.
  const label = $derived(summary.quote && /^(Selected text|Observed text)$/.test(summary.label) ? null : summary.label);
</script>
{#snippet body()}
  {#if label}<span class="block truncate text-xs font-medium text-muted" title={label}>{label}</span>{/if}
  {#if summary.quote}<span class="line-clamp-2 break-words text-xs italic text-foreground/80">“{summary.quote}”</span>{/if}
  {#if summary.quoteKind === 'observed'}<span class="sr-only">Observed in the rendered page</span>{/if}
{/snippet}
{#if onLocate}
  <button type="button" class="block w-full rounded-r-md border-l-2 border-accent/60 py-0.5 pl-2 text-left transition-colors hover:bg-surface-hover" title="Show in source" onclick={onLocate}>{@render body()}</button>
{:else}
  <div class="border-l-2 border-accent/60 py-0.5 pl-2" data-feedback-target-summary>{@render body()}</div>
{/if}
