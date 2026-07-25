<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import type { ChartType as ChartJsType } from 'chart.js';
  import type Chart from 'chart.js/auto';
  import type { RichBlock } from '$lib/rich-deliverable';
  import { chartPaletteColors, chartPointSummary, chartRangeOptions, chartXLabel, DEFAULT_CHART_THEME, formatChartValue, neutralChartConfig, normalizeChartData, type ChartThemeColors, type PinnedChartPoint } from '$lib/rich-data';

  export let block: RichBlock;

  let canvas: HTMLCanvasElement;
  let wrapperEl: HTMLDivElement;
  let chart: Chart<ChartJsType> | null = null;

  /** Resolve live `--rich-*` design tokens into concrete canvas-safe colors.
   * Chart.js draws on <canvas> and cannot read CSS custom properties itself,
   * so colors must be resolved here at render time to stay theme-aware
   * (light/dark) instead of the previous hardcoded dark-only values. */
  function resolveChartTheme(el: Element | undefined): ChartThemeColors {
    if (typeof window === 'undefined' || !el) return DEFAULT_CHART_THEME;
    const style = getComputedStyle(el);
    const muted = style.getPropertyValue('--rich-muted').trim();
    const text = style.getPropertyValue('--rich-text').trim();
    const textSecondary = style.getPropertyValue('--rich-text-secondary').trim();
    if (!muted || !text || !textSecondary) return DEFAULT_CHART_THEME;
    return {
      gridColor: `color-mix(in srgb, ${muted} 30%, transparent)`,
      tickColor: textSecondary,
      textColor: text,
    };
  }
  let activeRange = 'all';
  let hiddenSeries = new Set<string>();
  let hoverPoint: PinnedChartPoint | null = null;
  let pinnedPoint: PinnedChartPoint | null = null;
  let destroyed = false;

  $: ranges = chartRangeOptions(block);
  $: if (ranges.length > 0 && !ranges.some((range) => range.id === activeRange)) {
    activeRange = ranges[0].id;
  }
  $: normalized = normalizeChartData(block, activeRange);
  $: isProgress = normalized.chartType === 'progress';
  $: isTimeSeries = normalized.xAxis.type === 'time';
  // Trend baseline and click-to-pin only make sense for a real time series
  // rendered as a continuous shape. On categorical/linear or bar-shaped
  // charts they produce a meaningless diagonal line and awkward "pinned"
  // chrome (see docs/specs — rich deliverable chart correctness).
  $: showBaseline = isTimeSeries && ['line', 'area', 'sparkline'].includes(normalized.chartType);
  $: allowPinning = isTimeSeries;
  $: showLegend = normalized.legendPosition !== 'none'
    && (normalized.chartType === 'donut' ? normalized.labels.length > 1 : normalized.series.length > 1);
  $: legendColors = chartPaletteColors(normalized.paletteToken);
  $: fallbackRows = normalized.labels.map((label, index) => ({
      label,
      values: normalized.series.map((series) => ({
        label: series.label,
        value: series.points.find((point) => chartXLabel(point.x) === label)?.y ?? null,
    })),
  }));
  $: baselinePoints = showBaseline ? staticPoints(normalized.series[0]?.points.map((point) =>
    typeof point.y === 'number' ? point.y : (point.y[0] + point.y[1]) / 2,
  ) ?? []) : '';

  function prefersReducedMotion(): boolean {
    return typeof window !== 'undefined' && (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);
  }

  function updateChart() {
    const config = neutralChartConfig(block, activeRange, hiddenSeries, resolveChartTheme(wrapperEl), prefersReducedMotion());
    if (!config || !chart) return;
    chart.data.labels = config.data.labels;
    chart.data.datasets = config.data.datasets;
    chart.options = config.options ?? {};
    normalized.series.forEach((series, index) => {
      chart?.setDatasetVisibility?.(index, !hiddenSeries.has(series.id));
    });
    chart.update();
  }

  $: if (chart) updateChart();
  $: if (!allowPinning && pinnedPoint) pinnedPoint = null;

  onMount(async () => {
    const config = neutralChartConfig(block, activeRange, hiddenSeries, resolveChartTheme(wrapperEl), prefersReducedMotion());
    if (config && canvas && canRenderCanvas(canvas)) {
      const { default: Chart } = await import('chart.js/auto');
      if (destroyed) return;
      chart = new Chart(canvas, config);
    }
  });

  onDestroy(() => {
    destroyed = true;
    chart?.destroy();
  });

  function toggleSeries(id: string) {
    const next = new Set(hiddenSeries);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    hiddenSeries = next;
  }

  function pointFromEvent(event: MouseEvent, mode: 'hover' | 'pin'): PinnedChartPoint | null {
    if (!canvas || normalized.labels.length === 0) return null;
    const elements = chart?.getElementsAtEventForMode(event, 'index', { axis: isProgress ? 'y' : 'x', intersect: false }, false) ?? [];
    const index = elements[0]?.index ?? fallbackIndexFromMouse(event);
    const label = String(normalized.labels[index] ?? '');
    if (!label) return null;
    const summary = chartPointSummary(
      {
        labels: normalized.labels,
        series: normalized.series.filter((series) => !hiddenSeries.has(series.id)),
      },
      label,
    );
    if (mode === 'hover') hoverPoint = summary;
    return summary;
  }

  function fallbackIndexFromMouse(event: MouseEvent): number {
    const rect = canvas.getBoundingClientRect();
    const size = isProgress ? rect.height : rect.width;
    const coordinate = isProgress ? event.clientY - rect.top : event.clientX - rect.left;
    const ratio = size > 0 ? Math.min(1, Math.max(0, coordinate / size)) : 0;
    return Math.min(normalized.labels.length - 1, Math.max(0, Math.round(ratio * (normalized.labels.length - 1))));
  }

  function handleMousemove(event: MouseEvent) {
    pointFromEvent(event, 'hover');
  }

  function handleClick(event: MouseEvent) {
    if (!allowPinning) return;
    pinnedPoint = pointFromEvent(event, 'pin');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (!allowPinning) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    const label = normalized.labels[normalized.labels.length - 1];
    if (label) pinnedPoint = chartPointSummary({ labels: normalized.labels, series: normalized.series.filter((series) => !hiddenSeries.has(series.id)) }, label);
  }

  function canRenderCanvas(target: HTMLCanvasElement): boolean {
    if (typeof navigator !== 'undefined' && navigator.userAgent.toLowerCase().includes('jsdom')) return false;
    try {
      return Boolean(target.getContext('2d'));
    } catch {
      return false;
    }
  }

  function staticPoints(values: unknown[]): string {
    const numbers = values.map(Number).filter(Number.isFinite);
    if (numbers.length === 0) return '';
    const min = Math.min(...numbers);
    const span = Math.max(...numbers) - min || 1;
    return numbers.map((value, index) => {
      const x = numbers.length === 1 ? 50 : 4 + (index / (numbers.length - 1)) * 92;
      const y = 34 - ((value - min) / span) * 28;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }
</script>

<div class="rich-chart-controls" aria-label="Chart controls">
  {#if ranges.length > 1}
    <div class="rich-chart-ranges" role="group" aria-label="Chart range">
      {#each ranges as range}
        <button type="button" class:active={range.id === activeRange} aria-pressed={range.id === activeRange} on:click={() => activeRange = range.id}>
          {range.label}
        </button>
      {/each}
    </div>
  {/if}
</div>

<div class="rich-chart-layout legend-{normalized.legendPosition}">
  {#if showLegend}
    <div class="rich-chart-legend" role="group" aria-label="Chart series">
      {#if normalized.chartType === 'donut'}
        {#each normalized.labels as label, index}
          <span class="rich-chart-legend-item" style:--legend-color={legendColors[index % legendColors.length]} data-chart-category={label}>{label}</span>
        {/each}
      {:else}
        {#each normalized.series as series}
          <button type="button" class:inactive={hiddenSeries.has(series.id)} aria-pressed={!hiddenSeries.has(series.id)} on:click={() => toggleSeries(series.id)}>
            {series.label}
          </button>
        {/each}
      {/if}
    </div>
  {/if}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -- role and tabindex are
       always set in lockstep below (button+0 when pinning is allowed,
       img+-1 otherwise), so the noninteractive+tabindex combination the
       linter flags can never actually occur at runtime. -->
  <div
    bind:this={wrapperEl}
    class:chart-ready={Boolean(chart)}
    class="rich-chart-canvas"
    role={allowPinning ? 'button' : 'img'}
    tabindex={allowPinning ? 0 : -1}
    aria-label={allowPinning ? 'Interactive chart area' : 'Chart area'}
    on:mousemove={handleMousemove}
    on:mouseleave={() => hoverPoint = null}
    on:click={handleClick}
    on:keydown={handleKeydown}
  >
    {#if showBaseline && baselinePoints}
      <svg class="rich-chart-baseline" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true" data-testid="rich-chart-baseline">
        <path d="M4 35.5H96M4 21H96M4 6.5H96" />
        <polyline points={baselinePoints} />
      </svg>
    {/if}
    <canvas bind:this={canvas} aria-label="Interactive chart"></canvas>
  </div>
</div>

{#if fallbackRows.length > 0}
  <details class="rich-chart-fallback">
    <summary>Chart data table</summary>
    <div>
      <table>
        <thead><tr><th>{normalized.xAxis.label || 'Category'}</th>{#each normalized.series as series}<th>{series.label}</th>{/each}</tr></thead>
        <tbody>
          {#each fallbackRows as row}
            <tr><th>{row.label}</th>{#each row.values as item}<td>{item.value === null ? '—' : formatChartValue(item.value)}</td>{/each}</tr>
          {/each}
        </tbody>
      </table>
    </div>
  </details>
{/if}

{#if hoverPoint}
  <div class="rich-chart-tooltip" data-testid="rich-chart-tooltip" aria-live="polite">
    <strong>{hoverPoint.label}</strong>
    {#each hoverPoint.values as item}
      <span>{item.series}: {formatChartValue(item.value)}</span>
    {/each}
  </div>
{/if}

{#if pinnedPoint}
  <div class="rich-chart-pinned" data-testid="rich-chart-pinned">
    <span>Pinned datapoint</span>
    <strong>{pinnedPoint.label}</strong>
    {#each pinnedPoint.values as item}
      <em>{item.series}: {formatChartValue(item.value)}</em>
    {/each}
  </div>
{/if}

<style>
  .rich-chart-controls {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.75rem;
    margin: 1rem 0 0.75rem;
  }

  .rich-chart-ranges,
  .rich-chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .rich-chart-controls button,
  .rich-chart-legend button,
  .rich-chart-legend-item {
    border: 1px solid var(--rich-line);
    border-radius: 999px;
    background: var(--rich-surface-raised);
    color: var(--rich-text-secondary);
    padding: 0.38rem 0.62rem;
    font-size: 0.74rem;
    font-weight: 800;
  }

  .rich-chart-controls button.active,
  .rich-chart-controls button:hover,
  .rich-chart-legend button:hover {
    border-color: color-mix(in srgb, var(--rich-accent) 42%, transparent);
    background: color-mix(in srgb, var(--rich-accent) 18%, transparent);
    color: var(--rich-text);
  }

  .rich-chart-legend button.inactive {
    opacity: 0.48;
    text-decoration: line-through;
  }

  .rich-chart-legend-item {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  .rich-chart-legend-item::before {
    width: 0.65rem;
    height: 0.65rem;
    border-radius: 999px;
    background: var(--legend-color);
    content: '';
  }

  .rich-chart-layout {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .rich-chart-layout.legend-top .rich-chart-legend { order: 0; }
  .rich-chart-layout.legend-top .rich-chart-canvas { order: 1; }
  .rich-chart-layout.legend-bottom .rich-chart-canvas { order: 0; }
  .rich-chart-layout.legend-bottom .rich-chart-legend { order: 1; }

  .rich-chart-layout.legend-right {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
  }

  /* Explicit grid-row is required alongside grid-column: without it, both
     items get an auto row position and the legend (first in DOM order)
     claims row 1 entirely for itself before the canvas is placed, pushing
     the canvas onto row 2 instead of sharing row 1 side-by-side. That
     silently doubled the card height for legend-right (donut) charts. */
  .rich-chart-layout.legend-right .rich-chart-canvas {
    grid-column: 1;
    grid-row: 1;
  }
  .rich-chart-layout.legend-right .rich-chart-legend {
    grid-column: 2;
    grid-row: 1;
    flex-direction: column;
  }

  .rich-chart-canvas {
    position: relative;
    min-height: 16rem;
    height: 18rem;
    border: 1px solid var(--rich-line);
    border-radius: 1rem;
    background:
      linear-gradient(180deg, color-mix(in srgb, var(--rich-surface-wash) 82%, transparent), color-mix(in srgb, var(--rich-surface-wash-deep) 50%, transparent)),
      radial-gradient(circle at 50% 0%, color-mix(in srgb, var(--rich-accent) 8%, transparent), transparent 40%);
    padding: 1rem;
  }

  .rich-chart-canvas canvas {
    display: block;
    position: relative;
    z-index: 1;
    width: 100% !important;
    height: 100% !important;
    max-height: none;
  }

  .rich-chart-baseline {
    position: absolute;
    inset: 1rem;
    width: calc(100% - 2rem);
    height: calc(100% - 2rem);
    color: color-mix(in srgb, var(--rich-accent) 72%, transparent);
    transition: opacity .18s ease;
  }

  .rich-chart-baseline path { stroke: var(--rich-line); stroke-width: .25; }
  .rich-chart-baseline polyline { fill: none; stroke: currentColor; stroke-width: 1.15; stroke-linejoin: round; stroke-linecap: round; }
  .chart-ready .rich-chart-baseline { opacity: .12; }

  @media (prefers-reduced-motion: reduce) {
    .rich-chart-baseline { transition: none; }
  }

  .rich-chart-tooltip,
  .rich-chart-pinned {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.55rem;
    margin-top: 0.75rem;
    border: 1px solid color-mix(in srgb, var(--rich-accent) 18%, transparent);
    border-radius: 0.9rem;
    background: var(--rich-surface-raised);
    color: var(--rich-text-secondary);
    padding: 0.65rem 0.75rem;
    font-size: 0.82rem;
  }

  .rich-chart-tooltip strong,
  .rich-chart-pinned strong {
    color: var(--rich-text);
  }

  .rich-chart-pinned span {
    color: var(--rich-accent-soft);
    font-size: 0.68rem;
    font-weight: 850;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .rich-chart-pinned em {
    font-style: normal;
  }

  .rich-chart-fallback {
    margin-top: 0.7rem;
    color: var(--rich-muted);
    font-size: 0.78rem;
  }

  .rich-chart-fallback summary {
    cursor: pointer;
    font-weight: 750;
  }

  .rich-chart-fallback > div {
    max-width: 100%;
    overflow-x: auto;
  }

  .rich-chart-fallback table {
    width: 100%;
    margin-top: 0.55rem;
    border-collapse: collapse;
  }

  .rich-chart-fallback th,
  .rich-chart-fallback td {
    border-bottom: 1px solid var(--rich-line);
    padding: 0.4rem 0.5rem;
    text-align: left;
  }
</style>
