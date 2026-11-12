<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import DataTable from '$lib/components/ui/DataTable.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import Stat from '$lib/components/ui/Stat.svelte';
  import StackedBars from './StackedBars.svelte';
  import { bucketLabels, bucketUnit, formatDuration, formatTokens, tokenColors, type BarBucket } from './bars';
  import type { ChartPoint } from './chart-data';
  import { formatCompactDecimalMoney } from './decimal';
  import type { UsageCost, UsageGroup, UsageQuery, UsageView } from './model';

  /** Companion services (Mnemory, Intaris, …): which one costs how much, and on what. */
  let { view, query, onquery, onpoint, onrow }: {
    view: UsageView;
    query: UsageQuery;
    onquery: (query: UsageQuery) => void;
    onpoint: (point: ChartPoint) => void;
    onrow: (row: UsageGroup) => void;
  } = $props();

  const unit = $derived(bucketUnit(view.buckets));
  function priced(group: { cost: UsageCost; costs?: UsageCost[] }): UsageCost | null {
    return [...(group.costs ?? []), group.cost].find((item) => item.amount !== null && item.currency) ?? null;
  }
  function money(cost: UsageCost | null): string {
    if (!cost?.amount || !cost.currency) return '—';
    try { return formatCompactDecimalMoney(cost.amount, cost.currency); } catch { return cost.amount; }
  }
  const amount = (group: UsageGroup) => { const value = Number(priced(group)?.amount); return Number.isFinite(value) ? value : null; };
  const mean = (group: UsageGroup) => {
    const span = group.performance?.providerSpan;
    return span && span.totalMs !== null && span.samples > 0 ? span.totalMs / span.samples : null;
  };
  const totalCost = $derived(view.groups.reduce((sum, group) => sum + (amount(group) ?? 0), 0));
  const share = (group: UsageGroup) => { const value = amount(group); return totalCost > 0 && value !== null ? value / totalCost : null; };
  const bars = $derived<BarBucket[]>(view.buckets.map((bucket) => {
    const t = bucket.totals;
    const fresh = t?.input != null && t.cacheRead != null && t.cacheRead <= t.input ? t.input - t.cacheRead : t?.cacheRead == null ? t?.input ?? null : null;
    return { key: bucket.start, ...bucketLabels(bucket.start, bucket.end), partial: !bucket.complete, point: null,
      segments: [
        { key: 'fresh', label: 'Fresh input', value: fresh, color: tokenColors.fresh },
        { key: 'cached', label: 'Cached input', value: t?.cacheRead ?? null, color: tokenColors.cached },
        { key: 'output', label: 'Output', value: t?.output ?? null, color: tokenColors.output }
      ], display: `${formatTokens(t?.input != null && t.output != null ? t.input + t.output : null)} tokens · ${(t?.attempts ?? 0).toLocaleString('en')} requests` };
  }));
  const selected = $derived(query.serviceId ?? null);
  const totals = $derived(view.totals);
  const columns = (first: string) => [
    { key: 'name', label: first, sortable: true }, { key: 'cost', label: 'Est. cost', numeric: true, sortable: true },
    { key: 'share', label: 'Share', numeric: true, sortable: true }, { key: 'requests', label: 'Requests', numeric: true, sortable: true },
    { key: 'input', label: 'Input', numeric: true, sortable: true }, { key: 'output', label: 'Output', numeric: true, sortable: true },
    { key: 'time', label: 'Avg time', numeric: true, sortable: true }
  ];
  const value = (row: UsageGroup, key: string) => ({ name: row.label, cost: amount(row), share: share(row), requests: row.attempts,
    input: row.input, output: row.output, time: mean(row) } as Record<string, number | string | null>)[key];
</script>

{#snippet cell(row: UsageGroup, column: { key: string }, open: ((row: UsageGroup) => void) | null)}
  {#if column.key === 'name'}
    {#if open && row.id}
      <Button variant="link" size="inline" class="block max-w-[320px] truncate" title={row.label} onclick={() => open(row)}>{row.label}</Button>
    {:else}<span class={row.id ? '' : 'text-secondary'} title={row.id ? row.label : 'Requests without a service tag'}>{row.label}</span>{/if}
    {#if share(row) !== null}<span class="mt-1 block h-[3px] overflow-hidden rounded-full bg-separator/60" aria-hidden="true"><span class="block h-full bg-accent/70" style:width={`${Math.max(1.5, share(row)! * 100)}%`}></span></span>{/if}
  {:else if column.key === 'cost'}<span class="font-semibold">{money(priced(row))}</span>
  {:else if column.key === 'share'}{share(row) === null ? '—' : `${Math.round(share(row)! * 100)}%`}
  {:else if column.key === 'requests'}{row.attempts.toLocaleString('en')}
  {:else if column.key === 'input'}{formatTokens(row.input)}
  {:else if column.key === 'output'}{formatTokens(row.output)}
  {:else}{mean(row) === null ? '—' : formatDuration(mean(row)!)}{/if}
{/snippet}
{#snippet serviceCell(row: UsageGroup, column: { key: string })}{@render cell(row, column, (group) => onquery({ ...query, serviceId: group.id ?? undefined, operation: undefined }))}{/snippet}
{#snippet operationCell(row: UsageGroup, column: { key: string })}{@render cell(row, column, onrow)}{/snippet}
{#snippet modelCell(row: UsageGroup, column: { key: string })}{@render cell(row, column, null)}{/snippet}

{#if selected}
  <nav class="mb-3 flex items-center gap-2 text-sm" aria-label="Service breadcrumb">
    <Button variant="link" size="inline" onclick={() => onquery({ ...query, serviceId: undefined, operation: undefined })}>All services</Button>
    <span aria-hidden="true" class="text-secondary">›</span><strong>{selected}</strong>
  </nav>
  <section class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label="Service summary">
    <Stat label="Estimated cost" value={totals ? money(priced(totals)) : '—'}/>
    <Stat label="Requests" value={totals ? totals.attempts.toLocaleString('en') : '—'}/>
    <Stat label="Tokens" value={formatTokens(totals?.input != null && totals.output != null ? totals.input + totals.output : null)}
      baseline={totals ? `${formatTokens(totals.input)} in · ${formatTokens(totals.output)} out` : undefined}/>
  </section>
  <Panel class="mb-4" title={`Tokens per ${unit}`} label="Service requests over time">
    <StackedBars buckets={bars} label={`Tokens per ${unit}`} format={formatTokens} {onpoint} height={180}/>
  </Panel>
  <div class="grid gap-4 xl:grid-cols-2">
    <Panel title="By operation" label="Operation breakdown">
      <DataTable label="Operations" rows={view.groups} rowKey={(row) => row.id ?? row.label} cell={operationCell} {value} sortKey="cost" columns={columns('Operation')} empty="No operations in this range"/>
    </Panel>
    <Panel title="By model" label="Model breakdown">
      <DataTable label="Models" rows={view.serviceModels ?? []} rowKey={(row) => row.id ?? row.label} cell={modelCell} {value} sortKey="cost" columns={columns('Model')} empty="No models in this range"/>
    </Panel>
  </div>
{:else}
  <Panel title="Companion services" label="Service breakdown">
    <DataTable label="Companion services" rows={view.groups} rowKey={(row) => row.id ?? row.label} cell={serviceCell} {value} sortKey="cost"
      columns={columns('Service')} empty="No companion service requests in this range"/>
  </Panel>
{/if}
