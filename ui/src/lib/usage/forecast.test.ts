import { describe, expect, it } from 'vitest';
import { linearForecast } from './forecast';
import { forecastHistory } from './adapter';

const day = 86_400_000;
const series = (values: (number | null)[]) => values.map((value, index) => ({ at: index * day, value }));

describe('linear usage forecast', () => {
  it('extends an exact linear trend with a zero-width range', () => {
    const result = linearForecast(series([1, 2, 3, 4, 5]), day, 3)!;
    expect(result.points.map((p) => +p.value.toFixed(6))).toEqual([6, 7, 8]);
    expect(result.points[0].at).toBe(5 * day);
    expect(result.total.value).toBeCloseTo(21);
    expect(result.total.high - result.total.low).toBeCloseTo(0);
    expect(result.slope).toBeCloseTo(1);
  });
  it('widens the range with noise and with distance from the data', () => {
    const result = linearForecast(series([4, 6, 3, 7, 5, 6]), day, 4)!;
    const widths = result.points.map((p) => p.high - p.low);
    expect(widths[0]).toBeGreaterThan(0);
    expect(widths[3]).toBeGreaterThan(widths[0]);
    expect(result.total.low).toBeLessThan(result.total.value);
  });
  it('never projects below zero and skips unknown buckets', () => {
    const result = linearForecast(series([9, null, 6, 4, 2, 1]), day, 5)!;
    expect(result.samples).toBe(5);
    expect(result.points.every((p) => p.value >= 0 && p.low >= 0)).toBe(true);
  });
  it('uses its own history window: 3 days hourly for short ranges, 30–90 days daily otherwise', () => {
    const now = new Date('2026-11-04T13:20:00Z');
    expect(forecastHistory({ start: '2026-11-04T00:00:00Z', end: '2026-11-05T00:00:00Z' }, now))
      .toEqual({ start: '2026-11-01T14:00:00.000Z', end: '2026-11-04T14:00:00.000Z', bucket: 'hour' });
    expect(forecastHistory({ start: '2026-10-29T00:00:00Z', end: '2026-11-05T00:00:00Z' }, now))
      .toEqual({ start: '2026-10-06T00:00:00.000Z', end: '2026-11-05T00:00:00.000Z', bucket: 'day' });
    expect(forecastHistory({ start: '2026-01-01T00:00:00Z', end: '2026-11-05T00:00:00Z' }, now).start).toBe('2026-08-07T00:00:00.000Z');
  });
  it('withholds a forecast without enough history', () => {
    expect(linearForecast(series([1, null, 2]), day, 3)).toBeNull();
  });
});
