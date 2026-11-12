import { describe, expect, it } from 'vitest';
import { bucketLabels, bucketUnit } from './bars';
import { fixtureQuery } from './fixtures';
import { bucketFor, wireQuery } from './query-contract';

const range = (start: string, end: string) => wireQuery({ ...fixtureQuery, filters: { ...fixtureQuery.filters, start, end } });

describe('adaptive chart resolution', () => {
  it('charts short ranges by hour and longer ranges by local day, reading hours up to 90 days', () => {
    const unit = (start: string, end: string) => { const body = range(start, end); return bucketFor(new Date(body.start), new Date(body.end)); };
    expect(unit('2026-11-04', '2026-11-04')).toBe('hour');
    expect(unit('2026-11-02', '2026-11-04')).toBe('hour');
    expect(unit('2026-11-01', '2026-11-04')).toBe('day');
    // Local days are summed from hourly server buckets.
    expect(range('2026-10-06', '2026-11-04').bucket).toBe('hour');
    expect(range('2026-01-01', '2026-11-04').bucket).toBe('day');
  });
  it('labels hourly buckets with local times and keeps the date at local midnight', () => {
    // Europe/Prague (UTC+1 in November).
    expect(bucketLabels('2026-11-04T13:00:00Z', '2026-11-04T14:00:00Z').label).toBe('14:00');
    expect(bucketLabels('2026-11-03T23:00:00Z', '2026-11-04T00:00:00Z').label).toBe('Nov 4');
    expect(bucketLabels('2026-11-04T13:00:00Z', '2026-11-04T14:00:00Z').title).toMatch(/14:00$/);
    expect(bucketLabels('2026-11-03T23:00:00Z', '2026-11-04T23:00:00Z').label).toBe('Nov 4');
    // Archived UTC days say so.
    expect(bucketLabels('2026-11-04T00:00:00Z', '2026-11-05T00:00:00Z').title).toMatch(/\(UTC\)$/);
    expect(bucketUnit([{ start: '2026-11-04T13:00:00Z', end: '2026-11-04T14:00:00Z' }])).toBe('hour');
    expect(bucketUnit([{ start: '2026-11-04T00:00:00Z', end: '2026-11-05T00:00:00Z' }])).toBe('day');
  });
});
