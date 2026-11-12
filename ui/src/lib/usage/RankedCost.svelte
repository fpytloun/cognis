<script lang="ts">
  import { costComposition, hasCostCohortDrill, type CompositionRow } from './composition';
  import { formatCompactDecimalMoney } from './decimal';
  import type { UsageAttributionDimension, UsageCostBand, UsageCostCohort } from './model';
  let { title, dimension, band, oncohort, detailAvailable = false, limit = 6 }: {
    title: string; dimension: UsageAttributionDimension; band?: UsageCostBand;
    oncohort: (cohort: UsageCostCohort) => void; detailAvailable?: boolean; limit?: number;
  } = $props();
  let expanded = $state(false);
  const composition = $derived(band ? costComposition(band, dimension) : null);
  const rows = $derived(composition?.rows ?? []);
  const visible = $derived(expanded ? rows : rows.slice(0, limit));
  // Bars share one scale: the largest returned amount, so rows compare by length.
  const top = $derived(Math.max(0, ...rows.map((row) => row.cohort.amount === null ? 0 : Number(row.cohort.amount))));
  function amount(value: string | null): string {
    if (value === null || !band) return '—';
    try { return formatCompactDecimalMoney(value, band.currency); } catch { return value; }
  }
  function width(row: CompositionRow): number {
    const value = row.cohort.amount === null ? 0 : Number(row.cohort.amount);
    return top > 0 && Number.isFinite(value) ? Math.max(value > 0 ? 1.5 : 0, value / top * 100) : 0;
  }
  function action(row: CompositionRow) {
    return detailAvailable && hasCostCohortDrill(row.cohort) ? 'Inspect filtered requests' : 'Explain unavailable detail';
  }
</script>

<section class="ranked" aria-label={`${title} cost attribution`}>
  <header><h3>{title}</h3>{#if rows.length}<span>{rows.length}</span>{/if}</header>
  {#if !composition}
    <p class="muted">No breakdown for this range</p>
  {:else if !rows.length}
    <p class="muted">{composition.reason ?? 'No priced usage'}</p>
  {:else}
    <ol>
      {#each visible as row (row.cohort.key)}
        <li>
          <button onclick={() => oncohort(row.cohort)} aria-label={`${row.cohort.label}: ${row.display}, ${row.percentage}. ${action(row)}`}>
            <span class="name" title={row.cohort.label}>{row.cohort.label}</span>
            <span class="value" title={row.display}>{amount(row.cohort.amount)}</span>
            <span class="share">{row.share === null ? '' : row.percentage}</span>
            <span class="track" aria-hidden="true"><span style:width={`${width(row)}%`}></span></span>
          </button>
        </li>
      {/each}
    </ol>
    {#if rows.length > limit}
      <button class="more" onclick={() => expanded = !expanded}>{expanded ? 'Show less' : `Show all ${rows.length}`}</button>
    {/if}
    {#if composition.reason}<p class="note">{composition.reason}</p>{/if}
  {/if}
</section>

<style>
  .ranked { min-width: 0; }
  header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
  h3 { margin: 0; font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: .06em; color: rgb(var(--color-secondary)); }
  header span { font-size: 11px; color: rgb(var(--color-quiet, var(--color-secondary))); font-variant-numeric: tabular-nums; }
  ol { margin: 0; padding: 0; list-style: none; }
  li button { display: grid; grid-template-columns: minmax(0, 1fr) auto 3.2em; grid-template-areas: "name value share" "track track track"; align-items: baseline; gap: 3px 10px; width: 100%; min-height: 40px; padding: 6px 4px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
  li button:hover { background: rgb(var(--color-surface-hover)); }
  li button:focus-visible, .more:focus-visible { outline: 2px solid rgb(var(--color-accent)); outline-offset: 1px; }
  .name { grid-area: name; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12.5px; }
  .value { grid-area: value; font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .share { grid-area: share; text-align: right; font-size: 11px; color: rgb(var(--color-secondary)); font-variant-numeric: tabular-nums; }
  .track { grid-area: track; display: block; height: 4px; border-radius: 2px; background: rgb(var(--color-separator) / .6); overflow: hidden; }
  .track span { display: block; height: 100%; border-radius: inherit; background: rgb(var(--color-accent) / .75); }
  .more { margin-top: 4px; min-height: 36px; padding: 4px; border: 0; background: transparent; color: rgb(var(--color-accent)); font: inherit; font-size: 12px; cursor: pointer; }
  .muted, .note { margin: 6px 0; font-size: 12px; color: rgb(var(--color-secondary)); }
  .note { font-size: 11px; }
  @media (max-width: 767px), (pointer: coarse) { li button { min-height: 44px; } .more { min-height: 44px; } }
</style>
