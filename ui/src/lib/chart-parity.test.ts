import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { neutralChartConfig, normalizeChartData, type ChartYValue } from './rich-data';

interface GoldenSeries {
  id: string;
  label: string;
  stack: string | null;
  values: (ChartYValue | null)[];
}

interface GoldenAxis {
  type: 'time' | 'category' | 'linear';
  label: string | null;
  unit: string | null;
  min: number | null;
  max: number | null;
}

interface GoldenExpected {
  chart_type: string;
  chartjs_type: string;
  ordered_x: string[];
  series: GoldenSeries[];
  x_axis: GoldenAxis;
  y_axis: GoldenAxis;
  chartjs_x_type: string | null;
  chartjs_y_type: string | null;
  stack: boolean;
  legend_position: 'top' | 'right' | 'bottom' | 'none';
  palette_token: string;
  typescript_palette: string[];
  typescript_point_palette?: string[];
}

interface GoldenCase {
  name: string;
  spec: Record<string, unknown>;
  expected: GoldenExpected;
}

interface GoldenFixture {
  spec_version: string;
  cases: GoldenCase[];
}

const golden = JSON.parse(
  readFileSync(resolve(process.cwd(), '../tests/fixtures/chart_parity.json'), 'utf8'),
) as GoldenFixture;

function alignedValues(
  labels: string[],
  points: { x: string | number; y: ChartYValue }[],
): (ChartYValue | null)[] {
  const values = new Map(points.map((point) => [String(point.x), point.y]));
  return labels.map((label) => values.get(label) ?? null);
}

describe('canonical chart renderer parity golden', () => {
  it.each(golden.cases)('$name agrees with Python semantics and Chart.js config', ({ spec, expected }) => {
    const normalized = normalizeChartData(spec);
    const config = neutralChartConfig(spec);

    expect(normalized.specVersion).toBe(golden.spec_version);
    expect(normalized.chartType).toBe(expected.chart_type);
    expect(normalized.labels).toEqual(expected.ordered_x);
    expect(normalized.series.map((series) => ({
      id: series.id,
      label: series.label,
      stack: series.stack,
      values: alignedValues(normalized.labels, series.points),
    }))).toEqual(expected.series);
    expect(normalized.xAxis).toEqual(expected.x_axis);
    expect(normalized.yAxis).toEqual(expected.y_axis);
    expect(normalized.stack).toBe(expected.stack);
    expect(normalized.legendPosition).toBe(expected.legend_position);
    expect(normalized.paletteToken).toBe(expected.palette_token);

    expect(config?.type).toBe(expected.chartjs_type);
    expect(config?.data.labels).toEqual(expected.ordered_x);
    expect(config?.data.datasets.map((dataset) => ({
      id: (dataset as unknown as Record<string, unknown>).cognisSeriesId,
      label: dataset.label,
      stack: dataset.stack ?? null,
      values: dataset.data,
      color: dataset.borderColor,
    }))).toEqual(expected.series.map((series, index) => ({
      id: series.id,
      label: series.label,
      stack: series.stack ?? (expected.stack ? 'default' : null),
      values: series.values,
      color: expected.typescript_palette[index],
    })));
    expect(config?.options?.plugins?.legend?.position).toBe(expected.legend_position);
    if (expected.typescript_point_palette) {
      expect(config?.data.datasets.map((dataset) => dataset.backgroundColor)).toEqual(
        expected.series.map(() => expected.typescript_point_palette),
      );
    }

    const scales = config?.options?.scales as Record<string, Record<string, unknown>> | undefined;
    expect(scales?.x?.type ?? null).toBe(expected.chartjs_x_type);
    expect(scales?.y?.type ?? null).toBe(expected.chartjs_y_type);
    if (scales?.x && scales.y) {
      expect(scales.x).toMatchObject({
        min: expected.x_axis.min ?? undefined,
        max: expected.x_axis.max ?? undefined,
        stacked: expected.chart_type === 'stacked_bar' || expected.stack,
      });
      expect(scales.y).toMatchObject({
        min: expected.y_axis.min ?? undefined,
        max: expected.y_axis.max ?? undefined,
        stacked: expected.chart_type === 'stacked_bar' || expected.stack,
      });
    }
  });
});
