import { afterEach, describe, expect, it, vi } from 'vitest';

import { FetchTimeoutError, fetchWithTimeout } from './fetch';

describe('fetchWithTimeout', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('aborts stalled requests after the configured timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    }));

    const promise = fetchWithTimeout('/api/test', {}, { fetchImpl: fetchMock, timeoutMs: 25 })
      .catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(25);

    await expect(promise).resolves.toBeInstanceOf(FetchTimeoutError);
  });

  it('rejects on timeout even when fetch ignores abort and never settles', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(() => new Promise(() => {}));

    const promise = fetchWithTimeout('/api/test', {}, { fetchImpl: fetchMock, timeoutMs: 25 })
      .catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(25);

    await expect(promise).resolves.toBeInstanceOf(FetchTimeoutError);
  });

  it('preserves caller abort signals', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    }));

    const promise = fetchWithTimeout(
      '/api/test',
      { signal: controller.signal },
      { fetchImpl: fetchMock, timeoutMs: 1_000 }
    );
    const reason = new Error('caller aborted');
    controller.abort(reason);

    await expect(promise).rejects.toBe(reason);
  });
});
