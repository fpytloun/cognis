<script lang="ts">
  import { statusPresentation } from '$lib/status';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import Stat from '$lib/components/ui/Stat.svelte';
  import StackedBars from './StackedBars.svelte';
  import { bucketLabels, bucketUnit, formatTokens, tokenColors, type BarBucket } from './bars';
  import RankedCost from './RankedCost.svelte';
  import Donut, { type DonutSlice } from './Donut.svelte';
  import TopWork from './TopWork.svelte';
  import type { UsageDataSource } from './api';
  import { compositionColors } from './composition';
  import SubscriptionLimits from './SubscriptionLimits.svelte';
  import { costSeries, detailAvailable, type ChartPoint } from './chart-data';
  import { attributionDimensions, costBandKey, overviewCostBands } from './composition';
  import { moneyTitle, formatCompactDecimalMoney, scaleDecimal } from './decimal';
  import type { UsageCost, UsageCostCohort, UsageQuery, UsageView } from './model';

  let { view, query, window, source, onwork, onpoint, onbucket, onfilter, oncohort, ontotal }: {
    view: UsageView;
    query: UsageQuery;
    /** Bucket click: a day zooms to its hours; an hour opens its requests. */
    onbucket?: (bucket: BarBucket) => void;
    /** Narrows the whole dashboard to one provider. */
    onfilter?: (dimension: 'provider', id: string) => void;
    source?: UsageDataSource;
    /** Opens the Work view. */
    onwork?: () => void;
    /** Exact UTC interval of the query, for per-unit baselines and limit history. */
    window: { start: string; end: string } | null;
    onpoint: (point: ChartPoint) => void;
    oncohort: (cohort: UsageCostCohort) => void;
    ontotal: (metric: string) => void;
  } = $props();

  let selectedBand = $state('');
  let metric = $state<'cost' | 'tokens'>('cost');

  const bands = $derived(overviewCostBands(view));
  const band = $derived(bands.find((item) => item.key === selectedBand) ?? bands[0]);
  const breakdown = $derived(view.costBreakdowns?.find((item) => costBandKey(item) === band?.key));
  const totals = $derived(view.totals);
  const bandCost = $derived<UsageCost | null>(breakdown
    ? { amount: breakdown.amount, currency: breakdown.currency, basis: breakdown.basis }
    : (totals?.costs ?? (totals ? [totals.cost] : [])).find((cost) => costBandKey(cost) === band?.key) ?? null);

  const unit = $derived(bucketUnit(view.buckets));
  // Elapsed part of the range only: a running day is not divided by future hours.
  const elapsedSeconds = $derived.by(() => {
    if (!window) return null;
    const start = Date.parse(window.start), end = Math.min(Date.parse(window.end), Date.now());
    return end > start ? Math.round((end - start) / 1000) : null;
  });
  const costLabel = $derived(bandCost?.basis === 'provider_reported' ? 'Cost' : 'Estimated cost');
  function money(amount: string | null | undefined, currency = bandCost?.currency ?? 'USD'): string {
    if (amount == null) return '—';
    try { return formatCompactDecimalMoney(amount, currency); } catch { return `${amount} ${currency}`; }
  }
  const whole = (value: number | null | undefined) => value == null ? '—' : new Intl.NumberFormat('en').format(value);
  const pct = (part: number, total: number) => total > 0 ? `${(part / total * 100).toLocaleString('en', { maximumFractionDigits: part / total < .1 ? 1 : 0 })}%` : '—';
  const rateUnit = $derived(unit === 'hour' ? 3_600 : 86_400);
  const perUnit = $derived.by(() => {
    if (!bandCost?.amount || !elapsedSeconds) return null;
    try { return scaleDecimal(bandCost.amount, rateUnit, Math.max(rateUnit, elapsedSeconds)); } catch { return null; }
  });

  const fresh = $derived(totals?.input != null && totals.cacheRead != null && totals.cacheRead <= totals.input ? totals.input - totals.cacheRead : null);
  const tokenTotal = $derived(totals?.input != null && totals.output != null ? totals.input + totals.output : null);
  const failed = $derived(view.lifecycle?.failed ?? null);
  const finished = $derived(view.lifecycle ? view.lifecycle.completed + view.lifecycle.failed + view.lifecycle.cancelled : null);
  const failureRatio = $derived(failed !== null && finished ? failed / finished : null);
  const unpriced = $derived(totals?.unpriced ?? null);
  const unknownUsage = $derived(totals?.unknown ?? null);
  const unallocatable = $derived(totals?.unallocatable ?? null);
  const attention = $derived((unpriced ?? 0) + (unknownUsage ?? 0));

  // The trend is the ungrouped population, independent of breakdown grouping.
  const costPoints = $derived(costSeries({ ...view, groupBy: undefined }).find((item) => item.key === band?.key)?.series[0]?.points ?? []);
  const bars = $derived<BarBucket[]>(view.buckets.map((bucket, index) => {
    const point = costPoints[index] ?? null;
    const { label, title } = bucketLabels(bucket.start, bucket.end);
    if (metric === 'cost') {
      return { key: bucket.start, label, title, partial: !bucket.complete, point,
        segments: [{ key: 'cost', label: costLabel, value: point?.value ?? null, color: tokenColors.cost }],
        display: point?.value == null ? '—' : money((bucket.costs ?? [bucket.cost]).find((cost) => costBandKey(cost) === band?.key)?.amount) };
    }
    const t = bucket.totals;
    const freshTokens = t?.input != null && t.cacheRead != null && t.cacheRead <= t.input ? t.input - t.cacheRead : t?.cacheRead == null ? t?.input ?? null : null;
    const sum = t?.input != null && t.output != null ? t.input + t.output : null;
    return { key: bucket.start, label, title, partial: !bucket.complete, point,
      segments: [
        { key: 'fresh', label: 'Fresh input', value: freshTokens, color: tokenColors.fresh },
        { key: 'cached', label: 'Cached input', value: t?.cacheRead ?? null, color: tokenColors.cached },
        { key: 'output', label: 'Output', value: t?.output ?? null, color: tokenColors.output }
      ], display: `${formatTokens(sum)} tokens` };
  }));
  // In My usage a single-user breakdown says nothing; keep it when several subjects appear.
  const singleUser = $derived(query.mode === 'personal'
    && (breakdown?.dimensions.user.state !== 'ready' || breakdown.dimensions.user.groups.length <= 1));
  // Provider share is the donut; the ranked lists cover the remaining dimensions.
  const dimensions = $derived(attributionDimensions
    .filter((dimension) => dimension.key !== 'provider' && !(dimension.key === 'user' && singleUser))
    .sort((a, b) => ['model', 'agent', 'user'].indexOf(a.key) - ['model', 'agent', 'user'].indexOf(b.key)));
  const providerSlices = $derived.by<DonutSlice[]>(() => {
    const marginal = breakdown?.dimensions.provider;
    if (!marginal || marginal.state !== 'ready' || !breakdown?.currency) return [];
    const known = marginal.groups.filter((group) => group.amount !== null && Number(group.amount) > 0)
      .sort((a, b) => Number(b.amount) - Number(a.amount));
    const head = known.slice(0, 5), tail = known.slice(5);
    const slices = head.map((group, index) => ({ key: group.key ?? `group-${index}`, label: group.label,
      value: Number(group.amount), display: money(group.amount), color: compositionColors[index % compositionColors.length] }));
    if (tail.length) {
      const rest = tail.reduce((sum, group) => sum + Number(group.amount), 0);
      slices.push({ key: 'other', label: `Other (${tail.length})`, value: rest, display: money(String(rest.toFixed(6))), color: compositionColors[5] });
    }
    return slices;
  });
