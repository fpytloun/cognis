<script lang="ts" module>
  export interface DonutSlice { key: string; label: string; value: number; display: string; color: string }
</script>

<script lang="ts">
  import { cubicOut } from 'svelte/easing';
  import { Tween, prefersReducedMotion } from 'svelte/motion';
  import Button from '$lib/components/ui/Button.svelte';

  /**
   * Share of one total. Slices are drawn in the given order; the legend carries
   * the exact values, so the ring is never the only encoding.
   */
  let { slices, total, caption, label, size = 148, onselect }: {
    slices: DonutSlice[];
    /** Centre text: the formatted total. */
    total: string;
    caption?: string;
    label: string;
    size?: number;
    onselect?: (slice: DonutSlice) => void;
  } = $props();

  const stroke = 18;
  const radius = $derived((size - stroke) / 2);
  const circumference = $derived(2 * Math.PI * radius);
  const sum = $derived(slices.reduce((acc, slice) => acc + Math.max(0, slice.value), 0));
  const sweep = new Tween(1, { duration: 700, easing: cubicOut });
  const dataKey = $derived(slices.map((slice) => `${slice.key}:${slice.value}`).join('|'));
  $effect(() => {
    void dataKey;
    if (prefersReducedMotion.current) return;
    void sweep.set(0, { duration: 0 }).then(() => sweep.set(1));
  });
  const arcs = $derived.by(() => {
    let offset = 0;
    return slices.map((slice) => {
      const share = sum > 0 ? Math.max(0, slice.value) / sum : 0;
      const arc = { slice, share, start: offset };
      offset += share;
      return arc;
    });
  });
  let hover = $state<string | null>(null);
  const pct = (share: number) => share > 0 && share < .01 ? '<1%' : `${Math.round(share * 100)}%`;
</script>

<div class="flex min-w-0 flex-wrap items-center gap-5">
  <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${label}: ${arcs.map((arc) => `${arc.slice.label} ${arc.slice.display} (${pct(arc.share)})`).join(', ')}`} class="shrink-0 -rotate-90">
    <circle cx={size / 2} cy={size / 2} r={radius} fill="none" class="stroke-separator/60" stroke-width={stroke}/>
    {#each arcs as arc (arc.slice.key)}
      {#if arc.share > 0}
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke={arc.slice.color} stroke-width={hover === arc.slice.key ? stroke + 4 : stroke}
          stroke-dasharray={`${Math.max(0, arc.share * circumference * sweep.current - 1.5)} ${circumference}`}
          stroke-dashoffset={-arc.start * circumference * sweep.current}
          opacity={hover && hover !== arc.slice.key ? .45 : 1}/>
      {/if}
    {/each}
    <g class="rotate-90" style:transform-origin="center">
      <text x="50%" y="48%" text-anchor="middle" class="fill-foreground text-base font-semibold tabular-nums">{total}</text>
      {#if caption}<text x="50%" y="62%" text-anchor="middle" class="fill-secondary text-xs">{caption}</text>{/if}
    </g>
  </svg>
  <ul class="m-0 grid min-w-[180px] flex-1 list-none gap-1.5 p-0 text-sm">
    {#each arcs as arc (arc.slice.key)}
      <li class="flex min-w-0 items-center gap-2" onmouseenter={() => hover = arc.slice.key} onmouseleave={() => hover = null}>
        <i class="h-2.5 w-2.5 shrink-0 rounded-sm" style:background={arc.slice.color} aria-hidden="true"></i>
        {#if onselect}
          <Button variant="link" size="inline" class="min-w-0 flex-1 truncate !text-foreground hover:!text-accent" title={arc.slice.label}
            onclick={() => onselect(arc.slice)} onfocus={() => hover = arc.slice.key} onblur={() => hover = null}>{arc.slice.label}</Button>
        {:else}<span class="min-w-0 flex-1 truncate" title={arc.slice.label}>{arc.slice.label}</span>{/if}
        <span class="tabular-nums font-medium">{arc.slice.display}</span>
        <span class="w-10 text-right text-xs tabular-nums text-secondary">{pct(arc.share)}</span>
      </li>
    {/each}
  </ul>
</div>
