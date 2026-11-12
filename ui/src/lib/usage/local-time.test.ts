import { describe, expect, it } from 'vitest';
import { boundaries, localDate, localDays, localMidnight } from './local-time';
import { wireQuery } from './query-contract';
import { fixtureQuery } from './fixtures';
import type { UsageBucket } from './model';

// Tests run in Europe/Prague (vite.config.ts).
const hour = (start: string, amount: string, attempts: number): UsageBucket => ({
  start, end: new Date(Date.parse(start) + 3_600_000).toISOString(), complete: true, source: 'retained', tokens: 10,
  cost: { amount, currency: 'USD', basis: 'api_estimate' }, costs: [{ amount, currency: 'USD', basis: 'api_estimate' }],
  totals: { input: 8, output: 2, cacheRead: 0, cacheWrite: 0, cacheEligibleInput: 0, reasoning: 0, attempts, unknown: 0, partial: 0, unpriced: 0,
    cost: { amount, currency: 'USD', basis: 'api_estimate' } }
});

describe('usage in the browser time zone', () => {
  it('maps calendar days to local midnights, including across the DST change', () => {
    expect(localMidnight('2026-11-05').toISOString()).toBe('2026-11-04T23:00:00.000Z');
    expect(localMidnight('2026-07-01').toISOString()).toBe('2026-06-30T22:00:00.000Z');
    // Oct 25 2026 has 25 hours in Prague.
    const days = boundaries('2026-10-24T22:00:00.000Z', '2026-10-26T23:00:00.000Z', 'day');
    expect(days.map((at) => new Date(at).toISOString())).toEqual(['2026-10-24T22:00:00.000Z', '2026-10-25T23:00:00.000Z']);
    expect(localDate(new Date('2026-11-04T23:30:00Z'))).toBe('2026-11-05');
  });
  it('queries local days at the hourly grain up to 90 days and UTC days beyond', () => {
    const local = wireQuery({ ...fixtureQuery, filters: { ...fixtureQuery.filters, start: '2026-11-05', end: '2026-11-05' } });
    expect(local).toMatchObject({ start: '2026-11-04T23:00:00.000Z', end: '2026-11-05T23:00:00.000Z', bucket: 'hour' });
    const long = wireQuery({ ...fixtureQuery, filters: { ...fixtureQuery.filters, start: '2026-01-01', end: '2026-11-05' } });
    expect(long).toMatchObject({ start: '2026-01-01T00:00:00.000Z', end: '2026-11-06T00:00:00.000Z', bucket: 'day' });
  });
  it('sums hours into local days with exact decimal costs and pooled counts', () => {
    const days = localDays([hour('2026-11-03T23:00:00.000Z', '0.1', 2), hour('2026-11-04T22:00:00.000Z', '0.2', 3), hour('2026-11-04T23:00:00.000Z', '5', 1)],
      '2026-11-03T23:00:00.000Z', '2026-11-05T23:00:00.000Z', Date.parse('2026-11-06T00:00:00Z'));
    expect(days.map((day) => [day.start, day.costs?.[0].amount, day.totals?.attempts])).toEqual([
      ['2026-11-03T23:00:00.000Z', '0.3', 5],
      ['2026-11-04T23:00:00.000Z', '5', 1]
    ]);
  });
  it('keeps the known priced subtotal of a day when some hours have no priced requests', () => {
    const unpriced = { ...hour('2026-11-04T01:00:00.000Z', '0', 4), cost: { amount: null, currency: 'USD', basis: 'api_estimate' as const },
      costs: [{ amount: null, currency: 'USD', basis: 'api_estimate' as const }] };
    const [day] = localDays([unpriced, hour('2026-11-04T02:00:00.000Z', '1.5', 2)], '2026-11-03T23:00:00.000Z', '2026-11-04T23:00:00.000Z', Date.parse('2026-11-06T00:00:00Z'));
    expect(day.costs?.[0].amount).toBe('1.5');
  });
});
