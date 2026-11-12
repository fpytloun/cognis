<script lang="ts">
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import Stat from '$lib/components/ui/Stat.svelte';
  import { bucketUnit, formatTokens, niceMax } from './bars';
  import { costSeries } from './chart-data';
  import { formatCompactDecimalMoney } from './decimal';
  import { linearForecast, MIN_FORECAST_SAMPLES } from './forecast';
  import type { UsageView } from './model';

  let { view }: { view: UsageView } = $props();

  type Metric = 'cost' | 'tokens' | 'requests';
  let metric = $state<Metric>('cost');
  let width = $state(0);

  const unit = $derived(bucketUnit(view.buckets));
  const step = $derived(unit === 'hour' ? 3_600_000 : 86_400_000);
  const band = $derived(costSeries({ ...view, groupBy: undefined })[0]);
  const currency = $derived(band?.key.split(':')[0] ?? 'USD');
  const values = $derived(view.buckets.map((bucket, index) => {
    if (metric === 'cost') return band?.series[0]?.points[index]?.value ?? null;
    if (metric === 'tokens') return bucket.totals?.input != null && bucket.totals.output != null ? bucket.totals.input + bucket.totals.output : null;
    return bucket.totals?.attempts ?? null;
  }));
  // Only complete buckets train the fit; the running bucket would read as a drop.
  const history = $derived(view.buckets.map((bucket, index) => ({ at: Date.parse(bucket.start), value: bucket.complete ? values[index] : null })));
  const horizon = $derived(unit === 'hour' ? 24 : Math.min(30, Math.max(7, view.buckets.length)));
  const forecast = $derived(history.length ? linearForecast(history, step, horizon) : null);

  function format(value: number): string {
    if (metric === 'cost') { try { return formatCompactDecimalMoney(String(Number(value.toPrecision(6))), currency); } catch { return value.toFixed(2); } }
    return metric === 'tokens' ? formatTokens(value) : Math.round(value).toLocaleString('en');
  }
  const horizonLabel = $derived(unit === 'hour' ? 'next 24 hours' : `next ${horizon} days`);
  const trend = $derived(forecast ? (Math.abs(forecast.slope) < 1e-9 ? 'Flat' : forecast.slope > 0 ? 'Rising' : 'Falling') : '—');

  // Chart: actual bars-as-line, projection dashed with its 80 % band.
  const H = 220, PAD = { left: 56, right: 12, top: 10, bottom: 24 };
  const geometry = $derived.by(() => {
    const w = Math.max(320, width || 640), inner = w - PAD.left - PAD.right, h = H - PAD.top - PAD.bottom;
    const all = [...history.map((p) => p.at), ...(forecast?.points.map((p) => p.at) ?? [])];
    const t0 = all[0] ?? 0, t1 = all.at(-1) ?? 1;
    const max = niceMax(Math.max(0, ...values.filter((v): v is number => v !== null), ...(forecast?.points.map((p) => p.high) ?? [])));
    const x = (at: number) => PAD.left + (t1 > t0 ? (at - t0) / (t1 - t0) : .5) * inner;
    const y = (v: number) => PAD.top + h - v / max * h;
    let actual = '', open = false;
    view.buckets.forEach((bucket, index) => {
      const v = values[index];
      if (v === null) { open = false; return; }
      actual += `${open ? 'L' : 'M'}${x(Date.parse(bucket.start)).toFixed(1)},${y(v).toFixed(1)}`;
      open = true;
    });
    const lastKnown = [...history].reverse().find((p) => p.value !== null);
    const projected = forecast && lastKnown ? `M${x(lastKnown.at).toFixed(1)},${y(lastKnown.value!).toFixed(1)}` + forecast.points.map((p) => `L${x(p.at).toFixed(1)},${y(p.value).toFixed(1)}`).join('') : '';
    const area = forecast ? forecast.points.map((p, i) => `${i ? 'L' : 'M'}${x(p.at).toFixed(1)},${y(p.high).toFixed(1)}`).join('')
      + [...forecast.points].reverse().map((p) => `L${x(p.at).toFixed(1)},${y(p.low).toFixed(1)}`).join('') + 'Z' : '';
    const fmt = new Intl.DateTimeFormat('en', unit === 'hour' ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' } : { month: 'short', day: 'numeric' });
    const ticks = [0, .25, .5, .75, 1].map((part) => ({ x: PAD.left + part * inner, label: fmt.format(new Date(t0 + part * (t1 - t0))) }));
    const now = forecast?.points[0] ? x(forecast.points[0].at - step) : null;
    return { w, y, max, actual, projected, area, ticks, now };
  });
</script>

<section class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label="Forecast summary">
  <Stat label={`Projected · ${horizonLabel}`} value={forecast ? format(forecast.total.value) : '—'} tone={forecast ? 'neutral' : 'unknown'}
    baseline={forecast ? `80% range ${format(forecast.total.low)} – ${format(forecast.total.high)}` : `Needs ${MIN_FORECAST_SAMPLES} complete ${unit}s`}/>
  <Stat label={`Projected per ${unit}`} value={forecast ? format(forecast.rate) : '—'} tone={forecast ? 'neutral' : 'unknown'}
    baseline={forecast ? `${forecast.samples} ${unit}s of history` : undefined}/>
  <Stat label="Trend" value={trend} tone={forecast ? 'neutral' : 'unknown'}
    baseline={forecast ? `${forecast.slope >= 0 ? '+' : '−'}${format(Math.abs(forecast.slope))} per ${unit}` : undefined}/>
</section>

<Panel title={`${metric === 'cost' ? 'Estimated cost' : metric === 'tokens' ? 'Tokens' : 'Requests'} per ${unit}`} label="Forecast chart">
  {#snippet actions()}
    <SegmentedControl label="Forecast metric" value={metric} onchange={(value) => metric = value}
      options={[{ value: 'cost', label: 'Cost' }, { value: 'tokens', label: 'Tokens' }, { value: 'requests', label: 'Requests' }]}/>
  {/snippet}
  <div class="min-w-0" bind:clientWidth={width}>
    {#if values.some((v) => v !== null)}
      <svg viewBox={`0 0 ${geometry.w} ${H}`} height={H} class="block w-full overflow-visible" role="img"
        aria-label={forecast ? `Actual and projected ${metric} per ${unit}; projected ${format(forecast.total.value)} over the ${horizonLabel}` : `Actual ${metric} per ${unit}`}>
        {#each [0, .5, 1] as part}
          <line x1={PAD.left} x2={geometry.w - PAD.right} y1={geometry.y(geometry.max * part)} y2={geometry.y(geometry.max * part)} class="stroke-separator"/>
          <text x={PAD.left - 8} y={geometry.y(geometry.max * part) + 3} text-anchor="end" class="fill-secondary text-[10px] tabular-nums">{format(geometry.max * part)}</text>
        {/each}
        {#if geometry.area}<path d={geometry.area} class="fill-accent/15"/>{/if}
        {#if geometry.now !== null}<line x1={geometry.now} x2={geometry.now} y1={PAD.top} y2={H - PAD.bottom} class="stroke-secondary/50" stroke-dasharray="2 3"/>{/if}
        <path d={geometry.actual} fill="none" class="stroke-accent" stroke-width="2" stroke-linejoin="round"/>
        {#if geometry.projected}<path d={geometry.projected} fill="none" class="stroke-accent" stroke-width="2" stroke-dasharray="5 4"/>{/if}
        {#each geometry.ticks as tick, index}
          <text x={tick.x} y={H - 6} text-anchor={index === 0 ? 'start' : index === geometry.ticks.length - 1 ? 'end' : 'middle'} class="fill-secondary text-[10px] tabular-nums">{tick.label}</text>
        {/each}
      </svg>
      <p class="mt-2 text-xs text-secondary">Solid: actual · dashed: linear trend · shaded: 80% range</p>
    {:else}
      <p class="py-8 text-center text-sm text-secondary">No data for this range</p>
    {/if}
  </div>
</Panel>
