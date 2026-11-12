import { beforeEach, expect, it } from 'vitest';
import { GuestFeedbackDrafts, type GuestCommentRequest } from './guest-drafts';
import { savePublicFeedbackSession } from './public-session';

beforeEach(() => {
  sessionStorage.clear();
  savePublicFeedbackSession({
    access: { share_id: 'guest-draft-share', capability: 'public-draft-capability', expires_at: '2099-01-01T00:00:00Z',
      allow_comments: true, discussion: 'own_only', permitted_locator_kinds: ['whole'],
      source: { source_token: 'public-source-proof', content_digest: 'a'.repeat(64),
        content_ref: 'art_87d6238b098f4798919dc4b299894379', format: 'plain', title: null, canonical_text: null } },
    guest: { guest_session_id: 'draft-guest', guest_capability: 'guest-draft-capability', expires_at: '2099-01-01T00:00:00Z' },
  });
});
const request: GuestCommentRequest = { kind: 'thread', body: 'Original comment', original_locator: null,
  anchor: { schema_version: 1, source_token: 'public-source-proof', content_digest: 'a'.repeat(64), locator: { kind: 'whole' } } };
it('retains an immutable guest creation key through response loss, edits, and reload', () => {
  const first = new GuestFeedbackDrafts('guest-draft-share', 'draft-guest', 'a'.repeat(64));
  first.write({ body: request.body, locator: null, reply: '', reply_thread_id: null });
  const pending = first.freeze(request);
  first.write({ body: 'Newer input', locator: null, reply: '', reply_thread_id: null });
  const reloaded = new GuestFeedbackDrafts('guest-draft-share', 'draft-guest', 'a'.repeat(64));
  expect(reloaded.read()).toMatchObject({ body: 'Newer input', pending });
  expect(reloaded.freeze({ ...request, body: 'Do not replace original' })).toEqual(pending);
  reloaded.complete(pending.creation_key, { body: 'Newer input', locator: null, reply: '', reply_thread_id: null });
  expect(reloaded.read()).toMatchObject({ body: 'Newer input', pending: null });
});
it('retains exact reply versions and refuses storage under another guest identity', () => {
  const drafts = new GuestFeedbackDrafts('guest-draft-share', 'draft-guest', 'a'.repeat(64));
  const pending = drafts.freeze({ kind: 'reply', thread_id: 'guest-thread', expected_thread_version: 4,
    reply_to_entry_id: 'guest-parent', body: 'Original reply' });
  expect(new GuestFeedbackDrafts('guest-draft-share', 'draft-guest', 'a'.repeat(64)).read()?.pending).toEqual(pending);
  expect(() => new GuestFeedbackDrafts('guest-draft-share', 'another-guest', 'a'.repeat(64)).freeze(request)).toThrow('guest session');
});
