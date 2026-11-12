<script lang="ts">
  import { untrack } from 'svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import SegmentedControl from '$lib/components/ui/SegmentedControl.svelte';
  import StackedBars from '$lib/usage/StackedBars.svelte';
  import { bucketLabels, formatTokens, seriesColors, tokenColors, type BarBucket } from '$lib/usage/bars';
  import { formatCompactDecimalMoney } from '$lib/usage/decimal';
  import { loadConversationBreakdown, type BreakdownIssue, type BreakdownRange, type BreakdownRow, type ConversationBreakdown } from '$lib/usage/conversation-breakdown';

  /** Cost over time and where it went (conversations, agents, models) for one conversation tree. */
  let { conversationId, inclusion, version = 0 }: {
    conversationId: string;
    inclusion: 'direct' | 'inclusive';
    /** Bumped by the parent on live usage updates. */
    version?: number;
  } = $props();

  let range = $state<BreakdownRange>('auto');
  let data = $state<ConversationBreakdown | null>(null);
  let failed = $state(false);
  /** A user change (conversation, scope, range) is loading; live refreshes stay silent. */
  let switching = $state(true);
  let open = $state<string | null>(null);
  let shownKey = '';
  $effect(() => {
    const id = conversationId, scope = inclusion, window = range;
    void version;
    const key = `${id}\u0001${scope}\u0001${window}`;
    const sameView = untrack(() => data !== null && shownKey === key);
    const controller = new AbortController();
    if (!sameView) switching = true;
    loadConversationBreakdown(id, scope, controller.signal, window)
      .then((value) => {
        // A section whose read failed keeps what this view showed before.
        const previous = sameView ? untrack(() => data) : null;
        data = previous ? { ...value, ...Object.fromEntries(value.missing.map((name) => [name, previous[name]])) } : value;
        shownKey = key;
        failed = false;
      })
      .catch(() => { if (!controller.signal.aborted && !untrack(() => data)) failed = true; })
      .finally(() => { if (!controller.signal.aborted) switching = false; });
    return () => controller.abort();
  });
  const failedCalls = $derived((data?.issues ?? []).filter((item) => item.failed).reduce((sum, item) => sum + item.calls, 0));
  const stoppedCalls = $derived((data?.issues ?? []).filter((item) => !item.failed).reduce((sum, item) => sum + item.calls, 0));
  const unpricedModels = $derived((data?.models ?? []).filter((row) => row.unpriced > 0));
  const plural = (count: number, one: string, many = `${one}s`) => `${count.toLocaleString('en')} ${count === 1 ? one : many}`;

  function money(value: number): string {
    if (!data?.currency) return '—';
    try { return formatCompactDecimalMoney(String(Number(value.toPrecision(6))), data.currency); } catch { return '—'; }
  }
  const bars = $derived<BarBucket[]>((data?.points ?? []).map((point) => {
    const end = new Date(Date.parse(point.start) + (data?.unit === 'hour' ? 3_600_000 : 86_400_000)).toISOString();
    return { key: point.start, ...bucketLabels(point.start, end), partial: false, point: null,
      segments: [{ key: 'cost', label: 'Cost', value: point.cost, color: tokenColors.cost }],
      display: point.cost === null ? '—' : money(point.cost) };
  }));
  /** Top rows plus an "Other" remainder so shares add up to the listed total. */
  function share(rows: BreakdownRow[], limit = 5) {
    const total = rows.reduce((sum, row) => sum + row.costValue, 0);
    const head = rows.slice(0, limit), tail = rows.slice(limit);
    const items = head.map((row, index) => ({ row, color: seriesColors[index % seriesColors.length] }));
    if (tail.length) {
      const rest = tail.reduce((sum, row) => sum + row.costValue, 0);
      items.push({ row: { id: null, label: `Other (${tail.length})`, cost: money(rest), costValue: rest, attempts: tail.reduce((s, r) => s + r.attempts, 0),
        input: null, output: null, cached: null, unpriced: tail.reduce((s, r) => s + r.unpriced, 0) }, color: 'rgb(var(--color-secondary))' });
    }
    return { total, items };
  }
  const pct = (value: number, total: number) => total > 0 ? (value / total < .01 && value > 0 ? '<1%' : `${Math.round(value / total * 100)}%`) : '—';
  const perCall = (row: BreakdownRow) => row.attempts > 0 ? money(row.costValue / row.attempts) : '—';
  const rangeOptions: { value: BreakdownRange; label: string }[] = [
    { value: 'auto', label: 'Lifetime' }, { value: '24h', label: '24h' }, { value: '7d', label: '7d' }, { value: '30d', label: '30d' }
  ];
