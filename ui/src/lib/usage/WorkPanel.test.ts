import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UsageDataSource } from './api';
import { fixtureQuery } from './fixtures';
import type { UsageGroup } from './model';
import WorkPanel from './WorkPanel.svelte';
import { ApiError } from '$lib/api/client';

afterEach(cleanup);
const cost = (amount: string | null) => ({ amount, currency: 'USD', basis: 'api_estimate' as const });
function group(id: string | null, label: string, amount: string | null, attempts: number, unpriced = 0): UsageGroup {
  return { id, label, attempts, input: 1000, output: 100, cacheRead: null, reasoning: null, cost: cost(amount), costs: [cost(amount)], unpriced } as unknown as UsageGroup;
}

describe('work cost ranking', () => {
  it('ranks scheduled tasks, one-off tasks and conversations and drills into their requests', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async (_query, kind) => kind === 'schedule'
      ? [group('sched_1', 'Daily brief', '2.50', 10), group('sched_2', 'Inbox cleanup', '0.40', 4, 1)]
      : kind === 'task' ? [group('task_1', 'Refactor usage', '1.20', 6)] : [group('conv_1', 'Usage dashboard', '9.00', 30)]);
    const onrow = vi.fn();
    render(WorkPanel, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource, onrow });
    const table = await screen.findByRole('table', { name: 'Schedule cost ranking' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toMatch(/Daily brief.*\$2\.50.*10.*\$0\.25/);
    expect(rows[1].textContent).toContain('+?');
    await fireEvent.click(within(table).getByRole('button', { name: 'Daily brief' }));
    expect(onrow).toHaveBeenCalledWith({ schedule_ids: ['sched_1'] }, 'Schedule: Daily brief');

    await fireEvent.click(screen.getByRole('radio', { name: 'One-off tasks' }));
    await waitFor(() => expect(ranking).toHaveBeenLastCalledWith(expect.anything(), 'task', {}, { task_origin: 'one_off' }, expect.anything()));
    await fireEvent.click(screen.getByRole('radio', { name: 'Conversations' }));
    const conversations = await screen.findByRole('table', { name: 'Conversation cost ranking' });
    expect(within(conversations).getByRole('button', { name: 'Usage dashboard' })).toBeTruthy();
    // The share donut drills the same way as the table.
    await fireEvent.click(within(screen.getByRole('region', { name: 'Cost share by conversation' })).getByRole('button', { name: 'Usage dashboard' }));
    expect(onrow).toHaveBeenLastCalledWith({ conversation_ids: ['conv_1'] }, 'Conversation: Usage dashboard');
  });

  it('splits the range total into conversations, scheduled and one-off tasks from exact empty groups', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async (_query, kind) => kind === 'task'
      ? [group(null, 'Not a task', '6.00', 50), group('task_1', 'Refactor usage', '3.00', 6)]
      : kind === 'schedule' ? [group(null, 'Not scheduled', '7.00', 55), group('sched_1', 'Daily brief', '3.00', 10)]
      : kind === 'provider' ? [group('anthropic', 'anthropic', '8.00', 40), group('codex', 'codex', '2.00', 26)] : []);
    render(WorkPanel, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource, onrow: vi.fn() });
    const donut = await screen.findByRole('img', { name: /Estimated cost by work type/ });
    // 10 total: 6 outside tasks, 10 - 7 = 3 scheduled, remaining 1 one-off.
    expect(donut.getAttribute('aria-label')).toBe('Estimated cost by work type: Conversations $6.00 (60%), Scheduled tasks $3.00 (30%), One-off tasks $1.00 (10%)');
  });

  it('states an empty range plainly and surfaces load errors', async () => {
    const ranking = vi.fn<NonNullable<UsageDataSource['ranking']>>(async () => []);
    const { unmount } = render(WorkPanel, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource, onrow: vi.fn() });
    expect(await screen.findByText('No scheduled task ran in this range')).toBeTruthy();
    unmount();
    const failing = vi.fn<NonNullable<UsageDataSource['ranking']>>(async () => { throw new ApiError('Older work details expired. Choose a recent range.', {
      status: 422, code: 'resource_detail_expired', details: { operation: 'Load usage', correlation_id: 'work-test-17' }
    }); });
    render(WorkPanel, { query: fixtureQuery, source: { ranking: failing } as unknown as UsageDataSource, onrow: vi.fn() });
    expect(await screen.findByText(/Older work details expired.*Reference work-test-17/)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(failing.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});
