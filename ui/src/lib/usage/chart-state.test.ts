import { describe, expect, it } from 'vitest';
import { cacheSeries, costSeries, performanceCharts, tokenSeries } from './chart-data';
import { fixtureQuery } from './fixtures';
import { fixtureScreen } from './screen-fixture';
import { drillUsage, readUsageQuery, usageRequestKey, writeUsageQuery } from './query-state';
import type { UsageQuery } from './model';

describe('analytical drill boundaries', () => {
  it('uses keyed bucket groups, preserving absent/null groups, exact filters, currencies and boundary populations', () => {
    const view = fixtureScreen(fixtureQuery);
    view.groupBy = 'provider';
    const alpha = { id: 'usage-fixture-provider-alpha', label: 'Alpha', totals: { ...view.buckets[0].totals!, input: 125, output: null }, performance: { ...view.buckets[0].performance!, failedAttempts: null }, costComplete: false };
    const beta = { id: 'usage-fixture-provider-beta', label: 'Beta', totals: { ...view.buckets[0].totals!, input: 75, costs: [{ amount: '2.250000000000000001', currency: 'EUR', basis: 'api_equivalent' as const }] }, performance: view.buckets[0].performance!, costComplete: true };
    view.buckets[0].groups = [alpha, beta];
    view.buckets[1].groups = [{ ...beta, totals: { ...beta.totals, input: 0 } }];
    const series = tokenSeries(view);
    expect(series).toHaveLength(6);
    expect(series[0].label).toBe('Alpha · Known input tokens');
    expect(series[0].points[0]).toMatchObject({ value: 125, patch: { filters: { provider: alpha.id }, preciseStart: view.buckets[0].start, preciseEnd: view.buckets[0].end } });
    expect(series[0].points[1]).toMatchObject({ value: null, detailAvailable: false });
    expect(series[1].points[1].value).toBe(0);
    expect(series[2].points[0].value).toBeNull();
    const chart = performanceCharts(view).find((item) => item.title === 'Attempt failure rate')!;
    expect(chart.series[0].points[0].value).toBeNull();
    expect(costSeries(view).map((band) => band.key)).toContain('EUR:api_equivalent');
    expect(costSeries(view).find((band) => band.key === 'EUR:api_equivalent')!.series[1].points[0].display).toContain('2.25');
    const query = { ...fixtureQuery, filters: { ...fixtureQuery.filters, model: 'usage-fixture-model' }, serviceId: 'Mnemory', errorCategories: ['timeout'] };
    expect(drillUsage(query, series[0].points[0].patch, 'Alpha', 'input')).toMatchObject({ filters: { provider: alpha.id, model: 'usage-fixture-model' }, errorCategories: ['timeout'], serviceId: 'Mnemory' });
  });
  it('never manufactures an identity or an overall substitute for missing keyed groups', () => {
    const view = fixtureScreen(fixtureQuery);
    view.groupBy = 'agent';
    view.buckets.forEach((bucket) => { bucket.groups = []; });
    expect(tokenSeries(view)).toEqual([]);
    view.buckets[0].groups = [{ id: null, label: 'Unallocated logical cohort', totals: view.buckets[0].totals!, performance: view.buckets[0].performance!, costComplete: false }];
    expect(tokenSeries(view)[0].points[0]).toMatchObject({ detailAvailable: false, patch: { preciseStart: view.buckets[0].start } });
    expect(tokenSeries(view)[0].points[0].patch.filters).toBeUndefined();
  });
  it('round-trips precise filters and source breadcrumbs without expanding fixed project authority', () => {
    const projectScope = { id: 'fixed-project', label: 'Fixed project' };
    const query: UsageQuery = { ...fixtureQuery, mode: 'project', projectScope, surface: 'performance', cursor: 'old-page', selectedAttempt: 'old-selection' };
    const next = drillUsage(query, { filters: { ...query.filters, provider: 'provider-a' }, preciseStart: '2026-10-29T12:00:00Z', preciseEnd: '2026-10-29T13:00:00Z', state: 'failed' }, 'Provider A · partial bucket', 'failed_attempts');
    expect(next).toMatchObject({ mode: 'project', projectScope, surface: 'explorer', explorerMode: 'requests', cursor: undefined, selectedAttempt: undefined, state: 'failed' });
    const url = writeUsageQuery(new URL('http://localhost/projects/fixed-project?tab=usage'), next);
    expect(url.searchParams.get('tab')).toBe('usage');
    expect(readUsageQuery(url, new Date('2026-10-29T00:00:00Z'), projectScope)).toMatchObject({ preciseStart: next.preciseStart, preciseEnd: next.preciseEnd, returnTo: next.returnTo, filters: { provider: 'provider-a' } });
    expect(usageRequestKey(next)).toBe(usageRequestKey({ ...next, selectedAttempt: 'visible-row', contextLabel: 'Presentation-only change' }));
  });
  it('never turns absent metrics or archived buckets into actionable request points', () => {
    const view = fixtureScreen(fixtureQuery);
    view.buckets[0].totals!.input = null;
    view.buckets[1].totals!.input = 0;
    view.availability!.resolution = 'mixed';
    view.buckets[1].source = 'archive';
    const points = tokenSeries(view)[0].points;
    expect(points[0]).toMatchObject({ value: null, detailAvailable: false });
    expect(points[1]).toMatchObject({ value: 0, detailAvailable: false });
    expect(points[2].detailAvailable).toBe(true);
    expect(points.at(-1)?.population).toContain('Partial data');
  });
  it('keeps price bands, cache populations and eligible weighted throughput distinct', () => {
    const view = fixtureScreen(fixtureQuery);
    view.buckets[0].costs = [{ amount: '0.123456789012345678', currency: 'USD', basis: 'api_estimate' }, { amount: '2.25', currency: 'EUR', basis: 'api_equivalent' }];
    const bands = costSeries(view);
    expect(bands).toHaveLength(2);
    expect(bands.map((band) => band.series[0].points[0].metric)).toEqual(['estimated_cost:USD:api_estimate', 'estimated_cost:EUR:api_equivalent']);
    const totals = view.buckets[0].totals!;
    totals.cacheRatioRead = 50; totals.cacheEligibleInput = 200; totals.cacheRead = 9000;
    expect(cacheSeries(view)[0].points[0].value).toBe(25);
    totals.cacheEligibleInput = 0;
    expect(cacheSeries(view)[0].points[0].value).toBeNull();
    view.buckets[0].performance!.generation = { outputTokens: 300, durationMs: 6000, samples: 2 };
    expect(performanceCharts(view).find((chart) => chart.unit === 'tok/s')!.series[0].points[0].value).toBe(50);
  });
});
