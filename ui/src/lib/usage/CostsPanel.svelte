<script lang="ts">
  import Select from '$lib/components/ui/Select.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import DataTable from '$lib/components/ui/DataTable.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import StackedBars from './StackedBars.svelte';
  import LineChart from './LineChart.svelte';
  import FlowPanel from './FlowPanel.svelte';
  import SystemWork from './SystemWork.svelte';
  import type { UsageDataSource } from './api';
  import type { FlowLevel } from './flow';
  import { bucketLabels, bucketUnit, formatTokens, seriesColors, tokenColors, type BarBucket } from './bars';
  import { costSeries, type ChartPoint } from './chart-data';
  import { costBandKey } from './composition';
  import { moneyTitle, formatCompactDecimalMoney } from './decimal';
  import { costBasis, type UsageCost, type UsageGroup, type UsageGrouping, type UsageQuery, type UsageView } from './model';

  let { view, query, source, onfilter, onquery, onpoint, onbucket, onrow }: {
    source?: UsageDataSource;
    onfilter?: (level: FlowLevel, id: string) => void;
    view: UsageView;
    query: UsageQuery;
    onquery: (query: UsageQuery) => void;
    onpoint: (point: ChartPoint) => void;
    /** Bucket click: a day zooms to its hours; an hour opens its requests. */
    onbucket?: (bucket: BarBucket) => void;
    onrow: (row: UsageGroup) => void;
  } = $props();

  let bandKey = $state('');

  const allGroupings: { value: UsageGrouping; label: string }[] = [
    { value: 'model', label: 'Model' }, { value: 'provider', label: 'Provider' }, { value: 'agent', label: 'Agent' },
    { value: 'purpose', label: 'Purpose' }, { value: 'effort', label: 'Effort' }, { value: 'project', label: 'Project' },
    { value: 'user', label: 'User' }, { value: 'traffic_origin', label: 'Origin' }, { value: 'client', label: 'API client' }, { value: 'key', label: 'API key' }
  ];
  // Client and key identity is private; project usage cannot group by it.
  const groupings = $derived(allGroupings.filter((item) => !(item.value === 'user' && query.mode === 'personal')
    && !((item.value === 'client' || item.value === 'key') && query.mode === 'project')));

  const bands = $derived(costSeries({ ...view, groupBy: undefined }));
  const band = $derived(bands.find((item) => item.key === bandKey) ?? bands[0]);
  const costLabel = $derived(band?.key.endsWith(':provider_reported') ? 'Cost' : 'Estimated cost');
  const unit = $derived(bucketUnit(view.buckets));
  const labels = $derived(view.buckets.map((bucket) => bucketLabels(bucket.start, bucket.end)));
  const colors = tokenColors;

  function costIn(costs: UsageCost[] | undefined, fallback: UsageCost | undefined): UsageCost | null {
    return [...(costs ?? []), ...(fallback ? [fallback] : [])].find((cost) => costBandKey(cost) === band?.key) ?? null;
  }
  function money(amount: string | null | undefined): string {
    if (amount == null || !band) return '—';
    const currency = band.key.split(':')[0];
    try { return formatCompactDecimalMoney(amount, currency); } catch { return `${amount} ${currency}`; }
  }
  const costPoints = $derived(band?.series[0]?.points ?? []);
  // Stacked by the selected grouping: the six largest groups of the range, the rest as Other.
  const series = $derived.by(() => {
    const totalsByGroup = new Map<string, { id: string | null; label: string; amount: number }>();
    for (const bucket of view.buckets) for (const group of bucket.groups ?? []) {
      const key = group.id ?? `\u0000${group.label}`;
      const value = Number(costIn(group.totals.costs, group.totals.cost)?.amount ?? 0) || 0;
      const item = totalsByGroup.get(key) ?? { id: group.id, label: group.label, amount: 0 };
      item.amount += value;
      totalsByGroup.set(key, item);
    }
    const ranked = [...totalsByGroup.entries()].sort((a, b) => b[1].amount - a[1].amount);
    return { head: ranked.slice(0, 6).map(([key, item], index) => ({ key, label: item.label, color: seriesColors[index % seriesColors.length] })),
      tail: new Set(ranked.slice(6).map(([key]) => key)) };
  });
  const costBars = $derived<BarBucket[]>(view.buckets.map((bucket, index) => {
    const total = costPoints[index]?.value ?? null;
    const groups = bucket.groups ?? [];
    const valueOf = (key: string) => {
      const group = groups.find((item) => (item.id ?? `\u0000${item.label}`) === key);
      return group ? Number(costIn(group.totals.costs, group.totals.cost)?.amount ?? 0) || 0 : 0;
    };
    // Periods without requests have no groups; they stack as zeros, not as a separate series.
    const segments = series.head.length
      ? [...series.head.map((item) => ({ key: item.key, label: item.label, value: valueOf(item.key), color: item.color })),
        ...(series.tail.size ? [{ key: 'other', label: 'Other', value: [...series.tail].reduce((acc, key) => acc + valueOf(key), 0), color: 'rgb(var(--color-secondary) / .6)' }] : [])]
      : [{ key: 'cost', label: costLabel, value: total, color: tokenColors.cost }];
    return { key: bucket.start, ...labels[index], partial: !bucket.complete, point: costPoints[index] ?? null, segments,
      display: money(costIn(bucket.costs, bucket.cost)?.amount) };
  }));
  const tokenBars = $derived<BarBucket[]>(view.buckets.map((bucket, index) => {
    const t = bucket.totals;
    const fresh = t?.input != null && t.cacheRead != null && t.cacheRead <= t.input ? t.input - t.cacheRead : t?.cacheRead == null ? t?.input ?? null : null;
    return { key: bucket.start, ...labels[index], partial: !bucket.complete, point: costPoints[index] ?? null,
      segments: [
        { key: 'fresh', label: 'Fresh input', value: fresh, color: colors.fresh },
        { key: 'cached', label: 'Cached input', value: t?.cacheRead ?? null, color: colors.cached },
        { key: 'output', label: 'Output', value: t?.output ?? null, color: colors.output }
      ], display: `${formatTokens(t?.input != null && t.output != null ? t.input + t.output : null)} tokens` };
  }));
  const cacheRatio = $derived(view.buckets.map((bucket) => {
    const t = bucket.totals;
    // Cached input is a subset of input; a larger value is inconsistent, so unknown.
    return t?.input != null && t.input > 0 && t.cacheRead != null && t.cacheRead <= t.input ? t.cacheRead / t.input : null;
  }));

  const totals = $derived(view.totals);
  const rows = $derived(view.groups.map((group) => {
    const cost = costIn(group.costs, group.cost);
    const amount = cost?.amount == null ? null : Number(cost.amount);
    const fresh = group.input != null && group.cacheRead != null && group.cacheRead <= group.input ? group.input - group.cacheRead : null;
    return { group, cost, amount, attempts: group.attempts, input: fresh, cached: group.cacheRead ?? null, output: group.output,
      perRequest: amount !== null && group.attempts > 0 ? amount / group.attempts : null };
  }));
  const top = $derived(Math.max(0, ...rows.map((row) => row.amount ?? 0)));
  const groupLabel = $derived(groupings.find((item) => item.value === query.groupBy)?.label ?? 'Group');
  type Row = (typeof rows)[number];
  const whole = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString('en');
  const perRequest = (value: number | null) => value === null ? '—' : money(value.toPrecision(4).replace(/\.?0+$/, '') === '0' ? '0' : String(Number(value.toPrecision(4))));
