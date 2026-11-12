<script lang="ts">
  import { costBasis, money, type UsageCost } from './model';
  let { cost, costs, compact = false }: { cost: UsageCost; costs?: UsageCost[]; compact?: boolean } = $props();
  const values = $derived(costs?.length ? costs : [cost]);
</script>
<span class:compact>{#each values as value, i (`${value.currency}:${value.basis}:${i}`)}
  <span class="band" title={`${value.currency} · ${costBasis(value)}`}>{money(value)}<small>{value.currency} · {costBasis(value)}</small></span>
{/each}</span>
<style>.band { display:block; font-variant-numeric:tabular-nums; } small { display:block; color:rgb(var(--color-secondary)); font-size:10px; font-weight:400; } .compact small { display:none; }</style>
