import type { ChartConfiguration, ChartType as ChartJsType } from 'chart.js';

export const CHART_SPEC_VERSION = 'cognis.chart.v1' as const;
export const CANONICAL_CHART_TYPES = [
  'line',
  'area',
  'bar',
  'grouped_bar',
  'stacked_bar',
  'sparkline',
  'progress',
  'range',
  'donut',
] as const;
export const CHART_AXIS_TYPES = ['time', 'category', 'linear'] as const;
export const CHART_LEGEND_POSITIONS = ['top', 'right', 'bottom', 'none'] as const;
export const CHART_PALETTE_TOKENS = ['default', 'cool', 'warm', 'categorical'] as const;

export type ChartType = (typeof CANONICAL_CHART_TYPES)[number];
export type ChartAxisType = (typeof CHART_AXIS_TYPES)[number];
export type ChartLegendPosition = (typeof CHART_LEGEND_POSITIONS)[number];
export type ChartPaletteToken = (typeof CHART_PALETTE_TOKENS)[number];
export type ChartYValue = number | readonly [number, number];
export type RichRangeId = '7d' | '30d' | 'all' | string;

export interface NormalizedChartPoint {
  x: string | number;
  y: ChartYValue;
  label: string | null;
}

export interface NormalizedChartSeries {
  id: string;
  label: string;
  points: NormalizedChartPoint[];
  stack: string | null;
}

export interface NormalizedChartAxis {
  type: ChartAxisType;
  label: string | null;
  unit: string | null;
  min: number | null;
  max: number | null;
}

export interface NormalizedChartData {
  specVersion: typeof CHART_SPEC_VERSION;
  chartType: ChartType;
  labels: string[];
  series: NormalizedChartSeries[];
  xAxis: NormalizedChartAxis;
  yAxis: NormalizedChartAxis;
  stack: boolean;
  legendPosition: ChartLegendPosition;
  paletteToken: ChartPaletteToken;
  sourceIds: string[];
  source: string | null;
  sourceUrl: string | null;
  observedAt: string | null;
  description: string;
}

export interface RangeOption {
  id: RichRangeId;
  label: string;
}

export interface PinnedChartPoint {
  label: string;
  values: { series: string; value: ChartYValue }[];
}

const palettes: Record<ChartPaletteToken, readonly string[]> = {
  default: ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb7185', '#2dd4bf', '#c084fc'],
  cool: ['#38bdf8', '#2dd4bf', '#818cf8', '#22d3ee', '#60a5fa', '#a78bfa'],
  warm: ['#fb7185', '#fb923c', '#fbbf24', '#f472b6', '#f97316', '#facc15'],
  categorical: ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb7185', '#2dd4bf', '#c084fc'],
};

export function chartPaletteColors(token: ChartPaletteToken): readonly string[] {
  return palettes[token];
}

function blockType(block: Record<string, unknown>): string {
  return String(block.type ?? 'unknown');
}

function recordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          Boolean(item) && typeof item === 'object' && !Array.isArray(item),
      )
    : [];
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function toTimestamp(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value)) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function dateLabel(timestamp: number): string {
  return new Date(timestamp).toISOString().slice(0, 10);
}

function text(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  return normalized || null;
}

function memberOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === 'string' && values.includes(value as T);
}

function emptyChartData(): NormalizedChartData {
  return {
    specVersion: CHART_SPEC_VERSION,
    chartType: 'line',
    labels: [],
    series: [],
    xAxis: { type: 'category', label: null, unit: null, min: null, max: null },
    yAxis: { type: 'linear', label: null, unit: null, min: null, max: null },
    stack: false,
    legendPosition: 'bottom',
    paletteToken: 'default',
    sourceIds: [],
    source: null,
    sourceUrl: null,
    observedAt: null,
    description: 'Chart',
  };
}

function normalizeAxis(value: unknown, fallback: ChartAxisType): NormalizedChartAxis | null {
  if (value === undefined || value === null) {
    return { type: fallback, label: null, unit: null, min: null, max: null };
  }
  if (typeof value !== 'object' || Array.isArray(value)) return null;
  const axis = value as Record<string, unknown>;
  const type = axis.type ?? fallback;
  if (!memberOf(type, CHART_AXIS_TYPES)) return null;
  return {
    type,
    label: text(axis.label),
    unit: text(axis.unit),
    min: toNumber(axis.min),
    max: toNumber(axis.max),
  };
}

