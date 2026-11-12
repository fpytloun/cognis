import { describe, expect, it } from 'vitest';
import { ApiError } from '$lib/api/client';
import { createUsageDataSource, mapAttempt, mapTotals, wireQuery, type UsageTransport } from './adapter';
import { fixtureQuery } from './fixtures';
import { attempt, catalog, meta, metrics, response } from './wire.test-data';
import type * as W from './wire';
import { costSeries } from './chart-data';
const query = { ...fixtureQuery, mode: 'personal' as const };
const signal = () => new AbortController().signal;
function transport(handler: (path: string, body: W.Query) => unknown) {
  const calls: { path: string; body: W.Query }[] = [];
  const send: UsageTransport = async <T>(path: string, options: Parameters<UsageTransport>[1] = {}): Promise<T> => { const body = JSON.parse(options.body as string || '{}') as W.Query; calls.push({ path, body }); return (path === '/api/v1/usage/catalog' ? catalog(body.scope) : await handler(path, body)) as T; };
  return { send, calls };
}
describe('frozen public operation catalog and bounded active views', () => {
  it('preserves known monetary components of partial valuations in the headline trend, without ranking', async () => {
    const payload = response();
    for (const measured of [payload.total, ...payload.buckets.map((bucket) => bucket.metrics)]) {
      measured.costs[0] = { ...measured.costs[0], known_amount: '0.125000000001', quality: 'partial', priced_attempts: 0, unpriced_attempts: 2 };
    }
    const { send, calls } = transport(() => payload);
    const result = await createUsageDataSource(send).query(query, signal());
    expect(calls.map((call) => call.body.view)).toEqual([undefined, 'cost_breakdowns', 'trend']);
    if (result.state !== 'ready') throw new Error('Expected known subtotal');
    expect(result.view.totals?.cost.amount).toBe('0.125000000001');
    const headline = costSeries({ ...result.view, groupBy: undefined });
    expect(headline[0].series[0].points[0]).toMatchObject({ value: 0.125000000001 });
    expect(headline[0].series[0].points[0].display).toBe('$0.13');
  });
  it('loads only an explicitly selected owner-scoped provider, preserving numeric nulls and refusing broader scope', async () => {
    const { send, calls } = transport(() => ({ provider_id: 'synthetic-provider', next_cursor: null, limitations: [], samples: [{
      sample_id: 'coalesced-sample-a', observed_at: '2026-10-01T01:00:00Z', first_observed_at: '2026-10-01T00:59:00Z', account_generation: 'generation-a', reset_at: null, window: '5h',
      utilization: null, maximum_utilization: '75', limit: '100', remaining: '42.0000', source: 'stored provider response', sample_count: 3
    }] }));
    const source = createUsageDataSource(send);
    const selected = { ...query, filters: { ...query.filters, provider: 'synthetic-provider' } };
    for (const mode of ['admin', 'project'] as const) await expect(source.providerHistory!({ ...selected, mode }, 'synthetic-provider', null, signal())).rejects.toThrow('provider ownership');
    await expect(source.providerHistory!(query, 'synthetic-provider', null, signal())).rejects.toThrow('Select this provider');
    await expect(source.providerHistory!(selected, 'different-provider', null, signal())).rejects.toThrow('Select this provider');
    expect(calls).toEqual([]);
    const mapped = await source.providerHistory!(selected, 'synthetic-provider', null, signal());
    expect(calls).toHaveLength(1);
    const url = new URL(calls[0].path, 'https://cognis.invalid');
    expect(url.pathname).toBe('/api/v1/usage/providers/synthetic-provider/history');
    expect(JSON.parse(url.searchParams.get('scope')!)).toEqual({ mode: 'personal' });
    expect(mapped.samples[0]).toMatchObject({ utilization: null, maximumUtilization: '75', limit: '100', sampleId: 'coalesced-sample-a', firstObservedAt: '2026-10-01T00:59:00Z', remaining: '42.0000', account: 'generation-a', window: '5h', sampleCount: 3 });
  });
  it('keeps missing-tier and missing-tariff contributions in the aggregate count, even outside the only cost band', () => {
    const mixed = metrics();
    mixed.unpriced_attempts = 2;
    mixed.costs[0].unpriced_attempts = 1;
    expect(mapTotals(mixed)).toMatchObject({ unpriced: 2, cost: { amount: mixed.costs[0].known_amount } });
    expect(mapTotals({ ...mixed, costs: [] })).toMatchObject({ unpriced: 2, cost: { amount: null } });
  });
  it('does not infer unpriced coverage by summing currency bands or treating multiple currencies as unknown', () => {
    const mixed = metrics();
    mixed.unpriced_attempts = 3;
    mixed.costs = [mixed.costs[0], { ...mixed.costs[0], currency: 'EUR' }];
    mixed.costs.forEach((band) => band.unpriced_attempts = 1);
    const totals = mapTotals(mixed);
    expect(totals.unpriced).toBe(3);
    expect(totals.cost.amount).toBeNull();
    expect(totals.costs?.map((cost) => cost.currency)).toEqual(['USD', 'EUR']);
  });
  it('explains an incomplete cost ranking without partial sorting, metric substitution, or exposing the error payload', async () => {
    const { send, calls } = transport((_path, body) => {
      if (body.view === 'ranking') throw new ApiError('Private fixture payload must not be displayed', {
        status: 422, code: 'incomplete_cost_ranking', details: null
      });
      return response();
    });
    const result = await createUsageDataSource(send).query({ ...query, surface: 'costs', groupBy: 'provider' }, signal());
    expect(calls.map((call) => call.body.view)).toEqual([undefined, 'trend', 'ranking']);
    expect(calls.at(-1)?.body).toMatchObject({ view: 'ranking', group_by: 'provider', filters: calls[1].body.filters });
    // Groups rank by request count, never by a substituted partial cost.
    expect(calls.at(-1)?.body.metric).toBe('attempts');
    if (result.state !== 'ready') throw new Error('Expected retained trend and explicit ranking refusal');
    expect(result.view.groups).toEqual([]);
    expect(result.view.totals?.attempts).toBe(12);
    expect(result.view.sectionErrors?.[0]).toContain('not priced');
    expect(result.view.sectionErrors?.[0]).toContain('Filter to priced models');
    expect(JSON.stringify(result)).not.toMatch(/Private fixture payload|expired|Historical detail/);
  });
  it.each([['max', 'standard'], ['auto', 'batch']])('retains supported reasoning effort %s and service tier %s', (effort, tier) => {
    const row = attempt();
    for (const snapshot of [row.parameters!.requested, row.parameters!.effective]) {
      snapshot.reasoning_effort = { value: effort, provenance: 'explicit' };
      snapshot.service_tier = { value: tier, provenance: 'explicit' };
    }
    const mapped = mapAttempt(row, query);
    expect(mapped.logicalId).toBe('usage-fixture-logical-1');
    expect(mapped.detail?.parameters).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'reasoning_effort', requested: effort, effective: effort }),
      expect.objectContaining({ name: 'service_tier', requested: tier, effective: tier })
    ]));
  });
  it('opens Overview with one scope catalogue and one data POST, preserving exact values and populations', async () => {
    const payload = response();
    payload.total.costs[0].known_amount = '9007199254740993.010000000001';
    const { send, calls } = transport(() => payload);
    const result = await createUsageDataSource(send).query(query, signal());
    expect(calls).toHaveLength(3);
    expect(calls[0]).toMatchObject({ path: '/api/v1/usage/catalog', body: { scope: { mode: 'personal' } } });
    expect(calls[1]).toMatchObject({ path: '/api/v1/usage/query', body: { view: 'cost_breakdowns', scope: { mode: 'personal' }, include_pending: false } });
    expect(calls[2]).toMatchObject({ body: { view: 'trend', scope: calls[1].body.scope, filters: calls[1].body.filters } });
    expect(calls[1].body.forecast).toBeUndefined();
    if (result.state !== 'ready') throw new Error('Expected ready observations');
    expect(result.view.totals?.cost.amount).toBe('9007199254740993.010000000001');
    expect(result.view.coverage).toMatchObject({ dropped: 3, gaps: 1, projectionLagSeconds: 60 });
    expect(result.view.sessions).toHaveLength(0);
  });
  it('loads Costs with only trend and one chosen ranking, not session or per-provider fanout', async () => {
    const { send, calls } = transport(() => response());
    await createUsageDataSource(send).query({ ...query, surface: 'costs', groupBy: 'provider' }, signal());
    expect(calls.map((c) => c.body.view)).toEqual([undefined, 'trend', 'ranking']);
    // Forecasts are their own view; the Costs trend asks for none.
    expect(calls[1].body.forecast).toBeUndefined();
    expect(calls[2].body).toMatchObject({ group_by: 'provider', metric: 'attempts' });
  });
  it('keeps successful trends degraded if its one ranking fails, but clears on authorization failure', async () => {
    const { send } = transport((_path, body) => { if (body.view === 'ranking') throw new ApiError('Unavailable', { status: 503 }); return response(); });
    const result = await createUsageDataSource(send).query({ ...query, surface: 'costs' }, signal());
    expect(result.state).toBe('ready');
    if (result.state === 'ready') { expect(result.view.sectionErrors?.length).toBe(1); expect(result.view.buckets).toHaveLength(1); }
    const denied = transport(() => { throw new ApiError('private body', { status: 403 }); });
    expect(await createUsageDataSource(denied.send).query(query, signal())).toEqual({ state: 'error', message: 'Load usage failed: access denied. Choose My usage.' });
  });
  it('loads companion service/operation trends with one bounded ranking and never synthesizes sessions', async () => {
    const { send, calls } = transport(() => ({ ...response(), groups: [{ key: 'memory.extract', label: 'Memory extraction', metrics: metrics() }] }));
    const result = await createUsageDataSource(send).query({ ...query, surface: 'companions', serviceId: 'fixture-service' }, signal());
    // One service: operations and models explain what it spends on.
    expect(calls.map((c) => c.body.view)).toEqual([undefined, 'trend', 'ranking', 'ranking']);
    expect(calls[2].body).toMatchObject({ group_by: 'operation', filters: { service_ids: ['fixture-service'] } });
    expect(calls[3].body).toMatchObject({ group_by: 'model', filters: { service_ids: ['fixture-service'] } });
    if (result.state !== 'ready') throw new Error('Expected mapped operation observations');
    expect(result.view.sessions).toEqual([]);
    expect(result.view.groups[0].performance).toMatchObject({
      providerSpan: { totalMs: metrics().performance.latency_seconds_sum! * 1000, samples: metrics().performance.latency_known_samples },
      failedAttempts: metrics().performance.failed_dispatched_attempts
    });
  });
  it('keeps unallocated totals in accounting, not as companion comparison series', async () => {
    const payload = response();
    payload.groups = [
      { key: null, label: 'Unallocated group', metrics: metrics() },
      { key: 'mnemory', label: 'Memory', metrics: metrics() }
    ];
    const { send } = transport(() => payload);
    const result = await createUsageDataSource(send).query({ ...query, surface: 'companions' }, signal());
    if (result.state !== 'ready') throw new Error('Expected companion accounting');
    expect(result.view.totals).toEqual(mapTotals(payload.total, payload.meta.availability));
    expect(result.view.groups.map((group) => group.id)).toEqual([null, 'mnemory']);
    expect(result.view.companions?.map((group) => group.service)).toEqual(['mnemory']);
    expect(result.view.groups[1].label).toBe('Memory');
    expect(result.view.groups[0].label).toBe('Unallocated group');
  });
  it('retains selected-service trends on explicit incomplete ranking, but never on authorization failure', async () => {
    for (const denied of [false, true]) {
      const { send, calls } = transport((_path, body) => {
        if (body.view === 'ranking') throw new ApiError('Private response', { status: denied ? 403 : 422, code: denied ? 'usage_access_denied' : 'incomplete_cost_ranking' });
        return response();
      });
      const result = await createUsageDataSource(send).query({ ...query, surface: 'companions', serviceId: 'fixture-service' }, signal());
      expect(calls.map((call) => call.body.view)).toEqual([undefined, 'trend', 'ranking']);
      expect(calls[2].body.filters).toEqual(calls[1].body.filters);
      if (denied) expect(result.state).toBe('error');
      else {
        if (result.state !== 'ready') throw new Error('Expected known trend with explicit ranking refusal');
        expect(result.view.buckets).toHaveLength(1);
        expect(result.view.groups).toEqual([]);
        expect(result.view.sectionErrors?.[0]).toContain('not priced');
      }
      expect(JSON.stringify(result)).not.toContain('Private response');
    }
  });
  it('loads physical attempts with a scope catalogue and one nested-filter POST, without hidden aggregates', async () => {
    const { send, calls } = transport(() => ({ meta: meta(), items: [attempt()], next_cursor: 'opaque-next' }));
    const result = await createUsageDataSource(send).query({ ...query, surface: 'explorer', explorerMode: 'attempts', state: 'failed', sessionId: 'fixture-cognis-session', cursor: 'opaque-current', preciseStart: '2026-10-01T01:00:00Z', preciseEnd: '2026-10-01T02:00:00Z' }, signal());
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ path: '/api/v1/usage/attempts/search', body: { filters: { states: ['failed'], session_ids: ['fixture-cognis-session'] }, start: '2026-10-01T01:00:00.000Z', cursor: 'opaque-current' } });
    if (result.state === 'ready') { expect(result.view.totals).toBeNull(); expect(result.view.requestPage?.rows).toHaveLength(1); }
  });
  it('does not return sensitive identifiers in project mode or accept a mismatched scope', async () => {
    const project = { ...query, mode: 'project' as const, projectScope: { id: 'fixture-project', label: 'Fixture project' }, clientId: 'must-not-send', keyId: 'must-not-send' };
    const mapped = mapAttempt(attempt(), project);
    expect(mapped.detail?.identifiers).toBeNull();
    expect(() => wireQuery(project)).toThrow('private');
    const { send } = transport(() => response());
    expect((await createUsageDataSource(send).query(project, signal())).state).toBe('error');
  });
  it('keeps multiple cost bands and unknown values distinct, without default currency, price or client receipt', () => {
    const m = metrics();
    m.tokens.output = null; m.costs = [];
    expect(mapTotals(m)).toMatchObject({ output: null, unpriced: 2, cost: { amount: null, currency: null }, costs: [] });
    const record = attempt();
    expect(mapAttempt(record, query).detail?.timings.clientSources).toBeUndefined();
    expect(mapAttempt(record, query).detail?.timings.clientLatency).toEqual({ totalMs: null, samples: 0 });
    record.logical_timing!.client = { source: 'cognis_ui', clock: 'client_monotonic', correlation_verified: true, first_chunk_seconds: 1.5, last_chunk_seconds: 4.5 };
    expect(mapAttempt(record, query).detail?.timings.clientSources).toEqual([{ source: 'cognis_ui · client_monotonic', first: { totalMs: 1500, samples: 1 }, last: { totalMs: 4500, samples: 1 } }]);
    record.parameters!.requested.seed = { value: 5, provenance: 'explicit' };
    record.parameters!.effective.seed = { value: null, provenance: 'unknown' };
    expect(mapAttempt(record, query).detail?.parameters.find((p) => p.name === 'seed')?.effective).toBeNull();
    expect(mapAttempt(record, query)).toMatchObject({ cycleId: record.cycle_id, source: 'Not exposed for this attempt', sessionId: null, serviceId: 'Mnemory', operation: 'memory.extract', status: 'failed' });
    // Local midnight after the last selected day (Europe/Prague, UTC+1).
    expect(wireQuery(query).end).toBe('2026-10-29T23:00:00.000Z');
  });
  it('discards detail explicitly expired by retention, even if a malformed page includes a row', async () => {
    const m = meta(); m.availability!.resolution = 'detail_expired';
    const { send } = transport(() => ({ meta: m, items: [attempt()], next_cursor: 'not-followed' }));
    expect(await createUsageDataSource(send).attempts(query, {}, null, signal())).toMatchObject({ rows: [], nextCursor: null, availability: { resolution: 'detail_expired' } });
  });
  it('batches current-page summary selectors once with explicit personal/self scope', async () => {
    const measured = metrics();
    measured.costs[0].known_amount = '9007199254740993.010000000001';
    const { send, calls } = transport(() => ({ meta: meta(), items: [{ selector: { kind: 'conversation', id: 'visible' }, available: true, inclusion: 'self', metrics: measured }] }));
    const result = await createUsageDataSource(send).sidebar([{ kind: 'conversation', id: 'visible' }], signal());
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/api/v1/usage/session-summaries');
    expect(calls[0].body).toMatchObject({ scope: { mode: 'personal' }, inclusion: 'self', items: [{ kind: 'conversation', id: 'visible' }] });
    expect(result['conversation:visible']?.cost.amount).toBe('9007199254740993.010000000001');
  });
});
