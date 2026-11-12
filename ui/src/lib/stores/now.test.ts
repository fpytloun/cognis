import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatRelativeTime } from '../time';

describe('now store', () => {
  const subscriptions: (() => void)[] = [];

  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-04T10:59:00Z'));
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  });

  afterEach(() => {
    subscriptions.splice(0).forEach(unsubscribe => unsubscribe());
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('refreshes immediately when first subscribed long after module import', async () => {
    const { now } = await import('./now');
    vi.setSystemTime(new Date('2026-11-04T11:11:00Z'));
    expect(get(now)).toBe(Date.now());
    expect(formatRelativeTime('2026-11-04T11:10:00Z', new Date(get(now)))).toBe('1 minute ago');
  });

  it('updates on tick', async () => {
    const { now } = await import('./now');
    subscriptions.push(now.subscribe(() => {}));
    const initial = get(now);
    vi.advanceTimersByTime(30_000);
    expect(get(now)).toBe(initial + 30_000);
  });

  it('refreshes immediately after the last subscriber has been absent', async () => {
    const { now } = await import('./now');
    expect(get(now)).toBe(Date.now());
    expect(vi.getTimerCount()).toBe(0);
    vi.setSystemTime(new Date('2026-11-04T11:11:00Z'));
    expect(get(now)).toBe(Date.now());
  });

  it('shares one timer and cleans it up after the last subscriber', async () => {
    const { now } = await import('./now');
    const first = now.subscribe(() => {});
    const second = now.subscribe(() => {});
    subscriptions.push(first, second);
    expect(vi.getTimerCount()).toBe(1);
    first();
    expect(vi.getTimerCount()).toBe(1);
    second();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('pauses while hidden and refreshes immediately when visible again', async () => {
    const { now } = await import('./now');
    subscriptions.push(now.subscribe(() => {}));
    const initial = get(now);
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    document.dispatchEvent(new Event('visibilitychange'));
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(12 * 60_000);
    expect(get(now)).toBe(initial);
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    document.dispatchEvent(new Event('visibilitychange'));
    expect(get(now)).toBe(Date.now());
    expect(vi.getTimerCount()).toBe(1);
  });
});
