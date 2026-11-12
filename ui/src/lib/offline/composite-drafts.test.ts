import { beforeEach, expect, it } from 'vitest';
import 'fake-indexeddb/auto';
import { DocumentDrafts, type CompositeInput } from './document-drafts';
import { documentCoordination } from '$lib/document-coordination';

beforeEach(async () => {
  localStorage.clear();
  await documentCoordination().initialize();
  await documentCoordination().invalidate(false);
  await documentCoordination().verified('composite@cognis.test');
});
const input = () => ({ input_revision: crypto.randomUUID(), text: 'text',
  attachments: [], chat_mode: null });
const empty = (): CompositeInput => ({ ...input(), text: '' });

for (const pin of [false, true]) {
  it(`recovers stale ${pin ? 'submission' : 'autosave'} against an unconsumed empty draft`, async () => {
    const drafts = new DocumentDrafts(), sibling = new DocumentDrafts();
    drafts.readComposite('draft');
    await sibling.writeComposite('draft', empty());
    const value = input();
    const saved = await (pin ? drafts.persistComposite('draft', value) : drafts.writeComposite('draft', value));
    expect(saved).toMatchObject({ ...value, consumed: false });
    expect(new DocumentDrafts().readComposite('draft')).toMatchObject(value);
    expect(Object.keys(localStorage).some((key) => key.includes(':retained:'))).toBe(false);
    const submitted = await drafts.persistComposite('draft', value);
    expect(await drafts.consumeComposite(submitted!, () => true)).toBe(true);
  });
}

const meaningful: [string, () => CompositeInput][] = [
  ['text', input],
  ['whitespace', () => ({ ...empty(), text: ' ' })],
  ['attachment', () => ({ ...empty(), attachments: [{ artifact_id: crypto.randomUUID(),
    filename: 'draft.txt', kind: 'file', mime_type: 'text/plain', size_bytes: 1 }] })],
  ['feedback', () => ({ ...empty(), feedback: { creation_key: crypto.randomUUID(),
    preflight_id: crypto.randomUUID(), manifest_hash: 'a'.repeat(64) } })],
  ['mode', () => ({ ...empty(), chat_mode: 'plan' })],
];

for (const [label, competitor] of meaningful) {
  for (const pin of [false, true]) {
    it(`preserves ${label}-bearing competitors against stale ${pin ? 'submission' : 'autosave'}`, async () => {
      const drafts = new DocumentDrafts(), sibling = new DocumentDrafts();
      drafts.readComposite('draft');
      const winner = competitor(), loser = input();
      await sibling.writeComposite('draft', winner);
      expect(await (pin ? drafts.persistComposite('draft', loser) : drafts.writeComposite('draft', loser))).toBeNull();
      expect(new DocumentDrafts().readComposite('draft')).toMatchObject(winner);
      const retainedKey = Object.keys(localStorage).find((key) => key.endsWith(`:retained:${loser.input_revision}`));
      expect(JSON.parse(localStorage.getItem(retainedKey!)!)).toMatchObject(loser);
    });
  }
}

it('does not overwrite a queued local clear when pinning older input', async () => {
  const drafts = new DocumentDrafts(), value = input();
  await drafts.writeComposite('draft', value);
  const cleared = empty();
  const saving = drafts.writeComposite('draft', cleared);
  const pinning = drafts.persistComposite('draft', value);
  await saving;
  expect(await pinning).toBeNull();
  expect(new DocumentDrafts().readComposite('draft')).toMatchObject(cleared);
});

it('does not rebase a stale edit over an empty consumed tombstone', async () => {
  const drafts = new DocumentDrafts(), sibling = new DocumentDrafts();
  drafts.readComposite('draft');
  const submitted = await sibling.persistComposite('draft', empty());
  await sibling.consumeComposite(submitted!, () => true);
  expect(await drafts.writeComposite('draft', input())).toBeNull();
  expect(new DocumentDrafts().readComposite('draft')).toMatchObject({
    input_revision: submitted!.input_revision, text: '', consumed: true,
  });
});

it('does not treat a missing canonical draft as empty initialization', async () => {
  const drafts = new DocumentDrafts();
  await drafts.writeComposite('draft', input());
  localStorage.removeItem(documentCoordination().storageName('composer-v3:draft'));
  expect(await drafts.persistComposite('draft', input())).toBeNull();
});