</script>

<section class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Usage summary">
  <Stat label={costLabel} value={money(bandCost?.amount)} title={moneyTitle(bandCost?.amount, bandCost?.currency)}
    baseline={perUnit ? `${money(perUnit)} per ${unit}` : 'No priced usage'} onclick={() => ontotal('estimated_cost')}/>
  <Stat label="Tokens" value={formatTokens(tokenTotal)} baseline={`${formatTokens(fresh)} fresh · ${formatTokens(totals?.cacheRead)} cached · ${formatTokens(totals?.output)} out`}
    onclick={() => ontotal('input_tokens')}>
    {#if tokenTotal}
      <span class="my-0.5 flex h-1.5 overflow-hidden rounded-full bg-separator/60" aria-hidden="true">
        <i style:width={pct(fresh ?? 0, tokenTotal)} style:background={tokenColors.fresh}></i><i style:width={pct(totals?.cacheRead ?? 0, tokenTotal)} style:background={tokenColors.cached}></i><i style:width={pct(totals?.output ?? 0, tokenTotal)} style:background={tokenColors.output}></i>
      </span>
    {/if}
  </Stat>
  <Stat label="Requests" value={whole(totals?.attempts)} onclick={() => ontotal('finalized_attempts')}
    tone={statusPresentation(failureRatio === null ? 'unknown' : failureRatio >= .2 ? 'failed' : failureRatio >= .05 ? 'warning' : 'ready').statTone}
    baseline={failed === null ? '— failed' : failed === 0 ? 'No failures' : `${whole(failed)} failed · ${pct(failed, finished ?? 0)}`}/>
  {#if unpriced === null}
    <Stat label="Pricing coverage" value="—" tone="unknown"/>
  {:else if attention === 0}
    <Stat label="Pricing coverage" value="All priced"/>
  {:else}
    <Stat label="Pricing coverage" value={`${whole(unpriced)} not fully priced`} tone="warning"
      baseline={[unknownUsage ? `${whole(unknownUsage)} usage unknown` : '', unallocatable ? `${whole(unallocatable)} price unknown` : ''].filter(Boolean).join(' · ') || 'Set a model price'}/>
  {/if}
</section>
{#if view.lifecycle?.inflight}
  <p class="mb-4 text-xs text-secondary">In progress <strong class="tabular-nums text-foreground">{whole(view.lifecycle.inflight)}</strong></p>
{/if}

<div class="mb-4 grid gap-4 xl:grid-cols-3">
<Panel class="xl:col-span-2" title={metric === 'cost' ? `${costLabel} per ${unit}` : `Tokens per ${unit}`} label="Usage over time">
  {#snippet actions()}
    {#if metric === 'cost' && bands.length > 1}
      <select class="min-h-[40px] rounded-xl border border-control-border bg-surface px-2 text-xs md:min-h-[30px]" aria-label="Overview cost band" value={band?.key ?? ''} onchange={(event) => selectedBand = event.currentTarget.value}>
        {#each bands as item}<option value={item.key}>{item.label}</option>{/each}
      </select>
    {/if}
    <SegmentedControl label="Trend metric" value={metric} onchange={(value) => metric = value}
      options={[{ value: 'cost', label: 'Cost' }, { value: 'tokens', label: 'Tokens' }]}/>
  {/snippet}
  <StackedBars buckets={bars} label={metric === 'cost' ? `${costLabel} per ${unit}` : `Tokens per ${unit}`}
    format={metric === 'cost' ? (value) => money(String(Number(value.toPrecision(6)))) : formatTokens} {onpoint} {onbucket}
    hint={onbucket ? (unit === 'day' ? 'Click to see this day by hour' : 'Click to open these requests') : undefined}/>
</Panel>
<Panel title="Cost by provider" label="Cost share by provider">
  {#if providerSlices.length}
    <Donut slices={providerSlices} total={money(bandCost?.amount)} caption={bandCost?.basis === 'provider_reported' ? 'reported' : 'estimated'} label={`${costLabel} by provider`}
      onselect={(slice) => {
        const marginal = breakdown?.dimensions.provider;
        const group = marginal?.state === 'ready' ? marginal.groups.find((item) => item.key === slice.key) : undefined;
        if (group?.id && onfilter) onfilter('provider', group.id); else if (group) oncohort(group);
      }}/>
  {:else}
    <p class="m-0 py-6 text-center text-sm text-secondary">No priced usage in this range</p>
  {/if}
</Panel>
</div>

{#if source?.ranking && onwork}
  <div class="mb-4"><TopWork {query} {source} onopen={onwork}/></div>
{/if}

<Panel class="mb-4" title="Where the cost comes from" label="Cost breakdown">
  {#if totals?.attempts === 0}
    <p class="m-0 text-sm text-secondary">No cost breakdown for this range.</p>
  {:else}
  <div class="grid gap-x-7 gap-y-3 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
    {#each dimensions as dimension (dimension.key)}
      <RankedCost title={dimension.label} dimension={dimension.key} band={breakdown} {oncohort} detailAvailable={detailAvailable(view)}/>
    {/each}
  </div>
  {/if}
</Panel>

{#if query.mode !== 'project' && window}
  <div class="mb-4"><SubscriptionLimits start={window.start} end={window.end}/></div>
{/if}
