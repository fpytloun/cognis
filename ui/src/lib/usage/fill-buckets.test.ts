import { describe, expect, it } from 'vitest';
import { fillBuckets } from './bars';
import type { UsageBucket } from './model';

const bucket = (start: string, amount: string, attempts: number): UsageBucket => ({
  start, end: new Date(Date.parse(start) + 3_600_000).toISOString(), complete: true, source: 'retained', tokens: 10,
  cost: { amount, currency: 'USD', basis: 'api_estimate' }, costs: [{ amount, currency: 'USD', basis: 'api_estimate' }],
  totals: { input: 8, output: 2, cacheRead: 0, cacheWrite: 0, cacheEligibleInput: 0, reasoning: 0, attempts, unknown: 0, partial: 0, unpriced: 0,
    cost: { amount, currency: 'USD', basis: 'api_estimate' } }
});

describe('continuous usage axis', () => {
  it('fills hours without requests as zero up to now and keeps server buckets', () => {
    const now = Date.parse('2026-11-05T05:30:00Z');
    const result = fillBuckets([bucket('2026-11-05T01:00:00.000Z', '1.5', 3), bucket('2026-11-05T03:00:00.000Z', '2', 4)],
      '2026-11-05T00:00:00.000Z', '2026-11-06T00:00:00.000Z', 'hour', now);
    expect(result.map((b) => b.start.slice(11, 13))).toEqual(['00', '01', '02', '03', '04', '05']);
    expect(result.map((b) => b.totals?.attempts)).toEqual([0, 3, 0, 4, 0, 0]);
    expect(result[0].costs).toEqual([{ amount: '0', currency: 'USD', basis: 'api_estimate' }]);
    // The running hour is incomplete; nothing is invented after now.
    expect(result.at(-1)!.complete).toBe(false);
  });
  it('leaves archived ranges alone', () => {
    const archived = { ...bucket('2026-11-01T00:00:00.000Z', '1', 1), source: 'archive' as const };
    expect(fillBuckets([archived], '2026-11-01T00:00:00.000Z', '2026-11-05T00:00:00.000Z', 'day', Date.parse('2026-11-06T00:00:00Z'))).toEqual([archived]);
  });
});
