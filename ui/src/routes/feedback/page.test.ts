import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { PublicFeedbackAccess, PublicGuestSession } from '$lib/feedback/public-api';
import { savePublicFeedbackSession, readPublicFeedbackSession } from '$lib/feedback/public-session';
import Page from './+page.svelte';

const calls = vi.hoisted(() => ({ open: vi.fn(), resolve: vi.fn(), close: vi.fn() }));
vi.mock('$lib/feedback/public-api', () => ({
  PublicFeedbackClient: class {
    guest: PublicGuestSession | null = null;
    open = calls.open;
    resolve = calls.resolve;
    resume(_access: PublicFeedbackAccess, guest: PublicGuestSession | null) { this.guest = guest; }
    content = async () => {
      return { format: 'plain', mime_type: 'text/plain', filename: 'source.txt',
        content: 'Navigation source evidence', rich_payload: null, bytes_path: null };
    };
    threads = async () => ({ items: [], next_cursor: null });
    outcomes = async () => ({ items: [] });
    audio = () => ({
      context: vi.fn(), estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn(),
    });
    close = calls.close;
  },
}));

function access(allow_comments = true): PublicFeedbackAccess {
  return {
    share_id: crypto.randomUUID(), capability: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
    allow_comments, discussion: 'own_only', permitted_locator_kinds: ['whole'],
    source: { source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
      content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}`, title: 'Navigation source',
      format: 'plain', canonical_text: null },
  };
}

function navigate(hash: string) {
  const oldURL = location.href;
  history.replaceState(history.state, '', `/feedback${hash}`);
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: location.href }));
}

beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); history.replaceState({}, '', '/feedback');
  // jsdom has no matchMedia; the public viewer follows the system color scheme.
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: false, media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false }));
});
afterEach(() => {
  cleanup(); sessionStorage.clear(); history.replaceState({}, '', '/');
});

it('re-exchanges the same consumed link once per navigation and refreshes comment permission', async () => {
  const token = crypto.randomUUID();
  const initial = access(false);
  const refreshed = { ...initial, allow_comments: true };
  calls.open.mockResolvedValueOnce(initial).mockResolvedValueOnce(refreshed);
  history.replaceState({}, '', `/feedback#${token}`);
  render(Page);
  await screen.findByText('Navigation source evidence');
  expect(screen.queryByRole('button', { name: /^Comments(?:,|$)/ })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Join as guest' })).toBeNull();
  expect(location.hash).toBe('');
  navigate(`#${token}`);
  await waitFor(() => expect(calls.open).toHaveBeenCalledTimes(2));
  await screen.findByText('Navigation source evidence');
  await fireEvent.click(await screen.findByRole('button', { name: /^Comments(?:,|$)/ }));
  expect(await screen.findByRole('button', { name: 'Join as guest' })).toBeVisible();
  expect(location.hash).toBe('');
  expect(calls.open.mock.calls).toEqual([[token], [token]]);
  expect(calls.close).toHaveBeenCalledTimes(1);
  expect(readPublicFeedbackSession()?.access).toEqual(refreshed);
});

it('disposes the previous exchange and ignores its late result when a different link opens', async () => {
  const firstToken = crypto.randomUUID(), secondToken = crypto.randomUUID();
  const first = access(), second = access(false);
  let finishFirst!: (value: PublicFeedbackAccess) => void;
  let finishSecond!: (value: PublicFeedbackAccess) => void;
  calls.open.mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve; }))
    .mockReturnValueOnce(new Promise(resolve => { finishSecond = resolve; }));
  history.replaceState({}, '', `/feedback#${firstToken}`);
  render(Page);
  await waitFor(() => expect(calls.open).toHaveBeenCalledTimes(1));
  navigate(`#${secondToken}`);
  await waitFor(() => expect(calls.close).toHaveBeenCalledTimes(1));
  finishFirst(first);
  await Promise.resolve();
  expect(location.hash).toBe(`#${secondToken}`);
  expect(readPublicFeedbackSession()).toBeNull();
  finishSecond(second);
  await screen.findByText('Navigation source evidence');
  expect(location.hash).toBe('');
  expect(calls.open.mock.calls).toEqual([[firstToken], [secondToken]]);
  expect(readPublicFeedbackSession()?.access.source).toEqual(second.source);
});

it('restores a cached capability and guest on a fragment-free mount without re-exchanging', async () => {
  const cached = access();
  const guest = { guest_session_id: crypto.randomUUID(), guest_capability: crypto.randomUUID(),
    expires_at: '2099-01-01T00:00:00Z' };
  savePublicFeedbackSession({ access: cached, guest });
  calls.resolve.mockResolvedValue(cached);
  render(Page);
  await screen.findByText('Navigation source evidence');
  // The restored guest can comment straight away.
  await fireEvent.click(await screen.findByRole('button', { name: /^Comments(?:,|$)/ }));
  await fireEvent.click(await screen.findByRole('button', { name: 'Comment on document' }));
  expect(await screen.findByRole('textbox', { name: 'Comment' })).toBeVisible();
  expect(calls.open).not.toHaveBeenCalled();
  expect(calls.resolve).toHaveBeenCalledWith({ kind: 'document', content_ref: cached.source.content_ref });
  expect(readPublicFeedbackSession()?.guest).toEqual(guest);
});

it('does not let an earlier exchange strip a different fragment before hashchange is delivered', async () => {
  const firstToken = crypto.randomUUID(), secondToken = crypto.randomUUID();
  let finishFirst!: (value: PublicFeedbackAccess) => void;
  calls.open.mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve; }))
    .mockResolvedValueOnce(access());
  history.replaceState({}, '', `/feedback#${firstToken}`);
  render(Page);
  await waitFor(() => expect(calls.open).toHaveBeenCalledTimes(1));
  const oldURL = location.href;
  history.replaceState(history.state, '', `/feedback#${secondToken}`);
  const newURL = location.href;
  finishFirst(access());
  await screen.findByText('Navigation source evidence');
  expect(location.hash).toBe(`#${secondToken}`);
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL }));
  await waitFor(() => expect(calls.open).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(location.hash).toBe(''));
  expect(calls.open.mock.calls).toEqual([[firstToken], [secondToken]]);
});

it('uses the navigation event URL if its fragment was already consumed before event delivery', async () => {
  const token = crypto.randomUUID();
  calls.open.mockResolvedValue(access());
  history.replaceState({}, '', `/feedback#${token}`);
  render(Page);
  await screen.findByText('Navigation source evidence');
  const oldURL = location.href;
  history.replaceState(history.state, '', `/feedback#${token}`);
  const newURL = location.href;
  history.replaceState(history.state, '', '/feedback');
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL }));
  await waitFor(() => expect(calls.open).toHaveBeenCalledTimes(2));
  expect(calls.open.mock.calls).toEqual([[token], [token]]);
  expect(location.hash).toBe('');
});