function normalizeX(value: unknown, axisType: ChartAxisType): string | number | null {
  if (axisType === 'linear') {
    return toNumber(value);
  }
  if (axisType === 'category' && typeof value === 'number') {
    return Number.isFinite(value) ? numberXText(value) : null;
  }
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const normalized = String(value).trim();
  if (!normalized) return null;
  if (axisType === 'time' && toTimestamp(normalized) === null) return null;
  return normalized;
}

function numberXText(value: number): string {
  if (Number.isInteger(value)) return String(value);
  const [mantissa, exponent] = value.toExponential(15).split('e');
  return `${mantissa.replace(/0+$/, '').replace(/\.$/, '')}e${Number(exponent)}`;
}

export function chartXLabel(value: string | number): string {
  return typeof value === 'number' ? numberXText(value) : value;
}

function normalizeY(value: unknown, chartType: ChartType): ChartYValue | null {
  if (chartType === 'range') {
    if (!Array.isArray(value) || value.length !== 2) return null;
    const first = toNumber(value[0]);
    const second = toNumber(value[1]);
    if (first === null || second === null) return null;
    return first <= second ? [first, second] : [second, first];
  }
  return toNumber(value);
}

function yValue(value: ChartYValue): number {
  return typeof value === 'number' ? value : (value[0] + value[1]) / 2;
}

function pointTimestamp(point: NormalizedChartPoint): number | null {
  return typeof point.x === 'string' ? toTimestamp(point.x) : null;
}

function orderPoints(points: NormalizedChartPoint[], axisType: ChartAxisType): NormalizedChartPoint[] {
  const deduplicated = Array.from(new Map(points.map((point) => [point.x, point])).values());
  if (axisType === 'category') return deduplicated;
  return deduplicated.sort((a, b) => {
    if (axisType === 'linear') return Number(a.x) - Number(b.x);
    return (pointTimestamp(a) ?? 0) - (pointTimestamp(b) ?? 0);
  });
}

function orderedLabels(series: NormalizedChartSeries[], axisType: ChartAxisType): string[] {
  const values = Array.from(new Set(series.flatMap((item) => item.points.map((point) => point.x))));
  if (axisType !== 'category') {
    values.sort((a, b) =>
      axisType === 'linear'
        ? Number(a) - Number(b)
        : (toTimestamp(String(a)) ?? 0) - (toTimestamp(String(b)) ?? 0),
    );
  }
  return values.map(chartXLabel);
}

/**
 * Resolve just the x-axis type from a raw block, without full validation.
 * Used to gate chart chrome (range picker, trend baseline, point pinning)
 * that only makes sense for a real time series — never for categorical or
 * linear-numeric x-axes.
 */
function resolveXAxisType(block: Record<string, unknown>): ChartAxisType {
  const value = block.x_axis;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const type = (value as Record<string, unknown>).type;
    if (memberOf(type, CHART_AXIS_TYPES)) return type;
  }
  return 'category';
}

export function isTimeSeriesChart(block: Record<string, unknown>): boolean {
  return resolveXAxisType(block) === 'time';
}

export function chartRangeOptions(block: Record<string, unknown>): RangeOption[] {
  if (!isTimeSeriesChart(block)) return [];
  const configured = block.range_selector ?? block.ranges;
  if (Array.isArray(configured) && configured.length > 0) {
    return configured
      .map((item): RangeOption | null => {
        if (typeof item === 'string') return { id: item, label: item.toUpperCase() };
        if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
        const record = item as Record<string, unknown>;
        const id = String(record.id ?? record.value ?? '').trim();
        if (!id) return null;
        return { id, label: String(record.label ?? id.toUpperCase()) };
      })
      .filter((item): item is RangeOption => Boolean(item));
  }
  return [
    { id: '7d', label: '7D' },
    { id: '30d', label: '30D' },
    { id: 'all', label: 'All' },
  ];
}

