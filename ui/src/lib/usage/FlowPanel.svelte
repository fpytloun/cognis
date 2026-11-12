<script lang="ts">
  import Select from '$lib/components/ui/Select.svelte';
  import { cubicOut } from 'svelte/easing';
  import { Tween, prefersReducedMotion } from 'svelte/motion';
  import Panel from '$lib/components/ui/Panel.svelte';
  import type { UsageDataSource } from './api';
  import { seriesColors } from './bars';
  import { formatCompactDecimalMoney } from './decimal';
  import { layoutFlow, loadFlow, type Flow, type FlowLevel, type FlowNode } from './flow';
  import type { UsageQuery } from './model';

  /** Where the cost flows between attribution levels. Secondary analysis, below the main Costs views. */
  let { query, source, onfilter }: {
    query: UsageQuery;
    source: UsageDataSource;
    /** Narrows the dashboard to a node (provider, model, agent or project). */
    onfilter?: (level: FlowLevel, id: string) => void;
  } = $props();

  const presets: { value: string; label: string; levels: FlowLevel[] }[] = [
    { value: 'model-agent', label: 'Model → Agent', levels: ['model', 'agent'] },
    { value: 'agent-model', label: 'Agent → Model', levels: ['agent', 'model'] },
    { value: 'agent-conversation', label: 'Agent → Conversation', levels: ['agent', 'conversation'] },
    { value: 'project-agent-conversation', label: 'Project → Agent → Conversation', levels: ['project', 'agent', 'conversation'] },
    { value: 'provider-model-agent', label: 'Provider → Model → Agent', levels: ['provider', 'model', 'agent'] }
  ];
  let preset = $state('model-agent');
  let load = $state<{ status: 'loading' } | { status: 'ready'; flow: Flow } | { status: 'error' }>({ status: 'loading' });
  let width = $state(0);
  let hover = $state<string | null>(null);
  const levels = $derived(presets.find((item) => item.value === preset)?.levels ?? presets[0].levels);
  $effect(() => {
    const chosen = levels;
    const controller = new AbortController();
    load = { status: 'loading' };
    loadFlow(source, query, chosen, controller.signal)
      .then((flow) => { load = { status: 'ready', flow }; })
      .catch(() => { if (!controller.signal.aborted) load = { status: 'error' }; });
    return () => controller.abort();
  });

  const reveal = new Tween(1, { duration: 700, easing: cubicOut });
  $effect(() => {
    if (load.status !== 'ready' || prefersReducedMotion.current) return;
    void reveal.set(0, { duration: 0 }).then(() => reveal.set(1));
  });

  const LABEL = 150, HEIGHT = 300, NODE = 12;
  const flow = $derived(load.status === 'ready' ? load.flow : null);
  const layout = $derived(flow && width ? layoutFlow(flow, Math.max(240, width - LABEL), HEIGHT, seriesColors, NODE) : null);
  function money(value: number): string {
    if (!flow?.currency) return '—';
    try { return formatCompactDecimalMoney(String(Number(value.toPrecision(6))), flow.currency); } catch { return '—'; }
  }
  const total = $derived(flow ? flow.nodes.filter((node) => node.depth === 0).reduce((acc, node) => acc + node.value, 0) : 0);
  const related = (key: string, node: string) => {
    const [from, to] = key.split('\u0001');
    return from === node || to === node;
  };
  const filterable = (node: FlowNode) => Boolean(onfilter && node.id && !node.other && levels[node.depth] !== 'conversation' && levels[node.depth] !== 'schedule');
  const pct = (value: number) => total > 0 ? `${Math.round(value / total * 100)}%` : '';
</script>

<Panel title="Cost flow" label="Cost flow between levels">
  {#snippet actions()}
    <Select label="Flow levels" value={preset} options={presets} onchange={(value) => preset = value}/>
  {/snippet}
  <div class="relative min-w-0" bind:clientWidth={width}>
    {#if load.status === 'loading' && !layout}
      <div class="h-[300px] animate-pulse rounded-2xl bg-separator/30 motion-reduce:animate-none" role="status" aria-label="Loading cost flow"></div>
    {:else if load.status === 'error'}
      <p class="py-8 text-center text-sm text-secondary">Cost flow failed to load.</p>
    {:else if layout && layout.links.length}
      <svg viewBox={`0 0 ${layout.width + LABEL} ${HEIGHT}`} height={HEIGHT} class="block w-full overflow-visible" role="img"
        aria-label={`Estimated cost flow ${levels.join(' to ')}: ${layout.links.slice(0, 12).map((item) => `${layout.nodes.find((n) => n.key === item.source)?.label} to ${layout.nodes.find((n) => n.key === item.target)?.label} ${money(item.value)}`).join('; ')}`}
        class:opacity-60={load.status === 'loading'}>
        <g opacity={reveal.current}>
          {#each layout.links as item (item.source + item.target)}
            <path d={item.path} fill={item.color} fill-opacity={hover ? (related(`${item.source}\u0001${item.target}`, hover) ? .55 : .08) : .28}
              class="transition-[fill-opacity] duration-150">
              <title>{layout.nodes.find((n) => n.key === item.source)?.label} → {layout.nodes.find((n) => n.key === item.target)?.label}: {money(item.value)} ({pct(item.value)})</title>
            </path>
          {/each}
        </g>
        {#each layout.nodes as node (node.key)}
          {@const last = node.depth === levels.length - 1}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <g role={filterable(node) ? 'button' : 'img'} tabindex={filterable(node) ? 0 : undefined} aria-label={`${node.label}: ${money(node.value)}${filterable(node) ? '. Filter the dashboard' : ''}`}
            class={filterable(node) ? 'cursor-pointer focus-visible:outline-none' : ''}
            onmouseenter={() => hover = node.key} onmouseleave={() => hover = null} onfocus={() => hover = node.key} onblur={() => hover = null}
            onclick={() => { if (filterable(node)) onfilter!(levels[node.depth], node.id!); }}
            onkeydown={(event) => { if ((event.key === 'Enter' || event.key === ' ') && filterable(node)) { event.preventDefault(); onfilter!(levels[node.depth], node.id!); } }}>
            <rect x={node.x} y={node.y} width={NODE} height={node.h} rx="2" fill={node.color} opacity={hover && hover !== node.key ? .6 : 1}/>
            {#if node.h >= 9}
              <text x={node.x + NODE + 6} y={node.y + node.h / 2} dominant-baseline="middle" class="fill-foreground text-xs" paint-order="stroke" stroke="rgb(var(--color-surface))" stroke-width="3">
                {node.label.length > 26 ? `${node.label.slice(0, 25)}…` : node.label}<tspan dx="6" class="fill-secondary tabular-nums">{money(node.value)}</tspan>
              </text>
            {/if}
            <title>{node.label}: {money(node.value)} ({pct(node.value)})</title>
          </g>
        {/each}
      </svg>
      <div class="mt-2 flex flex-wrap justify-between gap-2 text-xs text-secondary">
        <span>{levels.map((level) => level[0].toUpperCase() + level.slice(1)).join(' → ')} · largest groups, the rest as Other</span>
        {#if onfilter}<span>Click a node to filter</span>{/if}
      </div>
    {:else}
      <p class="py-8 text-center text-sm text-secondary">No priced usage in this range</p>
    {/if}
  </div>
</Panel>
