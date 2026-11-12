import { describe, expect, it } from 'vitest';
import { ApiError } from '$lib/api/client';
import { createUsageDataSource, type UsageTransport } from './adapter';
import { fixtureQuery } from './fixtures';
import { catalog, metrics, response } from './wire.test-data';
import { costSeries, performanceCharts } from './chart-data';
import type * as W from './wire';

const populations = [
  { surface: 'costs' as const, groupBy: 'provider' as const },
  { surface: 'performance' as const, groupBy: 'provider' as const },
  { surface: 'companions' as const, serviceId: 'authorized-service' },
  { surface: 'companions' as const }
];
const refusals = ['mixed_cost_ranking', 'incomplete_cost_ranking'] as const;
function selected(population: typeof populations[number]) {
  return { ...fixtureQuery, ...population, mode: 'personal' as const,
    filters: { ...fixtureQuery.filters, provider: 'authorized-provider', model: 'authorized-model' },
    preciseStart: '2026-10-01T01:02:03Z', preciseEnd: '2026-10-02T04:05:06Z',
    detailFilters: { subject_keys: ['authorized-subject'], agent_ids: ['authorized-agent'] } };
}
function observations(body: W.Query): W.QueryResponse {
  const measured = metrics();
  measured.costs = [
    { ...measured.costs[0], known_amount: '0.125000000000000001', currency: 'USD', basis: 'api_estimate' },
    { ...measured.costs[0], known_amount: '0.500000000000000002', currency: 'EUR', basis: 'api_estimate' },
    { ...measured.costs[0], known_amount: null, currency: 'USD', basis: 'configured_equivalent', quality: 'unknown', priced_attempts: 0, partially_priced_attempts: 0 }
  ];
  measured.tokens.output = null;
  const payload = response();
  payload.meta.scope = body.scope;
  payload.total = measured;
  payload.groups = body.group_by ? [
    { key: 'authorized-service', label: 'Authorized returned cohort', metrics: measured },
    { key: null, label: null, metrics: { ...metrics(), costs: [], tokens: { ...metrics().tokens, input: null, output: null } } }
  ] : [];
  payload.buckets = payload.buckets.map((bucket) => ({ ...bucket, metrics: measured, groups: payload.groups }));
  return payload;
}
type Stage = 'primary' | 'secondary';
function setup(population: typeof populations[number], stage: Stage, code: string, options: {
  status?: number; recoveredScopeMismatch?: boolean; primaryScopeMismatch?: boolean;
  controller?: AbortController; abortAt?: 'refusal' | 'recovery'; empty?: boolean;
} = {}) {
  const calls: W.Query[] = [];
  let catalogueCalls = 0;
  let refused = false;
  const send: UsageTransport = async <T>(path: string, init: Parameters<UsageTransport>[1] = {}): Promise<T> => {
    const body = JSON.parse(String(init.body)) as W.Query;
    if (path.endsWith('/catalog')) { catalogueCalls++; return catalog(body.scope) as T; }
    calls.push(body);
    const shouldRefuse = stage === 'primary' ? calls.length === 1 : body.view === 'ranking';
    if (shouldRefuse && !refused && !options.primaryScopeMismatch) {
      refused = true;
      if (options.abortAt === 'refusal') options.controller!.abort(new DOMException('Owned cancellation', 'AbortError'));
      throw new ApiError('PRIVATE refusal payload must never be shown', { status: options.status ?? 422, code });
    }
    const payload = observations(body);
    if (options.primaryScopeMismatch && calls.length === 1 || options.recoveredScopeMismatch && refused) payload.meta.scope = { mode: 'admin' };
    if (options.abortAt === 'recovery' && refused) options.controller!.abort(new DOMException('Owned cancellation', 'AbortError'));
    if (options.empty) { payload.total = null; payload.buckets = []; payload.groups = []; }
    return payload as T;
  };
  return { source: createUsageDataSource(send), calls, catalogues: () => catalogueCalls };
}
function samePopulation(left: W.Query, right: W.Query) {
  const { view: _v, group_by: _g, limit: _l, forecast: _f, ...leftPopulation } = left;
  const { view: _rv, group_by: _rg, limit: _rl, forecast: _rf, ...rightPopulation } = right;
  // Recovery changes shape only; the ranking metric (request count) is never substituted.
  expect(rightPopulation).toEqual(leftPopulation);
  expect(right.metric).toBe(left.metric);
}
describe('real QueryService ranking refusal contracts (test_expansion.py:101–106)', () => {
  it.each(populations.flatMap((population) => refusals.map((code) => ({ population, code }))))(
    'recovers primary $population/$code by changing shape only once', async ({ population, code }) => {
      const { source, calls, catalogues } = setup(population, 'primary', code);
      const result = await source.query(selected(population), new AbortController().signal);
      expect(result.state).toBe('ready');
      if (result.state !== 'ready') throw new Error('Expected compatible unranked observations');
      expect(catalogues()).toBe(1);
      expect(calls).toHaveLength(2);
      expect(calls[1].view).toBe('trend');
      samePopulation(calls[0], calls[1]);
      expect(calls[1].forecast).toEqual(calls[0].forecast);
      const allServices = population.surface === 'companions' && !('serviceId' in population);
      expect(calls[1].group_by).toBe(allServices ? 'service' : undefined);
      expect(result.view.groupBy).toBe(allServices ? 'service' : undefined);
      expect(result.view.totals?.costs).toEqual([
        { amount: '0.125000000000000001', currency: 'USD', basis: 'api_estimate' },
        { amount: '0.500000000000000002', currency: 'EUR', basis: 'api_estimate' },
        { amount: null, currency: 'USD', basis: 'api_equivalent' }
      ]);
      expect(result.view.totals?.output).toBeNull();
      expect(costSeries(result.view)).toHaveLength(3);
      expect(performanceCharts(result.view)[0].series[0].points[0].value).not.toBeNull();
      expect(result.view.sectionErrors?.[0]).toContain(code === 'mixed_cost_ranking' ? 'more than one currency' : 'not priced');
      if (allServices) {
        expect(result.view.groups).toHaveLength(2);
        expect(result.view.groups[1].id).toBeNull();
        expect(result.view.groups[1].cost.amount).toBeNull();
        expect(result.view.companions).toHaveLength(1);
      } else expect(result.view.groups).toEqual([]);
      expect(JSON.stringify(result)).not.toContain('PRIVATE');
    }
  );
  it.each(populations.slice(0, 3).flatMap((population) => refusals.map((code) => ({ population, code }))))(
    'keeps successful grouped trends on secondary $population/$code without another request', async ({ population, code }) => {
      const { source, calls } = setup(population, 'secondary', code);
      const result = await source.query(selected(population), new AbortController().signal);
      expect(result.state).toBe('ready');
      if (result.state !== 'ready') throw new Error('Expected retained authorized trend');
      expect(calls.map((call) => call.view)).toEqual(['trend', 'ranking']);
      samePopulation(calls[0], calls[1]);
      expect(result.view.groups).toEqual([]);
      expect(result.view.buckets[0].costs?.[1].amount).toBe('0.500000000000000002');
      expect(result.view.sectionErrors?.[0]).toMatch(/not priced|more than one currency/);
    }
  );
  const fences = [
    { status: 401, code: 'mixed_cost_ranking' }, { status: 403, code: 'incomplete_cost_ranking' },
    { status: 410, code: 'invalid_usage_cursor' }, { status: 422, code: 'invalid_usage_cursor' },
    { status: 422, code: 'usage_selection_window' }, { status: 400, code: 'mixed_cost_ranking' }
  ];
  it.each(populations.flatMap((population) => fences.map((fence) => ({ population, fence }))))(
    'does not recover primary fences $population/$fence', async ({ population, fence }) => {
      const { source, calls } = setup(population, 'primary', fence.code, fence);
      const result = await source.query(selected(population), new AbortController().signal);
      expect(result.state).toBe('error');
      expect(calls).toHaveLength(1);
      expect(JSON.stringify(result)).not.toContain('PRIVATE');
    }
  );
  it.each(populations.slice(0, 3).flatMap((population) => fences.map((fence) => ({ population, fence }))))(
    'does not retain partial success on secondary fences $population/$fence', async ({ population, fence }) => {
      const { source, calls } = setup(population, 'secondary', fence.code, fence);
      const result = await source.query(selected(population), new AbortController().signal);
      expect(result.state).toBe('error');
      expect(calls).toHaveLength(2);
      expect(JSON.stringify(result)).not.toContain('PRIVATE');
    }
  );
  it.each(populations)('rejects mismatched recovered scope for %s', async (population) => {
    const { source, calls } = setup(population, 'primary', 'mixed_cost_ranking', { recoveredScopeMismatch: true });
    expect((await source.query(selected(population), new AbortController().signal)).state).toBe('error');
    expect(calls).toHaveLength(2);
  });
  it.each(populations)('does not remove unsupported primary grouping policy for %s', async (population) => {
    const { source, calls } = setup(population, 'primary', 'unsupported_usage_dimension');
    expect((await source.query(selected(population), new AbortController().signal)).state).toBe('error');
    expect(calls).toHaveLength(1);
  });
  it.each(populations.slice(0, 3))('preserves the established optional-dimension behavior for %s', async (population) => {
    const { source, calls } = setup(population, 'secondary', 'unsupported_usage_dimension');
    const result = await source.query(selected(population), new AbortController().signal);
    expect(result.state).toBe(population.surface === 'companions' ? 'error' : 'ready');
    if (result.state === 'ready') expect(result.view.sectionErrors?.[0]).toContain('This breakdown is unavailable. Choose another grouping.');
    expect(calls).toHaveLength(2);
  });
  it.each(populations)('does not substitute a population after primary scope mismatch for %s', async (population) => {
    const { source, calls } = setup(population, 'secondary', 'mixed_cost_ranking', { primaryScopeMismatch: true });
    expect((await source.query(selected(population), new AbortController().signal)).state).toBe('error');
    expect(calls).toHaveLength(1);
  });
  it.each(['refusal', 'recovery'] as const)('respects cancellation during %s without success or further recovery', async (abortAt) => {
    const controller = new AbortController();
    const { source, calls } = setup(populations[3], 'primary', 'mixed_cost_ranking', { controller, abortAt });
    let rejected: unknown;
    await expect(source.query(selected(populations[3]), controller.signal).catch((error) => { rejected = error; throw error; }))
      .rejects.toMatchObject({ name: 'AbortError', message: 'Owned cancellation' });
    expect(rejected).toBe(controller.signal.reason);
    expect(calls).toHaveLength(abortAt === 'refusal' ? 1 : 2);
  });
  it.each(populations.slice(0, 3))('aborts secondary ranking for %s without retaining the successful trend', async (population) => {
    const controller = new AbortController();
    const { source, calls } = setup(population, 'secondary', 'mixed_cost_ranking', { controller, abortAt: 'refusal' });
    let rejected: unknown;
    await expect(source.query(selected(population), controller.signal).catch((error) => { rejected = error; throw error; }))
      .rejects.toMatchObject({ name: 'AbortError' });
    expect(rejected).toBe(controller.signal.reason);
    expect(calls).toHaveLength(2);
  });
  it('retains an authorized empty response without manufacturing zero bands or rows', async () => {
    const { source, calls } = setup(populations[3], 'primary', 'incomplete_cost_ranking', { empty: true });
    const result = await source.query(selected(populations[3]), new AbortController().signal);
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') throw new Error('Expected empty authorized population');
    expect(result.view.totals).toBeNull();
    expect(result.view.groups).toEqual([]);
    expect(result.view.buckets).toEqual([]);
    expect(calls).toHaveLength(2);
  });
  it.each(populations.slice(0, 2))('preserves an honest successful ranking for %s', async (population) => {
    const calls: W.Query[] = [];
    const send: UsageTransport = async <T>(path: string, init: Parameters<UsageTransport>[1] = {}): Promise<T> => {
      const body = JSON.parse(String(init.body)) as W.Query;
      if (path.endsWith('/catalog')) return catalog(body.scope) as T;
      calls.push(body);
      const payload = response();
      payload.meta.scope = body.scope;
      const complete: W.Metrics = { ...metrics(), costs: [{ ...metrics().costs[0], quality: 'complete', unpriced_attempts: 0, partially_priced_attempts: 0 }],
        partially_priced_attempts: 0, unpriced_attempts: 0, unallocatable_cost_attempts: 0 };
      payload.total = complete;
      payload.groups = [{ key: 'authorized-provider', label: 'Authorized provider', metrics: complete }];
      return payload as T;
    };
    const result = await createUsageDataSource(send).query(selected(population), new AbortController().signal);
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') throw new Error('Expected honest ranking');
    expect(calls.map((call) => call.view)).toEqual(['trend', 'ranking']);
    expect(calls[1].limit).toBe(100);
    expect(result.view.groups).toHaveLength(1);
    expect(result.view.groups[0].id).toBe('authorized-provider');
    if (population.surface === 'performance') expect(result.view.performance).toHaveLength(1);
    expect(result.view.sectionErrors).toBeUndefined();
  });
});
