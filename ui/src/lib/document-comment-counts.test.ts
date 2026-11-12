import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';
import { auth } from '$lib/stores/auth';
import { watchDocumentCommentCount } from './document-comment-counts';
import { installFeedbackTransport, request } from './feedback/transport';

const mocks = vi.hoisted(() => ({ commentCounts: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { library: mocks } }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ user: null }) };
});
const stops: (() => void)[] = [];
function login(email: string | null) {
  (auth as unknown as Writable<{ user: { email: string } | null }>).set({ user: email ? { email } : null });
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
describe('Document listing comment counts', () => {
  beforeEach(() => { login(null); vi.clearAllMocks(); login('owner@test.local'); });
  afterEach(() => { for (const stop of stops.splice(0)) stop(); });
  it('batches references and keeps mounted copies synchronized after comment mutations', async () => {
    mocks.commentCounts.mockResolvedValue({ counts: { first: 2, second: 1 } });
    const first = vi.fn(), copy = vi.fn(), second = vi.fn();
    stops.push(watchDocumentCommentCount('first', first), watchDocumentCommentCount('first', copy), watchDocumentCommentCount('second', second));
    await flush();
    expect(first).toHaveBeenLastCalledWith(2, false);
    expect(copy).toHaveBeenLastCalledWith(2, false);
    expect(second).toHaveBeenLastCalledWith(1, false);
    expect(mocks.commentCounts.mock.calls).toEqual([[['first', 'second']]]);
    mocks.commentCounts.mockResolvedValue({ counts: { first: 1, second: 1 } });
    installFeedbackTransport(async <T>() => ({ state: 'resolved' } as T));
    await request('/api/v1/feedback/threads/thread/resolution', { method: 'POST' });
    await flush();
    expect(first).toHaveBeenLastCalledWith(1, false);
    expect(copy).toHaveBeenLastCalledWith(1, false);
  });
  it('discards counts after account changes and shows failed refreshes as unknown, not zero', async () => {
    let finish!: (value: { counts: Record<string, number> }) => void;
    mocks.commentCounts.mockReturnValue(new Promise(done => { finish = done; }));
    const listener = vi.fn();
    stops.push(watchDocumentCommentCount('private', listener));
    await flush();
    mocks.commentCounts.mockRejectedValue(new Error('Offline'));
    login('other@test.local');
    await flush();
    finish({ counts: { private: 8 } });
    await flush();
    expect(listener).toHaveBeenLastCalledWith(undefined, true);
    expect(listener.mock.calls.some(([count]) => count === 8)).toBe(false);
  });
  it('ignores a pre-mutation response that arrives after the refreshed result', async () => {
    let finish!: (value: { counts: Record<string, number> }) => void;
    mocks.commentCounts.mockReturnValueOnce(new Promise(done => { finish = done; }));
    const listener = vi.fn();
    stops.push(watchDocumentCommentCount('document', listener));
    await flush();
    mocks.commentCounts.mockResolvedValue({ counts: { document: 0 } });
    window.dispatchEvent(new Event('feedback-changed'));
    await flush();
    finish({ counts: { document: 3 } });
    await flush();
    expect(listener).toHaveBeenLastCalledWith(0, false);
  });
  it('does not let an older request replace a newer mounted copy result', async () => {
    let reject!: (error: Error) => void;
    mocks.commentCounts.mockReturnValueOnce(new Promise((_done, fail) => { reject = fail; }));
    const first = vi.fn(), second = vi.fn();
    stops.push(watchDocumentCommentCount('same', first));
    await flush();
    mocks.commentCounts.mockResolvedValue({ counts: { same: 4 } });
    stops.push(watchDocumentCommentCount('same', second));
    await flush();
    reject(new Error('Older request failed'));
    await flush();
    expect(first).toHaveBeenLastCalledWith(4, false);
    expect(second).toHaveBeenLastCalledWith(4, false);
  });
});
