<script lang="ts">
  import DataTable from '$lib/components/ui/DataTable.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import type { UsageDataSource } from './api';
  import { formatTokens } from './bars';
  import { formatCompactDecimalMoney, moneyTitle } from './decimal';
  import type { UsageCost, UsageGroup, UsageQuery } from './model';
  import { SYSTEM_AGENT_LABEL, systemFunctions, type SystemFunction } from './system-work';

  /** What the requests without an agent were for: guardrails, memory, compaction, titles and similar. */
  let { query, source }: { query: UsageQuery; source: UsageDataSource } = $props();

  let load = $state<{ status: 'loading' } | { status: 'ready'; rows: SystemFunction[] } | { status: 'error' }>({ status: 'loading' });
  $effect(() => {
    const current = query;
    const controller = new AbortController();
    load = { status: 'loading' };
    if (!source.ranking) { load = { status: 'error' }; return; }
    const scoped = { agent_scope: 'system' as const };
    Promise.all([
      source.ranking(current, 'purpose', {}, scoped, controller.signal),
      source.ranking(current, 'service', {}, scoped, controller.signal)
    ]).then(async ([purposes, services]) => {
      // Which services made each purpose's requests: one purpose ranking per named service;
      // requests not covered by a named service were made by Cognis itself.
      const byService = new Map<string, string[]>();
      const covered = new Map<string, number>();
      await Promise.all(services.filter((service) => service.id).map(async (service) => {
        const rows = await source.ranking!(current, 'purpose', { service_ids: [service.id!] }, scoped, controller.signal);
        for (const row of rows) if (row.id) {
          byService.set(row.id, [...(byService.get(row.id) ?? []), service.id!]);
          covered.set(row.id, (covered.get(row.id) ?? 0) + row.attempts);
        }
      }));
      for (const purpose of purposes) if (purpose.id && (covered.get(purpose.id) ?? 0) < purpose.attempts) {
        byService.set(purpose.id, [...(byService.get(purpose.id) ?? []), 'cognis']);
      }
      if (!controller.signal.aborted) load = { status: 'ready', rows: systemFunctions(purposes, byService) };
    }).catch(() => { if (!controller.signal.aborted) load = { status: 'error' }; });
    return () => controller.abort();
  });

  function priced(group: UsageGroup): UsageCost | null {
    return (group.costs ?? [group.cost]).find((cost) => cost.amount !== null && cost.currency) ?? null;
  }
  function money(group: UsageGroup): string {
    const cost = priced(group);
    if (!cost?.amount || !cost.currency) return '—';
    try { return formatCompactDecimalMoney(cost.amount, cost.currency); } catch { return '—'; }
  }
  const amount = (group: UsageGroup) => Number(priced(group)?.amount ?? 0) || 0;
  const rows = $derived(load.status === 'ready' ? load.rows : []);
  const top = $derived(Math.max(0, ...rows.map((row) => amount(row.group))));
</script>

{#snippet cell(row: SystemFunction, column: { key: string })}
  {#if column.key === 'label'}
    <span class="block max-w-[320px] truncate" title={row.label}>{row.label}</span>
    <span class="mt-1 block h-[3px] overflow-hidden rounded-full bg-separator/60" aria-hidden="true"><span class="block h-full bg-accent/70" style:width={`${top > 0 && amount(row.group) ? Math.max(1.5, amount(row.group) / top * 100) : 0}%`}></span></span>
  {:else if column.key === 'services'}<span class="text-secondary">{row.services || '—'}</span>
  {:else if column.key === 'cost'}<span class="font-semibold" title={moneyTitle(priced(row.group)?.amount, priced(row.group)?.currency) ?? 'Not priced'}>{money(row.group)}</span>
  {:else if column.key === 'attempts'}{row.group.attempts.toLocaleString('en')}
  {:else}{formatTokens(row.group.input)}{/if}
{/snippet}

<Panel title={`${SYSTEM_AGENT_LABEL} work`} label={`${SYSTEM_AGENT_LABEL} work by function`}>
  {#if load.status === 'error'}
    <p class="m-0 py-6 text-center text-sm text-secondary">Breakdown failed to load.</p>
  {:else}
    <DataTable label={`${SYSTEM_AGENT_LABEL} work by function`} {rows} rowKey={(row) => row.key} {cell} sortKey="cost"
      value={(row, key) => key === 'label' ? row.label : key === 'services' ? row.services : key === 'cost' ? amount(row.group) : key === 'attempts' ? row.group.attempts : row.group.input ?? 0}
      empty={load.status === 'loading' ? 'Loading…' : 'No system work in this range'}
      columns={[{ key: 'label', label: 'Function', sortable: true }, { key: 'services', label: 'Service', sortable: true },
        { key: 'cost', label: 'Est. cost', numeric: true, sortable: true }, { key: 'attempts', label: 'Requests', numeric: true, sortable: true },
        { key: 'input', label: 'Input', numeric: true, sortable: true }]}/>
  {/if}
</Panel>
