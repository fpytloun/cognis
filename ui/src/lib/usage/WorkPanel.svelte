<script lang="ts">
  import DataTable from '$lib/components/ui/DataTable.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { usageErrorMessage } from './adapter';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import type { UsageDataSource } from './api';
  import { formatDuration, formatTokens } from './bars';
  import { formatCompactDecimalMoney } from './decimal';
  import type { UsageCost, UsageGroup, UsageQuery, UsageTotals } from './model';
  import Donut, { type DonutSlice } from './Donut.svelte';
  import { compositionColors } from './composition';
  import type * as W from './wire';

  /** Which scheduled task, task or conversation costs the most. */
  let { query, source, onrow, totals = null }: {
    query: UsageQuery;
    source: UsageDataSource;
    /** Totals of the selected range and filters; the work-type split is derived from them. */
    totals?: UsageTotals | null;
    /** Opens the requests of one work item in the Requests view. */
    onrow: (filter: W.Filters, label: string) => void;
  } = $props();

  type Kind = 'schedule' | 'task' | 'conversation';
  let kind = $state<Kind>('schedule');
  let refreshVersion = $state(0);
  let load = $state<{ status: 'loading' } | { status: 'ready'; rows: UsageGroup[] } | { status: 'error'; message: string }>({ status: 'loading' });

  const kinds: { value: Kind; label: string }[] = [
    { value: 'schedule', label: 'Scheduled tasks' }, { value: 'task', label: 'One-off tasks' }, { value: 'conversation', label: 'Conversations' }
  ];
  $effect(() => {
    void refreshVersion;
    const current = kind;
    const controller = new AbortController();
    load = { status: 'loading' };
    if (!source.ranking) { load = { status: 'error', message: 'Not available on this server.' }; return; }
    const scoped: W.ScopedFilters = current === 'task' ? { task_origin: 'one_off' } : {};
    source.ranking(query, current, {}, scoped, controller.signal)
      .then((rows) => { if (!controller.signal.aborted) load = { status: 'ready', rows }; })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        load = { status: 'error', message: usageErrorMessage(error) };
      });
    return () => controller.abort();
  });

  const rows = $derived(load.status === 'ready' ? load.rows : []);
  // Work type split: chat = requests outside any task; scheduled = requests of scheduled
  // tasks; one-off = the remaining task requests. Both use the exact empty group of a
  // ranking plus the range total, not a sum of truncated top lists.
  let split = $state<{ chat: number; scheduled: number; total: number; currency: string } | null>(null);
  $effect(() => {
    void totals;
    const controller = new AbortController();
    split = null;
    if (!source.ranking) return;
    // The range total is the sum of a small complete partition (providers), so it
    // matches the same priced subtotal the task and schedule rankings use.
    Promise.all([source.ranking(query, 'provider', {}, {}, controller.signal), source.ranking(query, 'task', {}, {}, controller.signal),
      source.ranking(query, 'schedule', {}, {}, controller.signal)])
      .then(([providers, tasks, schedules]) => {
        if (controller.signal.aborted) return;
        const currency = providers.map((group) => priced(group)?.currency).find(Boolean);
        const sum = providers.reduce((acc, group) => acc + (amount(group) ?? 0), 0);
        if (!currency || sum <= 0) return;
        const empty = (groups: UsageGroup[]) => { const group = groups.find((item) => item.id === null); return group ? amount(group) ?? 0 : 0; };
        const chat = empty(tasks), notScheduled = empty(schedules);
        split = { chat, scheduled: Math.max(0, sum - notScheduled), total: sum, currency };
      })
      .catch(() => { split = null; });
    return () => controller.abort();
  });
  function format(value: number, currency: string): string {
    try { return formatCompactDecimalMoney(String(Number(value.toPrecision(6))), currency); } catch { return '—'; }
  }
  const typeSlices = $derived.by<DonutSlice[]>(() => {
    if (!split) return [];
    const oneOff = Math.max(0, split.total - split.chat - split.scheduled);
    return [
      { key: 'chat', label: 'Conversations', value: split.chat, display: format(split.chat, split.currency), color: compositionColors[0] },
      { key: 'schedule', label: 'Scheduled tasks', value: split.scheduled, display: format(split.scheduled, split.currency), color: compositionColors[1] },
      { key: 'task', label: 'One-off tasks', value: oneOff, display: format(oneOff, split.currency), color: compositionColors[2] }
    ].filter((slice) => slice.value > 0);
  });
  const topSlices = $derived.by<DonutSlice[]>(() => {
    const known = rows.filter((row) => row.id !== null && (amount(row) ?? 0) > 0).sort((a, b) => (amount(b) ?? 0) - (amount(a) ?? 0));
    const currency = known.map((row) => priced(row)?.currency).find(Boolean);
    if (!currency) return [];
    const head = known.slice(0, 6), tail = known.slice(6);
    const slices = head.map((row, index) => ({ key: row.id!, label: row.label, value: amount(row) ?? 0, display: money(row), color: compositionColors[index % compositionColors.length] }));
    const rest = tail.reduce((acc, row) => acc + (amount(row) ?? 0), 0);
    if (rest > 0) slices.push({ key: 'other', label: `Other (${tail.length})`, value: rest, display: format(rest, currency), color: 'rgb(var(--color-secondary))' });
    return slices;
  });
  const topTotal = $derived(topSlices.reduce((acc, slice) => acc + slice.value, 0));
  const topCurrency = $derived(rows.map((row) => priced(row)?.currency).find(Boolean) ?? 'USD');
  function priced(group: UsageGroup): UsageCost | null {
    return [...(group.costs ?? []), group.cost].find((item) => item.amount !== null && item.currency) ?? null;
  }
  function money(group: UsageGroup): string {
    const cost = priced(group);
    if (!cost?.amount || !cost.currency) return '—';
    try { return formatCompactDecimalMoney(cost.amount, cost.currency); } catch { return cost.amount; }
  }
  const amount = (group: UsageGroup) => { const value = Number(priced(group)?.amount); return Number.isFinite(value) ? value : null; };
  const top = $derived(Math.max(0, ...rows.map((row) => amount(row) ?? 0)));
  const per = (group: UsageGroup) => { const value = amount(group); return value !== null && group.attempts > 0 ? value / group.attempts : null; };
  function perRequest(group: UsageGroup): string {
    const value = per(group), currency = priced(group)?.currency;
    if (value === null || !currency) return '—';
    try { return formatCompactDecimalMoney(String(Number(value.toPrecision(4))), currency); } catch { return '—'; }
  }
  const mean = (group: UsageGroup) => {
    const span = group.performance?.providerSpan;
    return span && span.totalMs !== null && span.samples > 0 ? span.totalMs / span.samples : null;
  };
  const filterField: Record<Kind, keyof W.Filters> = { schedule: 'schedule_ids', task: 'task_ids', conversation: 'conversation_ids' };
  const nameLabel: Record<Kind, string> = { schedule: 'Schedule', task: 'Task', conversation: 'Conversation' };
  const value = (row: UsageGroup, key: string) => ({ name: row.label, cost: amount(row), requests: row.attempts, per: per(row),
    tokens: row.input != null && row.output != null ? row.input + row.output : null, unpriced: row.unpriced, time: mean(row) } as Record<string, number | string | null>)[key];
