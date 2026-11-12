import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiUrl } from '$lib/config';
import { documentCoordination } from '$lib/document-coordination';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { FeedbackDispatchJournal } from './dispatch-journal';
import type { FeedbackPreflight, SubmissionReceipt } from './types';

const preflight: FeedbackPreflight = { preflight_id: 'review-preflight', manifest_hash: 'a'.repeat(64),
  destination: { kind: 'new_followup', agent_id: 'laforge', project_id: null, title: null },
  update_mode: 'revise_source', selection: [], context_items: [], warnings: [],
  source_summaries: [], expires_at: '2099-01-01T00:00:00Z', task_consequences: null };
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
/** Hold the persistence boundary after each contender has observed its packet. */
function holdPins(contenders = 1) {
  const entered = deferred(), released = deferred();
  const persist = DocumentDrafts.prototype.persistSubmission;
  let calls = 0;
  const pin = vi.spyOn(DocumentDrafts.prototype, 'persistSubmission')
    .mockImplementation(async function(this: DocumentDrafts, key, value) {
      if (++calls === contenders) entered.resolve();
      await released.promise;
      return persist.call(this, key, value);
    });
  return { entered: entered.promise, release: released.resolve, pin };
}
function receipt(delivery: SubmissionReceipt['delivery'], key = 'original-request-key'): SubmissionReceipt {
  return { submission_id: 'known-submission', creation_key: key, manifest_hash: preflight.manifest_hash,
    delivery, admission: delivery === 'accepted'
      ? { kind: 'conversation_turn', conversation_id: 'follow-up-conversation', request_id: 'accepted-request', turn_id: 'accepted-turn' } : null,
    work: delivery === 'accepted' ? 'queued' : 'unavailable', rejection: null, reconciling: false, version: 1,
    check_delivery_available: delivery === 'uncertain', review_again_available: false };
}
beforeEach(async () => {
  localStorage.clear(); await documentCoordination().initialize(); await documentCoordination().beginLogin();
  await documentCoordination().verified('feedback-journal@cognis.test');
});
afterEach(() => { vi.restoreAllMocks(); });
describe('standalone dispatch journal', () => {
  it('retains the exact preflight and creation key across response loss and reload', async () => {
    await new FeedbackDispatchJournal(['source-subject']).freeze(preflight, 'original-request-key', 'Keep this exact instruction');
    const restored = await new FeedbackDispatchJournal(['source-subject']).load();
    expect(restored).toEqual({ creation_key: 'original-request-key', preflight, receipt: null, overall_message: 'Keep this exact instruction' });
    const rebound = await new FeedbackDispatchJournal(['source-subject']).freeze({ ...preflight, preflight_id: 'new-preflight' }, 'new-key', 'Keep this exact instruction');
    expect(rebound.creation_key).toBe('original-request-key');
    expect(rebound.preflight.preflight_id).toBe(preflight.preflight_id);
  });
  it('refuses fresh packets and clearing while delivery is unresolved', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    await expect(journal.freeze({ ...preflight, manifest_hash: 'b'.repeat(64) }, 'another-request-key')).rejects.toThrow('previous feedback delivery');
    await expect(journal.clearAccepted('original-request-key')).rejects.toThrow('Resolve delivery');
  });
  it('requires a matching definitive receipt before an explicit new review', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    const receipt: SubmissionReceipt = { submission_id: 'known-submission', creation_key: 'original-request-key', manifest_hash: preflight.manifest_hash,
      delivery: 'accepted', admission: { kind: 'conversation_turn', conversation_id: 'follow-up-conversation', request_id: 'accepted-request', turn_id: 'accepted-turn' },
      work: 'queued', rejection: null, reconciling: false, version: 1, check_delivery_available: false, review_again_available: false };
    await journal.record(receipt);
    await expect(journal.clearAccepted('different-reviewed-key')).rejects.toThrow('reviewed request changed');
    expect((await journal.load())?.receipt).toEqual(receipt);
    await journal.clearAccepted('original-request-key');
    expect(await journal.load()).toBeNull();
  });
  it('never reports two concurrent creation keys as durably frozen', async () => {
    const gate = holdPins(2);
    const first = new FeedbackDispatchJournal(['concurrent-subject']);
    const second = new FeedbackDispatchJournal(['concurrent-subject']);
    const pending = Promise.allSettled([
      first.freeze(preflight, 'first-concurrent-key'),
      second.freeze(preflight, 'second-concurrent-key'),
    ]);
    try { await gate.entered; } finally { gate.release(); }
    const results = await pending;
    const saved = await new FeedbackDispatchJournal(['concurrent-subject']).load();
    const admitted = results.filter(result => result.status === 'fulfilled');
    expect(admitted).toHaveLength(1);
    if (admitted[0].status === 'fulfilled') expect(admitted[0].value.creation_key).toBe(saved?.creation_key);
  });
  it('serializes distinct overlapping identities on the same journal without replacing the first packet', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    const gate = holdPins();
    const first = journal.freeze(preflight, 'original-request-key');
    try {
      await gate.entered;
      const second = journal.freeze({ ...preflight, preflight_id: 'later-preflight' }, 'later-request-key');
      gate.release();
      const original = await first;
      expect(await second).toEqual(original);
      expect(await journal.load()).toEqual(original);
      expect(original.creation_key).toBe('original-request-key');
      expect(original.preflight.preflight_id).toBe(preflight.preflight_id);
      expect(gate.pin).toHaveBeenCalledOnce();
    } finally { gate.release(); }
  });
  it('rejects an overlapping different manifest without overwriting the original identity', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    const gate = holdPins();
    const first = journal.freeze(preflight, 'original-request-key');
    try {
      await gate.entered;
      const rejected = expect(journal.freeze({ ...preflight, manifest_hash: 'b'.repeat(64) }, 'later-request-key'))
        .rejects.toThrow('previous feedback delivery');
      gate.release();
      await Promise.all([first, rejected]);
      expect((await journal.load())?.creation_key).toBe('original-request-key');
      expect(gate.pin).toHaveBeenCalledOnce();
    } finally { gate.release(); }
  });
  it.each(['accepted', 'rejected'] as const)('does not downgrade an overlapping %s receipt to uncertain', async delivery => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    const gate = holdPins();
    const definitive = journal.record(receipt(delivery));
    try {
      await gate.entered;
      const delayed = journal.record(receipt('uncertain'));
      gate.release();
      await Promise.all([definitive, delayed]);
      expect((await journal.load())?.receipt).toEqual(receipt(delivery));
      expect(gate.pin).toHaveBeenCalledOnce();
    } finally { gate.release(); }
  });
  it('does not let an interleaved load adopt a sibling revision for a stale pending pin', async () => {
    const subjects = ['interleaved-subject'];
    const journal = new FeedbackDispatchJournal(subjects);
    await journal.load();
    const gate = holdPins();
    const pending = Promise.allSettled([journal.freeze(preflight, 'stale-request-key')]);
    try {
      await gate.entered;
      gate.pin.mockRestore();
      const sibling = await new FeedbackDispatchJournal(subjects).freeze(preflight, 'sibling-request-key');
      const reading = journal.load();
      // Let an unqueued read reach its already-resolved key before releasing the pin.
      await Promise.resolve();
      gate.release();
      expect(await pending).toMatchObject([{ status: 'rejected', reason: expect.any(Error) }]);
      expect(await reading).toEqual(sibling);
      expect(await new FeedbackDispatchJournal(subjects).load()).toEqual(sibling);
    } finally { gate.release(); }
  });
  it('reuses the original packet when a concurrent journal hash resolves after persistence', async () => {
    const subjects = ['delayed-hash-subject'];
    const first = new FeedbackDispatchJournal(subjects);
    const gate = deferred();
    const digest = crypto.subtle.digest.bind(crypto.subtle);
    vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
      await gate.promise;
      return digest(algorithm, data);
    });
    const second = new FeedbackDispatchJournal(subjects);
    const pin = vi.spyOn(DocumentDrafts.prototype, 'persistSubmission');
    const original = first.freeze(preflight, 'original-request-key');
    const delayed = second.freeze(preflight, 'later-request-key');
    try {
      const saved = await original;
      gate.resolve();
      expect(await delayed).toEqual(saved);
      expect(await new FeedbackDispatchJournal(subjects).load()).toEqual(saved);
      expect(pin).toHaveBeenCalledOnce();
    } finally { gate.resolve(); }
  });
  it('releases the queue after a failed write without automatically replaying it', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => {
      throw new DOMException('Storage quota exhausted', 'QuotaExceededError');
    });
    const results = await Promise.allSettled([
      journal.freeze(preflight, 'failed-request-key'),
      journal.load(),
      journal.freeze(preflight, 'replacement-request-key'),
    ]);
    expect(results).toMatchObject([
      { status: 'rejected', reason: { name: 'QuotaExceededError', message: 'Storage quota exhausted' } },
      { status: 'fulfilled', value: null },
      { status: 'fulfilled', value: { creation_key: 'replacement-request-key' } },
    ]);
    expect(write).toHaveBeenCalledTimes(2);
    expect((await journal.load())?.creation_key).toBe('replacement-request-key');
  });
  it('retains the original recovery identity when acknowledgement fails after the write', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    const setItem = Storage.prototype.setItem;
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(function(this: Storage, key, value) {
      setItem.call(this, key, value);
      vi.spyOn(documentCoordination(), 'synchronize').mockRejectedValueOnce(new Error('Persistence acknowledgement lost'));
    });
    const results = await Promise.allSettled([
      journal.freeze(preflight, 'original-request-key'),
      journal.load(),
      journal.freeze(preflight, 'replacement-request-key'),
    ]);
    expect(results).toMatchObject([
      { status: 'rejected', reason: expect.any(Error) },
      { status: 'fulfilled', value: { creation_key: 'original-request-key' } },
      { status: 'fulfilled', value: { creation_key: 'original-request-key' } },
    ]);
    expect(write).toHaveBeenCalledOnce();
    expect((await new FeedbackDispatchJournal(['source-subject']).load())?.creation_key).toBe('original-request-key');
  });
  it('cancels a queued lock and later local operations without clearing the uncertain original packet', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    await journal.record(receipt('uncertain'));
    const storedKey = Object.keys(localStorage).find(key => key.startsWith('feedback-dispatch:'))!;
    const held = deferred(), release = deferred();
    const holding = navigator.locks.request(`cognis:${apiUrl('') || window.location.origin}:draft:${storedKey}`, {}, async () => {
      held.resolve();
      await release.promise;
    });
    await held.promise;
    const request = vi.spyOn(navigator.locks, 'request');
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const cancelled = Promise.allSettled([
      journal.record(receipt('accepted')),
      journal.load(),
      journal.freeze(preflight, 'replacement-request-key'),
      journal.clearAccepted('original-request-key'),
    ]);
    try {
      await vi.waitFor(() => expect(request).toHaveBeenCalled());
      await documentCoordination().beginLogin();
      await documentCoordination().verified('other-reviewer@cognis.test');
      expect((await cancelled).every(result => result.status === 'rejected')).toBe(true);
      expect(write).not.toHaveBeenCalled();
      expect(await new FeedbackDispatchJournal(['source-subject']).load()).toBeNull();
    } finally { release.resolve(); await holding; }
    await documentCoordination().beginLogin();
    await documentCoordination().verified('feedback-journal@cognis.test');
    expect(await new FeedbackDispatchJournal(['source-subject']).load()).toMatchObject({
      creation_key: 'original-request-key', receipt: receipt('uncertain'),
    });
    await expect(journal.load()).rejects.toThrow();
  });
  it('orders accepted clearing, a load and an explicit new review without nested queue waits', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    await journal.record(receipt('accepted'));
    const [cleared, empty, next] = await Promise.all([
      journal.clearAccepted('original-request-key'),
      journal.load(),
      journal.freeze({ ...preflight, manifest_hash: 'b'.repeat(64) }, 'next-reviewed-request-key'),
    ]);
    expect(cleared).toBeUndefined();
    expect(empty).toBeNull();
    expect(next.creation_key).toBe('next-reviewed-request-key');
    expect(await journal.load()).toEqual(next);
  });
  it('fences a journal from a later authenticated account', async () => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'original-request-key');
    await documentCoordination().beginLogin(); await documentCoordination().verified('other-reviewer@cognis.test');
    await expect(journal.load()).rejects.toThrow();
    await expect(journal.clearAccepted('original-request-key')).rejects.toThrow();
  });
  it.each(['uncertain', 'rejected'] as const)('retains a %s receipt instead of treating it as accepted handoff', async delivery => {
    const journal = new FeedbackDispatchJournal(['source-subject']);
    await journal.freeze(preflight, 'retained-request');
    const receipt: SubmissionReceipt = { submission_id: crypto.randomUUID(), creation_key: 'retained-request',
      manifest_hash: preflight.manifest_hash, delivery, admission: null, work: 'unavailable', rejection: null,
      reconciling: false, version: 1, check_delivery_available: true, review_again_available: false };
    await journal.record(receipt);
    await expect(journal.clearAccepted('retained-request')).rejects.toThrow('Resolve delivery');
    expect((await journal.load())?.receipt).toEqual(receipt);
  });
});
