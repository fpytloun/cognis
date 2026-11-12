import { beforeEach, describe, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { ComposerSubmission, type ComposerView } from './composer-submission';
import { DocumentDrafts, type CompositeDraft } from '$lib/offline/document-drafts';
import { documentCoordination } from '$lib/document-coordination';
import { IndexedDbChatV2Outbox, CoordinatedOutbox, type OutboxEntry } from './outbox';
import { sendOutboxEntry } from './outbox-send';
import type { SendMessageV2Response } from './types';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function fixture() {
  const owner = documentCoordination();
  await owner.verified('submission@cognis.test');
  const drafts = new DocumentDrafts();
  let current: ComposerView = { conversationId: 'submitted-conversation', generation: 1,
    draftKey: 'draft:submitted', revision: 1, input_revision: crypto.randomUUID(), chat_mode: 'plan', text: 'submitted text',
    attachments: [{ artifact_id: 'submitted-file', kind: 'file', filename: 'submitted.txt', mime_type: 'text/plain', size_bytes: 1 }] };
  const entry: OutboxEntry = { client_txn_id: 'stable-transaction', client_message_id: 'stable-message',
    conversation_id: current.conversationId, content: current.text, attachments: current.attachments,
    status: 'pending', chat_mode: 'plan', created_at: '2026-10-16T00:00:00Z', updated_at: '2026-10-16T00:00:00Z' };
  const outbox = new CoordinatedOutbox(new IndexedDbChatV2Outbox(indexedDB, crypto.randomUUID(), owner.capture()), owner);
  drafts.readComposite(current.draftKey!);
  // This suite isolates the consumption boundary; attachment-owner persistence
  // and authenticated receipts are exercised by attachment-uploads tests.
  const uploads = { prepare: vi.fn(async () => {}) };
  const submission = new ComposerSubmission(current, entry, owner.capture(), uploads);
  const clear = vi.fn(() => { current = { ...current, text: '', attachments: [], revision: current.revision + 1 }; });
  return { drafts, outbox, submission, get entry() { return submission.entry; }, clear, uploads, current: () => current,
    progress: () => { current = { ...current, attachments: current.attachments.map((ref) => ({ ...ref, upload_progress: 1, upload_state: 'uploaded' })) }; },
    edit: (patch: Partial<ComposerView>) => { current = { ...current, revision: current.revision + 1, input_revision: crypto.randomUUID(), ...patch }; } };
}

describe('normal composer submission boundary', () => {
  beforeEach(async () => {
    localStorage.clear();
    await documentCoordination().initialize();
    await documentCoordination().invalidate(false);
  });

  it('persists only an inadmissible reservation and does not consume while attachment durability is pending', async () => {
    const f = await fixture(), blocked = deferred();
    f.uploads.prepare.mockImplementation(() => blocked.promise);
    const preparing = f.submission.prepare(f.drafts, f.outbox);
    await vi.waitFor(() => expect(f.uploads.prepare).toHaveBeenCalledOnce());
    expect(await f.outbox.get(f.entry.client_txn_id)).toMatchObject({ status: 'preparing' });
    const transport = vi.fn();
    await expect(sendOutboxEntry(f.outbox, f.entry, transport)).rejects.toThrow('preparation');
    expect(transport).not.toHaveBeenCalled();
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(false);
    blocked.resolve();
    await preparing;
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(true);
  });

  it('keeps input and forbids consumption after attachment persistence fails', async () => {
    const f = await fixture();
    f.uploads.prepare.mockRejectedValue(new Error('byte commit failed'));
    await expect(f.submission.prepare(f.drafts, f.outbox)).rejects.toThrow('byte commit failed');
    expect(await f.outbox.get(f.entry.client_txn_id)).toMatchObject({ status: 'preparing' });
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(false);
    expect(f.clear).not.toHaveBeenCalled();
    expect(f.current().attachments).toHaveLength(1);
  });

  it('does not consume after a non-durable outbox reports success', async () => {
    const f = await fixture();
    vi.spyOn(f.outbox, 'isDurable').mockReturnValue(false);
    await expect(f.submission.prepare(f.drafts, f.outbox)).rejects.toThrow('Safe window coordination');
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(false);
    expect(f.clear).not.toHaveBeenCalled();
  });

  it('consumes unchanged input when only background upload progress changes', async () => {
    const f = await fixture(), blocked = deferred();
    f.uploads.prepare.mockImplementation(() => blocked.promise);
    const preparing = f.submission.prepare(f.drafts, f.outbox);
    await vi.waitFor(() => expect(f.uploads.prepare).toHaveBeenCalledOnce());
    f.progress();
    blocked.resolve();
    await preparing;
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(true);
  });

  it('waits for queued local autosave before pinning the submitted revision', async () => {
    const f = await fixture(), blocked = deferred(), entered = deferred();
    const owner = documentCoordination();
    f.drafts.readComposite(f.current().draftKey!);
    const held = owner.run(`draft:${owner.storageName(`composer-v3:${f.current().draftKey!}`)}`, owner.capture(), async () => {
      entered.resolve(); await blocked.promise;
    });
    await entered.promise;
    const autosave = f.drafts.writeComposite(f.current().draftKey!, f.current());
    const preparing = f.submission.prepare(f.drafts, f.outbox);
    blocked.resolve(); await held; await autosave; await preparing;
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(true);
    expect(f.clear).toHaveBeenCalledOnce();
    expect(new DocumentDrafts().readComposite('draft:submitted')?.consumed).toBe(true);
  });

  it('prepares and consumes local input when an empty sibling draft invalidated its observation', async () => {
    const f = await fixture();
    await new DocumentDrafts().writeComposite(f.current().draftKey!, {
      input_revision: crypto.randomUUID(), text: '', attachments: [], chat_mode: null,
    });
    await expect(f.submission.prepare(f.drafts, f.outbox)).resolves.toMatchObject({
      conversation_id: f.current().conversationId, content: f.current().text,
    });
    expect(await f.outbox.get(f.entry.client_txn_id)).toMatchObject({ status: 'pending' });
    expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(true);
    expect(f.clear).toHaveBeenCalledOnce();
  });

  for (const status of ['accepted', 'queued'] as const) {
    it(`flushes debounced autosave before delayed persistence and ${status} admission`, async () => {
      const f = await fixture(), blocked = deferred(), entered = deferred();
      const activate = f.outbox.activate.bind(f.outbox);
      vi.spyOn(f.outbox, 'activate').mockImplementation(async (id, attachments) => {
        entered.resolve();
        await blocked.promise;
        return activate(id, attachments);
      });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      let autosaving: Promise<CompositeDraft | null> | undefined;
      const timer = setTimeout(() => {
        autosaving = f.drafts.writeComposite(f.current().draftKey!, f.current());
      }, 300);
      const flush = vi.fn(() => {
        clearTimeout(timer);
        autosaving = f.drafts.writeComposite(f.current().draftKey!, f.current());
      });
      const preparing = f.submission.prepare(f.drafts, f.outbox, flush);
      try {
        await entered.promise;
        await vi.advanceTimersByTimeAsync(350);
        await autosaving;
        blocked.resolve();
        await preparing;
        expect(await f.submission.consume(f.drafts, f.current, f.clear)).toBe(true);
        expect(flush).toHaveBeenCalledOnce();
        expect(f.clear).toHaveBeenCalledOnce();
        expect(f.current().text).toBe('');
        const transport = vi.fn(async () => ({
          status, client_txn_id: f.entry.client_txn_id,
          client_message_id: f.entry.client_message_id,
          conversation_id: f.entry.conversation_id, server_time: f.entry.created_at,
        } as SendMessageV2Response));
        await sendOutboxEntry(f.outbox, f.entry, transport);
        expect(transport).toHaveBeenCalledOnce();
        expect(new DocumentDrafts().readComposite(f.current().draftKey!)?.consumed).toBe(true);
      } finally {
        clearTimeout(timer);
        blocked.resolve();
        vi.useRealTimers();
        await preparing.catch(() => {});
      }
    });
  }

  it('does not admit a submission whose pinned draft was superseded by a sibling', async () => {
    const f = await fixture();
    f.drafts.readComposite(f.current().draftKey!);
    const sibling = new DocumentDrafts();
    sibling.readComposite(f.current().draftKey!);
    await sibling.writeComposite(f.current().draftKey!, { ...f.current(), input_revision: crypto.randomUUID(), text: 'sibling input' });
    await expect(f.submission.prepare(f.drafts, f.outbox)).rejects.toThrow('Draft changed');
    expect(await f.outbox.get(f.entry.client_txn_id)).toBeNull();
    expect(f.current().text).toBe('submitted text');
    expect(sibling.readComposite(f.current().draftKey!)?.text).toBe('sibling input');
  });

  for (const change of ['typing', 'attachments', 'route', 'away-and-back']) {
    it(`preserves live and persisted input after delayed outbox persistence: ${change}`, async () => {
      const f = await fixture(), blocked = deferred();
      const activate = f.outbox.activate.bind(f.outbox);
      vi.spyOn(f.outbox, 'activate').mockImplementation(async (id, refs) => { await blocked.promise; return activate(id, refs); });
      const preparing = f.submission.prepare(f.drafts, f.outbox);
      await vi.waitFor(() => expect(f.outbox.activate).toHaveBeenCalledOnce());
      if (change === 'typing') f.edit({ text: 'new typing' });
      if (change === 'attachments') f.edit({ attachments: [] });
      if (change === 'route') f.edit({ conversationId: 'new-conversation', generation: 2, draftKey: 'draft:new', text: 'other draft' });
      if (change === 'away-and-back') f.edit({ generation: 3 });
      const latest = structuredClone(f.current());
      await f.drafts.writeComposite(latest.draftKey!, latest);
      blocked.resolve(); await preparing;
      await f.submission.consume(f.drafts, f.current, f.clear);
      expect(f.clear).not.toHaveBeenCalled();
      expect(f.current()).toEqual(latest);
      expect(f.drafts.readComposite(latest.draftKey!)?.text).toBe(latest.text);
      expect(await f.outbox.get(f.entry.client_txn_id)).toMatchObject(f.entry);
    });
  }

  it('does not delete a sibling revision, including equal text read after capture', async () => {
    const f = await fixture();
    await f.submission.prepare(f.drafts, f.outbox);
    const sibling = new DocumentDrafts();
    sibling.readComposite(f.current().draftKey!);
    await sibling.writeComposite(f.current().draftKey!, { ...f.current(), input_revision: crypto.randomUUID(), text: 'new sibling content' });
    f.drafts.readComposite(f.current().draftKey!); // observed is mutable, submission revision is not
    await f.submission.consume(f.drafts, f.current, f.clear);
    expect(sibling.readComposite(f.current().draftKey!)?.text).toBe('new sibling content');
    expect(f.clear).not.toHaveBeenCalled();
  });

  it('retains exactly the submission on persistence rejection without clearing any input', async () => {
    const f = await fixture();
    vi.spyOn(f.outbox, 'reserve').mockRejectedValue(new Error('storage unavailable'));
    await expect(f.submission.prepare(f.drafts, f.outbox)).rejects.toThrow('storage unavailable');
    expect(f.clear).not.toHaveBeenCalled();
    expect(new DocumentDrafts().readComposite(f.current().draftKey!)?.text).toBe('submitted text');
  });

  it('captures input before delayed draft persistence and never projects into a replacement view', async () => {
    const f = await fixture(), blocked = deferred();
    const persist = f.drafts.persistComposite.bind(f.drafts);
    vi.spyOn(f.drafts, 'persistComposite').mockImplementation(async (key, value) => {
      await blocked.promise;
      return persist(key, value);
    });
    const preparing = f.submission.prepare(f.drafts, f.outbox);
    f.edit({ conversationId: 'other', generation: 2, draftKey: 'draft:other', text: 'new text' });
    await f.drafts.writeComposite('draft:other', f.current());
    blocked.resolve(); await preparing;
    const optimistic = vi.fn();
    if (f.submission.isCurrentView(f.current())) optimistic();
    await f.submission.consume(f.drafts, f.current, f.clear);
    expect(optimistic).not.toHaveBeenCalled();
    expect(f.clear).not.toHaveBeenCalled();
    expect(await f.outbox.get(f.entry.client_txn_id)).toMatchObject(f.entry);
    expect(f.drafts.readComposite('draft:other')?.text).toBe('new text');
    f.edit({ conversationId: 'submitted-conversation', draftKey: 'draft:submitted', generation: 3 });
    expect(f.submission.isCurrentView(f.current())).toBe(false);
  });

  it('does not resize a composer edited after its clearing tick was scheduled', async () => {
    const f = await fixture();
    await f.submission.prepare(f.drafts, f.outbox);
    await f.submission.consume(f.drafts, f.current, f.clear);
    expect(f.submission.canResize(f.current())).toBe(true);
    f.edit({ text: 'new multi-line\ninput' });
    expect(f.submission.canResize(f.current())).toBe(false);
    f.edit({ text: '' });
    expect(f.submission.canResize(f.current())).toBe(false);
  });

  it('checks live input again after waiting for the draft cleanup lock', async () => {
    const f = await fixture(), blocked = deferred(), entered = deferred();
    await f.submission.prepare(f.drafts, f.outbox);
    const owner = documentCoordination();
    const holding = owner.run(`draft:${owner.storageName(`composer-v3:${f.current().draftKey!}`)}`, owner.capture(), async () => {
      entered.resolve(); await blocked.promise;
    });
    await entered.promise;
    const consuming = f.submission.consume(f.drafts, f.current, f.clear);
    f.edit({ text: 'typed while cleanup awaited' });
    const saving = f.drafts.writeComposite(f.current().draftKey!, f.current());
    blocked.resolve(); await holding; await consuming; await saving;
    expect(f.clear).not.toHaveBeenCalled();
    expect(f.drafts.readComposite(f.current().draftKey!)?.text).toBe('typed while cleanup awaited');
  });

  it('protects sibling ABA revisions even if the text returns to the submitted value', async () => {
    const f = await fixture();
    await f.submission.prepare(f.drafts, f.outbox);
    const sibling = new DocumentDrafts();
    sibling.readComposite(f.current().draftKey!);
    await sibling.writeComposite(f.current().draftKey!, { ...f.current(), input_revision: crypto.randomUUID(), text: 'sibling edit' });
    await sibling.writeComposite(f.current().draftKey!, { ...f.current(), input_revision: crypto.randomUUID(), text: f.entry.content });
    f.drafts.readComposite(f.current().draftKey!);
    await f.submission.consume(f.drafts, f.current, f.clear);
    expect(f.clear).not.toHaveBeenCalled();
    expect(sibling.readComposite(f.current().draftKey!)?.text).toBe(f.entry.content);
  });

  it('consumes an unchanged submission then admits the original transaction only once', async () => {
    const f = await fixture();
    await f.submission.prepare(f.drafts, f.outbox);
    await f.submission.consume(f.drafts, f.current, f.clear);
    expect(f.current().text).toBe('');
    const response = { status: 'accepted', client_txn_id: f.entry.client_txn_id,
      client_message_id: f.entry.client_message_id, conversation_id: f.entry.conversation_id,
      server_time: f.entry.created_at } as SendMessageV2Response;
    const transport = vi.fn(async () => response);
    await sendOutboxEntry(f.outbox, f.entry, transport);
    await sendOutboxEntry(f.outbox, f.entry, transport);
    expect(transport).toHaveBeenCalledOnce();
  });
});