</script>

{#snippet cell(row: UsageGroup, column: { key: string })}
  {#if column.key === 'name'}
    {#if row.id}
      <Button variant="link" size="inline" class="block max-w-[360px] truncate"
        title={row.label} onclick={() => onrow({ [filterField[kind]]: [row.id!] }, `${nameLabel[kind]}: ${row.label}`)}>{row.label}</Button>
    {:else}<span class="text-secondary">{row.label}</span>{/if}
    {#if top > 0 && amount(row)}<span class="mt-1 block h-[3px] overflow-hidden rounded-full bg-separator/60" aria-hidden="true"><span class="block h-full bg-accent/70" style:width={`${Math.max(1.5, amount(row)! / top * 100)}%`}></span></span>{/if}
  {:else if column.key === 'cost'}<span class="font-semibold">{money(row)}</span>{#if row.unpriced}<span class="ml-1 text-xs text-warning" title={`${row.unpriced} requests not priced`}>+?</span>{/if}
  {:else if column.key === 'requests'}{row.attempts.toLocaleString('en')}
  {:else if column.key === 'per'}{perRequest(row)}
  {:else if column.key === 'tokens'}{formatTokens(row.input != null && row.output != null ? row.input + row.output : null)}
  {:else}{mean(row) === null ? '—' : formatDuration(mean(row)!)}{/if}
{/snippet}

<div class="mb-4 grid gap-4 lg:grid-cols-2">
  <Panel title="Cost by work type" label="Cost by work type">
    {#if typeSlices.length && split}
      <Donut slices={typeSlices} total={format(split.total, split.currency)} caption="estimated" label="Estimated cost by work type"
        onselect={(slice) => { if (slice.key !== 'chat') kind = slice.key as Kind; else kind = 'conversation'; }}/>
    {:else}
      <p class="m-0 py-6 text-center text-sm text-secondary">{totals ? 'No priced usage in this range' : 'Loading…'}</p>
    {/if}
  </Panel>
  <Panel title={`Top ${nameLabel[kind].toLowerCase()}s`} label={`Cost share by ${nameLabel[kind].toLowerCase()}`}>
    {#if topSlices.length}
      <Donut slices={topSlices} total={format(topTotal, topCurrency)} caption="shown" label={`Estimated cost by ${nameLabel[kind].toLowerCase()}`}
        onselect={(slice) => { const row = rows.find((item) => item.id === slice.key); if (row?.id) onrow({ [filterField[kind]]: [row.id] }, `${nameLabel[kind]}: ${row.label}`); }}/>
    {:else}
      <p class="m-0 py-6 text-center text-sm text-secondary">{load.status === 'loading' ? 'Loading…' : 'No priced work in this range'}</p>
    {/if}
  </Panel>
</div>

<Panel title="Most expensive work" label="Work cost ranking">
  {#snippet actions()}
    <SegmentedControl label="Work type" value={kind} options={kinds} onchange={(value) => kind = value}/>
  {/snippet}
  {#if load.status === 'loading'}
    <div class="grid gap-2" role="status" aria-label="Loading">{#each [0, 1, 2, 3, 4] as index (index)}<span class="h-9 animate-pulse rounded-xl bg-separator/40 motion-reduce:animate-none"></span>{/each}</div>
  {:else if load.status === 'error'}
    <div class="grid justify-items-center gap-3 py-6"><p class="m-0 text-sm text-secondary" role="alert">{load.message}</p><Button variant="secondary" size="sm" onclick={() => refreshVersion++}>Try again</Button></div>
  {:else}
    <DataTable label={`${nameLabel[kind]} cost ranking`} {rows} rowKey={(row, index) => row.id ?? `${row.label}-${index}`} {cell} {value} sortKey="cost"
      empty={kind === 'schedule' ? 'No scheduled task ran in this range' : kind === 'task' ? 'No one-off task ran in this range' : 'No conversation usage in this range'}
      columns={[{ key: 'name', label: nameLabel[kind], sortable: true }, { key: 'cost', label: 'Est. cost', numeric: true, sortable: true },
        { key: 'requests', label: 'Requests', numeric: true, sortable: true }, { key: 'per', label: 'Per request', numeric: true, sortable: true },
        { key: 'tokens', label: 'Tokens', numeric: true, sortable: true }, { key: 'time', label: 'Avg time', numeric: true, sortable: true }]}/>
  {/if}
</Panel>
