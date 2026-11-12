<script lang="ts">
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { statusPresentation } from '$lib/status';
  import { count, type UsageAttempt, type UsageQuery, type UsageRequest, type UsageSession, type UsageView } from './model';
  import { drillUsage } from './query-state';
  import { errorCategories } from './query-contract';
  import CostValues from './CostValues.svelte';
  import AttemptDetail from './AttemptDetail.svelte';
  import AvailabilityNote from './AvailabilityNote.svelte';
  import { formatDuration, formatTokens } from './bars';
  let { view, query, onquery }: { view: UsageView; query: UsageQuery; onquery: (query: UsageQuery) => void } = $props();
  const mode = $derived(query.explorerMode ?? 'sessions');
  const rows = $derived(view.requestPage?.rows ?? []);
  let localSelection = $state<UsageAttempt | null>(null);
  const selected = $derived(localSelection && rows.includes(localSelection) ? localSelection : rows.find((row) => row.id !== null && row.id === query.selectedAttempt));
  const nextCursor = $derived(mode === 'sessions' ? view.sessionPage?.nextCursor : mode === 'requests' ? view.logicalRequestPage?.nextCursor : view.requestPage?.nextCursor);
  function filter(patch: Partial<UsageQuery>) { onquery({ ...query, ...patch, cursor: undefined, requiresCursor: false, selectedAttempt: undefined }); }
  function session(row: UsageSession) {
    if (!row.selectionRef) return;
    onquery(drillUsage(query, { sessionId: undefined, sessionSearch: undefined, requiresSessionSearch: false,
      selectionRef: row.selectionRef, selectionType: row.sessionType, requiresSelection: true, detailFilters: row.drillFilters },
      `${row.sessionType === 'proxy' ? 'Proxy' : 'Cognis'} session selection`, 'session'));
  }
  function request(row: UsageRequest) {
    if (!row.selectionRef) return;
    onquery(drillUsage(query, { explorerMode: 'attempts', selectionRef: row.selectionRef, selectionType: row.sessionType,
      requiresSelection: true, detailFilters: row.drillFilters }, 'Protected request selection · physical trail', 'request'));
  }
  function choose(row: UsageAttempt) {
    localSelection = row;
    if (row.id) onquery({ ...query, selectedAttempt: row.id });
  }
  function close() { localSelection = null; onquery({ ...query, selectedAttempt: undefined }); }
  const time = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  function when(value: string | null | undefined): string {
    const at = value ? new Date(value) : null;
    return at && !Number.isNaN(at.getTime()) ? time.format(at) : '—';
  }
  function ms(value: number | null | undefined): string { return value == null ? '—' : formatDuration(value); }
  /** Middle truncation keeps both ends of opaque identifiers recognizable. */
  function short(value: string | null | undefined, keep = 8): string {
    if (!value) return '—';
    return value.length <= keep * 2 + 1 ? value : `${value.slice(0, keep)}…${value.slice(-keep)}`;
  }
  const advancedActive = $derived([query.sessionId, query.cycleId, query.clientId, query.keyId].filter(Boolean).length + (query.errorCategories?.length ? 1 : 0) + (query.logicalRequestIds?.length ? 1 : 0));
  const modes = [{ key: 'sessions', label: 'Sessions' }, { key: 'requests', label: 'Logical requests' }, { key: 'attempts', label: 'Attempts' }] as const;