</script>

<Panel class="mb-4" title={`${costLabel} per ${unit}`} label="Cost over time">
  {#snippet actions()}
    <label class="flex items-center gap-2 text-xs text-secondary">Split by
      <Select label="Split cost by" value={query.groupBy} options={groupings} onchange={(value) => onquery({ ...query, groupBy: value })}/>
    </label>
    {#if bands.length > 1}
      <select class="min-h-[40px] rounded-xl border border-control-border bg-surface px-2 text-xs md:min-h-[30px]" aria-label="Cost series band" value={band?.key ?? ''} onchange={(event) => bandKey = event.currentTarget.value}>
        {#each bands as item}<option value={item.key}>{item.label}</option>{/each}
      </select>
    {/if}
  {/snippet}
  <StackedBars buckets={costBars} label={`${costLabel} per ${unit}`} format={(value) => money(String(Number(value.toPrecision(6))))} {onpoint} {onbucket} hint={onbucket ? (unit === 'day' ? 'Click to see this day by hour' : 'Click to open these requests') : undefined}/>
  {#if totals?.unpriced}<p class="mt-2 text-xs text-warning">{whole(totals.unpriced)} requests not fully priced</p>{/if}
</Panel>

<div class="mb-4 grid gap-4 xl:grid-cols-2">
  <Panel title={`Tokens per ${unit}`} label="Tokens over time">
    <StackedBars buckets={tokenBars} label={`Tokens per ${unit}`} format={formatTokens} {onpoint} height={180}/>
  </Panel>
  <Panel title="Cache read share" label="Cache reads over time">
    <LineChart label={`Cache read share per ${unit}`} labels={labels.map((l) => l.label)} titles={labels.map((l) => l.title)} height={180}
      partial={view.buckets.map((bucket) => !bucket.complete)} format={(value) => `${Math.round(value * 100)}%`}
      series={[{ key: 'cache', label: 'Cache reads', color: seriesColors[2], values: cacheRatio }]}/>
  </Panel>
</div>

{#snippet cell(row: Row, column: { key: string })}
  {#if column.key === 'group'}
    <Button variant="link" size="inline" class="block max-w-[320px] truncate" title={row.group.label} onclick={() => onrow(row.group)}>{row.group.label}</Button>
    <span class="mt-1 block h-[3px] overflow-hidden rounded-full bg-separator/60" aria-hidden="true"><span class="block h-full bg-accent/70" style:width={`${top > 0 && row.amount ? Math.max(1.5, row.amount / top * 100) : 0}%`}></span></span>
  {:else if column.key === 'cost'}<span class="font-semibold" title={moneyTitle(row.cost?.amount, row.cost?.currency) ?? 'Not priced'}>{money(row.cost?.amount)}</span>
  {:else if column.key === 'attempts'}{whole(row.attempts)}
  {:else if column.key === 'perRequest'}{perRequest(row.perRequest)}
  {:else if column.key === 'input'}{formatTokens(row.input)}
  {:else if column.key === 'cached'}{formatTokens(row.cached)}
  {:else}{formatTokens(row.output)}{/if}
{/snippet}
<Panel title={`By ${groupLabel.toLowerCase()}`} label={`Cost by ${groupLabel.toLowerCase()}`}>
  {#snippet actions()}
    <SegmentedControl label="Group costs by" value={groupings.some((item) => item.value === query.groupBy) ? query.groupBy : null}
      options={groupings} onchange={(value) => onquery({ ...query, groupBy: value })}/>
  {/snippet}
  <DataTable label={`Cost by ${groupLabel.toLowerCase()}`} {rows} rowKey={(row) => row.group.id ?? row.group.label} {cell} sortKey="cost"
    value={(row, key) => key === 'group' ? row.group.label : key === 'cost' ? row.amount : row[key as 'attempts' | 'input' | 'cached' | 'output' | 'perRequest']}
    empty="No usage in this range"
    columns={[{ key: 'group', label: groupLabel, sortable: true }, { key: 'cost', label: 'Est. cost', numeric: true, sortable: true },
      { key: 'attempts', label: 'Requests', numeric: true, sortable: true }, { key: 'perRequest', label: 'Per request', numeric: true, sortable: true },
      { key: 'input', label: 'Fresh input', numeric: true, sortable: true }, { key: 'cached', label: 'Cached', numeric: true, sortable: true },
      { key: 'output', label: 'Output', numeric: true, sortable: true }]}/>
</Panel>

{#if source?.ranking && query.groupBy === 'agent' && rows.some((row) => row.group.id === null)}
  <div class="mt-4"><SystemWork {query} {source}/></div>
{/if}

{#if source?.ranking}
  <div class="mt-4"><FlowPanel {query} {source} {onfilter}/></div>
{/if}
