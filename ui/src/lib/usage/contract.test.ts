import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { ApiError } from '$lib/api/client';
import { createUsageDataSource, type UsageTransport } from './adapter';
import { fixtureQuery } from './fixtures';
import { backUsage, drillUsage, readUsageQuery, restoreUsageHistory, usageHistoryState, writeUsageQuery } from './query-state';
import { detailBody, intersectFilters, wireQuery } from './query-contract';
import { catalog, attempt, meta, requestPage, requestRef, response, sessionPage, sessionRef } from './wire.test-data';
import type { UsageQuery } from './model';
import type * as W from './wire';

const own: UsageQuery = { ...fixtureQuery, mode: 'personal' };
const signal = () => new AbortController().signal;
const now = new Date('2026-10-29T20:00:00Z');
const origin = new URL('http://localhost/usage');
function validate(packets: { schema: string; value: unknown }[]) {
  const result = spawnSync('python3', ['e2e/usage/validate-fixtures.py'], { input: JSON.stringify(packets), encoding: 'utf8' });
  expect(result.error).toBeUndefined();
  expect(result.status, result.stderr).toBe(0);
}
function setup(override?: (path: string, body: W.DetailBody) => unknown) {
  const calls: { path: string; body: W.DetailBody; method?: string }[] = [];
  const send: UsageTransport = async <T>(path: string, options: Parameters<UsageTransport>[1] = {}): Promise<T> => {
    const body = JSON.parse(options.body as string) as W.DetailBody;
    calls.push({ path, body, method: options.method });
    const changed = override?.(path, body);
    if (changed !== undefined) return changed as T;
    if (path === '/api/v1/usage/catalog') return catalog(body.scope) as T;
    if (path === '/api/v1/usage/sessions') return sessionPage((body as W.SessionBody).session_type, body.scope) as T;
    if (path === '/api/v1/usage/requests') return requestPage(body.scope) as T;
    if (path === '/api/v1/usage/attempts/search') return { meta: { ...meta(), scope: body.scope }, items: [attempt()], next_cursor: null } as T;
    return { ...response(), meta: { ...meta(), scope: body.scope } } as T;
  };
  return { source: createUsageDataSource(send), calls };
}
describe('hash-frozen public usage interface (synthetic fixtures, not a shipping backend)', () => {
  it('validates both source DTOs, distinct request paging, nullable totals and the emitted POST bodies against JSON Schema', async () => {
    const fixture = setup();
    await fixture.source.query(own, signal());
    await fixture.source.query({ ...own, surface: 'costs', groupBy: 'provider' }, signal());
    for (const kind of ['cognis', 'proxy'] as const) {
      await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'sessions', sessionType: kind, sessionSearch: 'usage_fixture_unverified_label' }, signal());
    }
    await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'requests', selectionRef: sessionRef, requiresSelection: true }, signal());
    await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'attempts', errorCategories: ['rate_limit', 'timeout'], logicalRequestIds: ['usage-fixture-logical-1'], selectionRef: requestRef }, signal());
    const schema: Record<string, string> = {
      '/api/v1/usage/catalog': 'UsageCatalogRequest', '/api/v1/usage/query': 'UsageQuery',
      '/api/v1/usage/sessions': 'UsageSessionQuery', '/api/v1/usage/requests': 'UsageAttemptQuery', '/api/v1/usage/attempts/search': 'UsageAttemptQuery',
    };
    validate([
      ...fixture.calls.map((call) => ({ schema: schema[call.path], value: call.body })),
      { schema: 'UsageCatalog', value: catalog() }, { schema: 'UsageQueryResponse', value: response() },
      { schema: 'UsageQueryResponse', value: { ...response(), total: null } },
      { schema: 'UsageSessionPage', value: sessionPage() }, { schema: 'UsageSessionPage', value: sessionPage('proxy') },
      { schema: 'UsageSessionPage', value: sessionPage('proxy', { mode: 'project', project_id: 'usage-fixture-project' }) },
      { schema: 'UsageRequestPage', value: requestPage() }, { schema: 'UsageRequestPage', value: requestPage({ mode: 'project', project_id: 'usage-fixture-project' }) },
      { schema: 'UsageAttemptPage', value: { meta: meta(), items: [attempt()], next_cursor: null } },
    ]);
    expect(fixture.calls.every((call) => call.method === 'POST' && !call.path.includes('?'))).toBe(true);
  });
  it('maps real source/totals fields and never uses a proxy label or ref as Cognis session_id', async () => {
    const fixture = setup();
    const result = await fixture.source.query({ ...own, surface: 'explorer', sessionType: 'proxy' }, signal());
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') return;
    expect(result.view.sessions[0]).toMatchObject({ id: null, sessionType: 'proxy', label: 'usage_fixture_unverified_label', totalsScope: 'retained_detail', selectionRef: sessionRef });
    const next = drillUsage(own, { selectionRef: result.view.sessions[0].selectionRef!, requiresSelection: true, detailFilters: result.view.sessions[0].drillFilters }, 'Protected session selection', 'session');
    expect(detailBody(next)).toMatchObject({ selection_ref: sessionRef });
    expect(detailBody(next).filters.session_ids).toBeUndefined();
  });
  it('keeps request observations distinct from physical attempts and does not infer a logical failure', async () => {
    const fixture = setup();
    const result = await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'requests' }, signal());
    expect(fixture.calls.map((call) => call.path)).toEqual(['/api/v1/usage/catalog', '/api/v1/usage/requests']);
    if (result.state !== 'ready') throw new Error('Expected request observations');
    expect(result.view.requestPage).toBeUndefined();
    expect(result.view.logicalRequestPage).toMatchObject({ paginationUnit: 'attempt', rows: [{ requestId: 'usage-fixture-logical-1', logicalState: null, attemptState: 'failed', selectionRef: requestRef }] });
    await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'attempts', selectionRef: requestRef, cursor: 'usage-fixture-attempt-cursor', state: 'failed', logicalRequestIds: ['usage-fixture-logical-1'], errorCategories: ['rate_limit'] }, signal());
    expect(fixture.calls.at(-1)).toMatchObject({ path: '/api/v1/usage/attempts/search', method: 'POST', body: { selection_ref: requestRef, cursor: 'usage-fixture-attempt-cursor', filters: { states: ['failed'], logical_request_ids: ['usage-fixture-logical-1'], error_categories: ['rate_limit'] } } });
  });
  it('refuses to widen an exact hour into a UTC-day session summary, but supports exact request drill', async () => {
    const fixture = setup();
    const q: UsageQuery = { ...own, surface: 'explorer', sessionType: 'proxy', preciseStart: '2026-10-29T01:00:00Z', preciseEnd: '2026-10-29T02:00:00Z' };
    expect(await fixture.source.query(q, signal())).toMatchObject({ state: 'error', message: expect.stringContaining('preserved, not widened') });
    expect(fixture.calls.map((call) => call.path)).toEqual(['/api/v1/usage/catalog']);
    await fixture.source.query({ ...q, explorerMode: 'requests', selectionRef: sessionRef }, signal());
    expect(fixture.calls.at(-1)?.body).toMatchObject({ start: '2026-10-29T01:00:00.000Z', end: '2026-10-29T02:00:00.000Z', selection_ref: sessionRef });
  });
  it('does not discard unsupported filters, missing refs or unresolved searches to obtain broader data', async () => {
    const fixture = setup((path, body) => path === '/api/v1/usage/catalog' ? { ...catalog(body.scope), detail_filters: ['provider_ids'] } : undefined);
    expect(await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'requests', logicalRequestIds: ['usage-fixture-logical-1'] }, signal())).toMatchObject({ state: 'error', message: expect.stringContaining('no filter was discarded') });
    expect(fixture.calls).toHaveLength(1);
    for (const patch of [{ requiresSelection: true }, { requiresCursor: true }, { sessionSearch: 'usage_fixture_unverified_label' }]) {
      const sample = setup();
      expect((await sample.source.query({ ...own, surface: 'explorer', explorerMode: 'requests', ...patch }, signal())).state).toBe('error');
      expect(sample.calls.every((call) => call.path === '/api/v1/usage/catalog')).toBe(true);
    }
  });
  it.each([400, 410, 422, 403])('does not retry or expose token-bearing server error bodies after status %i', async (status) => {
    const fixture = setup((path) => {
      if (path !== '/api/v1/usage/catalog') throw new ApiError(`Rejected protected ref ${sessionRef}`, { status });
    });
    const result = await fixture.source.query({ ...own, surface: 'explorer', explorerMode: 'requests', selectionRef: sessionRef }, signal());
    expect(result.state).toBe('error');
    expect(JSON.stringify(result)).not.toContain(sessionRef);
    expect(fixture.calls.map((call) => call.path)).toEqual(['/api/v1/usage/catalog', '/api/v1/usage/requests']);
  });
  it('checks fresh catalogue scope and does not treat a missing global index as normal shipping data', async () => {
    const mismatch = setup((path) => path === '/api/v1/usage/catalog' ? catalog({ mode: 'admin' }) : undefined);
    expect((await mismatch.source.query(own, signal())).state).toBe('error');
    expect(mismatch.calls).toHaveLength(1);
    const unavailable = setup((path) => { if (path === '/api/v1/usage/query') throw new ApiError('Compatibility fixture: missing global index', { status: 503 }); });
    expect((await unavailable.source.query({ ...own, mode: 'admin' }, signal())).state).toBe('error');
    const actualIndex = setup();
    const result = await actualIndex.source.query({ ...own, mode: 'admin' }, signal());
    expect(result.state).toBe('ready');
  });
  it.each([{ selectionRef: sessionRef }, { requiresSelection: true }, { sessionSearch: 'usage_fixture_private_label' }, { requiresSessionSearch: true }])('refuses auxiliary facets rather than broadening a protected or unresolved population: %o', async (patch) => {
    const fixture = setup();
    await expect(fixture.source.facets!({ ...own, ...patch }, 'provider', signal())).rejects.toThrow('A request selection is active');
    expect(fixture.calls).toEqual([]);
  });
  it('intersects all server drill filters and fails closed on a disjoint population', () => {
    expect(intersectFilters({ provider_ids: ['usage-fixture-provider'] }, { provider_ids: ['usage-fixture-provider'], logical_request_ids: ['usage-fixture-logical-1'], cycle_ids: null })).toMatchObject({ provider_ids: ['usage-fixture-provider'], logical_request_ids: ['usage-fixture-logical-1'] });
    expect(() => intersectFilters({ states: ['succeeded'] }, { states: ['failed'] })).toThrow('do not intersect');
    expect(() => wireQuery({ ...own, mode: 'project', clientId: 'private-client' })).toThrow('private');
  });
});

