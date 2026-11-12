import { describe, expect, it, vi } from 'vitest';
import { UsageController, type UsageSnapshot } from './controller';
import { loadVisibleSummaries, type UsageDataSource } from './api';
import { fixtureAttempts, fixtureQuery, fixtureView } from './fixtures';
import type { UsageLoadState } from './model';
const ready = (): UsageLoadState => ({ state: 'ready', view: fixtureView(fixtureQuery) });
function setup() {
  let latest: UsageSnapshot;
  const source: UsageDataSource = { query: vi.fn(async () => ready()), attempts: vi.fn(async () => ({ rows: fixtureAttempts.slice(0, 2), nextCursor: 'fixture-next-page' })), sidebar: vi.fn(async () => ({})) };
  const controller = new UsageController(source, (snapshot) => latest = snapshot);
  return { controller, source, snapshot: () => latest };
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
describe('active usage boundary', () => {
  it('coalesces push invalidations and does no timer or hidden-view work', async () => {
    const { controller, source } = setup();
    controller.replace(fixtureQuery);
    await settle();
    expect(source.query).not.toHaveBeenCalled();
    controller.setActive(true);
    controller.invalidate(); controller.invalidate(); controller.invalidate();
    await settle();
    expect(source.query).toHaveBeenCalledTimes(1);
    controller.setActive(false); controller.invalidate();
    await settle();
    expect(source.query).toHaveBeenCalledTimes(1);
    controller.setActive(true);
    await settle();
    expect(source.query).toHaveBeenCalledTimes(2);
    controller.dispose();
  });
  it('discards stale responses and clears previous authority data immediately', async () => {
    const { controller, source, snapshot } = setup();
    const resolvers: ((value: UsageLoadState) => void)[] = [];
    vi.mocked(source.query).mockImplementation(() => new Promise((resolve) => resolvers.push(resolve)));
    controller.replace(fixtureQuery); controller.setActive(true); await settle();
    controller.replace({ ...fixtureQuery, mode: 'personal' }); await settle();
    expect(snapshot().result.state).toBe('loading');
    resolvers[0](ready()); await settle();
    expect(snapshot().result.state).toBe('loading');
    resolvers[1]({ state: 'error', message: 'Permission denied' }); await settle();
    expect(snapshot().result).toEqual({ state: 'error', message: 'Permission denied' });
    controller.dispose();
  });
  it('owns a deep immutable snapshot of new array filters, protected populations and back state', async () => {
    const { controller, source } = setup();
    const query = { ...fixtureQuery, logicalRequestIds: ['usage-fixture-logical-1'],
      detailFilters: { service_ids: ['Mnemory'] },
      returnState: { selectionRef: 'v2.usage-contract-session-fixture', detailFilters: { states: ['failed'] } } };
    controller.replace(query);
    query.logicalRequestIds[0] = 'usage-fixture-logical-2';
    query.detailFilters.service_ids.push('another-service');
    query.returnState.detailFilters.states[0] = 'succeeded';
    controller.setActive(true); await settle();
    expect(source.query).toHaveBeenCalledWith(expect.objectContaining({
      logicalRequestIds: ['usage-fixture-logical-1'], detailFilters: { service_ids: ['Mnemory'] },
      returnState: expect.objectContaining({ detailFilters: { states: ['failed'] } }),
    }), expect.any(AbortSignal));
    controller.dispose();
  });
  it('loads attempt pages only explicitly and clears drills on query changes', async () => {
    const { controller, source, snapshot } = setup();
    controller.replace(fixtureQuery); controller.setActive(true); await settle();
    expect(source.attempts).not.toHaveBeenCalled();
    await controller.inspect({ service: 'Mnemory', operation: 'embedding' });
    expect(source.attempts).toHaveBeenCalledWith(fixtureQuery, { service: 'Mnemory', operation: 'embedding' }, null, expect.any(AbortSignal));
    await controller.nextAttempts();
    expect(vi.mocked(source.attempts).mock.calls[1][2]).toBe('fixture-next-page');
    controller.replace({ ...fixtureQuery, mode: 'project' });
    expect(snapshot().attempts).toEqual([]);
    controller.dispose();
  });
  it('uses a single deduplicated visible-page summary request, never N+1', async () => {
    const { source } = setup();
    const signal = new AbortController().signal;
    await loadVisibleSummaries(source, [{ kind: 'conversation', id: 'fixture-conversation-01' }, { kind: 'conversation', id: 'fixture-conversation-01' }, { kind: 'activity', id: 'fixture-activity-01' }], signal);
    expect(source.sidebar).toHaveBeenCalledExactlyOnceWith([{ kind: 'conversation', id: 'fixture-conversation-01' }, { kind: 'activity', id: 'fixture-activity-01' }], signal);
    await loadVisibleSummaries(source, [], signal);
    expect(source.sidebar).toHaveBeenCalledTimes(1);
    await expect(loadVisibleSummaries(source, Array.from({ length: 101 }, (_, index) => ({ kind: 'session', id: `fixture-session-${index}` })), signal)).rejects.toThrow('exceeds 100');
    expect(source.sidebar).toHaveBeenCalledTimes(1);
  });
});
