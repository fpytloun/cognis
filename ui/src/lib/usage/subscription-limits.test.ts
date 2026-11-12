import { describe, expect, it } from 'vitest';
import type { ProviderMonitor } from '$lib/types/api';
import { historyWindow, pointsByWindow, providerLimits, windowLabel } from './subscription-limits';

const window = (used: number, mins: number | null, extra: Record<string, unknown> = {}) =>
  ({ used_percent: used, window_duration_mins: mins, resets_at: '2026-11-03T20:00:00Z', reset_after_seconds: null, ...extra });
function monitor(usage: Record<string, unknown>): ProviderMonitor {
  return { provider_id: 'provider', display_name: 'Provider', usage: {
    provider_id: 'provider', ok: true, source: 'test', usage_url: null, fetched_at: null, plan_type: 'pro', primary: null, secondary: null,
    credits: null, rate_limit_reached_type: null, allowed: true, limit_reached: false, additional_rate_limits: [], ...usage,
  } } as unknown as ProviderMonitor;
}
function sample(windowKey: string, first: string, last: string, utilization: string | null) {
  return { sample_id: `${windowKey}-${last}`, observed_at: last, account_generation: 'a', reset_at: null, window: windowKey, limit: null,
    utilization, maximum_utilization: null, first_observed_at: first, remaining: null, source: 'refresh', sample_count: 3 };
}
async function sha(value: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

describe('subscription limits', () => {
  it('labels windows by duration and keeps quota names', () => {
    expect(windowLabel('primary', window(1, 300) as never)).toBe('5 hours');
    expect(windowLabel('secondary', window(1, 10080) as never)).toBe('7 days');
    expect(windowLabel('primary', window(1, null) as never, 'Characters')).toBe('Characters');
    expect(windowLabel('primary', null)).toBe('Quota');
  });

  it('groups every window of one provider, including named limits keyed like stored samples', async () => {
    const usage = { primary: window(55, 300), secondary: window(10, 10080),
      additional_rate_limits: [{ limit_id: 'chars', limit_name: 'Characters', primary: window(16, null, { limit: 10000, remaining: 8400 }), secondary: null }] };
    const key = `additional:${await sha('chars')}:primary`;
    const result = await providerLimits(monitor(usage), [sample(key, '2026-11-03T10:00:00Z', '2026-11-03T11:00:00Z', '12')]);
    expect(result?.windows.map((item) => item.label)).toEqual(['5 hours', '7 days', 'Characters']);
    expect(result?.peak).toBe(55);
    expect(result!.windows[2].used).toBe(1600);
    expect(result!.windows[2].points.map((p) => p.percent)).toEqual([12, 12]);
  });

  it('plots both ends of a coalesced sample, orders by time and skips unknown utilization', () => {
    const points = pointsByWindow([
      sample('primary', '2026-11-03T12:00:00Z', '2026-11-03T12:30:00Z', '40'),
      sample('primary', '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z', '10'),
      sample('primary', '2026-11-03T11:00:00Z', '2026-11-03T11:00:00Z', null)
    ]).get('primary')!;
    expect(points.map((p) => p.percent)).toEqual([10, 40, 40]);
    expect(points[1].at).toBe(Date.parse('2026-11-03T12:00:00Z'));
  });

  it('clamps history to the 90-day API window', () => {
    const range = historyWindow('2026-01-01T00:00:00.000Z', '2026-11-03T00:00:00.000Z');
    expect(Date.parse(range.end) - Date.parse(range.start)).toBe(90 * 86_400_000);
  });
});
