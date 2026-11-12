import { describe, expect, it, vi } from 'vitest';
import type { UsageDataSource } from './api';
import { fixtureQuery } from './fixtures';
import { layoutFlow, loadFlow } from './flow';
import type { UsageGroup } from './model';
import type * as W from './wire';

const group = (id: string | null, label: string, amount: number): UsageGroup =>
  ({ id, label, attempts: 1, cost: { amount: String(amount), currency: 'USD', basis: 'api_estimate' }, costs: [{ amount: String(amount), currency: 'USD', basis: 'api_estimate' }] }) as unknown as UsageGroup;

describe('cost flow', () => {
  it('reads each link with its full ancestor path and folds the tail into Other', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async (_query, level, filters: W.Filters) => {
      if (level === 'model') return [group('opus', 'claude-opus', 10), group('sol', 'gpt-sol', 6), group(null, 'Unknown model', 0)];
      // Agents under each model.
      return filters.model_ids?.[0] === 'opus'
        ? [group('laforge', 'LaForge', 5), group('lumi', 'Lumi', 3), group('riker', 'Riker', 1), group('a4', 'A4', .5), group('a5', 'A5', .5)]
        : [group('laforge', 'LaForge', 4), group('lumi', 'Lumi', 2)];
    });
    const flow = await loadFlow({ ranking } as unknown as UsageDataSource, fixtureQuery, ['model', 'agent'], new AbortController().signal);
    expect(ranking.mock.calls.map((call) => [call[1], call[2]])).toEqual([['model', {}], ['agent', { model_ids: ['opus'] }], ['agent', { model_ids: ['sol'] }]]);
    const node = (key: string) => flow.nodes.find((item) => item.key === key)!;
    // A shared agent merges across models; its total is the sum of incoming links.
    expect(node('1:laforge').value).toBe(9);
    expect(node('1:other')).toMatchObject({ label: 'Other', value: .5, other: true });
    expect(flow.links.find((item) => item.source === '0:opus' && item.target === '1:lumi')?.value).toBe(3);
    // Zero-cost groups carry no flow.
    expect(flow.nodes.some((item) => item.label === 'Unknown model')).toBe(false);
    expect(flow.currency).toBe('USD');
  });
  it('lays nodes out proportionally with link widths that fill their source', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async (_q, level) =>
      level === 'model' ? [group('opus', 'claude-opus', 10)] : [group('laforge', 'LaForge', 6), group('lumi', 'Lumi', 4)]);
    const flow = await loadFlow({ ranking } as unknown as UsageDataSource, fixtureQuery, ['model', 'agent'], new AbortController().signal);
    const layout = layoutFlow(flow, 400, 200, ['red'], 10, 10);
    const opus = layout.nodes.find((item) => item.key === '0:opus')!;
    const widths = layout.links.map((item) => item.width).reduce((acc, value) => acc + value, 0);
    expect(widths).toBeCloseTo(opus.h);
    expect(layout.nodes.find((item) => item.key === '1:laforge')!.x).toBe(390);
  });
});
