import { beforeEach, describe, expect, it, vi } from 'vitest';
import { request } from '$lib/api/client';
import type { UsageSummaryRequest, UsageSummaryResponse } from '$lib/types/api';
import { fetchUsageSummaries } from './conversation-api';

vi.mock('$lib/api/client', () => ({ request: vi.fn() }));

const response: UsageSummaryResponse = {
  meta: {
    scope: { mode: 'personal' }, precision: 'lifetime',
    coverage_start: null, observed_through: null, complete: false,
    accounting_degraded: false, private_resources_excluded: false,
    excluded_fields: [], limitations: ['Capture coverage unknown'],
    capture: {
      state: 'unknown', epoch_started_at: null,
      dropped_observations: null, gap_incidents: null,
    },
  },
  items: [],
};

describe('raw conversation accounting transport', () => {
  beforeEach(() => {
    vi.mocked(request).mockReset();
  });

  it('forwards the exact lifetime selector, inclusion and cancellation signal', async () => {
    const body: UsageSummaryRequest = {
      items: [{ kind: 'conversation', id: 'conv_retained_history' }],
      inclusion: 'direct',
    };
    const controller = new AbortController();
    vi.mocked(request).mockResolvedValue(response);
    expect(await fetchUsageSummaries(body, { signal: controller.signal })).toBe(response);
    expect(request).toHaveBeenCalledExactlyOnceWith('/api/v1/usage/session-summaries', {
      method: 'POST', body: JSON.stringify(body), signal: controller.signal,
    });
    expect(body.items[0].kind).toBe('conversation');
  });

  it('preserves the focused execution anchor across both inclusion modes', async () => {
    vi.mocked(request).mockResolvedValue(response);
    for (const inclusion of ['direct', 'inclusive'] as const) {
      await fetchUsageSummaries({
        scope: { mode: 'personal' },
        items: [{ kind: 'execution', id: 'sess_child_rotation' }], inclusion,
      });
    }
    const bodies = vi.mocked(request).mock.calls.map(([, options]) =>
      JSON.parse(String(options?.body)) as UsageSummaryRequest,
    );
    expect(bodies.map((body) => body.items)).toEqual([
      [{ kind: 'execution', id: 'sess_child_rotation' }],
      [{ kind: 'execution', id: 'sess_child_rotation' }],
    ]);
    expect(bodies.map((body) => body.inclusion)).toEqual(['direct', 'inclusive']);
  });

  it('does not replace an accounting or abort failure with zero metrics', async () => {
    const error = new DOMException('Cancelled', 'AbortError');
    vi.mocked(request).mockRejectedValue(error);
    await expect(fetchUsageSummaries({
      items: [{ kind: 'conversation', id: 'conv_retained_history' }], inclusion: 'inclusive',
    })).rejects.toBe(error);
  });
});
