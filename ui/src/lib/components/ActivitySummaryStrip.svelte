<script lang="ts">
  import DiffStat from './DiffStat.svelte';
  export type ActivitySummaryMetric = { id?: string; label: string; value: string | number; additions?: number | null; deletions?: number | null; tone?: 'default' | 'success' | 'warning' | 'danger' };
  let { metrics = [], onSelect }: { metrics?: ActivitySummaryMetric[]; onSelect?: (metric: ActivitySummaryMetric) => void } = $props();
</script>
<div class="grid grid-cols-2 gap-2 sm:grid-cols-4" data-testid="activity-summary-strip" aria-label="Activity summary">
  {#each metrics as metric (metric.label)}
    <button type="button" class="min-w-0 rounded-lg border border-slate-800/80 bg-slate-900/40 px-3 py-2 text-left hover:border-sky-500/40 hover:bg-slate-800/60" onclick={() => onSelect?.(metric)}>
      <span class="block truncate text-[10px] uppercase tracking-wide text-slate-500">{metric.label}</span>
      <span class="mt-1 block text-sm font-medium text-slate-100">{metric.value}</span>
      {#if metric.id === 'files'}<span class="mt-1 block"><DiffStat additions={metric.additions} deletions={metric.deletions} compact /></span>{/if}
    </button>
  {/each}
</div>
