import { describe, expect, it } from 'vitest';
import { ALL_TIME_RANGE, fromUtcIso, quickWorkRange, toUtcIso, workRangeOverlaps } from './workTimeRange';

describe('workTimeRange', () => {
  it('converts browser date-time input to UTC ISO and back', () => {
    expect(toUtcIso('2026-08-06T10:30')).toMatch(/2026-08-06T/);
    expect(fromUtcIso('2026-08-06T10:30:00.000Z')).toMatch(/^2026-08-06T/);
  });

  it('builds bounded quick ranges', () => {
    const range = quickWorkRange('Last 6h', new Date('2026-08-06T12:00:00Z'));
    expect(range).toEqual({ from: '2026-08-06T06:00:00.000Z', to: '2026-08-06T12:00:00.000Z', label: 'Last 6h' });
    expect(ALL_TIME_RANGE).toEqual({ from: null, to: null, label: 'All time' });
  });

  it('uses half-open local calendar ranges at midnight', () => {
    const now = new Date('2026-08-06T12:00:00Z');
    const yesterday = quickWorkRange('Yesterday', now);
    expect(yesterday).toMatchObject({
      label: 'Yesterday',
    });
    expect(new Date(yesterday.to!).getTime() - new Date(yesterday.from!).getTime()).toBe(24 * 60 * 60 * 1000);
    expect(quickWorkRange('Today', now)).toMatchObject({
      label: 'Today',
    });
    expect(quickWorkRange('Today', now).to).toBeNull();
    expect(quickWorkRange('This week', now)).toMatchObject({
      label: 'This week',
    });
    expect(quickWorkRange('This week', now).to).toBeNull();
    expect(workRangeOverlaps(yesterday, {
      from: yesterday.to, to: '2026-08-07T00:00:00Z',
    })).toBe(false);
  });

  it('hides calendar presets only when available metadata proves no overlap', () => {
    expect(workRangeOverlaps(quickWorkRange('Today', new Date('2026-08-06T12:00:00Z')), {
      from: '2026-07-01T00:00:00Z', to: '2026-07-02T00:00:00Z',
    })).toBe(false);
    expect(workRangeOverlaps(ALL_TIME_RANGE, null)).toBe(true);
  });
});
