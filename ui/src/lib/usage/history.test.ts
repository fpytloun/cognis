import { describe, expect, it } from 'vitest';
import { createUsageSnapshot } from './history.svelte';
import { fixtureQuery } from './fixtures';
import { detailBody } from './query-contract';
import { readUsageQuery, restoreUsageHistory, usageHistoryState, writeUsageQuery, type UsageHistoryState } from './query-state';
import { sessionRef, requestRef } from './wire.test-data';
import type { UsageQuery } from './model';

const authority = 'usage-fixture@test.invalid:user';
const now = new Date('2026-10-29T20:00:00Z');
const origin = new URL('http://localhost/usage');
const query: UsageQuery = { ...fixtureQuery, mode: 'personal', surface: 'explorer', explorerMode: 'requests',
  selectionRef: sessionRef, requiresSelection: true, sessionType: 'proxy', sessionSearch: 'usage_fixture_private_label',
  detailFilters: { provider_ids: ['usage-fixture-provider'] }, contextLabel: 'Private fixture label' };
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

describe('canonical SvelteKit usage snapshots', () => {
  it('restores bound private scope/filters/refs after SPA entry resets page.state, including another reload', () => {
    let pageState: UsageHistoryState = usageHistoryState(query, authority);
    const snapshot = createUsageSnapshot(() => pageState);
    const saved = copy(snapshot.capture());
    pageState = {};
    const reloaded = createUsageSnapshot(() => pageState);
    reloaded.restore(saved);
    const marker = readUsageQuery(writeUsageQuery(origin, query), now);
    const restored = restoreUsageHistory(marker, reloaded.state, authority);
    expect(restored).toMatchObject({ mode: 'personal', selectionRef: sessionRef, detailFilters: query.detailFilters, sessionSearch: query.sessionSearch });
    expect(detailBody(restored)).toMatchObject({ selection_ref: sessionRef, filters: { provider_ids: ['usage-fixture-provider'] } });
    const emptyEntry: UsageHistoryState = {};
    const again = createUsageSnapshot(() => emptyEntry);
    again.restore(copy(reloaded.capture()));
    expect(restoreUsageHistory(marker, again.state, authority).selectionRef).toBe(sessionRef);
  });
  it('lets current SvelteKit back/forward state override a restored snapshot instead of caching a different selection', () => {
    let pageState: UsageHistoryState = {};
    const snapshot = createUsageSnapshot(() => pageState);
    snapshot.restore(usageHistoryState(query, authority));
    const request = { ...query, selectionRef: requestRef };
    pageState = usageHistoryState(request, authority);
    const marker = readUsageQuery(writeUsageQuery(origin, request), now);
    expect(restoreUsageHistory(marker, snapshot.state, authority).selectionRef).toBe(requestRef);
    pageState = usageHistoryState(query, authority);
    expect(restoreUsageHistory(marker, snapshot.state, authority).selectionRef).toBe(sessionRef);
  });
  it('never reuses a restored fallback for a different empty entry or overrides explicit current state', () => {
    let pageState: UsageHistoryState = {};
    const snapshot = createUsageSnapshot(() => pageState);
    snapshot.restore(usageHistoryState(query, authority));
    expect(snapshot.state.usage?.context.selectionRef).toBe(sessionRef);
    pageState = {};
    expect(snapshot.state.usage).toBeUndefined();
    expect(snapshot.capture().usage).toBeUndefined();
    snapshot.restore(usageHistoryState(query, authority));
    pageState.usage = undefined;
    expect(snapshot.state.usage).toBeUndefined();
  });
  it('fails closed for fresh/missing/corrupt, other-actor and other-project state', () => {
    const project: UsageQuery = { ...query, mode: 'project', projectScope: { id: 'usage-fixture-project', label: 'Fixture' },
      filters: { ...query.filters, project: 'usage-fixture-project' } };
    const marker = readUsageQuery(writeUsageQuery(origin, project), now, project.projectScope);
    const valid = usageHistoryState(project, authority);
    for (const history of [{}, ...[undefined, null, [], 'corrupt'].map((context) => ({ usage: { ...valid.usage!, context } })), usageHistoryState(query, authority)]) {
      const restored = restoreUsageHistory(marker, history as UsageHistoryState, authority);
      expect(restored.requiresSelection).toBe(true);
      expect(() => detailBody(restored)).toThrow('no broader query was sent');
    }
    expect(() => detailBody(restoreUsageHistory(marker, valid, 'different-actor:user'))).toThrow('no broader query was sent');
  });
});
