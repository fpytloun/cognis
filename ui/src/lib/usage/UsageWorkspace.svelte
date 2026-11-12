<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { auth } from '$lib/stores/auth';
  import { usageAnalyticsEnabled } from '$lib/system';
  import { wsClient, wsState } from '$lib/ws/client';
  import UsageDashboard from './UsageDashboard.svelte';
  import { createUsageDataSource, usageErrorMessage } from './adapter';
  import { UsageController, type UsageSnapshot } from './controller';
  import { usageEventAction } from './events';
  import { enterSurface, readUsageQuery, restoreUsageHistory, usageHistoryState, writeUsageQuery, usageRequestKey, type UsageHistoryState } from './query-state';
  import type { UsageDataSource } from './api';
  import type { QuotaHistory, UsageDimension, UsageQuery, UsageView } from './model';
  let { url, project, historyState, navigate, source = createUsageDataSource() }: {
    url: URL; project?: { id: string; label: string }; historyState?: UsageHistoryState;
    navigate?: (url: URL, state: UsageHistoryState) => void; source?: UsageDataSource;
  } = $props();
  let snapshot = $state<UsageSnapshot>({ result: { state: 'loading' }, attempts: [], attemptsPending: false, attemptError: null, nextCursor: null });
  let controller: UsageController | null = null;
  let mounted = $state(false);
  let query = $state<UsageQuery>(untrack(() => readUsageQuery(url, new Date(), project)));
  let facets = $state<Partial<UsageView['facets']>>({});
  let quota = $state<QuotaHistory | null>(null);
  let auxiliaryError = $state<string | null>(null);
  let auxiliaryPending = $state(false);
  let facetRequest: AbortController | null = null;
  let quotaRequest: AbortController | null = null;
  let activeKey = '';
  const authority = $derived(`${$auth.user?.email ?? ''}:${$auth.user?.role ?? ''}`);
  const canLoad = $derived($usageAnalyticsEnabled && $auth.status === 'authenticated' && Boolean($auth.user) && !$auth.offline);
  const restorationKey = $derived(JSON.stringify([authority, url.href, project?.id, historyState]));
  let restoredFor = $state<string | null>(null);
  const ready = $derived(canLoad && restoredFor === restorationKey);
  const result = $derived(snapshot.result.state === 'ready' ? { state: 'ready' as const, view: { ...snapshot.result.view, facets: { ...snapshot.result.view.facets, ...facets }, quota } } : snapshot.result);
  function clearAuxiliary() {
    facetRequest?.abort(); quotaRequest?.abort(); facetRequest = null; quotaRequest = null;
    facets = {}; quota = null; auxiliaryError = null; auxiliaryPending = false;
  }
  function change(next: UsageQuery) {
    if (JSON.stringify(next.filters) !== JSON.stringify(query.filters) || next.mode !== query.mode) next = { ...next, cursor: undefined, requiresCursor: false, selectedAttempt: undefined };
    if (next.filters.start !== query.filters.start || next.filters.end !== query.filters.end) next = { ...next, preciseStart: undefined, preciseEnd: undefined, cursor: undefined, requiresCursor: false };
    if (project) next = { ...next, projectScope: project, mode: next.mode === 'project' ? 'project' : 'personal' };
    next = $state.snapshot(next);
    query = next;
    navigate?.(writeUsageQuery(url, next), usageHistoryState(next, authority));
  }
  $effect(() => {
    if (!canLoad) { restoredFor = null; return; }
    const next = restoreUsageHistory(readUsageQuery(url, new Date(), project), historyState, authority);
    if (JSON.stringify(next) !== JSON.stringify(untrack(() => query))) query = next;
    restoredFor = restorationKey;
  });
  $effect(() => {
    const key = JSON.stringify([authority, ready, usageRequestKey(query)]);
    if (!mounted || !controller || key === activeKey) return;
    activeKey = key; clearAuxiliary();
    controller.setActive(false);
    if (ready) {
      controller.replace($state.snapshot(query));
      controller.setActive(document.visibilityState !== 'hidden' && query.surface !== 'limits');
    }
  });
  async function facet(field: UsageDimension) {
    if (!source.facets || !ready || document.visibilityState === 'hidden' || facets[field]) return;
    facetRequest?.abort();
    const request = new AbortController(); facetRequest = request;
    try {
      const items = await source.facets(query, field, request.signal);
      if (facetRequest === request && !request.signal.aborted) facets = { ...facets, [field]: items };
    } catch { if (!request.signal.aborted) auxiliaryError = 'Filter choices unavailable. Other filters remain usable.'; }
  }
  onMount(() => {
    controller = new UsageController(source, (value) => { snapshot = value; });
    mounted = true;
    const visibility = () => {
      if (document.visibilityState === 'hidden') clearAuxiliary();
      controller?.setActive(ready && document.visibilityState !== 'hidden' && query.surface !== 'limits');
    };
    document.addEventListener('visibilitychange', visibility);
    const events = wsClient.subscribe((event) => {
      const action = usageEventAction(event);
      if (action === 'clear') { clearAuxiliary(); controller?.replace($state.snapshot(query)); }
      else if (action === 'refresh') controller?.invalidate();
    });
    let last = '';
    const connection = wsState.subscribe((state) => {
      if (state.status === 'connected' && last && last !== 'connected') controller?.invalidate();
      last = state.status;
    });
    return () => { document.removeEventListener('visibilitychange', visibility); events(); connection(); controller?.dispose(); clearAuxiliary(); mounted = false; };
  });
