import { describe, expect, it } from 'vitest';
import { count, money, type UsageBucket } from './model';
import { forecast } from './fixture-forecast';
import { fixtureQuery, fixtureView } from './fixtures';

describe('usage view semantics', () => {
  it('never formats a missing measurement as zero', () => {
    expect(count(null)).toBe('Unknown');
    expect(count(0)).toBe('0');
    expect(money({ amount: null, currency: 'USD', basis: 'unknown' })).toBe('Estimate unavailable');
    expect(money({ amount: '0', currency: 'USD', basis: 'api_estimate' })).toBe('$0.00');
  });
  it('forecasts only complete consecutive buckets and preserves unknown costs', () => {
    const buckets: UsageBucket[] = [1, 2, 3, 4, 5, 6, 7].map((n) => ({
      start: `2026-10-0${n}`, complete: true, tokens: 100,
      cost: { amount: n === 2 ? null : '1', currency: 'USD', basis: 'api_estimate' }, costComplete: n !== 2
    }));
    expect(forecast(buckets)).toMatchObject({ tokens: 700, samples: 7, cost: { amount: null } });
    expect(forecast(buckets.slice(0, 2))).toBeNull();
    expect(forecast(buckets.map((bucket, index) => index === 1 ? { ...bucket, tokens: null } : bucket))).toBeNull();
    expect(forecast([buckets[0], buckets[2], { ...buckets[2], start: '2026-10-05' }])).toBeNull();
    expect(forecast(buckets.map((bucket) => ({ ...bucket, cost: { ...bucket.cost, amount: '1' }, costComplete: false })))).toMatchObject({ tokens: 700, cost: { amount: null } });
    expect(forecast(buckets.map((bucket) => ({ ...bucket, cost: { ...bucket.cost, amount: '1' }, costComplete: true })))).toMatchObject({ tokens: 700, cost: { amount: '7.000000000000' } });
  });
  it('fixture modes scope facets and exclude private project resources', () => {
    const personal = fixtureView({ ...fixtureQuery, mode: 'personal' });
    expect(personal.facets.user.map((item) => item.value)).toEqual(['Alex']);
    const project = fixtureView({ ...fixtureQuery, mode: 'project' });
    expect(project.quota).toBeNull();
    expect(project.facets.project.map((item) => item.value)).toEqual(['Cognis']);
    expect(project.sessions.some((session) => session.label === 'Private investigation')).toBe(false);
  });
  it('filters all supported dimensions and retains unknown price coverage', () => {
    const result = fixtureView({ ...fixtureQuery, filters: { ...fixtureQuery.filters, purpose: 'Embedding' }, groupBy: 'purpose' });
    expect(result.groups.map((item) => item.label)).toEqual(['Embedding']);
    expect(result.totals.cost.amount).toBeNull();
    expect(result.totals.unpriced).toBeGreaterThan(0);
    expect(result.totals.cacheWrite).toBeNull();
  });
});
