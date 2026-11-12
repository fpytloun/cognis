<script lang="ts">
  import { count, money, type UsageBucket } from './model';
  import CostValues from './CostValues.svelte';
  let { buckets, metric = 'tokens' }: { buckets: UsageBucket[]; metric?: 'tokens' | 'cost' } = $props();
  let selectedBand = $state('');
  const bands = $derived([...new Map(buckets.flatMap((bucket) => bucket.costs ?? [bucket.cost]).filter((cost) => cost.currency && cost.basis !== 'unknown').map((cost) => [`${cost.currency}:${cost.basis}`, cost])).entries()]);
  const activeBand = $derived(bands.some(([key]) => key === selectedBand) ? selectedBand : bands[0]?.[0] ?? '');
  const band = $derived(bands.find(([key]) => key === activeBand)?.[1] ?? { amount: null, currency: null, basis: 'unknown' as const });
  const costs = $derived(buckets.map((bucket) => (bucket.costs ?? [bucket.cost]).find((cost) => `${cost.currency}:${cost.basis}` === activeBand) ?? { ...band, amount: null }));
  // Number conversion is rendering-only. The source monetary values stay decimal text.
  const values = $derived(buckets.map((bucket, index) => metric === 'tokens' ? bucket.tokens : costs[index].amount === null ? null : Number(costs[index].amount)));
  const max = $derived(Math.max(metric === 'tokens' ? 1 : .000001, ...values.map((value) => value ?? 0)));
</script>

{#if metric === 'cost'}<label class="band">Currency / estimate basis <select aria-label="Cost series band" value={activeBand} onchange={(event) => selectedBand = event.currentTarget.value}>{#each bands as [key, value]}<option value={key}>{value.currency} · {value.basis.replaceAll('_', ' ')}</option>{/each}</select></label>{/if}
<div class="trend" aria-label={metric === 'tokens' ? 'Daily token usage' : 'Daily estimated cost'}>
  <div class="axis"><span>{metric === 'tokens' ? count(max) : money({ ...band, amount: bands.length ? max.toFixed(6) : null })}</span><span>{metric === 'tokens' ? 'Known tokens' : `Estimate (${band.currency ?? 'unknown currency'})`} / UTC day</span></div>
  <div class="bars">
    {#each buckets as bucket, index}
      <div class="column">
        <div class:incomplete={!bucket.complete} class="bar" style={`height:${values[index] === null ? 0 : values[index] === 0 ? 0 : Math.max(2, values[index]! / max * 100)}%`}
          title={`${bucket.start}: ${count(bucket.tokens)} tokens · ${money(costs[index])} · ${bucket.complete ? 'Ready observations' : 'Partial or unavailable'}`}></div>
        {#if values[index] === null}<span class="unknown">?</span>{/if}
      </div>
    {/each}
  </div>
  <div class="axis"><span>{buckets[0]?.start.slice(5)}</span><span>{buckets.at(-1)?.start.slice(5)} · dashed = incomplete</span></div>
</div>
<details>
  <summary>Daily tokens and cost estimates</summary>
  <div class="table-wrap"><table><thead><tr><th>UTC day</th><th>Tokens</th><th>Cost estimate</th><th>Coverage</th></tr></thead><tbody>
    {#each buckets as bucket}<tr><td>{bucket.start}</td><td>{count(bucket.tokens)}</td><td><CostValues costs={bucket.costs} cost={bucket.cost}/></td><td>{bucket.complete ? 'Ready observations' : 'Partial or unavailable'}</td></tr>{/each}
  </tbody></table></div>
</details>

<style>
  .trend { padding-top: .7rem; }
  .band { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:8px; font-size:11px; color:rgb(var(--color-secondary)); } select { max-width:100%; background:transparent; border:1px solid rgb(var(--color-control-border)); border-radius:4px; padding:6px; color:inherit; } select:focus-visible,summary:focus-visible { outline:2px solid rgb(var(--color-accent)); outline-offset:3px; }
  .axis { display:flex; justify-content:space-between; color:rgb(var(--color-secondary)); font-size:.8rem; gap:1rem; }
  .bars { height:135px; display:flex; align-items:stretch; gap:6px; border-bottom:1px solid rgb(var(--color-separator)); margin:.5rem 0; }
  .column { flex:1; min-width:0; display:flex; flex-direction:column; justify-content:flex-end; position:relative; }
  .bar { background:rgb(var(--color-accent) / .65); border-radius:2px 2px 0 0; }
  .incomplete { background:rgb(var(--color-accent) / .12); border:1px dashed rgb(var(--color-accent)); }
  .unknown { text-align:center; color:rgb(var(--color-secondary)); }
  details { margin-top:1rem; font-size:.85rem; } summary { cursor:pointer; color:rgb(var(--color-accent)); }
  .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; margin-top:.5rem; } th,td { padding:.4rem; text-align:left; border-bottom:1px solid rgb(var(--color-separator)); white-space:nowrap; }
</style>