export function normalizeChartData(
  block: Record<string, unknown>,
  activeRange: RichRangeId = 'all',
): NormalizedChartData {
  if (
    blockType(block) !== 'chart' ||
    block.spec_version !== CHART_SPEC_VERSION ||
    !memberOf(block.chart_type, CANONICAL_CHART_TYPES)
  ) {
    return emptyChartData();
  }
  const xAxis = normalizeAxis(block.x_axis, 'category');
  const yAxis = normalizeAxis(block.y_axis, 'linear');
  const legendPosition = block.legend_position ?? 'bottom';
  const paletteToken = block.palette_token ?? 'default';
  if (
    !xAxis ||
    !yAxis ||
    !memberOf(legendPosition, CHART_LEGEND_POSITIONS) ||
    !memberOf(paletteToken, CHART_PALETTE_TOKENS)
  ) {
    return emptyChartData();
  }

  const chartType = block.chart_type;
  let series = recordList(block.series)
    .map((item, seriesIndex): NormalizedChartSeries | null => {
      const id = text(item.id) ?? `series-${seriesIndex + 1}`;
      const points = recordList(item.points)
        .map((point): NormalizedChartPoint | null => {
          const x = normalizeX(point.x, xAxis.type);
          const y = normalizeY(point.y, chartType);
          if (x === null || y === null) return null;
          return { x, y, label: text(point.label) };
        })
        .filter((point): point is NormalizedChartPoint => Boolean(point));
      if (points.length === 0) return null;
      return {
        id,
        label: text(item.label) ?? id,
        points: orderPoints(points, xAxis.type),
        stack: text(item.stack),
      };
    })
    .filter((item): item is NormalizedChartSeries => Boolean(item));

  if (xAxis.type === 'time') series = filterSeriesByRange(series, activeRange);
  return {
    specVersion: CHART_SPEC_VERSION,
    chartType,
    labels: orderedLabels(series, xAxis.type),
    series,
    xAxis,
    yAxis,
    stack: block.stack === true,
    legendPosition,
    paletteToken,
    sourceIds: Array.isArray(block.source_ids)
      ? block.source_ids.map(text).filter((item): item is string => item !== null)
      : [],
    source: text(block.source),
    sourceUrl: text(block.source_url),
    observedAt: text(block.observed_at),
    description: text(block.description) ?? 'Chart',
  };
}

export function filterSeriesByRange(
  series: NormalizedChartSeries[],
  range: RichRangeId,
): NormalizedChartSeries[] {
  if (range === 'all') return series;
  const match = /^(\d+)d$/.exec(String(range));
  if (!match) return series;
  const days = Number(match[1]);
  const timestamps = series.flatMap((item) =>
    item.points.map(pointTimestamp).filter((value): value is number => value !== null),
  );
  if (!timestamps.length) return series;
  const max = Math.max(...timestamps);
  const min = max - (days - 1) * 24 * 60 * 60 * 1000;
  return series.map((item) => ({
    ...item,
    points: item.points.filter((point) => {
      const timestamp = pointTimestamp(point);
      return timestamp === null || timestamp >= min;
    }),
  }));
}

export function fillEmptyTimeBuckets(series: NormalizedChartSeries[]): NormalizedChartSeries[] {
  const timestamps = series.flatMap((item) =>
    item.points.map(pointTimestamp).filter((value): value is number => value !== null),
  );
  if (!timestamps.length) return series;
  const start = Math.min(...timestamps);
  const end = Math.max(...timestamps);
  const dayMs = 24 * 60 * 60 * 1000;
  const labels: string[] = [];
  for (let current = start; current <= end; current += dayMs) labels.push(dateLabel(current));

  return series.map((item) => {
    const pointsByLabel = new Map(item.points.map((point) => [point.x, point]));
    return {
      ...item,
      points: labels.map(
        (label) => pointsByLabel.get(label) ?? { x: label, y: 0, label: null },
      ),
    };
  });
}

