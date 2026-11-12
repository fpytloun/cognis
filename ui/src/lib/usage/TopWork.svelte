<script lang="ts">
  import { untrack } from 'svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import type { UsageDataSource } from './api';
  import { formatCompactDecimalMoney } from './decimal';
  import type { UsageCost, UsageGroup, UsageQuery } from './model';
  import type * as W from './wire';

  /** Overview card: the five most expensive schedules, conversations or one-off tasks. */
  let { query, source, onopen }: {
    query: UsageQuery;
    source: UsageDataSource;
    /** Opens the full Work ranking. */
    onopen: () => void;
  } = $props();

  type Kind = 'conversation' | 'schedule' | 'task';
  let kind = $state<Kind>('conversation');
  let load = $state<{ status: 'loading' } | { status: 'ready'; rows: UsageGroup[] } | { status: 'error'; message: string }>({ status: 'loading' });
  $effect(() => {
    const current = kind;
    const controller = new AbortController();
    if (!source.ranking) { load = { status: 'error', message: 'Not available on this server.' }; return; }
    // Keep the previous rows visible while the next ranking loads.
    if (untrack(() => load.status) !== 'ready') load = { status: 'loading' };
    const scoped: W.ScopedFilters = current === 'task' ? { task_origin: 'one_off' } : {};
    source.ranking(query, current, {}, scoped, controller.signal)
      .then((rows) => { load = { status: 'ready', rows: rows.slice(0, 5) }; })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) load = { status: 'error', message: error instanceof Error && error.message ? error.message : 'Failed to load.' };
      });
    return () => controller.abort();
  });
  function priced(group: UsageGroup): UsageCost | null {
    return [...(group.costs ?? []), group.cost].find((item) => item.amount !== null && item.currency) ?? null;
  }
  const amount = (group: UsageGroup) => { const value = Number(priced(group)?.amount); return Number.isFinite(value) ? value : 0; };
  const rows = $derived(load.status === 'ready' ? load.rows : []);
  const top = $derived(Math.max(0, ...rows.map(amount)));
  function money(group: UsageGroup): string {
    const cost = priced(group);
    if (!cost?.amount || !cost.currency) return '—';
    try { return formatCompactDecimalMoney(cost.amount, cost.currency); } catch { return '—'; }
  }
  const empty: Record<Kind, string> = { conversation: 'No conversation usage in this range', schedule: 'No scheduled task ran in this range', task: 'No one-off task ran in this range' };
</script>

<Panel title="Top spend" label="Top spend by work">
  {#snippet actions()}
    <SegmentedControl label="Work type" value={kind} onchange={(value) => kind = value}
      options={[{ value: 'conversation', label: 'Conversations' }, { value: 'schedule', label: 'Schedules' }, { value: 'task', label: 'Tasks' }]}/>
  {/snippet}
  {#if load.status === 'loading'}
    <div class="grid gap-2" role="status" aria-label="Loading top spend">{#each [0, 1, 2, 3, 4] as index (index)}<span class="h-8 animate-pulse rounded-lg bg-separator/40 motion-reduce:animate-none"></span>{/each}</div>
  {:else if load.status === 'error'}
    <p class="m-0 py-4 text-sm text-secondary">{load.message}</p>
  {:else if !rows.length}
    <p class="m-0 py-4 text-sm text-secondary">{empty[kind]}</p>
  {:else}
    <ol class="m-0 grid list-none gap-2.5 p-0">
      {#each rows as row, index (row.id ?? `${row.label}-${index}`)}
        <li class="grid min-w-0 gap-1">
          <div class="flex min-w-0 items-baseline justify-between gap-3 text-sm">
            <span class="min-w-0 truncate" title={row.label}>{row.label}</span>
            <span class="shrink-0 font-semibold tabular-nums">{money(row)}</span>
          </div>
          <span class="block h-1.5 overflow-hidden rounded-full bg-separator/60" aria-hidden="true">
            <span class="block h-full rounded-full bg-accent/70 transition-[width] duration-500 ease-out" style:width={`${top > 0 ? Math.max(1.5, amount(row) / top * 100) : 0}%`}></span>
          </span>
          <span class="text-xs tabular-nums text-secondary">{row.attempts.toLocaleString('en')} requests</span>
        </li>
      {/each}
    </ol>
  {/if}
  <div class="mt-3"><Button variant="link" size="inline" class="text-xs" onclick={onopen}>Open Work</Button></div>
</Panel>
