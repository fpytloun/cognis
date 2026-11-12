<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import DataTable from '$lib/components/ui/DataTable.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import Stat from '$lib/components/ui/Stat.svelte';
  import { statusPresentation } from '$lib/status';
  import LineChart from './LineChart.svelte';
  import { bucketLabels, bucketUnit, formatDuration, seriesColors } from './bars';
  import type { ChartPoint } from './chart-data';
  import type { TimingMeasure, ThroughputMeasure, UsageGroup, UsageGrouping, UsagePerformance, UsageQuery, UsageView } from './model';

  let { view, query, onquery, onpoint, onrow }: {
    view: UsageView;
    query: UsageQuery;
    onquery: (query: UsageQuery) => void;
    onpoint: (point: ChartPoint) => void;
    onrow: (row: UsageGroup) => void;
  } = $props();

  const allGroupings: { value: UsageGrouping; label: string }[] = [
    { value: 'provider', label: 'Provider' }, { value: 'model', label: 'Model' },
    { value: 'agent', label: 'Agent' }, { value: 'purpose', label: 'Purpose' }, { value: 'client', label: 'API client' }
  ];
  const groupings = $derived(allGroupings.filter((item) => !(item.value === 'client' && query.mode === 'project')));

  const mean = (m: TimingMeasure | undefined) => !m || m.totalMs === null || m.samples <= 0 ? null : m.totalMs / m.samples;
  function rate(m: ThroughputMeasure | undefined): number | null {
    if (!m) return null;
    if (m.rate !== undefined) return m.rate;
    return m.outputTokens === null || m.durationMs === null || m.durationMs <= 0 || m.samples <= 0 ? null : m.outputTokens / m.durationMs * 1000;
  }
  const failure = (p: UsagePerformance | undefined) =>
    !p || p.dispatchedAttempts === null || p.failedAttempts === null || p.dispatchedAttempts <= 0 ? null : p.failedAttempts / p.dispatchedAttempts;
  /** Ratio of sums over buckets, never a mean of per-bucket means. */
  function pooled(pick: (p: UsagePerformance) => TimingMeasure | undefined): number | null {
    let total = 0, samples = 0;
    for (const bucket of view.buckets) {
      const m = bucket.performance ? pick(bucket.performance) : undefined;
      if (m && m.totalMs !== null && m.samples > 0) { total += m.totalMs; samples += m.samples; }
    }
    return samples ? total / samples : null;
  }
  const latency = $derived(pooled((p) => p.providerSpan));
  const ttft = $derived(pooled((p) => p.providerTtft));
  const speed = $derived.by(() => {
    let tokens = 0, ms = 0;
    for (const bucket of view.buckets) {
      const g = bucket.performance?.generation;
      if (g && g.outputTokens !== null && g.durationMs !== null && g.samples > 0) { tokens += g.outputTokens; ms += g.durationMs; }
    }
    return ms > 0 ? tokens / ms * 1000 : null;
  });
  const failures = $derived.by(() => {
    let failed = 0, dispatched = 0;
    for (const bucket of view.buckets) {
      const p = bucket.performance;
      if (p && p.dispatchedAttempts !== null && p.failedAttempts !== null) { failed += p.failedAttempts; dispatched += p.dispatchedAttempts; }
    }
    return dispatched ? { failed, dispatched, ratio: failed / dispatched } : null;
  });
  const status = (ratio: number | null) => statusPresentation(ratio === null ? 'unknown' : ratio >= .2 ? 'failed' : ratio >= .05 ? 'warning' : 'ready');

  const unit = $derived(bucketUnit(view.buckets));
  const labels = $derived(view.buckets.map((bucket) => bucketLabels(bucket.start, bucket.end)));
  const partial = $derived(view.buckets.map((bucket) => !bucket.complete));
  const points = $derived(view.buckets.map((bucket): ChartPoint => ({
    key: bucket.start, label: bucket.start, value: null, display: '', metric: 'latency',
    patch: { preciseStart: bucket.start, preciseEnd: bucket.end }, detailAvailable: Boolean(bucket.end),
    explanation: '', population: ''
  })));

  type Row = { group: UsageGroup; requests: number; latency: number | null; ttft: number | null; speed: number | null; failure: number | null };
  const rows = $derived<Row[]>(view.groups.map((group) => ({ group, requests: group.attempts,
    latency: mean(group.performance?.providerSpan), ttft: mean(group.performance?.providerTtft),
    speed: rate(group.performance?.generation), failure: failure(group.performance) })));
  const groupLabel = $derived(groupings.find((item) => item.value === query.groupBy)?.label ?? 'Group');
  const pct = (value: number | null) => value === null ? '—' : `${(value * 100).toLocaleString('en', { maximumFractionDigits: value < .1 ? 1 : 0 })}%`;
  const ms = (value: number | null) => value === null ? '—' : formatDuration(value);
  const tps = (value: number | null) => value === null ? '—' : `${value.toLocaleString('en', { maximumFractionDigits: value < 10 ? 1 : 0 })} tok/s`;
