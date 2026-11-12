<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { usageErrorMessage } from './adapter';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import type { UsageDataSource } from './api';
  import AttemptDetail from './AttemptDetail.svelte';
  import { mediaQuantity, operationLabel } from './media';
  import { formatDuration, formatTokens } from './bars';
  import { formatCompactDecimalMoney } from './decimal';
  import type { UsageAttempt, UsageQuery } from './model';
  import type * as W from './wire';

  /**
   * Request log in the style of an LLM gateway: one row per logical request,
   * newest first, with client identity for API traffic and a detail drawer.
   */
  let { query, source, filters = {} }: { query: UsageQuery; source: UsageDataSource; filters?: W.Filters } = $props();

  type Origin = 'all' | 'api' | 'cognis';
  type Status = 'all' | 'errors';
  let origin = $state<Origin>('all');
  let status = $state<Status>('all');
  let authType = $state<'' | W.AuthType>('');
  let pages = $state<W.RequestLog[][]>([]);
  let cursor = $state<string | null>(null);
  let refreshVersion = $state(0);
  let load = $state<'loading' | 'ready' | 'more' | 'error'>('loading');
  let message = $state('');
  let open = $state<string | null>(null);
  let detail = $state<{ status: 'loading' } | { status: 'ready'; request: W.RequestLog | null; attempts: UsageAttempt[] } | { status: 'error'; message: string } | null>(null);

  const scoped = $derived<W.Filters>({
    ...filters,
    ...(origin === 'all' ? {} : { traffic_origins: [origin] }),
    ...(authType ? { auth_types: [authType] } : {}),
    ...(status === 'errors' ? { states: ['failed', 'cancelled'] } : {})
  });
  async function fetchPage(next: string | null, signal: AbortSignal) {
    if (!source.requestLog) throw new Error('Not available on this server.');
    return source.requestLog(query, scoped, {}, next, signal);
  }
  $effect(() => {
    const controller = new AbortController();
    void scoped; void query; void refreshVersion;
    load = 'loading'; pages = []; cursor = null; open = null; detail = null;
    fetchPage(null, controller.signal)
      .then((page) => { pages = [page.items]; cursor = page.nextCursor; load = 'ready'; })
      .catch((error: unknown) => { if (!controller.signal.aborted) { message = usageErrorMessage(error); load = 'error'; } });
    return () => controller.abort();
  });
  async function more() {
    if (!cursor) return;
    load = 'more';
    try { const page = await fetchPage(cursor, new AbortController().signal); pages = [...pages, page.items]; cursor = page.nextCursor; load = 'ready'; }
    catch (error) { message = usageErrorMessage(error); load = 'error'; }
  }
  async function toggle(row: W.RequestLog) {
    const key = row.selection_ref ?? row.request_id ?? null;
    if (!key || open === key) { open = null; detail = null; return; }
    open = key;
    if (!row.selection_ref || !source.requestDetail) { detail = { status: 'ready', request: row, attempts: [] }; return; }
    detail = { status: 'loading' };
    try { const result = await source.requestDetail(query, row.selection_ref, new AbortController().signal); if (open === key) detail = { status: 'ready', ...result }; }
    catch (error) { if (open === key) detail = { status: 'error', message: usageErrorMessage(error) }; }
  }

  const rows = $derived(pages.flat());
  const time = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  const when = (value: string | null) => { const at = value ? new Date(value) : null; return at && !Number.isNaN(at.getTime()) ? time.format(at) : '—'; };
  const seconds = (value: number | null) => value === null ? '—' : formatDuration(value * 1000);
  function cost(row: W.RequestLog): string {
    const item = row.costs.find((c) => c.known_amount !== null && c.currency);
    if (!item?.known_amount) return row.unpriced_attempts ? 'Unpriced' : '—';
    try { return formatCompactDecimalMoney(item.known_amount, item.currency); } catch { return item.known_amount; }
  }
  const authLabel: Record<W.AuthType, string> = { api_key: 'API key', user_jwt: 'User token', service: 'Service', browser_session: 'Browser', unknown: 'Unknown' };
  function who(row: W.RequestLog): string {
    if (row.traffic_origin === 'cognis') return row.session_label ?? row.service_id ?? 'Cognis';
    const app = row.app_name ? `${row.app_name}${row.app_version ? ` ${row.app_version}` : ''}` : null;
    return app ?? row.client_id ?? (row.auth_type ? authLabel[row.auth_type] : 'API client');
  }
  function outcome(row: W.RequestLog): string {
    return row.error_category ? 'failed' : row.state ?? 'unknown';
  }
  const private_ = $derived(query.mode !== 'project');