describe('URL filters and protected client history', () => {
  it('round-trips actual error categories and logical IDs, preserving parent filters and exact bounds', () => {
    const query: UsageQuery = { ...own, errorCategories: ['rate_limit', 'timeout'], logicalRequestIds: ['usage-fixture-logical-1', 'usage-fixture-logical-2'],
      preciseStart: '2026-10-29T01:00:00Z', preciseEnd: '2026-10-29T02:00:00Z', state: 'failed', serviceId: 'Mnemory' };
    const restored = readUsageQuery(writeUsageQuery(origin, query), now);
    expect(restored).toMatchObject({ errorCategories: query.errorCategories, logicalRequestIds: query.logicalRequestIds, filters: query.filters, state: 'failed', serviceId: 'Mnemory', preciseStart: query.preciseStart, preciseEnd: query.preciseEnd });
    expect(wireQuery(restored).filters).toMatchObject({ error_categories: query.errorCategories, logical_request_ids: query.logicalRequestIds });
  });
  it('keeps refs, cursors, private proxy search and labels out of URLs and binds history to query/authority', () => {
    const query: UsageQuery = { ...own, surface: 'explorer', explorerMode: 'requests', sessionType: 'proxy', selectionType: 'proxy',
      selectionRef: sessionRef, requiresSelection: true, cursor: 'usage-fixture-protected-cursor', sessionSearch: 'usage_fixture_private_label', contextLabel: 'Private fixture label' };
    const url = writeUsageQuery(origin, query);
    for (const value of [sessionRef, query.cursor!, query.sessionSearch!, query.contextLabel!]) expect(url.href).not.toContain(encodeURIComponent(value));
    const marker = readUsageQuery(url, now);
    expect(marker).toMatchObject({ requiresSelection: true, requiresCursor: true, requiresSessionSearch: true });
    expect(() => detailBody(marker)).toThrow();
    const saved = usageHistoryState(query, 'usage-fixture@test.invalid:user');
    expect(restoreUsageHistory(marker, saved, 'usage-fixture@test.invalid:user')).toMatchObject({ selectionRef: sessionRef, cursor: query.cursor, sessionSearch: query.sessionSearch, selectionType: 'proxy' });
    expect(restoreUsageHistory(marker, saved, 'different-actor:user').selectionRef).toBeUndefined();
    const changed = { ...marker, filters: { ...marker.filters, provider: 'usage-fixture-provider' } };
    expect(restoreUsageHistory(changed, saved, 'usage-fixture@test.invalid:user').selectionRef).toBeUndefined();
  });
  it('restores nested session/request/source navigation including every drill filter and opaque cursor', () => {
    const source: UsageQuery = { ...own, surface: 'explorer', sessionType: 'proxy', sessionSearch: 'usage_fixture_private_label', cursor: 'usage-fixture-session-cursor', errorCategories: ['timeout'] };
    const request = drillUsage(source, { selectionRef: sessionRef, requiresSelection: true, sessionSearch: undefined, detailFilters: { service_ids: ['Mnemory'] } }, 'Protected session', 'session');
    const attempt = drillUsage(request, { explorerMode: 'attempts', selectionRef: requestRef, detailFilters: { logical_request_ids: ['usage-fixture-logical-1'] } }, 'Protected request', 'request');
    const requestAgain = backUsage(attempt, now)!;
    expect(requestAgain).toMatchObject({ selectionRef: sessionRef, detailFilters: { service_ids: ['Mnemory'] }, errorCategories: ['timeout'] });
    const sourceAgain = backUsage(requestAgain, now)!;
    expect(sourceAgain).toMatchObject({ surface: 'explorer', sessionType: 'proxy', sessionSearch: source.sessionSearch, cursor: source.cursor, errorCategories: ['timeout'] });
    expect(wireQuery(drillUsage(own, { filters: { provider: 'usage-fixture-provider' } }, 'Provider', 'input')).filters).toMatchObject({ provider_ids: ['usage-fixture-provider'] });
  });
});