</script>

<section class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Performance summary">
  <Stat label="Avg request time" value={ms(latency)} tone={latency === null ? 'unknown' : 'neutral'}/>
  <Stat label="Avg first token" value={ms(ttft)} tone={ttft === null ? 'unknown' : 'neutral'}/>
  <Stat label="Output speed" value={tps(speed)} tone={speed === null ? 'unknown' : 'neutral'}/>
  <Stat label="Failure rate" value={pct(failures?.ratio ?? null)} tone={status(failures?.ratio ?? null).statTone}
    baseline={failures ? `${failures.failed.toLocaleString('en')} of ${failures.dispatched.toLocaleString('en')} requests` : undefined}/>
</section>

<div class="mb-4 grid gap-4 xl:grid-cols-2">
  <Panel title={`Latency per ${unit}`}>
    <LineChart label={`Average request time and first token per ${unit}`} labels={labels.map((l) => l.label)} titles={labels.map((l) => l.title)} {partial} {points} {onpoint}
      format={formatDuration}
      series={[
        { key: 'latency', label: 'Request time', color: seriesColors[0], values: view.buckets.map((b) => mean(b.performance?.providerSpan)) },
        { key: 'ttft', label: 'First token', color: seriesColors[1], values: view.buckets.map((b) => mean(b.performance?.providerTtft)), dashed: true }
      ]}/>
  </Panel>
  <Panel title={`Speed and failures per ${unit}`}>
    <div class="grid gap-5">
      <LineChart label={`Output speed per ${unit}`} labels={labels.map((l) => l.label)} titles={labels.map((l) => l.title)} {partial} {points} {onpoint} height={120}
        format={(value) => `${Math.round(value)} tok/s`}
        series={[{ key: 'speed', label: 'Output speed', color: seriesColors[2], values: view.buckets.map((b) => rate(b.performance?.generation)) }]}/>
      <LineChart label={`Failure rate per ${unit}`} labels={labels.map((l) => l.label)} titles={labels.map((l) => l.title)} {partial} {points} {onpoint} height={110}
        format={(value) => `${Math.round(value * 100)}%`}
        series={[{ key: 'failure', label: 'Failure rate', color: 'rgb(var(--color-danger))', values: view.buckets.map((b) => failure(b.performance)) }]}/>
    </div>
  </Panel>
</div>

{#snippet cell(row: Row, column: { key: string })}
  {#if column.key === 'group'}
    <Button variant="link" size="inline" class="block max-w-[320px] truncate" title={row.group.label} onclick={() => onrow(row.group)}>{row.group.label}</Button>
  {:else if column.key === 'requests'}{row.requests.toLocaleString('en')}
  {:else if column.key === 'latency'}{ms(row.latency)}
  {:else if column.key === 'ttft'}{ms(row.ttft)}
  {:else if column.key === 'speed'}{tps(row.speed)}
  {:else}<span class={status(row.failure).textClass}>{pct(row.failure)}</span>{/if}
{/snippet}

<Panel title={`By ${groupLabel.toLowerCase()}`} label={`Performance by ${groupLabel.toLowerCase()}`}>
  {#snippet actions()}
    <SegmentedControl label="Group performance by" value={groupings.some((item) => item.value === query.groupBy) ? query.groupBy : null}
      options={groupings} onchange={(value) => onquery({ ...query, groupBy: value })}/>
  {/snippet}
  <DataTable label={`Performance by ${groupLabel.toLowerCase()}`} rows={rows} rowKey={(row) => row.group.id ?? row.group.label} {cell}
    sortKey="requests" value={(row, key) => key === 'group' ? row.group.label : row[key as keyof Omit<Row, 'group'>]}
    empty="No requests in this range"
    columns={[{ key: 'group', label: groupLabel, sortable: true }, { key: 'requests', label: 'Requests', numeric: true, sortable: true },
      { key: 'latency', label: 'Avg time', numeric: true, sortable: true }, { key: 'ttft', label: 'First token', numeric: true, sortable: true },
      { key: 'speed', label: 'Speed', numeric: true, sortable: true }, { key: 'failure', label: 'Failures', numeric: true, sortable: true }]}/>
</Panel>
