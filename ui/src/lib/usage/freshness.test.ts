import { describe, expect, it } from 'vitest';
import { dataFreshness } from './adapter';
import { meta } from './wire.test-data';

const now = Date.parse('2026-11-05T17:30:00Z');
const projection = (state: 'ready' | 'lagging', lag: number | null) =>
  ({ ...meta(), observed_through: null, projection: { state, captured_through: null, projected_through: '2026-11-05T00:00:12Z', lag_seconds: lag, unprojected_observations: 0 } });

describe('usage data freshness', () => {
  it('measures freshness from the reported projection lag, not the day-boundary marker', () => {
    expect(dataFreshness(projection('lagging', 600), now)).toBe('2026-11-05T17:20:00.000Z');
  });
  it('treats a ready projection without a lag as current and anything else as unknown', () => {
    expect(dataFreshness(projection('ready', null), now)).toBe('2026-11-05T17:30:00.000Z');
    expect(dataFreshness(projection('lagging', null), now)).toBe('Unknown');
  });
});
