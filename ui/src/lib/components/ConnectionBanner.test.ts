import { fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ retry: vi.fn() }));

vi.mock('$lib/connectivity', async () => {
  const { writable: makeWritable } = await import('svelte/store');
  const store = makeWritable({ level: 'ok', degraded: false, lastFailure: null as string | null, attempts: 0 });
  return {
    connectivity: store,
    retryConnectivityNow: mocks.retry,
    __store: store,
  };
});

import ConnectionBanner from './ConnectionBanner.svelte';
import * as connectivityModule from '$lib/connectivity';

const store = (connectivityModule as unknown as { __store: ReturnType<typeof writable> }).__store;

describe('ConnectionBanner', () => {
  beforeEach(() => {
    mocks.retry.mockReset();
    store.set({ level: 'ok', degraded: false, lastFailure: null, attempts: 0 });
  });

  it('shows one notice per connectivity level after the grace period', async () => {
    render(ConnectionBanner, { showAfterMs: 0 });
    store.set({ level: 'unreachable', degraded: true, lastFailure: 'Request timed out after 30 seconds.', attempts: 0 });
    expect(await screen.findByText('Cognis is not responding. Retrying…')).toBeTruthy();
    expect(screen.getAllByTestId('connection-banner')).toHaveLength(1);
    expect(screen.getByTestId('connection-banner').className).not.toContain('fixed');

    await fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(mocks.retry).toHaveBeenCalledTimes(1);

    store.set({ level: 'ok', degraded: false, lastFailure: null, attempts: 0 });
    await tick();
    expect(screen.queryByTestId('connection-banner')).toBeNull();
  });

  it('never exceeds the screen width and expands a truncated label with details', async () => {
    render(ConnectionBanner, { showAfterMs: 0 });
    store.set({ level: 'offline', degraded: true, lastFailure: null, attempts: 0 });
    const label = await screen.findByTestId('connection-banner-label');
    const pill = label.closest('[role="status"]') as HTMLElement;
    expect(pill.className).toContain('max-w-full');
    expect(pill.className).toContain('min-w-0');
    expect(label.className).toContain('truncate');
    // No Retry while offline: reconnecting waits for the network.
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();

    // Simulate an overflowing label (jsdom has no layout).
    Object.defineProperty(label, 'scrollWidth', { configurable: true, value: 600 });
    Object.defineProperty(label, 'clientWidth', { configurable: true, value: 240 });
    // A label change re-measures (ResizeObserver does in a real browser).
    store.set({ level: 'stalled', degraded: true, lastFailure: 'Failed to fetch', attempts: 4 });
    const expand = await screen.findByTestId('connection-banner-expand');
    expect(expand.getAttribute('aria-expanded')).toBe('false');
    expect(expand.className).toContain('h-11');
    expect(expand.className).toContain('w-11');

    await fireEvent.click(expand);
    expect(expand.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('connection-banner-label').className).not.toContain('truncate');
    const details = screen.getByTestId('connection-banner-details').textContent ?? '';
    expect(details).toContain('Last error: Failed to fetch');
    expect(details).toContain('Reconnect attempt 4');
    expect(details).toContain('Unsent messages are kept');
  });
});