</script>
<div class="usage-workspace">
  {#snippet scopeControls()}<div class="controls">
    <label>Scope <select aria-label="Usage scope" value={query.mode} onchange={(event) => change({ ...query, mode: event.currentTarget.value as UsageQuery['mode'], filters: { ...query.filters, user: '' } })}>
      <option value="personal">{project ? 'My project usage' : 'My usage'}</option>
      {#if project}<option value="project">Project-visible usage</option>{:else}<option value="admin">All users · permission required</option>{/if}
    </select></label>
    <label class="pending"><input type="checkbox" checked={query.includePending} onchange={(event) => change({ ...query, includePending: event.currentTarget.checked })}/> Include observed pending counts</label>
  </div>{/snippet}
  {#if !$usageAnalyticsEnabled}
    <section aria-label="Usage analytics disabled"><h1>Usage analytics is disabled</h1><p>An administrator has disabled collection and analytics. Stored history is preserved. Re-enable Usage analytics in Cognis Settings and restart the controller to use this page.</p></section>
  {:else if !canLoad}<p role="status">Sign in to view usage analytics.</p>{:else}
    <UsageDashboard {result} {query} {source} onquery={change} onrefresh={() => controller?.invalidate()} onfacet={facet}
        {scopeControls} surface={query.surface ?? 'overview'} onsurface={(surface) => change(enterSurface(query, surface))}/>
    {#if auxiliaryError}<p role="status">{auxiliaryError}</p>{/if}
  {/if}
</div>
<style>
  .usage-workspace { min-width:0; } .controls { display:flex; flex-wrap:wrap; gap:14px; align-items:center; margin-bottom:16px; }
  .controls { margin:0; gap:10px; }
  .pending { color:rgb(var(--color-secondary)); font-size:11px; }
  label { display:flex; align-items:center; gap:8px; font-size:12px; } select { border:1px solid rgb(var(--color-control-border)); border-radius:6px; background:rgb(var(--color-surface)); color:rgb(var(--color-foreground)); padding:8px; font-size:12px; }
  :is(select,input):focus-visible { outline:2px solid rgb(var(--color-accent)); outline-offset:2px; }
  @media (max-width: 767px), (pointer: coarse) {
    .usage-workspace :global(button),
    .usage-workspace :global(select),
    .usage-workspace :global(summary),
    .usage-workspace :global(input:not([type="checkbox"]):not([type="radio"])) {
      box-sizing: border-box;
      min-height: 44px;
      min-width: 44px;
    }
    .pending { min-height: 44px; }
  }
</style>
