import { describe, expect, it } from 'vitest';

import {
  CANONICAL_CHART_TYPES,
  CHART_SPEC_VERSION,
  chartPointSummary,
  chartRangeOptions,
  fillEmptyTimeBuckets,
  neutralChartConfig,
  normalizeChartData,
} from './rich-data';

function chart(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'chart',
    spec_version: CHART_SPEC_VERSION,
    chart_type: 'line',
    series: [
      {
        id: 'requests',
        label: 'Requests',
        stack: 'traffic',
        points: [
          { x: '2026-07-09', y: 10, label: 'Thursday' },
          { x: '2026-07-10', y: 12 },
        ],
      },
    ],
    x_axis: { type: 'time', label: 'Date', unit: 'UTC', min: null, max: null },
    y_axis: { type: 'linear', label: 'Requests', unit: 'req/s', min: 0, max: 100 },
    stack: false,
    legend_position: 'bottom',
    palette_token: 'cool',
    source_ids: ['source-1'],
    source: 'Metrics',
    source_url: 'https://metrics.example.test',
    observed_at: '2026-07-10T12:00:00Z',
    description: 'Request volume.',
    ...overrides,
  };
}

describe('canonical rich chart data', () => {
  it.each(CANONICAL_CHART_TYPES)('parses the %s chart type', (chartType) => {
    const series =
      chartType === 'range'
        ? [{ id: 'band', label: 'Band', points: [{ x: 'A', y: [2, 7], label: 'Expected' }] }]
        : [{ id: 'value', label: 'Value', points: [{ x: 'A', y: 4, label: 'Current' }] }];
    const normalized = normalizeChartData(
      chart({
        chart_type: chartType,
        x_axis: { type: 'category' },
        series,
      }),
    );

    expect(normalized.chartType).toBe(chartType);
    expect(normalized.series).toHaveLength(1);
    expect(normalized.series[0].points[0].label).toBe(chartType === 'range' ? 'Expected' : 'Current');
  });

  it.each([
    ['line', 'line'],
    ['area', 'line'],
    ['bar', 'bar'],
    ['grouped_bar', 'bar'],
    ['stacked_bar', 'bar'],
    ['sparkline', 'line'],
    ['progress', 'bar'],
    ['range', 'bar'],
    ['donut', 'doughnut'],
  ] as const)('builds a Chart.js configuration for %s', (canonicalType, renderedType) => {
    const config = neutralChartConfig(
      chart({
        chart_type: canonicalType,
        x_axis: { type: 'category', label: 'Period', unit: 'day', min: 0, max: 10 },
        y_axis: { type: 'linear', label: 'Value', unit: 'ms', min: 1, max: 99 },
        legend_position: 'right',
        palette_token: 'warm',
        stack: canonicalType === 'stacked_bar',
        series: [{
          id: 'first',
          label: 'First',
          stack: 'shared',
          points: [{ x: 'A', y: canonicalType === 'range' ? [2, 7] : 4 }],
        }],
      }),
    );

    expect(config?.type).toBe(renderedType);
    expect(config?.options?.plugins?.legend?.position).toBe('right');
    expect(config?.options?.animation).toMatchObject({ duration: canonicalType === 'sparkline' ? 0 : 240 });
    expect(config?.data.datasets[0].borderColor).toBe('#fb7185');
    expect(typeof config?.data.datasets[0].backgroundColor).toBe(
      canonicalType === 'donut' ? 'object' : 'function',
    );
    const dataset = config?.data.datasets[0] as Record<string, unknown> | undefined;
    if (canonicalType === 'area') expect(dataset?.fill).toBe(true);
    if (canonicalType === 'sparkline') expect(dataset?.pointRadius).toBe(0);
    if (canonicalType === 'progress') expect(config?.options?.indexAxis).toBe('y');
    if (canonicalType === 'range') expect(config?.data.datasets[0].data).toEqual([[2, 7]]);
    if (canonicalType === 'stacked_bar') {
      expect(config?.data.datasets[0].stack).toBe('shared');
      const scales = config?.options?.scales as Record<string, Record<string, unknown>> | undefined;
      expect(scales?.x.stacked).toBe(true);
      expect(scales?.y.stacked).toBe(true);
    }
  });

  it('applies axis bounds, titles, units, and range filtering without resetting legend state', () => {
    const source = chart({
      legend_position: 'none',
      range_selector: ['7d', 'all'],
      series: [
        {
          id: 'requests',
          label: 'Requests',
          points: [
            { x: '2026-07-01', y: 1 },
            { x: '2026-07-15', y: 2 },
          ],
        },
        {
          id: 'errors',
          label: 'Errors',
          points: [
            { x: '2026-07-01', y: 3 },
            { x: '2026-07-15', y: 4 },
          ],
        },
      ],
    });
    const config = neutralChartConfig(source, '7d', new Set(['errors']));

    expect(chartRangeOptions(source)).toEqual([{ id: '7d', label: '7D' }, { id: 'all', label: 'ALL' }]);
    expect(config?.data.labels).toEqual(['2026-07-15']);
    expect(config?.data.datasets.map((dataset) => dataset.hidden)).toEqual([false, true]);
    expect(config?.options?.plugins?.legend).toMatchObject({ display: false, position: 'top' });
    expect(config?.options?.scales?.x).toMatchObject({
      min: undefined,
      max: undefined,
      title: { display: true, text: 'Date (UTC)' },
    });
    expect(config?.options?.scales?.y).toMatchObject({
      min: 0,
      max: 100,
      title: { display: true, text: 'Requests (req/s)' },
    });
  });

  it('orders temporal multi-series points and labels chronologically', () => {
    const normalized = normalizeChartData(
      chart({
        series: [
          {
            id: 'requests',
            label: 'Requests',
            stack: 'traffic',
            points: [
              { x: '2026-07-10T09:00:00Z', y: 20 },
              { x: '2026-07-08T09:00:00Z', y: 10 },
            ],
          },
          {
            id: 'errors',
            label: 'Errors',
            points: [
              { x: '2026-07-09T09:00:00Z', y: 2 },
              { x: '2026-07-08T09:00:00Z', y: 1 },
            ],
          },
        ],
      }),
    );

    expect(normalized.labels).toEqual([
      '2026-07-08T09:00:00Z',
      '2026-07-09T09:00:00Z',
      '2026-07-10T09:00:00Z',
    ]);
    expect(normalized.series[0].points.map((point) => point.x)).toEqual([
      '2026-07-08T09:00:00Z',
      '2026-07-10T09:00:00Z',
    ]);
    expect(normalized.sourceIds).toEqual(['source-1']);
    expect(normalized.specVersion).toBe(CHART_SPEC_VERSION);
    expect(normalized.xAxis).toEqual({
      type: 'time',
      label: 'Date',
      unit: 'UTC',
      min: null,
      max: null,
    });
    expect(normalized.yAxis).toEqual({
      type: 'linear',
      label: 'Requests',
      unit: 'req/s',
      min: 0,
      max: 100,
    });
    expect(normalized.series[0].stack).toBe('traffic');
  });

  it('normalizes range bounds, summarizes their midpoint, and preserves bounds for Chart.js', () => {
    const normalized = normalizeChartData(
      chart({
        chart_type: 'range',
        x_axis: { type: 'category' },
        series: [{ id: 'latency', label: 'Latency', points: [{ x: 'p95', y: [20, 10] }] }],
      }),
    );

    expect(normalized.series[0].points[0].y).toEqual([10, 20]);
    expect(chartPointSummary(normalized, 'p95')).toEqual({
      label: 'p95',
      values: [{ series: 'Latency', value: [10, 20] }],
    });
    expect(neutralChartConfig(chart({
      chart_type: 'range',
      x_axis: { type: 'category' },
      series: [{ id: 'latency', label: 'Latency', points: [{ x: 'p95', y: [10, 20] }] }],
    }))?.data.datasets[0].data).toEqual([[10, 20]]);
  });

  it('resolves category tick labels and maps progress values to the horizontal scale', () => {
    const lineConfig = neutralChartConfig(chart());
    const lineX = lineConfig?.options?.scales?.x as unknown as {
      ticks: { callback: (this: { getLabelForValue(value: number): string }, value: number) => string };
    };
    expect(lineX.ticks.callback.call({ getLabelForValue: () => '2026-07-09' }, 0)).toBe('2026-07-09 UTC');

    const progressConfig = neutralChartConfig(chart({
      chart_type: 'progress',
      x_axis: { type: 'category', label: 'Service', unit: null, min: null, max: null },
      y_axis: { type: 'linear', label: 'Completion', unit: '%', min: 0, max: 100 },
    }));
    const progressScales = progressConfig?.options?.scales as unknown as Record<string, Record<string, unknown>>;
    expect(progressConfig?.options?.indexAxis).toBe('y');
    expect(progressScales.x).toMatchObject({ type: 'linear', min: 0, max: 100 });
    expect(progressScales.y).toMatchObject({ type: 'category' });
  });

  it('uses gaps rather than zero-width ranges for missing observations', () => {
    const config = neutralChartConfig(chart({
      chart_type: 'range',
      x_axis: { type: 'category' },
      series: [
        { id: 'first', label: 'First', points: [{ x: 'A', y: [2, 7] }] },
        { id: 'second', label: 'Second', points: [{ x: 'B', y: [3, 8] }] },
      ],
    }));

    expect(config?.data.labels).toEqual(['A', 'B']);
    expect(config?.data.datasets[0].data).toEqual([[2, 7], null]);
    expect(config?.data.datasets[1].data).toEqual([null, [3, 8]]);
  });

  it('formats doughnut tooltip values from the native parsed number', () => {
    const config = neutralChartConfig(chart({
      chart_type: 'donut',
      x_axis: { type: 'category' },
      y_axis: { type: 'linear', unit: 'ms' },
    }));
    const tooltip = config?.options?.plugins?.tooltip as unknown as {
      callbacks: { label: (context: { dataset: { label: string }; parsed: number }) => string };
    };

    expect(tooltip.callbacks.label({ dataset: { label: 'Value' }, parsed: 42 })).toBe('Value: 42 ms');
  });

  it('rejects legacy and degraded canonical input without throwing', () => {
    const degraded = normalizeChartData({ type: 'chart', data: [{ label: 'A', value: 1 }] });
    expect(degraded.series).toEqual([]);
    expect(degraded.xAxis).toEqual({
      type: 'category',
      label: null,
      unit: null,
      min: null,
      max: null,
    });
    expect(degraded.yAxis).toEqual({
      type: 'linear',
      label: null,
      unit: null,
      min: null,
      max: null,
    });
    expect(normalizeChartData(chart({ spec_version: 'future', series: [] })).series).toEqual([]);
    expect(normalizeChartData(chart({ chart_type: 'line', series: [{ points: [{ x: 'bad', y: Number.NaN }] }] })).series).toEqual([]);
    expect(neutralChartConfig(chart({ series: [] }))).toBeNull();
  });

  it('does not time-filter category axes and keeps the last duplicate point', () => {
    const normalized = normalizeChartData(
      chart({
        x_axis: { type: 'category' },
        source_ids: ['source-1', 2],
        source: 3,
        series: [
          {
            id: 4,
            label: 5,
            points: [
              { x: '2026-01-01', y: 1 },
              { x: '2026-01-01', y: 2 },
              { x: '2026-07-15', y: 3 },
            ],
          },
        ],
      }),
      '7d',
    );

    expect(normalized.labels).toEqual(['2026-01-01', '2026-07-15']);
    expect(normalized.series[0]).toMatchObject({
      id: 'series-1',
      label: 'series-1',
      points: [
        { x: '2026-01-01', y: 2 },
        { x: '2026-07-15', y: 3 },
      ],
    });
    expect(normalized.sourceIds).toEqual(['source-1']);
    expect(normalized.source).toBeNull();
  });

  it('matches linear and strict time x semantics', () => {
    const linear = normalizeChartData(
      chart({
        x_axis: { type: 'linear' },
        series: [{
          id: 'value',
          label: 'Value',
          points: [
            { x: 2, y: 2 },
            { x: 1, y: 1 },
            { x: 1.23452, y: 4 },
            { x: 1.23451, y: 3 },
            { x: 0.000001, y: 0 },
          ],
        }],
      }),
    );

    expect(linear.labels).toEqual(['1e-6', '1', '1.23451e0', '1.23452e0', '2']);
    expect(linear.series[0].points.map((point) => point.x)).toEqual([
      0.000001,
      1,
      1.23451,
      1.23452,
      2,
    ]);
    expect(normalizeChartData(
      chart({ series: [{ id: 'bad', label: 'Bad', points: [{ x: '2026', y: 1 }] }] }),
    ).series).toEqual([]);
    expect(normalizeChartData(
      chart({ series: [{ id: 'bad', label: 'Bad', points: [{ x: '20260715', y: 1 }] }] }),
    ).series).toEqual([]);
    expect(normalizeChartData(
      chart({ series: [{ id: 'bad', label: 'Bad', points: [{ x: '2026-W29-3', y: 1 }] }] }),
    ).series).toEqual([]);
    const category = normalizeChartData(
      chart({
        x_axis: { type: 'category' },
        series: [{
          id: 'value',
          label: 'Value',
          points: [
            { x: 1, y: 1 },
            { x: 1.0, y: 2 },
            { x: 0.000001, y: 3 },
            { x: '0.000001', y: 4 },
          ],
        }],
      }),
    );
    expect(category.labels).toEqual(['1', '1e-6', '0.000001']);
    expect(category.series[0].points[0].y).toBe(2);
  });

  it('fills empty daily buckets while preserving canonical point fields', () => {
    const filled = fillEmptyTimeBuckets([
      {
        id: 'errors',
        label: 'Errors',
        stack: null,
        points: [
          { x: '2026-07-01', y: 3, label: null },
          { x: '2026-07-03', y: 1, label: null },
        ],
      },
    ]);

    expect(filled[0].points).toEqual([
      { x: '2026-07-01', y: 3, label: null },
      { x: '2026-07-02', y: 0, label: null },
      { x: '2026-07-03', y: 1, label: null },
    ]);
  });
});
