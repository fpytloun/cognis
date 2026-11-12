import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { savePublicFeedbackSession } from '$lib/feedback/public-session';
import PublicFeedbackHost from './PublicFeedbackHost.svelte';
import type { FeedbackThread } from '$lib/feedback/types';

const calls = vi.hoisted(() => ({ create: vi.fn(), join: vi.fn(), threads: vi.fn(async () => ({ items: [] as FeedbackThread[], next_cursor: null })) }));
const publicState = vi.hoisted(() => ({
  joined: true,
  guest: { guest_session_id: 'recoverable-guest', guest_capability: 'recoverable-guest-capability', expires_at: '2099-01-01T00:00:00Z' },
  access: { share_id: 'recoverable-share', capability: 'recoverable-public-capability', expires_at: '2099-01-01T00:00:00Z',
    allow_comments: true, discussion: 'own_only' as const, permitted_locator_kinds: ['whole'] as ['whole'],
    source: { source_token: 'recoverable-source-proof', content_digest: 'a'.repeat(64),
      content_ref: 'art_87d6238b098f4798919dc4b299894379', title: 'Public source', format: 'plain', canonical_text: null } },
}));
vi.mock('$lib/feedback/public-api', () => ({
  PublicFeedbackClient: class {
    guest = publicState.joined ? publicState.guest : null;
    join = async (name: string | null) => { await calls.join(name); this.guest = publicState.guest; };
    adoptCapability = async () => publicState.access;
    resume() {}
    resolve = async () => publicState.access;
    content = async () => ({ format: 'plain', mime_type: 'text/plain', filename: 'source.txt',
      content: 'Shared source evidence', rich_payload: null, bytes_path: null });
    audio = () => ({});
    threads = calls.threads;
    outcomes = async () => ({ items: [] });
    create = calls.create;
    close() {}
  },
}));
beforeEach(() => {
  publicState.joined = true;
  calls.join.mockReset();
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })));
});
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); });
it('opens guest registration from Comment without enabling composition before a successful join', async () => {
  publicState.joined = false;
  calls.join.mockRejectedValueOnce(new Error('Join unavailable')).mockResolvedValueOnce(undefined);
  savePublicFeedbackSession({ access: publicState.access, guest: null });
  render(PublicFeedbackHost);
  const comment = (await screen.findAllByRole('button', { name: /^Comments/ }))[0];
  expect(comment).toBeEnabled();
  await fireEvent.click(comment);
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Display name (optional)' }), { target: { value: 'Reviewer' } });
  expect(screen.queryByRole('textbox', { name: 'Comment' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Join as guest' }));
  expect(await screen.findByText('Join unavailable')).toBeVisible();
  expect(screen.queryByRole('textbox', { name: 'Comment' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Join as guest' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Join as guest' })).toBeNull());
  await fireEvent.click(await screen.findByRole('button', { name: 'Close comments' }));
  await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
  await fireEvent.click(await screen.findByRole('button', { name: 'Comment on document' }));
  expect(await screen.findByRole('textbox', { name: 'Comment' })).toBeVisible();
});

it('replays the original guest save after response loss, newer input, and component reload', async () => {
  sessionStorage.clear();
  savePublicFeedbackSession({ access: publicState.access, guest: publicState.guest });
  calls.create.mockRejectedValueOnce(new TypeError('Response lost')).mockResolvedValue({});
  const props = { capability: publicState.access.capability, contentRef: publicState.access.source.content_ref };
  const first = render(PublicFeedbackHost, props);
  await fireEvent.click((await screen.findAllByRole('button', { name: /^Comments/ }))[0]);
  await fireEvent.click(await screen.findByRole('button', { name: 'Comment on document' }));
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: 'Original guest comment' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
  await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await screen.findByText('Response lost');
  await fireEvent.input(screen.getByRole('textbox', { name: 'Comment' }), { target: { value: 'Newer guest draft' } });
  first.unmount();
  render(PublicFeedbackHost, props);
  await fireEvent.click((await screen.findAllByRole('button', { name: /^Comments/ }))[0]);
  // An unconfirmed send is recovered the next time the guest comments; there is no separate draft menu.
  await fireEvent.click(await screen.findByRole('button', { name: 'Comment on document' }));
  expect(await screen.findByRole('textbox', { name: 'Comment' })).toHaveValue('Newer guest draft');
  await fireEvent.click(await screen.findByRole('button', { name: 'Retry original send' }));
  await waitFor(() => expect(calls.create).toHaveBeenCalledTimes(2));
  expect(calls.create.mock.calls[1]).toEqual(calls.create.mock.calls[0]);
  expect(calls.create.mock.calls[1][2]).toBe('Original guest comment');
  expect(screen.getByRole('textbox', { name: 'Comment' })).toHaveValue('Newer guest draft');
});

it('shows guest comments without reply threads or execution UI', async () => {
  const thread: FeedbackThread = { thread_id: 'guest-existing-thread', subject_id: publicState.access.share_id, version: 1, state: 'open', hidden: false, audience: 'share',
    anchor: { anchor_id: 'guest-existing-anchor', schema_version: 1, source: { kind: 'document', content_ref: publicState.access.source.content_ref }, content_digest: publicState.access.source.content_digest, locator: { kind: 'whole' } },
    entries: [{ entry_id: 'guest-existing-entry', entry_version_id: 'guest-existing-entry-version', version: 1, thread_id: 'guest-existing-thread',
      body: 'Existing discussion', author: { kind: 'guest_unverified', label: 'Guest reviewer', is_self: true }, audience: 'share', reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }] };
  calls.threads.mockResolvedValue({ items: [thread], next_cursor: null });
  savePublicFeedbackSession({ access: publicState.access, guest: publicState.guest });
  render(PublicFeedbackHost);
  await fireEvent.click((await screen.findAllByRole('button', { name: /^Comments/ }))[0]);
  expect(await screen.findByText('Existing discussion')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Reply' })).toBeNull();
  expect(screen.queryByRole('button', { name: /Review selected|Send feedback|Start follow-up|Request task revision/ })).toBeNull();
});

it('hides comment controls for links that do not allow comments', async () => {
  const previous = publicState.access.allow_comments;
  publicState.access.allow_comments = false;
  try {
    savePublicFeedbackSession({ access: publicState.access, guest: null });
    render(PublicFeedbackHost);
    await screen.findByText('Shared source evidence');
    expect(screen.queryByRole('button', { name: /^Comments/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Listen to this document' })).toBeVisible();
    expect(screen.getByLabelText('Download document')).toBeVisible();
  } finally { publicState.access.allow_comments = previous; }
});
