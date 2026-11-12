import { beforeEach, describe, expect, it, vi } from 'vitest';
import { metrics } from './wire.test-data';

// Tests run in Europe/Prague (vite.config.ts): November is UTC+1.
const calls: Record<string, unknown>[] = [];
const failing = new Set<string>();
vi.mock('$lib/api/client', () => ({
  request: vi.fn(async (path: string, options: { body: string }) => {
    const body = JSON.parse(options.body);
    calls.push({ ...body, path });
    if (failing.has(String(body.group_by ?? path))) throw new Error('unavailable');
    if (path === '/api/v1/usage/attempts/search') return { meta: {}, next_cursor: null, items: [
      // One image request refused for billing and retried three times, one stopped turn.
      ...[0, 1, 2, 3].map(() => ({ state: 'failed', status_code: 402, error_category: 'invalid_request', resolved_model: 'gemini-nano-banana', requested_model: null })),
      { state: 'cancelled', status_code: null, error_category: 'cancelled', resolved_model: 'gpt-6.1-sol', requested_model: null }
    ] };
    const withCost = (amount: string, attempts: number) => {
      const row = metrics();
      row.finalized_attempts = attempts;
      row.costs = [{ ...row.costs[0], currency: 'USD', known_amount: amount }];
      row.unpriced_attempts = 0;
      row.unknown_usage_attempts = 0;
      return row;
    };
    if (body.view === 'ranking' && body.group_by === 'conversation') return { meta: {}, total: null, buckets: [], groups: [
      { key: 'conv_1', label: 'Cost tracking design ideas', metrics: withCost('2.476954810000', 40) },
      { key: 'conv_child', label: 'Usage: attribute cost', metrics: withCost('0.5', 5) }
    ] };
    if (body.view === 'ranking' && body.group_by === 'purpose') return { meta: {}, total: null, buckets: [], groups: [
      { key: 'compaction', label: null, metrics: withCost('0.2', 2) }, { key: null, label: null, metrics: withCost('0', 1) }
    ] };
    if (body.view === 'ranking' && body.group_by === 'model') {
      // gpt-image-2: 3 unpriced calls, 2 of them without any usage (failed before a response).
      const image = withCost('0', 3);
      image.unpriced_attempts = 3;
      image.unknown_usage_attempts = 2;
      return { meta: {}, total: null, buckets: [], groups: [
        { key: 'claude-opus-5-5', label: null, metrics: withCost('2.98', 45) }, { key: 'gpt-image-2', label: null, metrics: image }
      ] };
    }
    if (body.view === 'ranking') return { meta: {}, total: null, buckets: [], groups: [
      { key: body.group_by === 'agent' ? 'agent_laforge' : 'claude-opus-5-5', label: body.group_by === 'agent' ? 'LaForge' : null, metrics: withCost('2.98', 45) },
      { key: null, label: null, metrics: withCost('0', 1) }
    ] };
    // Hourly server buckets: 23:00Z on Nov 3 is 00:00 on Nov 4 in Prague.
    return { meta: {}, total: null, groups: [], buckets: [
      { start: '2026-11-03T23:00:00.000Z', end: '2026-11-04T00:00:00.000Z', metrics: withCost('1', 10) },
      { start: '2026-11-04T10:00:00.000Z', end: '2026-11-04T11:00:00.000Z', metrics: withCost('2', 5) }
    ] };
  })
}));

const { loadConversationBreakdown } = await import('./conversation-breakdown');
const now = new Date('2026-11-05T12:20:00Z');

describe('conversation usage breakdown', () => {
  beforeEach(() => { calls.length = 0; failing.clear(); });
  it('fits the chart to the conversation lifetime in local hours and groups by conversation, agent and model', async () => {
    const result = await loadConversationBreakdown('conv_1', 'inclusive', new AbortController().signal, 'auto', now);
    expect(calls[0]).toMatchObject({ view: 'trend', bucket: 'hour', filters: { conversation_tree: { id: 'conv_1', inclusion: 'inclusive' } } });
    expect(calls.filter((c) => c.view === 'trend')).toHaveLength(1);
    // The server rejects hourly windows longer than 90 days.
    for (const call of calls) expect(Date.parse(String(call.end)) - Date.parse(String(call.start))).toBeLessThanOrEqual(90 * 86_400_000);
    // The purpose split is read only because the agents include system work.
    expect(calls.filter((c) => c.view === 'ranking').map((c) => c.group_by).sort()).toEqual(['agent', 'conversation', 'model', 'purpose']);
    expect(result.unit).toBe('hour');
    // Continuous hourly axis from first activity to the current hour, with zeros between.
    expect(result.points[0]).toMatchObject({ start: '2026-11-03T23:00:00.000Z', cost: 1 });
    expect(result.points[1].cost).toBe(0);
    expect(result.points.find((p) => p.start === '2026-11-04T10:00:00.000Z')?.cost).toBe(2);
    expect(result.points.at(-1)!.start).toBe('2026-11-05T12:00:00.000Z');
    // Compacted sessions of one chat are one conversation row.
    expect(result.conversations.map((s) => [s.label, s.cost])).toEqual([['Cost tracking design ideas', '$2.48'], ['Usage: attribute cost', '$0.50']]);
    expect(result.agents.map((s) => s.label)).toEqual(['LaForge', 'Cognis system']);
    // System work (no agent) is split by what it was for.
    expect(calls.find((c) => c.group_by === 'purpose')).toMatchObject({ filters: { agent_scope: 'system' } });
    expect(result.system.map((s) => s.label)).toEqual(['Context compaction', 'Other']);
    // Calls that failed before any usage have nothing to price.
    expect(result.models.map((s) => [s.label, s.unpriced])).toEqual([['claude-opus-5-5', 0], ['gpt-image-2', 1]]);
    // Failures of the listed conversations, grouped by model and reason.
    expect(calls.find((c) => c.path === '/api/v1/usage/attempts/search')).toMatchObject({ filters: { conversation_ids: ['conv_1', 'conv_child'], states: ['failed', 'cancelled'] } });
    expect(result.issues.map((i) => [i.model, i.reason, i.calls, i.failed])).toEqual([
      ['gemini-nano-banana', 'Provider billing (payment required)', 4, true], ['gpt-6.1-sol', 'Stopped', 1, false]]);
    expect(result.missing).toEqual([]);
  });
  it('reports a section whose read failed instead of returning it empty', async () => {
    failing.add('conversation');
    const result = await loadConversationBreakdown('conv_1', 'inclusive', new AbortController().signal, 'auto', now);
    expect(result.missing).toEqual(['conversations', 'issues']);
  });
  it('sums hours into local days for longer ranges and skips the conversation ranking without children', async () => {
    const result = await loadConversationBreakdown('conv_1', 'direct', new AbortController().signal, '7d', now);
    // Local midnight Oct 29 (Prague, UTC+1) is 23:00Z on Oct 28.
    expect(calls.find((c) => c.view === 'trend')).toMatchObject({ bucket: 'hour', start: '2026-10-28T23:00:00.000Z' });
    expect(calls.some((c) => c.group_by === 'conversation')).toBe(false);
    expect(result.unit).toBe('day');
    expect(result.points).toHaveLength(8);
    // Both hours fall on the local day Nov 4.
    expect(result.points.find((p) => p.start === '2026-11-03T23:00:00.000Z')?.cost).toBe(3);
  });
});
