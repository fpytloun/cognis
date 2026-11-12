import 'fake-indexeddb/auto';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { FeedbackDispatchJournal } from '$lib/feedback/dispatch-journal';
import { ConversationFeedback, conversationFeedback } from '$lib/feedback/staging';
import { conversationComposer } from '$lib/chat-v2/conversation-composer';
import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackStagingSnapshot, SubmissionReceipt } from '$lib/feedback/types';
import FeedbackReview from './FeedbackReview.svelte';

const api = vi.hoisted(() => ({
  destinations: vi.fn(), preflight: vi.fn(), receipt: vi.fn(), submit: vi.fn(),
  staging: vi.fn(), stage: vi.fn(),
}));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: api }));

function fixture() {
  const subject = crypto.randomUUID(), conversation = crypto.randomUUID();
  const entry: FeedbackEntryVersion = {
    entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), version: 3,
    thread_id: crypto.randomUUID(), body: 'Keep this exact reviewed comment.',
    author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, audience: 'private',
    reply_to_entry_id: null, created_at: '2026-10-27T00:00:00Z', delivery: [],
  };
  const source = { kind: 'document' as const, content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}` };
  const packet: FeedbackPreflight = {
    preflight_id: crypto.randomUUID(), manifest_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    destination: { kind: 'producer_conversation', conversation_id: conversation }, update_mode: 'answer_only',
    selection: [{ entry_version_id: entry.entry_version_id, expected_entry_version: 3 }],
    context_items: [{ role: 'instruction', entry, anchor: {
      schema_version: 1, anchor_id: crypto.randomUUID(), source, locator: { kind: 'whole' }, content_digest: 'c'.repeat(64),
    } }],
    source_summaries: [{ subject_id: subject, source_token: crypto.randomUUID(), source,
      content_digest: 'c'.repeat(64), availability: 'available', permitted_locator_kinds: ['whole'],
      context: { kind: 'standalone' }, lineage_id: crypto.randomUUID(), revision: 2, latest_content_ref: null,
      capabilities: { read: true, comment: true, moderate: true, dispatch: true, disclose: false },
      title: 'Pinned rich dashboard', format: 'rich', canonical_text: null }],
    warnings: ['Selected exact entry version.'], task_consequences: null,
  };
  return { subject, conversation, entry, packet };
}

beforeEach(async () => {
  vi.resetAllMocks();
  localStorage.clear();
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('reactive-feedback-review@cognis.test');
  api.receipt.mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('sends request-driven feedback without a Result choice and preserves the selected destination', async () => {
  const f = fixture();
  const next = { kind: 'new_followup' as const, agent_id: crypto.randomUUID() };
  api.destinations.mockResolvedValue({ options: [
    { destination: f.packet.destination, label: 'Producing conversation', available: true, recommended: true },
    { destination: next, label: 'New conversation', available: true },
    { destination: null, label: 'Original task', available: false, reason: 'Task is unavailable.' },
  ] });
  api.preflight.mockImplementation(async request => ({ ...f.packet, destination: request.destination, update_mode: request.update_mode }));
  render(FeedbackReview, { props: { entries: [f.entry], subjectIds: [f.subject],
    context: { kind: 'standalone' }, onClose: vi.fn() } });
  const destination = await screen.findByRole('combobox', { name: 'Send to' });
  await waitFor(() => expect(destination).toHaveValue('0'));
  expect(screen.getByRole('option', { name: 'Original task — Task is unavailable.' })).toBeDisabled();
  expect(screen.queryByRole('combobox', { name: 'Result' })).not.toBeInTheDocument();
  await fireEvent.change(destination, { target: { value: '1' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send 1 comment' })).toBeEnabled());
  await fireEvent.click(screen.getByRole('button', { name: 'Send 1 comment' }));
  await waitFor(() => expect(api.submit).toHaveBeenCalledOnce());
  expect(api.preflight).toHaveBeenCalledOnce();
  expect(api.preflight.mock.lastCall?.[0]).toMatchObject({ destination: next, update_mode: 'follow_request',
    selection: [{ entry_version_id: f.entry.entry_version_id, expected_entry_version: 3 }] });
  expect((await new FeedbackDispatchJournal([f.subject]).load())?.preflight).toMatchObject({
    destination: next, update_mode: 'follow_request',
  });
});

async function previewStandalone(f: ReturnType<typeof fixture>) {
  api.destinations.mockResolvedValue({ options: [{ destination: f.packet.destination,
    label: 'Producing conversation', available: true, reason: null }] });
  api.preflight.mockResolvedValue(f.packet);
  const component = render(FeedbackReview, { props: { entries: [f.entry], subjectIds: [f.subject],
    context: { kind: 'standalone' }, onClose: vi.fn() } });
  await screen.findByRole('option', { name: 'Producing conversation' });
  await fireEvent.change(screen.getByRole('combobox', { name: 'Send to' }), { target: { value: '0' } });
  const note = screen.getByRole('textbox', { name: 'Instruction' });
  for (const value of ['P', 'Preserve this overall instruction.']) await fireEvent.input(note, { target: { value } });
  // Typing never prepares a packet or locks the note; preparation happens on Send.
  await new Promise(resolve => setTimeout(resolve, 250));
  expect(api.preflight).not.toHaveBeenCalled();
  expect(note).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Send 1 comment' })).toBeEnabled();
  return component;
}

it('freezes the full mounted reactive preflight and replays its exact request after response loss and reload', async () => {
  const f = fixture();
  api.submit.mockRejectedValueOnce(new TypeError('Response lost'));
  const first = await previewStandalone(f);
  await fireEvent.click(screen.getByRole('button', { name: 'Send 1 comment' }));
  await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(1));
  expect(await screen.findByRole('alert')).toHaveTextContent('Response lost');
  const request = structuredClone(api.submit.mock.calls[0][0]);
  const frozen = await new FeedbackDispatchJournal([f.subject]).load();
  expect(frozen).toEqual({ creation_key: request.creation_key, preflight: f.packet,
    receipt: null, overall_message: 'Preserve this overall instruction.' });
  expect(request).toEqual({ creation_key: frozen!.creation_key, preflight_id: f.packet.preflight_id,
    manifest_hash: f.packet.manifest_hash, replaces_rejected_submission_id: null });
  first.unmount();

  const receipt: SubmissionReceipt = { submission_id: crypto.randomUUID(), creation_key: request.creation_key,
    manifest_hash: f.packet.manifest_hash, delivery: 'accepted', work: 'queued', reconciling: false, version: 1,
    admission: { kind: 'conversation_turn', conversation_id: f.conversation,
      request_id: crypto.randomUUID(), turn_id: crypto.randomUUID() }, rejection: null,
    check_delivery_available: false, review_again_available: false };
  api.submit.mockResolvedValueOnce(receipt);
  // New caller input cannot replace the already-frozen packet or its instruction.
  render(FeedbackReview, { props: { entries: [{ ...f.entry, version: 4, body: 'New caller input' }],
    subjectIds: [f.subject], context: { kind: 'standalone' }, onClose: vi.fn() } });
  const retry = await screen.findByRole('button', { name: 'Retry original request' });
  expect(screen.getByRole('textbox', { name: 'Instruction' })).toHaveValue('Preserve this overall instruction.');
  expect(screen.getByRole('textbox', { name: 'Instruction' })).toBeDisabled();
  await fireEvent.click(retry);
  await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(2));
  expect(api.submit.mock.calls[1][0]).toEqual(request);
  // Once accepted, the retained request is cleared and a short confirmation remains.
  expect(await screen.findByText(/^Sent 1 comment to/)).toBeVisible();
  expect(await new FeedbackDispatchJournal([f.subject]).load()).toBeNull();
  expect(api.preflight).toHaveBeenCalledTimes(1);
});

it('hands a cloneable complete DTO to real chat confirmation and retains only the latest included manifest', async () => {
  const f = fixture();
  const original = ConversationFeedback.prototype.confirm;
  const handedOff: FeedbackPreflight[] = [];
  vi.spyOn(ConversationFeedback.prototype, 'confirm').mockImplementation(async function(
    this: ConversationFeedback, packet: FeedbackPreflight, lease?: Pick<DocumentLease, 'assertCurrent'>,
  ) {
    // A faithful immutable handoff boundary: reject Svelte proxies using native clone.
    handedOff.push(structuredClone(packet));
    return original.call(this, packet, lease);
  });
  const composer = conversationComposer();
  composer.update(f.conversation, { ...composer.hydrate(f.conversation).input, text: 'Keep the latest typed instruction.',
    input_revision: crypto.randomUUID() });
  for (const [index, hash] of ['d'.repeat(64), 'e'.repeat(64)].entries()) {
    const packet: FeedbackPreflight = { ...f.packet, preflight_id: crypto.randomUUID(), manifest_hash: hash,
      destination: { kind: 'origin_chat', conversation_id: f.conversation }, update_mode: 'revise_source',
      selection: [{ entry_version_id: f.entry.entry_version_id, expected_entry_version: 3, staging_version: index + 1 }] };
    const snapshot: FeedbackStagingSnapshot = { conversation_id: f.conversation, version: index + 1, receipts: [],
      items: [{ entry_id: f.entry.entry_id, entry_version_id: f.entry.entry_version_id, staging_version: index + 1,
        inclusion: 'included', subject_id: f.subject, anchor_id: packet.context_items[0].anchor.anchor_id }] };
    api.staging.mockResolvedValue(snapshot);
    api.stage.mockResolvedValue(snapshot.items[0]);
    api.preflight.mockResolvedValue(packet);
    const close = vi.fn();
    const component = render(FeedbackReview, { props: { entries: [f.entry], subjectIds: [f.subject],
      context: { kind: 'chat', conversation_id: f.conversation }, onClose: close } });
    await fireEvent.click(screen.getByRole('button', { name: 'Include 1 comment in message' }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    const input = composer.hydrate(f.conversation).input;
    expect(input.text).toBe('Keep the latest typed instruction.');
    expect(input.feedback).toMatchObject({ preflight_id: packet.preflight_id, manifest_hash: hash });
    expect(conversationFeedback(f.conversation).review(input.feedback!)).toEqual(packet);
    expect(handedOff[index]).toEqual(packet);
    component.unmount();
  }
  expect(api.submit).not.toHaveBeenCalled();
});

it('keeps a mounted review account-fenced across confirmation', async () => {
  const f = fixture();
  await previewStandalone(f);
  await documentCoordination().beginLogin();
  await documentCoordination().verified('different-reactive-reviewer@cognis.test');
  await fireEvent.click(screen.getByRole('button', { name: 'Send 1 comment' }));
  await screen.findByRole('alert');
  expect(api.submit).not.toHaveBeenCalled();
  expect(await new FeedbackDispatchJournal([f.subject]).load()).toBeNull();
});

it('retains every nested task consequence only after explicit consequence confirmation', async () => {
  const f = fixture();
  f.packet.destination = { kind: 'task_revision', task_id: crypto.randomUUID(), expected_attempt: 2, target_step: 'implement' };
  f.packet.update_mode = 'revise_source';
  f.packet.task_consequences = {
    safety_token: crypto.randomUUID(), expected_attempt: 2, workflow_definition_digest: 'f'.repeat(64),
    target_step: 'implement', downstream_steps: ['validate', 'review'],
    side_effect_warning: 'Reopens implementation and invalidates downstream work.',
    pending_interactions: [{ notification_id: crypto.randomUUID(), revision: 3,
      notification_type: 'question', status: 'pending' }],
  };
  api.submit.mockRejectedValueOnce(new TypeError('Response lost'));
  await previewStandalone(f);
  const send = screen.getByRole('button', { name: 'Send 1 comment' });
  await fireEvent.click(send);
  const confirm = await screen.findByRole('checkbox', { name: 'I confirm these task consequences' });
  expect(send).toBeDisabled();
  expect(api.submit).not.toHaveBeenCalled();
  await fireEvent.click(confirm);
  await fireEvent.click(send);
  await waitFor(() => expect(api.submit).toHaveBeenCalledOnce());
  expect((await new FeedbackDispatchJournal([f.subject]).load())?.preflight).toEqual(f.packet);
});

function acceptedReceipt(f: ReturnType<typeof fixture>, creationKey: string): SubmissionReceipt {
  return { submission_id: crypto.randomUUID(), creation_key: creationKey, manifest_hash: f.packet.manifest_hash,
    delivery: 'accepted', work: 'queued', reconciling: false, version: 1, rejection: null,
    admission: { kind: 'conversation_turn', conversation_id: f.conversation,
      request_id: crypto.randomUUID(), turn_id: crypto.randomUUID() },
    check_delivery_available: false, review_again_available: false };
}

it('starts a deliberate new review from current caller versions after an accepted queued handoff and remount', async () => {
  const f = fixture();
  f.packet.update_mode = 'answer_only';
  api.submit.mockImplementation(async request => acceptedReceipt(f, request.creation_key));
  const first = await previewStandalone(f);
  await fireEvent.click(screen.getByRole('button', { name: 'Send 1 comment' }));
  // The sent state is a short confirmation, not delivery diagnostics.
  expect(await screen.findByText('Sent 1 comment to Producing conversation.')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open conversation' })).toHaveAttribute('href', `/chat/${encodeURIComponent(f.conversation)}`);
  expect(screen.queryByRole('button', { name: 'Retry original request' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Check saved send' })).not.toBeInTheDocument();
  expect(screen.queryByText(/Delivery:|immutable/)).not.toBeInTheDocument();
  // An accepted send needs no recovery: the retained request is cleared and the form is ready again.
  const journal = new FeedbackDispatchJournal([f.subject]);
  expect(await journal.load()).toBeNull();
  expect(screen.getByRole('textbox', { name: 'Instruction' })).toHaveValue('');
  expect(api.submit).toHaveBeenCalledTimes(1);
  first.unmount();
  const current = { ...f.entry, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(),
    version: 1, body: 'New comment selected by the current caller.' };
  const nextPacket: FeedbackPreflight = { ...f.packet, preflight_id: crypto.randomUUID(), manifest_hash: 'b'.repeat(64),
    selection: [{ entry_version_id: current.entry_version_id, expected_entry_version: 1 }],
    context_items: [{ ...f.packet.context_items[0], entry: current }] };
  api.preflight.mockResolvedValue(nextPacket);
  api.submit.mockImplementation(async request => ({ ...acceptedReceipt(f, request.creation_key),
    manifest_hash: nextPacket.manifest_hash }));
  // Reopening after an accepted send starts a fresh review with the current comments.
  render(FeedbackReview, { props: { entries: [current], subjectIds: [f.subject],
    context: { kind: 'standalone' }, onClose: vi.fn() } });
  await waitFor(() => expect(screen.getByRole('group', { name: 'Send to' })).toBeEnabled());
  expect(screen.getByRole('textbox', { name: 'Instruction' })).toHaveValue('');
  expect(screen.getByText(current.body)).toBeVisible();
  expect(screen.queryByText(f.entry.body)).not.toBeInTheDocument();
  expect(api.submit).toHaveBeenCalledTimes(1);
  await fireEvent.change(screen.getByRole('combobox', { name: 'Send to' }), { target: { value: '0' } });
  await fireEvent.input(screen.getByRole('textbox', { name: 'Instruction' }), { target: { value: 'New deliberate instruction.' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send 1 comment' })).toBeEnabled());
  await fireEvent.click(screen.getByRole('button', { name: 'Send 1 comment' }));
  await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(2));
  expect(api.preflight.mock.lastCall?.[0]).toMatchObject({ selection: nextPacket.selection,
    update_mode: 'follow_request', overall_message: 'New deliberate instruction.' });
  // Each send uses its own idempotency key.
  const keys = api.submit.mock.calls.map(call => call[0].creation_key);
  expect(new Set(keys).size).toBe(2);
  await waitFor(async () => expect(await journal.load()).toBeNull());
});

it.each(['ready', 'uncertain', 'rejected'] as const)('never offers an accepted reset for a %s journal', async delivery => {
  const f = fixture(), key = crypto.randomUUID();
  const journal = new FeedbackDispatchJournal([f.subject]);
  await journal.freeze(f.packet, key, 'Retain the original instruction.');
  if (delivery !== 'ready') await journal.record({ ...acceptedReceipt(f, key), delivery,
    admission: null, work: 'unavailable', check_delivery_available: delivery === 'uncertain' });
  api.destinations.mockResolvedValue({ options: [] });
  render(FeedbackReview, { props: { entries: [{ ...f.entry, body: 'Different unreviewed caller input' }],
    subjectIds: [f.subject], context: { kind: 'standalone' }, onClose: vi.fn() } });
  await screen.findByRole('button', { name: 'Check saved send' });
  expect(screen.queryByRole('button', { name: 'Done' })).not.toBeInTheDocument();
  expect(screen.getByRole('group', { name: 'Send to' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: 'Instruction' })).toHaveValue('Retain the original instruction.');
  expect((await journal.load())?.creation_key).toBe(key);
  await expect(journal.clearAccepted(key)).rejects.toThrow('Resolve delivery');
  expect(api.submit).not.toHaveBeenCalled();
});

it('fences clearing an accepted send if the account changes while it is pending', async () => {
  const f = fixture(), key = crypto.randomUUID();
  const journal = new FeedbackDispatchJournal([f.subject]);
  await journal.freeze(f.packet, key);
  const receipt = acceptedReceipt(f, key);
  await journal.record(receipt);
  api.destinations.mockResolvedValue({ options: [] });
  let resume!: () => void;
  const pending = new Promise<void>(resolve => { resume = resolve; });
  const clear = FeedbackDispatchJournal.prototype.clearAccepted;
  const clearing = vi.spyOn(FeedbackDispatchJournal.prototype, 'clearAccepted')
    .mockImplementation(async function(this: FeedbackDispatchJournal, reviewedKey: string) {
      await pending; return clear.call(this, reviewedKey);
    });
  render(FeedbackReview, { props: { entries: [f.entry], subjectIds: [f.subject],
    context: { kind: 'standalone' }, onClose: vi.fn() } });
  await waitFor(() => expect(clearing).toHaveBeenCalledWith(key));
  await documentCoordination().beginLogin();
  await documentCoordination().verified('different-reset-owner@cognis.test');
  resume();
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: 'Send 1 comment' })).toBeDisabled();
  expect(api.submit).not.toHaveBeenCalled();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('reactive-feedback-review@cognis.test');
  expect((await new FeedbackDispatchJournal([f.subject]).load())?.receipt).toEqual(receipt);
});
