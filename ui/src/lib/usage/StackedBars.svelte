<script lang="ts">
  import { cubicOut } from 'svelte/easing';
  import { Tween, prefersReducedMotion } from 'svelte/motion';
  import ChartLegend from '$lib/components/ui/ChartLegend.svelte';
  import type { ChartPoint } from './chart-data';
  import { niceMax, type BarBucket, type BarSegment } from './bars';
  let { buckets, format, legend = true, height = 200, onpoint, onbucket, hint, label }: {
    buckets: BarBucket[];
    format: (value: number) => string;
    legend?: boolean;
    height?: number;
    onpoint?: (point: ChartPoint) => void;
    /** Bucket click; takes precedence over `onpoint`. */
    onbucket?: (bucket: BarBucket) => void;
    /** Tooltip footer describing the click, for example "Click to see hours". */
    hint?: string;
    label: string;
  } = $props();
  let width = $state(0);
  let hover = $state<number | null>(null);
  /** Series hidden from the legend; the scale follows the visible ones. */
  let hidden = $state<string[]>([]);
  let focusSeries = $state<string | null>(null);
  const shown = (segment: BarSegment) => !hidden.includes(segment.key);
  function toggle(key: string) { hidden = hidden.includes(key) ? hidden.filter((item) => item !== key) : [...hidden, key]; }
  // Bars grow from the baseline whenever the data set changes (range, filters, metric).
  const grow = new Tween(1, { duration: 500, easing: cubicOut });
  const dataKey = $derived(buckets.map((bucket) => `${bucket.key}:${bucket.segments.map((s) => s.value).join(',')}`).join('|'));
  $effect(() => {
    void dataKey;
    if (prefersReducedMotion.current) return;
    void grow.set(0, { duration: 0 }).then(() => grow.set(1));
  });
  const plot = $derived({ left: 52, right: 8, top: 10, bottom: 24 });
  const totals = $derived(buckets.map((bucket) => bucket.segments.filter(shown).reduce((sum, segment) => sum + (segment.value ?? 0), 0)));
  const known = $derived(buckets.map((bucket) => bucket.segments.some((segment) => segment.value !== null)));
  const max = $derived(niceMax(Math.max(0, ...totals)));
  const innerWidth = $derived(Math.max(1, (width || 640) - plot.left - plot.right));
  const innerHeight = $derived(height - plot.top - plot.bottom);
  const slot = $derived(innerWidth / Math.max(1, buckets.length));
  const barWidth = $derived(Math.max(2, Math.min(28, slot * 0.72)));
  const ticks = $derived([0, 0.5, 1].map((part) => max * part));
  const y = (value: number) => plot.top + innerHeight - (value / max) * innerHeight;
  /** Animated bar geometry; ticks and labels use the static scale. */
  const yBar = (value: number) => y(value * grow.current);
  const labelEvery = $derived(Math.max(1, Math.ceil(buckets.length / Math.max(1, Math.floor(innerWidth / 56)))));
  const series = $derived([...new Map(buckets.flatMap((bucket) => bucket.segments).map((segment) => [segment.key, segment])).values()]);
  function activate(index: number) {
    const bucket = buckets[index];
    if (!bucket) return;
    if (onbucket) { onbucket(bucket); return; }
    if (bucket.point && onpoint) onpoint(bucket.point);
  }
  const clickable = $derived(Boolean(onbucket || onpoint));
</script>

