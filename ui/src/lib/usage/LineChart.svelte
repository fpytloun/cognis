<script lang="ts">
  import { cubicOut } from 'svelte/easing';
  import { Tween, prefersReducedMotion } from 'svelte/motion';
  import { niceMax, type LineSeries } from './bars';
  import type { ChartPoint } from './chart-data';

  let { labels, titles, series, format, label, height = 200, partial = [], points = [], onpoint }: {
    /** Short axis label per bucket. */
    labels: string[];
    /** Full label per bucket for tooltips and accessible names. */
    titles: string[];
    series: LineSeries[];
    format: (value: number) => string;
    label: string;
    height?: number;
    /** Buckets drawn as incomplete. */
    partial?: boolean[];
    /** Optional drill target per bucket. */
    points?: (ChartPoint | null)[];
    onpoint?: (point: ChartPoint) => void;
  } = $props();

  let width = $state(0);
  let hover = $state<number | null>(null);
  // Lines draw in from the left whenever the data set changes.
  const grow = new Tween(1, { duration: 650, easing: cubicOut });
  const dataKey = $derived(series.map((item) => `${item.key}:${item.values.join(',')}`).join('|'));
  $effect(() => {
    void dataKey;
    if (prefersReducedMotion.current) return;
    void grow.set(0, { duration: 0 }).then(() => grow.set(1));
  });
  const plot = { left: 52, right: 10, top: 10, bottom: 24 };
  const w = $derived(width || 640);
  const innerWidth = $derived(Math.max(1, w - plot.left - plot.right));
  const innerHeight = $derived(height - plot.top - plot.bottom);
  const known = $derived(series.flatMap((item) => item.values).filter((value): value is number => value !== null && Number.isFinite(value)));
  const max = $derived(niceMax(Math.max(0, ...known)));
  const count = $derived(Math.max(1, labels.length));
  const x = (index: number) => plot.left + (count <= 1 ? innerWidth / 2 : index / (count - 1) * innerWidth);
  const y = (value: number) => plot.top + innerHeight - (value / max) * innerHeight;
  const labelEvery = $derived(Math.max(1, Math.ceil(count / Math.max(1, Math.floor(innerWidth / 56)))));
  function path(values: (number | null)[]): string {
    let open = false;
    return values.map((value, index) => {
      if (value === null || !Number.isFinite(value)) { open = false; return ''; }
      const segment = `${open ? 'L' : 'M'}${x(index).toFixed(1)},${y(value).toFixed(1)}`;
      open = true;
      return segment;
    }).join(' ');
  }
  function activate(index: number) {
    const point = points[index];
    if (point && onpoint) onpoint(point);
  }
</script>

<div class="line" bind:clientWidth={width}>
  {#if series.length > 1}
    <ul class="legend" aria-hidden="true">
      {#each series as item}<li><i style:background={item.color} class:dashed={item.dashed}></i>{item.label}</li>{/each}
    </ul>
  {/if}
  {#if known.length}
    <svg viewBox={`0 0 ${w} ${height}`} {height} role="group" aria-label={label}>
      {#each [0, 0.5, 1] as part}
        <line class="grid" x1={plot.left} x2={w - plot.right} y1={y(max * part)} y2={y(max * part)}/>
        <text class="tick" x={plot.left - 8} y={y(max * part) + 3} text-anchor="end">{format(max * part)}</text>
      {/each}
      {#each partial as isPartial, index}
        {#if isPartial}<rect class="partial" x={x(index) - innerWidth / count / 2} width={innerWidth / count} y={plot.top} height={innerHeight}/>{/if}
      {/each}
      {#if hover !== null}<line class="cursor" x1={x(hover)} x2={x(hover)} y1={plot.top} y2={plot.top + innerHeight}/>{/if}
      {#each series as item (item.key)}
        {#if item.dashed}
          <path d={path(item.values)} fill="none" stroke={item.color} stroke-width="2" stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round" opacity={grow.current}/>
        {:else}
          <path d={path(item.values)} pathLength="1" fill="none" stroke={item.color} stroke-width="2" stroke-dasharray="1" stroke-dashoffset={1 - grow.current} stroke-linejoin="round" stroke-linecap="round"/>
        {/if}
        {#each item.values as value, index}
          {#if value !== null && Number.isFinite(value) && (hover === index || item.values.filter((v) => v !== null).length === 1)}
            <circle cx={x(index)} cy={y(value)} r="3.5" fill={item.color}/>
          {/if}
        {/each}
      {/each}
      {#each labels as text, index}
        <rect class="hit" x={x(index) - innerWidth / count / 2} width={innerWidth / count} y={plot.top} height={innerHeight}
          role="button" tabindex="0"
          aria-label={`${titles[index]}: ${series.map((item) => `${item.label} ${item.values[index] == null ? '—' : format(item.values[index]!)}`).join(', ')}`}
          onmouseenter={() => hover = index} onmouseleave={() => hover = null} onfocus={() => hover = index} onblur={() => hover = null}
          onclick={() => activate(index)} onkeydown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(index); } }}/>
        {#if index % labelEvery === 0}<text class="tick" x={x(index)} y={height - 6} text-anchor="middle">{text}</text>{/if}
      {/each}
    </svg>
    {#if hover !== null}
      <div class="tooltip" role="status" style:left={`${Math.min(Math.max(x(hover), 90), w - 90)}px`}>
        <strong>{titles[hover]}{partial[hover] ? ' · partial' : ''}</strong>
        {#each series as item}<span class="row"><i style:background={item.color}></i>{item.label}<b>{item.values[hover] == null ? '—' : format(item.values[hover]!)}</b></span>{/each}
      </div>
    {/if}
  {:else}
    <p class="empty" style:height={`${height}px`}>No data for this range</p>
  {/if}
</div>

<style>
  .line { position: relative; min-width: 0; }
  svg { display: block; width: 100%; overflow: visible; }
  .grid { stroke: rgb(var(--color-separator)); }
  .cursor { stroke: rgb(var(--color-secondary) / .5); stroke-dasharray: 2 3; }
  .partial { fill: rgb(var(--color-separator) / .25); }
  .tick { font-size: 10px; fill: rgb(var(--color-secondary)); font-variant-numeric: tabular-nums; }
  .hit { fill: transparent; cursor: pointer; outline: none; }
  .hit:focus-visible { stroke: rgb(var(--color-accent)); stroke-width: 2; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 0 0 8px; padding: 0; list-style: none; font-size: 11px; color: rgb(var(--color-secondary)); }
  .legend li, .row { display: flex; align-items: center; gap: 6px; }
  i { width: 10px; height: 3px; border-radius: 2px; flex: none; }
  i.dashed { background-image: linear-gradient(90deg, transparent 50%, rgb(var(--color-surface)) 50%); background-size: 5px 3px; }
  .tooltip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; display: grid; gap: 3px; min-width: 160px; padding: 8px 10px; border: 1px solid rgb(var(--color-separator)); border-radius: 8px; background: rgb(var(--color-elevated)); box-shadow: 0 6px 18px rgb(0 0 0 / .12); font-size: 11px; z-index: 2; }
  .row { color: rgb(var(--color-secondary)); }
  .row b { margin-left: auto; padding-left: 12px; font-weight: 600; color: rgb(var(--color-foreground)); font-variant-numeric: tabular-nums; }
  .empty { display: grid; place-items: center; margin: 0; color: rgb(var(--color-secondary)); font-size: 12px; border: 1px dashed rgb(var(--color-separator)); border-radius: 8px; }
</style>
