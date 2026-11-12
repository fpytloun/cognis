import { render, screen, within } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import SystemWork from './SystemWork.svelte';
import type { UsageDataSource } from './api';
import { fixtureQuery } from './fixtures';
import type { UsageGroup } from './model';

const group = (id: string | null, amount: string, attempts: number): UsageGroup =>
  ({ id, label: id ?? '', attempts, input: 100, cost: { amount, currency: 'USD', basis: 'api_estimate' },
    costs: [{ amount, currency: 'USD', basis: 'api_estimate' }] }) as unknown as UsageGroup;

describe('Cognis system work', () => {
  it('names what agentless requests were for and which service made them', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async (_query, dimension, filters, scoped) => {
      expect(scoped).toEqual({ agent_scope: 'system' });
      if (dimension === 'service') return [group('intaris', '2.00', 120), group(null, '5.00', 10)];
      if (filters.service_ids?.[0] === 'intaris') return [group('systemone', '1.90', 100), group('compaction', '0.10', 20)];
      // All system purposes: compaction also ran inside Cognis itself.
      return [group('compaction', '6.00', 30), group('systemone', '1.90', 100)];
    });
    render(SystemWork, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource });
    const table = await screen.findByRole('table', { name: 'Cognis system work by function' });
    const compaction = (await within(table).findByText('Context compaction')).closest('tr')!;
    expect(within(compaction).getByText('Intaris, Cognis')).toBeTruthy();
    expect(within(compaction).getByText('$6.00')).toBeTruthy();
    const guardrails = within(table).getByText('Guardrail checks').closest('tr')!;
    expect(within(guardrails).getByText('Intaris')).toBeTruthy();
  });
});