<div class="bars" bind:clientWidth={width}>
  {#if legend && series.length > 1}
    <div class="legend"><ChartLegend label={`${label} series`} items={series.map((item) => ({ key: item.key, label: item.label, color: item.color, hidden: hidden.includes(item.key) }))}
      ontoggle={toggle} onhover={(key) => focusSeries = key}/></div>
  {/if}
  {#if known.some(Boolean)}
    <svg viewBox={`0 0 ${width || 640} ${height}`} {height} role="group" aria-label={label}>
      {#each ticks as tick}
        <line class="grid" x1={plot.left} x2={(width || 640) - plot.right} y1={y(tick)} y2={y(tick)}/>
        <text class="tick" x={plot.left - 8} y={y(tick) + 3} text-anchor="end">{format(tick)}</text>
      {/each}
      {#each buckets as bucket, index (bucket.key)}
        {@const x = plot.left + index * slot + (slot - barWidth) / 2}
        <g class:partial={bucket.partial} class:dim={hover !== null && hover !== index}>
          {#if known[index]}
            {@const stack = bucket.segments.filter(shown).reduce<{ offset: number; rects: { segment: BarSegment; from: number; to: number }[] }>(
              (acc, segment) => {
                const value = segment.value ?? 0;
                acc.rects.push({ segment, from: acc.offset, to: acc.offset + value });
                acc.offset += value;
                return acc;
              }, { offset: 0, rects: [] }).rects}
            {#each stack as part}
              {#if part.to > part.from}
                <rect x={x} width={barWidth} y={yBar(part.to)} height={Math.max(0.5, yBar(part.from) - yBar(part.to))} fill={part.segment.color} rx="1.5"
                  opacity={focusSeries && focusSeries !== part.segment.key ? .3 : 1}/>
              {/if}
            {/each}
          {:else}
            <rect class="missing" x={x} width={barWidth} y={plot.top + innerHeight - 2} height="2"/>
          {/if}
          <rect class="hit" x={plot.left + index * slot} width={slot} y={plot.top} height={innerHeight}
            role="button" tabindex="0" class:clickable
            aria-label={`${bucket.title}: ${bucket.display}${bucket.partial ? ' (partial day)' : ''}${bucket.segments.length > 1 ? `. ${bucket.segments.map((segment) => `${segment.label} ${segment.value === null ? '—' : format(segment.value)}`).join(', ')}` : ''}`}
            onmouseenter={() => hover = index} onmouseleave={() => hover = null} onfocus={() => hover = index} onblur={() => hover = null}
            onclick={() => activate(index)} onkeydown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(index); } }}/>
        </g>
        {#if index % labelEvery === 0}
          <text class="tick" x={plot.left + index * slot + slot / 2} y={height - 6} text-anchor="middle">{bucket.label}</text>
        {/if}
      {/each}
    </svg>
    {#if hover !== null && buckets[hover]}
      {@const bucket = buckets[hover]}
      {@const visibleSegments = bucket.segments.filter(shown)}
      {@const sum = visibleSegments.reduce((acc, segment) => acc + (segment.value ?? 0), 0)}
      <div class="tooltip" role="status" style:left={`${Math.min(Math.max(plot.left + hover * slot + slot / 2, 90), (width || 640) - 90)}px`}>
        <strong>{bucket.title}{bucket.partial ? ' · partial' : ''}</strong>
        <span class="total">{visibleSegments.length === bucket.segments.length ? bucket.display : format(sum)}</span>
        {#if visibleSegments.length > 1}
          {#each [...visibleSegments].sort((a, b) => (b.value ?? -1) - (a.value ?? -1)) as segment (segment.key)}
            <span class="row"><i style:background={segment.color}></i>{segment.label}<b>{segment.value === null ? '—' : format(segment.value)}</b><em>{sum > 0 && segment.value ? `${Math.round(segment.value / sum * 100)}%` : ''}</em></span>
          {/each}
        {/if}
        {#if hint && clickable}<span class="hint">{hint}</span>{/if}
      </div>
    {/if}
  {:else}
    <p class="empty" style:height={`${height}px`}>No data for this range</p>
  {/if}
</div>

<style>
  .bars { position: relative; min-width: 0; }
  svg { display: block; width: 100%; overflow: visible; }
  .grid { stroke: rgb(var(--color-separator)); stroke-width: 1; }
  .tick { font-size: 10px; fill: rgb(var(--color-secondary)); font-variant-numeric: tabular-nums; }
  g.dim rect:not(.hit) { opacity: .45; }
  g.partial rect:not(.hit):not(.missing) { opacity: .55; }
  .missing { fill: rgb(var(--color-separator)); }
  .hit { fill: transparent; outline: none; }
  .hit.clickable { cursor: pointer; }
  .row em { width: 2.4em; text-align: right; font-style: normal; color: rgb(var(--color-secondary)); font-variant-numeric: tabular-nums; }
  .hint { margin-top: 2px; color: rgb(var(--color-accent)); }
  .hit:focus-visible { stroke: rgb(var(--color-accent)); stroke-width: 2; }
  .legend { margin: 0 0 6px; }
  .row { display: flex; align-items: center; gap: 6px; }
  i { width: 8px; height: 8px; border-radius: 2px; flex: none; }
  .tooltip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; display: grid; gap: 3px; min-width: 150px; padding: 8px 10px; border: 1px solid rgb(var(--color-separator)); border-radius: 8px; background: rgb(var(--color-elevated)); box-shadow: 0 6px 18px rgb(0 0 0 / .12); font-size: 11px; z-index: 2; }
  .tooltip .total { font-size: 14px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .row { color: rgb(var(--color-secondary)); }
  .row b { margin-left: auto; padding-left: 12px; font-weight: 550; color: rgb(var(--color-foreground)); font-variant-numeric: tabular-nums; }
  .empty { display: grid; place-items: center; margin: 0; color: rgb(var(--color-secondary)); font-size: 12px; border: 1px dashed rgb(var(--color-separator)); border-radius: 8px; }
</style>