export function chartPointSummary(
  data: Pick<NormalizedChartData, 'labels' | 'series'>,
  label: string,
): PinnedChartPoint {
  return {
    label,
    values: data.series
      .map((series) => {
        const point = series.points.find((candidate) => chartXLabel(candidate.x) === label);
        return point ? { series: series.label, value: point.y } : null;
      })
      .filter((item): item is { series: string; value: ChartYValue } => item !== null),
  };
}

export function formatChartValue(value: ChartYValue): string {
  return Array.isArray(value) ? `${value[0]}–${value[1]}` : String(value);
}

type RenderedChartType = 'line' | 'bar' | 'doughnut';

function renderedChartType(chartType: ChartType): RenderedChartType {
  if (chartType === 'donut') return 'doughnut';
  return ['line', 'area', 'sparkline'].includes(chartType) ? 'line' : 'bar';
}

function colorWithAlpha(color: string, alpha: number): string {
  const hex = color.slice(1);
  const red = Number.parseInt(hex.slice(0, 2), 16);
  const green = Number.parseInt(hex.slice(2, 4), 16);
  const blue = Number.parseInt(hex.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

interface CanvasGradientContext {
  chart: {
    ctx: CanvasRenderingContext2D;
    chartArea?: { top: number; bottom: number };
  };
}

function canvasGradient(
  context: CanvasGradientContext,
  color: string,
  topAlpha: number,
  bottomAlpha = 0.03,
): CanvasGradient | string {
  const { chart } = context;
  if (!chart.chartArea) return colorWithAlpha(color, topAlpha);
  const gradient = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
  gradient.addColorStop(0, colorWithAlpha(color, topAlpha));
  gradient.addColorStop(1, colorWithAlpha(color, bottomAlpha));
  return gradient;
}

function axisTitle(axis: NormalizedChartAxis): string | undefined {
  const parts = [axis.label, axis.unit ? `(${axis.unit})` : null].filter(
    (value): value is string => value !== null,
  );
  return parts.length > 0 ? parts.join(' ') : undefined;
}

function tickLabel(value: string | number, unit: string | null): string {
  return unit ? `${value} ${unit}` : String(value);
}

/**
 * Theme-resolved colors for chart chrome (gridlines/ticks/titles). Charts are
 * rendered on a <canvas> and cannot read CSS custom properties on their own,
 * so the host component (RichChart.svelte) resolves the live `--rich-*`
 * design tokens for the current theme and passes concrete color strings in.
 * Defaults below match the dark-theme token values for callers (and tests)
 * that don't resolve a live theme.
 */
export interface ChartThemeColors {
  /** Gridline stroke color, should already include the desired opacity. */
  gridColor: string;
  /** Axis tick label color. */
  tickColor: string;
  /** Axis title / donut-center / high-emphasis text color. */
  textColor: string;
}

export const DEFAULT_CHART_THEME: ChartThemeColors = {
  gridColor: 'rgba(148, 163, 184, 0.28)',
  tickColor: 'rgb(203, 213, 225)',
  textColor: 'rgb(248, 250, 252)',
};

function axisTicks(
  axis: NormalizedChartAxis,
  category: boolean,
  theme: ChartThemeColors,
): {
  color: string;
  callback: (this: { getLabelForValue(value: number): string }, value: string | number) => string;
} {
  return {
    color: theme.tickColor,
    callback: function (this: { getLabelForValue(value: number): string }, value: string | number): string {
      return tickLabel(category ? this.getLabelForValue(Number(value)) : value, axis.unit);
    },
  };
}

export function neutralChartConfig(
  block: Record<string, unknown>,
  activeRange: RichRangeId = 'all',
  hiddenSeries: Set<string> = new Set(),
  theme: ChartThemeColors = DEFAULT_CHART_THEME,
  reducedMotion = false,
): ChartConfiguration<ChartJsType> | null {
  if (blockType(block) !== 'chart') return null;
  const normalized = normalizeChartData(block, activeRange);
  if (normalized.series.length === 0) return null;
  const chartType = renderedChartType(normalized.chartType);
  const isArea = normalized.chartType === 'area';
  const isSparkline = normalized.chartType === 'sparkline';
  const isDonut = normalized.chartType === 'donut';
  const isRange = normalized.chartType === 'range';
  const isProgress = normalized.chartType === 'progress';
  const stacked = normalized.chartType === 'stacked_bar' || normalized.stack;
  const colors = palettes[normalized.paletteToken];
  const xAxis = isProgress ? normalized.yAxis : normalized.xAxis;
  const yAxis = isProgress ? normalized.xAxis : normalized.yAxis;

  return {
    type: chartType,
    data: {
      labels: normalized.labels,
      datasets: normalized.series.map((series, index) => {
        const color = colors[index % colors.length];
        const valuesByLabel = new Map<string, number | [number, number]>(
          series.points.map((point) => {
            const value = isRange && Array.isArray(point.y)
              ? [point.y[0], point.y[1]] as [number, number]
              : yValue(point.y);
            return [chartXLabel(point.x), value];
          }),
        );
        return {
          cognisSeriesId: series.id,
          label: series.label,
          data: normalized.labels.map((label) => valuesByLabel.get(label) ?? null),
          borderColor: color,
          backgroundColor: isDonut
            ? normalized.labels.map((_, pointIndex) => colors[pointIndex % colors.length])
            : (context: CanvasGradientContext) =>
                canvasGradient(context, color, chartType === 'bar' ? 0.58 : 0.32),
          borderWidth: 2,
          tension: 0.28,
          fill: isArea,
          pointRadius: isSparkline ? 0 : 3,
          pointHoverRadius: isSparkline ? 0 : 5,
          stack: series.stack ?? (stacked ? 'default' : undefined),
          hidden: hiddenSeries.has(series.id),
          // Cap bar thickness so a handful of categories don't stretch into
          // oversized blocks with awkward dead space either side.
          ...(chartType === 'bar' ? { maxBarThickness: 96, categoryPercentage: 0.6, barPercentage: 0.86 } : {}),
        };
      }),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // Doughnut arcs otherwise meet the canvas edge exactly in constrained
      // cards, where antialiasing can visibly crop the lower curve.
      layout: { padding: isDonut ? 14 : 0 },
      indexAxis: isProgress ? 'y' : 'x',
      animation: { duration: reducedMotion || isSparkline ? 0 : 240, easing: 'easeOutQuart' },
      interaction: { mode: 'index', axis: isProgress ? 'y' : 'x', intersect: false },
      plugins: {
        legend: {
          display: false,
          position: normalized.legendPosition === 'none' ? 'top' : normalized.legendPosition,
        },
        tooltip: {
          enabled: !isSparkline,
          mode: 'index',
          intersect: false,
          callbacks: {
            label: (context) => {
              if (isRange && Array.isArray(context.raw)) {
                return `${context.dataset.label ?? ''}: ${formatChartValue(context.raw as [number, number])}${normalized.yAxis.unit ? ` ${normalized.yAxis.unit}` : ''}`;
              }
              const value = isDonut
                ? Number(context.parsed)
                : isProgress
                  ? context.parsed.x
                  : context.parsed.y;
              return `${context.dataset.label ?? ''}: ${tickLabel(value ?? 0, normalized.yAxis.unit)}`;
            },
          },
        },
      },
      scales: isDonut || isSparkline
        ? {}
        : {
            x: {
              type: xAxis.type === 'time' ? 'category' : xAxis.type,
              beginAtZero: isProgress || xAxis.min === null,
              min: xAxis.min ?? undefined,
              max: xAxis.max ?? undefined,
              stacked,
              title: {
                display: Boolean(axisTitle(xAxis)),
                text: axisTitle(xAxis),
                color: theme.textColor,
              },
              grid: { color: theme.gridColor },
              ticks: axisTicks(xAxis, xAxis.type !== 'linear', theme),
            },
            y: {
              type: yAxis.type === 'time' ? 'category' : yAxis.type,
              beginAtZero: !isProgress && yAxis.min === null,
              min: yAxis.min ?? undefined,
              max: yAxis.max ?? undefined,
              stacked,
              title: {
                display: Boolean(axisTitle(yAxis)),
                text: axisTitle(yAxis),
                color: theme.textColor,
              },
              grid: { color: theme.gridColor },
              ticks: axisTicks(yAxis, yAxis.type !== 'linear', theme),
            },
          },
    },
  };
}
