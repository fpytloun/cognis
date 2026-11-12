import { describe, expect, it, vi } from 'vitest';
import type { UsageSummaryResponse } from '$lib/types/api';
import { meta, metrics } from '$lib/usage/wire.test-data';
import { summaryResult, loadConversationUsage } from './conversation-usage-source';

const transport = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('$lib/usage/conversation-api', () => ({ fetchUsageSummaries: transport.fetch }));
const target = { kind: 'conversation', id: 'whole-retained-history' } as const;
function response(): UsageSummaryResponse {
  return {
    meta: meta(), items: [{
      selector: target, inclusion: 'direct', available: true,
      metrics: metrics(), physical_sessions: 2,
    }],
  };
}
describe('production conversation usage adapter', () => {
  it('preserves exact partial amounts, separate bases/currencies, nulls and represented sessions', () => {
    const data = response();
    data.items[0].metrics!.costs.push({
      known_amount: '0', currency: 'EUR', basis: 'configured_equivalent', quality: 'complete',
      priced_attempts: 1, unpriced_attempts: 0,
    });
    expect(summaryResult(data, target, 'direct')).toMatchObject({
      status: 'ready', summary: {
        coverage: 'partial', representedSessions: 2,
        costs: [
          { amount: '0.123456789012345678', currency: 'USD', basis: 'api_estimate', complete: false },
          { amount: '0', currency: 'EUR', basis: 'configured_equivalent', complete: true },
        ],
        tokens: { cacheWrite: null, reasoning: null },
      },
    });
  });
  it('treats null/unavailable as unavailable, never as zero', () => {
    const data = response();
    data.items[0].metrics = null;
    expect(summaryResult(data, target, 'direct').status).toBe('unavailable');
    data.items[0].metrics = metrics();
    data.items[0].available = false;
    expect(summaryResult(data, target, 'direct').status).toBe('unavailable');
  });
  it('does not promote no observations into measured zero attempts', () => {
    const data = response();
    const values = data.items[0].metrics!;
    values.observed_attempts = 0;
    values.finalized_attempts = 0;
    values.costs = [];
    values.states.succeeded = 0;
    values.states.failed = 0;
    data.items[0].physical_sessions = null;
    expect(summaryResult(data, target, 'direct')).toMatchObject({
      status: 'ready', summary: {
        coverage: 'unknown', unpricedAttempts: null, representedSessions: null,
        attempts: { total: null, succeeded: null, failed: null }, costs: [],
      },
    });
  });
  it('rejects mismatched selectors, inclusion and cardinality without a broader read', () => {
    const data = response();
    expect(() => summaryResult(data, target, 'inclusive')).toThrow();
    data.items[0].selector = { kind: 'session', id: target.id };
    expect(() => summaryResult(data, target, 'direct')).toThrow();
    data.items = [];
    expect(() => summaryResult(data, target, 'direct')).toThrow();
  });
  it('sends whole-conversation historical direct, forwards abort and propagates denial', async () => {
    const controller = new AbortController();
    transport.fetch.mockResolvedValueOnce(response());
    await loadConversationUsage(target, 'direct', controller.signal);
    expect(transport.fetch).toHaveBeenLastCalledWith({
      items: [target], inclusion: 'direct', pricing_basis: 'historical',
    }, { signal: controller.signal });
    transport.fetch.mockRejectedValueOnce(new Error('Denied'));
    await expect(loadConversationUsage(target, 'inclusive', controller.signal)).rejects.toThrow('Denied');
    controller.abort();
    await expect(loadConversationUsage(target, 'direct', controller.signal)).rejects.toThrow();
    expect(transport.fetch).toHaveBeenCalledTimes(2);
  });
});
