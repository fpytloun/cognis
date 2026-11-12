import { beforeEach, afterEach, expect, it } from 'vitest';
import { loginFixtureAccount } from '../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { ConversationComposer, conversationComposer } from './conversation-composer';

beforeEach(async () => { localStorage.clear(); await loginFixtureAccount(); });
afterEach(async () => { await auth.clear(); });

it('does not persist empty hydration, reopening, or upload progress as an edit', async () => {
  const composer = conversationComposer(), id = crypto.randomUUID();
  const initial = composer.hydrate(id);
  composer.update(id, initial.input);
  composer.update(id, { ...initial.input, input_revision: crypto.randomUUID() });
  expect(composer.hydrate(id)).toEqual(initial);
  await composer.drafts.settled();
  expect(new DocumentDrafts().readComposite(`cognis-chat-draft:${id}`)).toBeNull();
});

it('shares the complete input synchronously and only clears the submitted revision', async () => {
  const composer = conversationComposer(), id = crypto.randomUUID();
  const input = { ...composer.hydrate(id).input, input_revision: crypto.randomUUID(), text: 'draft',
    chat_mode: 'plan' as const, attachments: [{ artifact_id: crypto.randomUUID(), kind: 'file' as const,
      filename: 'input.txt', mime_type: 'text/plain', size_bytes: 1 }] };
  let seen = composer.hydrate(id).input;
  const stop = composer.subscribe(id, (next) => { seen = next; });
  composer.update(id, input);
  expect(seen).toEqual(input);
  expect(composer.hydrate(id).input).toEqual(input);
  composer.consumed(id, crypto.randomUUID());
  expect(seen).toEqual(input);
  composer.consumed(id, input.input_revision);
  expect(seen.text).toBe('');
  expect(seen.attachments).toEqual([]);
  expect(seen.input_revision).not.toBe(input.input_revision);
  stop();
  await composer.drafts.settled();
});

it('adopts clean external edits but preserves genuinely divergent local input', async () => {
  const lease = documentCoordination().capture(), id = crypto.randomUUID();
  const first = conversationComposer(), second = new ConversationComposer(lease);
  const original = first.hydrate(id).input;
  second.hydrate(id);
  first.update(id, { ...original, input_revision: crypto.randomUUID(), text: 'external edit' });
  await first.drafts.settled();
  const key = documentCoordination().storageName(`composer-v3:cognis-chat-draft:${id}`);
  window.dispatchEvent(new StorageEvent('storage', { storageArea: localStorage, key }));
  expect(second.hydrate(id).input.text).toBe('external edit');
  second.update(id, { ...second.hydrate(id).input, input_revision: crypto.randomUUID(), text: 'local edit' });
  window.dispatchEvent(new StorageEvent('storage', { storageArea: localStorage, key }));
  expect(second.hydrate(id).input.text).toBe('local edit');
  await second.drafts.settled();
});

it('ignores observers of a revoked composer without restoring its mutation authority', async () => {
  const owner = documentCoordination(), account = auth.getSnapshot().user!.email;
  const stale = conversationComposer(), id = crypto.randomUUID();
  const input = stale.hydrate(id).input;
  let deliveries = 0;
  const stop = stale.subscribe(id, () => { deliveries++; });
  await owner.beginLogin();
  expect(() => stale.subscribe(id, () => { deliveries++; })).not.toThrow();
  await owner.verified(account);
  const current = conversationComposer();
  expect(current).not.toBe(stale);
  current.hydrate(id);
  current.update(id, { ...input, input_revision: crypto.randomUUID(), text: 'New verified input' });
  expect(deliveries).toBe(1);
  expect(() => stale.hydrate(id)).toThrow('The account or server changed');
  expect(() => stale.update(id, input)).toThrow('The account or server changed');
  expect(() => stale.consumed(id, input.input_revision)).toThrow('The account or server changed');
  stop();
  await current.drafts.settled();
});