</script>

{#snippet field(label: string, value: string | null | undefined)}
  <div class="min-w-0"><dt class="text-xs text-secondary">{label}</dt><dd class="m-0 truncate text-sm" title={value ?? undefined}>{value || '—'}</dd></div>
{/snippet}

<Panel title="Request log" label="Request log">
  {#snippet actions()}
    <SegmentedControl label="Traffic" value={origin} onchange={(value) => origin = value}
      options={[{ value: 'all', label: 'All' }, { value: 'api', label: 'API clients' }, { value: 'cognis', label: 'Cognis' }]}/>
    {#if origin !== 'cognis'}
      <select class="min-h-[40px] rounded-xl border border-control-border bg-surface px-2 text-xs md:min-h-[30px]" aria-label="Authentication" bind:value={authType}>
        <option value="">Any auth</option>
        {#each ['api_key', 'user_jwt', 'service'] as const as type}<option value={type}>{authLabel[type]}</option>{/each}
      </select>
    {/if}
    <SegmentedControl label="Status" value={status} onchange={(value) => status = value}
      options={[{ value: 'all', label: 'All' }, { value: 'errors', label: 'Errors' }]}/>
  {/snippet}

  {#if load === 'loading'}
    <div class="grid gap-2" role="status" aria-label="Loading requests">{#each [0, 1, 2, 3, 4, 5] as index (index)}<span class="h-10 animate-pulse rounded-xl bg-separator/40 motion-reduce:animate-none"></span>{/each}</div>
  {:else if load === 'error' && !rows.length}
    <div class="grid justify-items-center gap-3 py-6"><p class="m-0 text-sm text-secondary" role="alert">{message}</p><Button variant="secondary" size="sm" onclick={() => refreshVersion++}>Refresh requests</Button></div>
  {:else}
    <div class="max-h-[720px] overflow-auto">
      <table class="w-full min-w-[960px] border-collapse text-sm" aria-label="Requests">
        <thead class="sticky top-0 z-[1] bg-surface">
          <tr class="text-xs text-secondary">
            {#each ['Time', 'Status', 'Model', 'Caller', 'Usage', 'Cost', 'Duration', 'First token'] as head, index}
              <th scope="col" class={['whitespace-nowrap border-b border-separator px-3 py-2 font-semibold', index >= 4 ? 'text-right' : 'text-left']}>{head}</th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each rows as row, index (row.selection_ref ?? row.request_id ?? index)}
            {@const key = row.selection_ref ?? row.request_id ?? null}
            {@const state = outcome(row)}
            <tr class={['cursor-pointer border-b border-separator/70 hover:bg-surface-hover', open === key && 'bg-accent-subtle']} onclick={() => toggle(row)}>
              <th scope="row" class="whitespace-nowrap px-3 py-2 text-left font-normal tabular-nums">
                <Button variant="ghost" size="sm" class="text-left" aria-expanded={open === key} aria-label={`Request ${when(row.started_at)} details`} onclick={(event) => { event.stopPropagation(); void toggle(row); }}>{when(row.started_at)}</Button>
              </th>
              <td class="px-3 py-2"><StatusBadge status={state}/>{#if row.http_status_code}<span class="ml-1 text-xs text-secondary">{row.http_status_code}</span>{/if}{#if row.retry_count}<span class="ml-1 text-xs text-secondary" title="Retries">↻{row.retry_count}</span>{/if}</td>
              <td class="max-w-[240px] px-3 py-2"><span class="block truncate" title={row.resolved_models.join(', ') || row.requested_model || ''}>{row.resolved_models[0] ?? row.requested_model ?? '—'}</span>
                <span class="block truncate text-xs text-secondary">{row.provider_ids.join(', ')}{row.streaming ? ' · stream' : ''}</span>
                {#if row.operations?.length}<span class="block text-xs text-secondary">{row.operations.map(operationLabel).join(', ')}</span>{/if}</td>
              <td class="max-w-[220px] px-3 py-2"><span class="block truncate" title={who(row)}>{who(row)}</span>
                <span class="block truncate text-xs text-secondary">{row.traffic_origin === 'api' ? `API${row.auth_type ? ` · ${authLabel[row.auth_type]}` : ''}` : 'Cognis'}</span></td>
              <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                {#if row.media_usage?.length}
                  {#each row.media_usage as quantity (quantity.name)}<div>{mediaQuantity(quantity)}</div>{/each}
                {:else}{formatTokens(row.tokens.input)} → {formatTokens(row.tokens.output)}{/if}
              </td>
              <td class="whitespace-nowrap px-3 py-2 text-right font-semibold tabular-nums">{cost(row)}</td>
              <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">{seconds(row.latency_seconds)}</td>
              <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">{seconds(row.ttft_seconds)}</td>
            </tr>
            {#if open === key}
              <tr class="border-b border-separator/70 bg-inset">
                <td colspan="8" class="px-4 py-4">
                  {#if row.error_category || row.error_summary}
                    <div class="mb-3 rounded-xl border border-danger-border bg-danger-subtle px-3 py-2 text-sm"><b class="text-danger">{row.error_category ?? 'Error'}</b>{#if row.error_summary}<span class="text-secondary"> · {row.error_summary}</span>{/if}</div>
                  {/if}
                  <dl class="m-0 grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
                    {@render field('Request ID', row.request_id)}
                    {@render field('Route', row.request_path)}
                    {@render field('Finish reason', row.finish_reason)}
                    {@render field('Attempts', `${row.attempt_count}${row.retry_count ? ` · ${row.retry_count} retries` : ''}${row.fallback_count ? ` · ${row.fallback_count} fallbacks` : ''}`)}
                    {@render field('Input', formatTokens(row.tokens.input))}
                    {@render field('Cached input', formatTokens(row.tokens.cache_read ?? null))}
                    {@render field('Output', formatTokens(row.tokens.output))}
                    {@render field('Usage', row.usage_quality)}
                    {#if private_}
                      {@render field('Client', row.client_id)}
                      {@render field('API key', row.key_id)}
                      {@render field('User agent', row.user_agent)}
                      {@render field('App', row.app_name ? `${row.app_name}${row.app_version ? ` ${row.app_version}` : ''}` : null)}
                      {#each Object.entries(row.client_labels ?? {}).filter(([, value]) => value) as [label, value]}{@render field(label.replaceAll('_', ' '), value)}{/each}
                    {/if}
                  </dl>
                  {#if detail?.status === 'loading'}
                    <p class="mt-3 text-xs text-secondary" role="status">Loading attempts…</p>
                  {:else if detail?.status === 'error'}
                    <p class="mt-3 text-xs text-secondary" role="alert">{detail.message}</p>
                  {:else if detail?.status === 'ready' && detail.attempts.length}
                    <div class="mt-4 grid gap-4">
                      {#each detail.attempts as attempt, attemptIndex (attempt.id ?? attemptIndex)}
                        <Panel label="Attempt details"><AttemptDetail {attempt} mode={query.mode}/></Panel>
                      {/each}
                    </div>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
      {#if !rows.length}<p class="py-6 text-center text-sm text-secondary">No requests for these filters</p>{/if}
    </div>
    {#if cursor}
      <div class="mt-3 flex justify-center"><Button variant="secondary" size="sm" disabled={load === 'more'} onclick={more}>{load === 'more' ? 'Loading…' : 'Load more'}</Button></div>
    {/if}
  {/if}
</Panel>