it('does not reuse one input revision for different content while rebasing empty initialization', async () => {
  const drafts = new DocumentDrafts(), blank = empty();
  drafts.readComposite('draft');
  await new DocumentDrafts().writeComposite('draft', blank);
  await expect(drafts.writeComposite('draft', { ...blank, text: 'changed' })).rejects.toThrow('Composite input revision mismatch');
});

it('allows only the first meaningful writer to replace empty initialization', async () => {
  const first = new DocumentDrafts(), second = new DocumentDrafts();
  first.readComposite('draft'); second.readComposite('draft');
  await new DocumentDrafts().writeComposite('draft', empty());
  const a = input(), b = input();
  const saved = await Promise.all([first.writeComposite('draft', a), second.writeComposite('draft', b)]);
  expect(saved.filter(Boolean)).toHaveLength(1);
  expect(new DocumentDrafts().readComposite('draft')?.input_revision)
    .toBe(saved.find(Boolean)!.input_revision);
});

it('keeps exact intent across reopen and tombstones it without stale-save resurrection', async () => {
  const drafts = new DocumentDrafts(), value = input();
  drafts.readComposite('draft');
  await drafts.writeComposite('draft', value);
  const sibling = new DocumentDrafts();
  expect(sibling.readComposite('draft')?.input_revision).toBe(value.input_revision);
  const captured = await drafts.persistComposite('draft', value);
  await drafts.consumeComposite(captured!, () => true);
  await sibling.writeComposite('draft', value);
  expect(new DocumentDrafts().readComposite('draft')?.consumed).toBe(true);
});

it('preserves the complete losing composite and refuses ABA consumption', async () => {
  const first = new DocumentDrafts(), sibling = new DocumentDrafts();
  first.readComposite('draft'); sibling.readComposite('draft');
  const a = input(), b = { ...input(), chat_mode: 'plan' as const };
  await first.writeComposite('draft', a);
  await sibling.writeComposite('draft', b);
  const values = Object.keys(localStorage).map((key) => localStorage.getItem(key)!);
  expect(values.some((value) => value.includes(b.input_revision) && value.includes('"plan"'))).toBe(true);
  const pinned = await first.persistComposite('draft', a);
  await first.writeComposite('draft', input());
  expect(await first.consumeComposite(pinned!, () => true)).toBe(false);
});

it('keeps queued newer intent canonical when older intent is consumed', async () => {
  const drafts = new DocumentDrafts(), a = input();
  drafts.readComposite('draft');
  const pinned = await drafts.persistComposite('draft', a);
  const b = { ...input(), text: 'newer B', chat_mode: 'plan' as const };
  const saving = drafts.writeComposite('draft', b);
  const consuming = drafts.consumeComposite(pinned!, () => true);
  await saving;
  expect(await consuming).toBe(false);
  expect(new DocumentDrafts().readComposite('draft')).toMatchObject({ ...b, consumed: false });
  expect(Object.keys(localStorage).some((key) => key.includes(':retained:'))).toBe(false);
});

it('does not overwrite queued newer intent when pinning older input', async () => {
  const drafts = new DocumentDrafts(), a = input();
  drafts.readComposite('draft');
  await drafts.writeComposite('draft', a);
  const b = { ...input(), text: 'newer B' };
  const saving = drafts.writeComposite('draft', b);
  const pinning = drafts.persistComposite('draft', a);
  await saving;
  expect(await pinning).toBeNull();
  expect(new DocumentDrafts().readComposite('draft')).toMatchObject({ ...b, consumed: false });
});

it('drains same-intent autosave before pinning and consuming exactly once', async () => {
  const drafts = new DocumentDrafts(), a = input();
  drafts.readComposite('draft');
  const saving = drafts.writeComposite('draft', a);
  const pinning = drafts.persistComposite('draft', a);
  await saving;
  const pinned = await pinning;
  expect(pinned?.input_revision).toBe(a.input_revision);
  const repeatedSave = drafts.writeComposite('draft', a);
  const consuming = drafts.consumeComposite(pinned!, () => true);
  await repeatedSave;
  expect(await consuming).toBe(true);
  await drafts.writeComposite('draft', a);
  expect(new DocumentDrafts().readComposite('draft')?.consumed).toBe(true);
});