</script>

{#snippet detail(row: BreakdownRow, href: string | null, functions: BreakdownRow[] = [])}
  {#if functions.length}
    <ul class="functions" aria-label="System work by function">
      {#each functions as item (item.label)}<li><span class="name">{item.label}</span><b>{item.cost}</b><span class="pct">{item.attempts.toLocaleString('en')}×</span></li>{/each}
    </ul>
  {/if}
  <dl class="detail">
    <div><dt>Calls</dt><dd>{row.attempts.toLocaleString('en')}</dd></div>
    <div><dt>Per call</dt><dd>{perCall(row)}</dd></div>
    <div><dt>Input</dt><dd>{formatTokens(row.input)}</dd></div>
    <div><dt>Cached</dt><dd>{formatTokens(row.cached)}</dd></div>
    <div><dt>Output</dt><dd>{formatTokens(row.output)}</dd></div>
  </dl>
  {#if href}<a class="open" href={href}>Open conversation</a>{/if}
{/snippet}

{#snippet distribution(title: string, rows: BreakdownRow[], kind: string)}
  {@const parts = share(rows)}
  <section class="block" aria-label={title}>
    <h3>{title}</h3>
    <span class="stack" aria-hidden="true">
      {#each parts.items as item (item.row.label)}<i style:width={`${parts.total > 0 ? item.row.costValue / parts.total * 100 : 0}%`} style:background={item.color}></i>{/each}
    </span>
    <ul>
      {#each parts.items as item (item.row.label)}
        {@const key = `${kind}:${item.row.id ?? item.row.label}`}
        <li>
          <Button variant="link" size="inline" class="row" aria-expanded={open === key} onclick={() => open = open === key ? null : key}>
            <i class="dot" style:background={item.color}></i>
            <span class="name" title={item.row.label}>{item.row.label}</span>
            <b>{item.row.cost}</b><span class="pct">{pct(item.row.costValue, parts.total)}</span>
          </Button>
          {#if open === key}{@render detail(item.row, kind === 'conversation' && item.row.id ? `/chat/${encodeURIComponent(item.row.id)}` : null,
            kind === 'agent' && item.row.id === null ? data?.system ?? [] : [])}{/if}
        </li>
      {/each}
    </ul>
  </section>
{/snippet}

{#snippet issueList(items: BreakdownIssue[])}
  <ul class="functions" aria-label="Failed and stopped calls">
    {#each items as item (item.key)}<li><span class="name" title={item.model}>{item.model}</span><span class="reason">{item.reason}</span><span class="pct">{item.calls.toLocaleString('en')}×</span></li>{/each}
  </ul>
{/snippet}

{#if data}
  <div class="wrap" class:refreshing={switching}>
    {#if failedCalls || stoppedCalls || unpricedModels.length}
      <section class="block" aria-label="Calls to check">
        {#if failedCalls || stoppedCalls}
          <ul>
            <li>
              <Button variant="link" size="inline" class="row notice" aria-expanded={open === 'issues'} onclick={() => open = open === 'issues' ? null : 'issues'}>
                <span class="name">{[failedCalls ? `${plural(failedCalls, 'call')} failed` : '', stoppedCalls ? `${plural(stoppedCalls, 'call')} stopped` : ''].filter(Boolean).join(' · ')}</span>
                <span class="pct">{open === 'issues' ? 'Hide' : 'Why'}</span>
              </Button>
              {#if open === 'issues'}{@render issueList(data.issues)}{/if}
            </li>
          </ul>
        {/if}
        {#if unpricedModels.length}
          <p class="meta">No price for {unpricedModels.map((row) => `${row.label} (${plural(row.unpriced, 'call')})`).join(', ')}</p>
        {/if}
      </section>
    {/if}
    <section class="block" aria-label="Cost over time">
      <div class="head">
        <h3>Cost per {data.unit}</h3>
        <SegmentedControl label="Chart range" value={range} options={rangeOptions} onchange={(value) => range = value}/>
      </div>
      {#if data.points.some((point) => point.cost)}
        <StackedBars buckets={bars} height={120} legend={false} format={money} label={`Cost per ${data.unit}`}/>
      {:else}
        <p class="meta">No cost in this range.</p>
      {/if}
    </section>
    {#if inclusion === 'inclusive' && data.conversations.length > 1}{@render distribution('Conversations', data.conversations, 'conversation')}{/if}
    {#if data.agents.length}{@render distribution('Agents', data.agents, 'agent')}{/if}
    {#if data.models.length}{@render distribution('Models', data.models, 'model')}{/if}
  </div>
{:else if failed}
  <p class="meta">Breakdown unavailable.</p>
{:else}
  <div class="loading" role="status" aria-label="Loading breakdown"><span></span><span></span></div>
{/if}

<style>
  .wrap { display: grid; gap: 1.1rem; transition: opacity 0.2s; }
  .wrap.refreshing { opacity: 0.6; }
  .block { min-width: 0; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.4rem; }
  h3 { margin: 0 0 0.4rem; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: rgb(var(--color-secondary)); }
  .head h3 { margin: 0; }
  .stack { display: flex; height: 6px; overflow: hidden; border-radius: 999px; background: rgb(var(--color-separator) / 0.6); margin-bottom: 0.35rem; }
  .stack i { display: block; height: 100%; transition: width 0.5s ease-out; }
  ul { margin: 0; padding: 0; list-style: none; display: grid; }
  ul :global(.row) { display: flex; align-items: center; gap: 0.5rem; width: 100%; min-height: 36px; padding: 0.2rem 0; border: 0; background: none; color: inherit; font: inherit; font-size: 0.875rem; text-align: left; cursor: pointer; }
  .name:hover { color: rgb(var(--color-accent)); }
  ul :global(.row):focus-visible { outline: 2px solid rgb(var(--color-accent)); outline-offset: 2px; border-radius: 4px; }
  .dot { width: 8px; height: 8px; border-radius: 2px; flex: none; }
  /* Long names scroll sideways (scrollbar hidden) so cost and share stay in view. */
  .name { flex: 1; min-width: 0; overflow-x: auto; overflow-y: hidden; white-space: nowrap; scrollbar-width: none; overscroll-behavior-x: contain; }
  .name::-webkit-scrollbar { display: none; }
  b { flex-shrink: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
  .pct { width: 2.6rem; flex-shrink: 0; text-align: right; font-size: 0.75rem; color: rgb(var(--color-secondary)); font-variant-numeric: tabular-nums; }
  .functions { margin: 0.1rem 0 0.35rem 1rem; padding: 0; display: grid; gap: 0.15rem; font-size: 0.8125rem; }
  .functions li { display: flex; align-items: center; gap: 0.5rem; }
  /* minmax(0, 1fr): long names truncate instead of widening the column past the panel. */
  ul { grid-template-columns: minmax(0, 1fr); }
  li { min-width: 0; }
  .reason { flex-shrink: 0; font-size: 0.75rem; color: rgb(var(--color-secondary)); }
  ul :global(.row.notice) { color: rgb(var(--color-warning)); }
  .detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(5.5rem, 1fr)); gap: 0.35rem 0.75rem; margin: 0.1rem 0 0.5rem 1rem; padding: 0.5rem 0.65rem; border-radius: 0.5rem; background: rgb(var(--color-inset)); }
  .detail dt { font-size: 0.6875rem; color: rgb(var(--color-secondary)); }
  .detail dd { margin: 0; font-size: 0.8125rem; font-variant-numeric: tabular-nums; }
  .open { display: inline-block; margin: 0 0 0.5rem 1rem; font-size: 0.8125rem; color: rgb(var(--color-accent)); }
  .meta { margin: 0; font-size: 0.75rem; color: rgb(var(--color-secondary)); }
  .loading { display: grid; gap: 0.5rem; }
  .loading span { height: 4rem; border-radius: 0.5rem; background: rgb(var(--color-separator) / 0.4); animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.5; } }
  @media (prefers-reduced-motion: reduce) { .stack i { transition: none; } .loading span { animation: none; } }
</style>
