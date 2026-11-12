<script lang="ts">
  import type { Snippet } from 'svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import type { UsageCostCohort, UsageDimension, UsageGroup, UsageLoadState, UsageQuery, UsageSurface, UsageView } from './model';
  import { detailAvailable, type ChartPoint } from './chart-data';
  import { backUsage, drillUsage, enterSurface } from './query-state';
  import UsageFilters from './UsageFilters.svelte';
  import AvailabilityNote from './AvailabilityNote.svelte';
  import UsageExplorer from './UsageExplorer.svelte';
  import OverviewPanel from './OverviewPanel.svelte';
  import PerformancePanel from './PerformancePanel.svelte';
  import CostsPanel from './CostsPanel.svelte';
  import ForecastPanel from './ForecastPanel.svelte';
  import WorkPanel from './WorkPanel.svelte';
  import RequestLogPanel from './RequestLogPanel.svelte';
  import type { UsageDataSource } from './api';
  import ServicesPanel from './ServicesPanel.svelte';
  import SubscriptionLimits from './SubscriptionLimits.svelte';
  import { hasCostCohortDrill } from './composition';
  import { bucketFor, intersectFilters, UsageQueryIssue, wireQuery } from './query-contract';
  import { fillBuckets } from './bars';
  import { bucketGrain, localDate, localDays } from './local-time';
  import { bucketUnit, type BarBucket } from './bars';
  let { result, query, onquery, onrefresh, onfacet, surface = $bindable('overview'), onsurface, scopeControls, source }: {
    result: UsageLoadState; query: UsageQuery; onquery: (query: UsageQuery) => void; onrefresh: () => void;
    /** Data source for views that load their own data (Work, request log). */
    source?: UsageDataSource;
    onfacet?: (field: UsageDimension) => void; surface?: UsageSurface; onsurface?: (surface: UsageSurface) => void;
    scopeControls?: Snippet;
  } = $props();
  const pages: { id: UsageSurface; label: string }[] = [
    { id: 'overview', label: 'Overview' }, { id: 'costs', label: 'Costs & tokens' }, { id: 'performance', label: 'Performance' },
    { id: 'forecast', label: 'Forecast' }, { id: 'work', label: 'Work' }, { id: 'explorer', label: 'Requests' }, { id: 'companions', label: 'Services' }, { id: 'limits', label: 'Limits' }
  ];
  let notice = $state('');
  // While the next result loads, the last view of the same tab stays on screen (dimmed)
  // so charts animate from data to data instead of flashing a skeleton.
  let lastReady = $state<{ surface: UsageSurface; view: UsageView } | null>(null);
  // Charts use a continuous time axis: hours/days without requests are zero, not skipped.
  const ready = $derived.by(() => {
    if (result.state !== 'ready') return null;
    if (surface === 'forecast' || !limitWindow) return result.view;
    const unit = bucketFor(new Date(limitWindow.start), new Date(limitWindow.end));
    // Hourly server buckets are summed into the browser's local days for longer ranges.
    const local = unit === 'day' && bucketGrain(result.view.buckets) === 'hour';
    const buckets = local ? localDays(result.view.buckets, limitWindow.start, limitWindow.end) : result.view.buckets;
    const utcDays = unit === 'day' && !local && bucketGrain(result.view.buckets) === 'day';
    return { ...result.view, buckets: fillBuckets(buckets, limitWindow.start, limitWindow.end, unit, Date.now(), utcDays ? 'utc' : 'local') };
  });
  $effect(() => { if (ready) lastReady = { surface, view: ready }; });
  const shown = $derived(ready ?? (result.state === 'loading' && lastReady?.surface === surface ? lastReady.view : null));
  const refreshing = $derived(result.state === 'loading' && shown !== null);
  const view = $derived(shown);
  $effect(() => { query.surface; query.filters; notice = ''; });
  const limitWindow = $derived.by(() => {
    try { const wire = wireQuery(query); return { start: wire.start, end: wire.end }; } catch { return null; }
  });
  /** Relative age of the newest projected observation; stale data must not look current. */
  function age(at: string | undefined): { text: string; stale: boolean } | null {
    const time = at ? Date.parse(at) : NaN;
    if (!Number.isFinite(time)) return null;
    const minutes = Math.max(0, Math.round((Date.now() - time) / 60_000));
    const text = minutes < 1 ? 'just now' : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.round(minutes / 60)} h ago` : `${Math.round(minutes / 1440)} d ago`;
    return { text, stale: minutes > 30 };
  }
  const freshness = $derived(view ? age(view.freshness) : null);
  function tab(value: UsageSurface) { surface = value; if (onsurface) onsurface(value); else onquery(enterSurface(query, value)); }
  function reset() { onquery(enterSurface({ ...query, groupBy: query.groupBy, filters: { ...query.filters, model: '', effort: '', agent: '', user: '', purpose: '' } }, query.surface ?? 'overview')); }
  const noDetail = 'Request details expired. Choose a recent range.';
  /** A day bar zooms to that local day (hourly); an hour bar opens its requests. */
  function zoom(bucket: BarBucket) {
    const start = Date.parse(bucket.key);
    if (shown && bucketUnit(shown.buckets) === 'day') {
      const day = localDate(new Date(start));
      onquery({ ...query, filters: { ...query.filters, start: day, end: day } });
      return;
    }
    if (bucket.point) point(bucket.point);
  }
  /** Narrows the dashboard to one value; it shows as a removable filter chip. */
  function filterBy(dimension: string, id: string) {
    const field = ({ provider: 'provider', model: 'model', agent: 'agent', project: 'project' } as Record<string, keyof UsageQuery['filters'] | undefined>)[dimension];
    if (field) onquery({ ...query, filters: { ...query.filters, [field]: id } });
  }
  function point(point: ChartPoint) {
    if (!point.detailAvailable) { notice = noDetail; return; }
    onquery(drillUsage(query, point.patch, point.label, point.metric));
  }
  function total(metric: string) {
    const totals = view?.totals;
    const known = metric === 'input_tokens' ? totals?.input != null : metric === 'output_tokens' ? totals?.output != null
      : metric === 'estimated_cost' ? [totals?.cost, ...(totals?.costs ?? [])].some((cost) => cost?.amount != null) : totals?.attempts != null;
    if (!known) { notice = 'No data for this value.'; return; }
    if (!view || !detailAvailable(view)) { notice = noDetail; return; }
    onquery(drillUsage(query, {}, `All requests · ${metric.replaceAll('_', ' ')}`, metric));
  }
  function row(row: UsageGroup) {
    if (!row.id || !view || !detailAvailable(view)) { notice = row.id ? noDetail : 'This group has no request filter.'; return; }
    const group = query.surface === 'companions' ? query.serviceId ? 'operation' : 'service' : query.groupBy;
    // Client identity groups narrow the request log by list filter, not by a visible filter field.
    const listFilter: Partial<Record<string, string>> = { client: 'client_ids', key: 'key_ids', auth_type: 'auth_types', traffic_origin: 'traffic_origins' };
    const patch: Partial<UsageQuery> = group === 'service' ? { serviceId: row.id } : group === 'operation' ? { operation: row.id } : group === 'hook' ? { hookId: row.id }
      : listFilter[group] ? { detailFilters: { [listFilter[group]!]: [row.id] } }
      : { filters: { ...query.filters, [group]: row.id } };
    onquery(drillUsage(query, patch, `${group}: ${row.label}`, 'attempts'));
  }
  function cohort(value: UsageCostCohort) {
    if (!hasCostCohortDrill(value) || !value.drill || !view || !detailAvailable(view)) { notice = value.drill ? noDetail : 'This group has no request filter.'; return; }
    try {
      const detailFilters = intersectFilters(query.detailFilters ?? {}, value.drill.detailFilters ?? {});
      onquery(drillUsage(query, { ...value.drill, detailFilters }, value.label, 'known_priced_subtotal'));
    } catch (error) {
      notice = error instanceof UsageQueryIssue ? error.message : 'These filters have no shared requests. Reset the filters.';
    }
  }
  function back() {
    if (!query.returnTo?.startsWith('?')) return;
    const next = backUsage(query, new Date());
    if (next) onquery(next);
  }
</script>
<div class="min-w-0 pb-[var(--app-safe-area-bottom,0px)] text-sm text-foreground">
  <header class="flex flex-wrap items-center justify-between gap-3 pb-3 pt-1">
    <div class="flex min-w-0 flex-wrap items-baseline gap-3">
      <h1 class="m-0 text-2xl font-semibold tracking-tight">Usage</h1>
      {#if view}
        <StatusBadge status={view.status !== 'ready' ? view.status : !freshness ? 'unknown' : freshness.stale ? 'stale' : 'ready'}/>
        <span class="text-xs text-secondary">{freshness ? `${freshness.stale ? 'Stale · ' : ''}Updated ${freshness.text}` : 'Update time unknown'}</span>
      {/if}
    </div>
    <div class="flex flex-wrap items-center gap-2">
      {#if scopeControls}{@render scopeControls()}{/if}
      <Button variant="secondary" size="icon" aria-label="Refresh active view" title="Refresh" onclick={onrefresh}>
        <svg viewBox="0 0 16 16" class="h-4 w-4 fill-none stroke-current" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v3h-3"/></svg>
      </Button>
    </div>
  </header>
  <div class="mb-3" data-usage-sticky-navigation>
    <AccessibleTabs tabs={pages} activeId={surface} idPrefix="usage" ariaLabel="Usage views" sticky onChange={(id: string) => tab(id as UsageSurface)}/>
  </div>
  <UsageFilters {query} facets={view?.facets} {onquery} {onfacet}/>
  {#if query.returnTo}
    <div class="mb-3 flex flex-wrap items-center gap-2 text-xs">
      <Button variant="ghost" size="sm" onclick={back}>‹ Back</Button><span class="text-secondary">{query.contextLabel ?? 'Filtered requests'}</span>
    </div>
  {/if}
  {#if notice}
    <div class="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-info-border bg-info-subtle px-3 py-2 text-xs" role="status">
      {notice}<Button variant="ghost" size="sm" aria-label="Dismiss" onclick={() => notice = ''}>Dismiss</Button>
    </div>
  {/if}
  <div id={`usage-panel-${surface}`} role="tabpanel" aria-labelledby={`usage-tab-${surface}`} class="min-w-0">
    {#if surface === 'limits'}
      {#if query.mode !== 'project' && limitWindow}
        <SubscriptionLimits start={limitWindow.start} end={limitWindow.end} detailed/>
      {:else}
        <p class="rounded-3xl border border-dashed border-separator p-6 text-center text-sm text-secondary">Limits are shown in My usage or All users.</p>
      {/if}
    {:else if result.state === 'loading' && !shown}
      <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" role="status" aria-live="polite" aria-label="Loading usage">
        {#each [0, 1, 2, 3] as index (index)}<span class="h-24 animate-pulse rounded-3xl bg-separator/40 motion-reduce:animate-none"></span>{/each}
        <span class="h-60 animate-pulse rounded-3xl bg-separator/40 motion-reduce:animate-none sm:col-span-2 xl:col-span-4"></span>
      </div>
    {:else if result.state === 'error'}
      <div class="rounded-3xl border border-separator p-6" role="alert">
        <h2 class="m-0 text-sm font-semibold">Usage unavailable</h2>
        <p class="my-2 text-sm text-secondary">{result.message}</p>
        <div class="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" onclick={onrefresh}>Try again</Button>
          <Button variant="secondary" size="sm" onclick={reset}>Clear filters</Button>
          {#if query.requiresCursor}<Button variant="secondary" size="sm" onclick={() => onquery({ ...query, cursor: undefined, requiresCursor: false })}>First page</Button>{/if}
        </div>
      </div>
    {:else if shown}
      {@const current = shown}
      <div class="relative min-w-0 transition-opacity duration-300" class:opacity-60={refreshing} aria-busy={refreshing}>
      {#if refreshing}<span class="absolute inset-x-0 -top-2 h-0.5 overflow-hidden rounded-full bg-separator/50" role="status" aria-label="Updating usage"><span class="block h-full w-1/3 animate-[usage-progress_1.1s_ease-in-out_infinite] rounded-full bg-accent motion-reduce:animate-none"></span></span>{/if}
      <AvailabilityNote value={current.availability}/>
      {#each current.sectionErrors ?? [] as error}<p class="mb-3 text-xs text-warning" role="status">{error}</p>{/each}
      {#if surface === 'overview'}
        <OverviewPanel view={current} {query} {source} onwork={() => tab('work')} window={limitWindow} onpoint={point} onbucket={zoom} onfilter={filterBy} oncohort={cohort} ontotal={total}/>
      {:else if surface === 'costs'}
        <CostsPanel view={current} {query} {source} onfilter={filterBy} {onquery} onpoint={point} onbucket={zoom} onrow={row}/>
      {:else if surface === 'performance'}
        <PerformancePanel view={current} {query} {onquery} onpoint={point} onrow={row}/>
      {:else if surface === 'forecast'}
        <ForecastPanel view={current}/>
      {:else if surface === 'companions'}
        <ServicesPanel view={current} {query} {onquery} onpoint={point} onrow={row}/>
      {:else if surface === 'work'}
        {#if source}<WorkPanel {query} {source} totals={current.totals} onrow={(filter, label) => onquery(drillUsage(query, { detailFilters: filter }, label, 'work'))}/>{/if}
      {:else if surface === 'explorer'}
        {#if source?.requestLog && !query.selectionRef && !query.requiresSelection && !query.sessionSearch && !query.cycleId && !query.sessionId && (!query.explorerMode || query.explorerMode === 'requests') && !query.preciseStart}
          <RequestLogPanel {query} {source} filters={query.detailFilters ?? {}}/>
        {:else}
          <UsageExplorer view={current} {query} {onquery}/>
        {/if}
      {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  @keyframes -global-usage-progress { from { transform: translateX(-100%); } to { transform: translateX(300%); } }
</style>