</script>
<section class="explorer" aria-label="Sessions and requests explorer">
  <header class="heading">
    <div class="segmented" role="group" aria-label="Explorer level">
      {#each modes as item}<button aria-pressed={mode === item.key} onclick={() => filter({ explorerMode: item.key })}>{item.label}</button>{/each}
    </div>
  </header>
  {#if query.contextLabel}<div class="context"><strong>{query.contextLabel}</strong>{#if query.metric}<small>From {query.metric.replaceAll('_', ' ')}</small>{/if}</div>{/if}
  <div class="filters">
    {#if mode === 'sessions'}
      <label class="field"><span>Source</span><select aria-label="Session source" value={query.sessionType ?? 'cognis'} onchange={(event) => filter({ sessionType: event.currentTarget.value as UsageQuery['sessionType'] })}>{#each ['cognis', 'proxy'] as kind}<option value={kind} disabled={view.catalog ? !view.catalog.selections.session_kinds.includes(kind as 'cognis' | 'proxy') : false}>{kind === 'cognis' ? 'Cognis sessions' : 'Proxy clients'}</option>{/each}</select></label>
      <label class="field grow"><span>Search</span><input aria-label="Session search" maxlength="160" placeholder={query.sessionType === 'proxy' ? 'Exact proxy label' : 'Exact session ID'} value={query.sessionSearch ?? ''} onchange={(event) => filter({ sessionSearch: event.currentTarget.value || undefined, requiresSessionSearch: false })}/></label>
    {/if}
    <label class="field"><span>State</span><select aria-label="Explorer attempt state" value={query.state ?? ''} onchange={(event) => filter({ state: event.currentTarget.value || undefined })}><option value="">All</option>{#each ['observed_start', 'inflight', 'succeeded', 'failed', 'cancelled', 'unknown'] as state}<option value={state}>{statusPresentation(state).label}</option>{/each}</select></label>
    <details class="more"><summary>More filters{advancedActive ? ` (${advancedActive})` : ''}</summary>
      <div class="advanced">
        <label class="field"><span>Session ID</span><input aria-label="Explorer session ID" value={query.sessionId ?? ''} onchange={(event) => filter({ sessionId: event.currentTarget.value || undefined })}/></label>
        <label class="field"><span>Cycle ID</span><input aria-label="Explorer cycle ID" value={query.cycleId ?? ''} onchange={(event) => filter({ cycleId: event.currentTarget.value || undefined })}/></label>
        <label class="field"><span>Error categories</span><select aria-label="Explorer error categories" multiple size="3" value={query.errorCategories ?? []} onchange={(event) => filter({ errorCategories: Array.from(event.currentTarget.selectedOptions, (option) => option.value) })}>{#each errorCategories as category}<option value={category}>{category.replaceAll('_', ' ')}</option>{/each}</select></label>
        <label class="field"><span>Logical request IDs (one per line)</span><textarea aria-label="Explorer logical request IDs" rows="2" maxlength="25499" value={query.logicalRequestIds?.join('\n') ?? ''} onchange={(event) => filter({ logicalRequestIds: event.currentTarget.value ? event.currentTarget.value.split(/\r?\n/u) : [] })}></textarea></label>
        {#if query.mode !== 'project'}
          <label class="field"><span>Client ID</span><input aria-label="Explorer client ID" value={query.clientId ?? ''} onchange={(event) => filter({ clientId: event.currentTarget.value || undefined, explorerMode: 'requests' })}/></label>
          <label class="field"><span>Key ID</span><input aria-label="Explorer key ID" value={query.keyId ?? ''} onchange={(event) => filter({ keyId: event.currentTarget.value || undefined, explorerMode: 'requests' })}/></label>
        {/if}
      </div>
    </details>
  </div>
  {#if (view.sessionPage?.availability ?? view.logicalRequestPage?.availability ?? view.requestPage?.availability ?? view.availability)?.resolution !== 'retained'}
    <AvailabilityNote value={view.sessionPage?.availability ?? view.logicalRequestPage?.availability ?? view.requestPage?.availability ?? view.availability}/>
  {/if}

  {#if mode === 'sessions'}
    <div class="table-scroll"><table>
      <thead><tr><th scope="col">Session</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Input</th><th scope="col" class="num">Output</th><th scope="col" class="num">Est. cost</th></tr></thead>
      <tbody>
        {#each view.sessions as row (row.selectionRef ?? row.id ?? row.label)}
          <tr>
            <th scope="row">
              <button class="link" disabled={!row.selectionRef} onclick={() => session(row)} title={row.id ?? row.label}>{row.label}</button>
              <small>{row.sessionType === 'proxy' ? 'API client' : 'Cognis session'}{row.id ? ` · ${short(row.id)}` : ''}</small>
            </th>
            <td class="num">{count(row.attempts)}</td><td class="num">{formatTokens(row.input)}</td><td class="num">{formatTokens(row.output)}</td>
            <td class="num strong"><CostValues compact costs={row.costs} cost={row.cost}/></td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {#if !view.sessions.length}<p class="empty">No sessions for these filters.</p>{/if}
  {:else if mode === 'requests'}
    <div class="table-scroll"><table>
      <thead><tr><th scope="col">Started</th><th scope="col">Request</th><th scope="col">Source</th><th scope="col">Attempt</th><th scope="col">Outcome</th><th scope="col"><span class="sr-only">Trail</span></th></tr></thead>
      <tbody>
        {#each view.logicalRequestPage?.rows ?? [] as row, index (row.attemptId ?? row.requestId ?? index)}
          <tr>
            <td class="mono">{when(row.startedAt)}</td>
            <td><code title={row.requestId ?? 'Protected request'}>{short(row.requestId) === '—' ? 'Protected' : short(row.requestId)}</code></td>
            <td>{row.sessionType === 'proxy' ? 'Proxy' : row.sessionType === 'cognis' ? 'Cognis' : '—'}</td>
            <td><StatusBadge status={row.attemptState}/></td>
            <td>{row.logicalState ?? 'Unknown'}</td>
            <td class="num"><button class="link" aria-label="Physical trail" disabled={!row.selectionRef} onclick={() => request(row)}>Attempts ›</button></td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {#if !view.logicalRequestPage?.rows.length}<p class="empty">No requests for these filters.</p>{/if}
  {:else}
    <div class="table-scroll"><table class="attempts">
      <thead><tr><th scope="col">Started</th><th scope="col">Model</th><th scope="col">Status</th><th scope="col" class="num">Input</th><th scope="col" class="num">Cached</th><th scope="col" class="num">Output</th><th scope="col" class="num">Est. cost</th><th scope="col" class="num">Time</th><th scope="col" class="num">First token</th></tr></thead>
      <tbody>
        {#each rows as row, index (row.id ?? index)}
          <tr class:selected={row === selected}>
            <td class="mono"><button class="link" onclick={() => choose(row)} aria-label={row.id ?? 'Protected attempt · details'}>{when(row.startedAt)}</button></td>
            <td><span class="model" title={row.model}>{row.model}</span><small>{row.providerId ?? row.detail?.provider ?? ''}{row.serviceId ? ` · ${row.serviceId}` : ''}</small>
              {#if row.logicalId}<small><code title="Logical request ID">{row.logicalId}</code></small>{/if}
              {#if row.cycleId}<button class="link cycle" aria-label={row.cycleId} title={`Show every attempt of cycle ${row.cycleId}`} onclick={() => onquery(drillUsage(query, { explorerMode: 'attempts', cycleId: row.cycleId! }, 'Exact cycle selection', 'cycle'))}>cycle {short(row.cycleId, 6)}</button>{/if}</td>
            <td><StatusBadge status={row.status ?? row.outcome}/>{#if row.detail?.error}<small>{row.detail.error.category ?? ''} {row.detail.error.code ?? ''}</small>{/if}</td>
            <td class="num">{formatTokens(row.input)}</td><td class="num">{formatTokens(row.cacheRead)}</td><td class="num">{formatTokens(row.output)}</td>
            <td class="num strong"><CostValues compact costs={row.costs} cost={row.cost}/></td>
            <td class="num">{ms(row.detail?.timings.providerSpan.totalMs)}</td><td class="num">{ms(row.detail?.timings.providerTtft?.totalMs)}</td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {#if !rows.length}<p class="empty">No attempts for these filters.</p>{/if}
    {#if selected}
      <div class="detail"><header><h3>Attempt detail</h3><button onclick={close}>Close attempt details</button></header><AttemptDetail attempt={selected} mode={query.mode}/></div>
    {:else if query.selectedAttempt}<p class="foot">The selected attempt is not on this page.</p>{/if}
  {/if}
  <div class="pagination">
    {#if query.cursor || query.requiresCursor}<button onclick={() => filter({})}>First page</button>{/if}
    {#if nextCursor}<button aria-label={`Next ${mode === 'sessions' ? 'session' : mode === 'requests' ? 'request' : 'attempt'} page`} onclick={() => onquery({ ...query, cursor: nextCursor!, requiresCursor: true, selectedAttempt: undefined })}>Next page ›</button>{/if}
  </div>
</section>
<style>
  .explorer { min-width: 0; padding: 16px 18px; border: 1px solid rgb(var(--color-separator)); border-radius: 12px; background: rgb(var(--color-surface)); }
  .heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .muted, .foot, .empty { font-size: 12px; color: rgb(var(--color-secondary)); margin: 0; }
  .foot { margin-top: 10px; font-size: 11px; } .empty { padding: 24px 0; text-align: center; }
  .segmented { display: inline-flex; padding: 2px; border: 1px solid rgb(var(--color-control-border)); border-radius: 8px; background: rgb(var(--color-inset, var(--color-surface))); }
  .segmented button { min-height: 30px; padding: 2px 12px; border: 0; border-radius: 6px; background: transparent; color: rgb(var(--color-secondary)); font: inherit; font-size: 12px; font-weight: 550; cursor: pointer; }
  .segmented button[aria-pressed="true"] { background: rgb(var(--color-surface)); color: rgb(var(--color-foreground)); box-shadow: 0 1px 2px rgb(0 0 0 / .08); }
  .context { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; padding: 8px 12px; margin: 0 0 12px; border-radius: 8px; background: rgb(var(--color-accent) / .07); font-size: 12px; }
  .filters { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px 14px; margin: 0 0 12px; }
  .field { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: rgb(var(--color-secondary)); min-width: 140px; }
  .field.grow { flex: 1 1 220px; }
  input, select, textarea { min-height: 32px; min-width: 0; padding: 4px 8px; border: 1px solid rgb(var(--color-control-border)); border-radius: 7px; background: rgb(var(--color-surface)); color: rgb(var(--color-foreground)); font: inherit; font-size: 12px; }
  textarea { resize: vertical; }
  .more { flex-basis: 100%; }
  .more summary { display: inline-flex; min-height: 32px; align-items: center; font-size: 12px; font-weight: 550; color: rgb(var(--color-accent)); cursor: pointer; }
  .advanced { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px 14px; padding-top: 8px; }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .attempts { min-width: 900px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid rgb(var(--color-separator) / .7); text-align: left; vertical-align: top; }
  thead th { font-size: 11px; font-weight: 600; color: rgb(var(--color-secondary)); white-space: nowrap; }
  tbody th { font-weight: 500; max-width: 360px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .strong { font-weight: 600; }
  .mono, code { font-variant-numeric: tabular-nums; white-space: nowrap; }
  code { font-size: 11.5px; }
  small { display: block; margin-top: 2px; font-size: 11px; color: rgb(var(--color-secondary)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 340px; }
  .model { display: block; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .link { border: 0; padding: 0; background: transparent; color: rgb(var(--color-accent)); font: inherit; cursor: pointer; text-align: left; overflow-wrap: anywhere; }
  .link:disabled { color: rgb(var(--color-secondary)); cursor: default; }
  .cycle { font-size: 11px; margin-top: 2px; }
  tbody tr:hover, tr.selected { background: rgb(var(--color-surface-hover)); }
  .detail { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgb(var(--color-separator)); }
  .detail header { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
  h3 { margin: 0; font-size: 13px; }
  .pagination { display: flex; gap: 8px; justify-content: flex-end; padding-top: 12px; }
  .pagination button, .detail button { min-height: 32px; padding: 4px 12px; border: 1px solid rgb(var(--color-control-border)); border-radius: 7px; background: transparent; color: inherit; font: inherit; font-size: 12px; cursor: pointer; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  :is(button, input, select, textarea, summary):focus-visible { outline: 2px solid rgb(var(--color-accent)); outline-offset: 2px; }
  @media (max-width: 520px) { .explorer { padding: 14px; } }
  @media (max-width: 767px), (pointer: coarse) { .segmented button, input, select, .more summary, .pagination button, .detail button, .link { min-height: 44px; } }
</style>
