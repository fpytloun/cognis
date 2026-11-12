import { expect, it } from 'vitest';
import { protocolFixture } from '../../../e2e/usage/protocol-fixture';
import { createUsageDataSource } from './adapter';
import { fixtureQuery } from './fixtures';
import type * as W from './wire';

it('screenshot totals reconcile with the actual trend and grouped series', async () => {
  const fixture = protocolFixture(() => 'ready');
  await createUsageDataSource(fixture.send).query(fixtureQuery, new AbortController().signal);
  const packets = fixture.packets.filter((packet) => packet.schema === 'UsageQueryResponse');
  expect(packets.length).toBeGreaterThan(0);
  for (const packet of packets) {
    const value = packet.value as W.QueryResponse;
    if (!value.total) throw new Error('The ready fixture must have aggregate totals');
    expect(value.total.finalized_attempts).toBe(value.buckets.reduce((sum, bucket) => sum + bucket.metrics.finalized_attempts, 0));
    expect(value.total.finalized_attempts).toBe(value.groups.reduce((sum, group) => sum + group.metrics.finalized_attempts, 0));
    expect(Number(value.total.costs[0].known_amount)).toBe(value.buckets.reduce((sum, bucket) => sum + Number(bucket.metrics.costs[0].known_amount), 0));
    for (const dimension of value.cost_breakdowns?.dimensions ?? []) {
      expect(value.total.finalized_attempts).toBe(dimension.groups.reduce((sum, group) => sum + group.metrics.finalized_attempts, 0));
    }
  }
});

it('the synthetic current request log folds attempts and preserves private detail only in personal scope', async () => {
  const source = createUsageDataSource(protocolFixture(() => 'ready').send);
  const page = await source.requestLog!(fixtureQuery, {}, {}, null, new AbortController().signal);
  expect(page.items).toHaveLength(2);
  expect(page.items[0].attempt_count).toBe(2);
  expect(page.items[0].app_name).toBe('Fixture SDK');
});

it('synthetic project request detail also follows the private-field reduction', async () => {
  const fixture = protocolFixture(() => 'ready');
  const detail = await fixture.send<W.RequestLogDetail>('/api/v1/usage/requests/detail', {
    method: 'POST', body: JSON.stringify({
      scope: { mode: 'project', project_id: 'fixture-project' },
      start: '2026-11-01T00:00:00Z', end: '2026-11-02T00:00:00Z',
      filters: {}, selection_ref: 'v2.usage-contract-logical-1'
    })
  });
  expect(detail.attempts.length).toBeGreaterThan(0);
  expect(detail.request?.client_id).toBeUndefined();
  expect(detail.attempts[0].subject_email).toBeNull();
  expect(JSON.stringify(detail)).not.toMatch(/private-client|private-key|private-request/);
});
