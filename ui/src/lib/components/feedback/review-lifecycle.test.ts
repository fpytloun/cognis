import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { FeedbackController } from '$lib/feedback/state';
import { FeedbackDispatchJournal } from '$lib/feedback/dispatch-journal';
import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackStagedItem, ResolvedFeedbackSource } from '$lib/feedback/types';
import FeedbackInclusionHarness from './FeedbackInclusionHarness.svelte';
import FeedbackReview from './FeedbackReview.svelte';

const calls = vi.hoisted(() => ({ staging: vi.fn(), stage: vi.fn(), preflight: vi.fn(), entries: vi.fn(),
  receipt: vi.fn(), submit: vi.fn(), destinations: vi.fn() }));
vi.mock('$lib/feedback/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/feedback/api')>();
  return { ...original, feedbackApi: { ...original.feedbackApi, ...calls } };
});
const source: ResolvedFeedbackSource = {
  subject_id: 'source-panel-subject', source_token: 'source-panel-proof',
  source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
  context: { kind: 'chat', conversation_id: 'source-panel-conversation' }, content_digest: 'a'.repeat(64),
  availability: 'available', permitted_locator_kinds: ['whole'], lineage_id: null, revision: 1,
  latest_content_ref: null, title: 'Panel source', format: 'plain', canonical_text: null,
  capabilities: { read: true, comment: false, moderate: false, dispatch: true, disclose: false },
};
const entry: FeedbackEntryVersion = { entry_id: 'panel-entry', entry_version_id: 'panel-entry-version',
  version: 1, thread_id: 'panel-thread', body: 'Reviewed source correction', audience: 'internal',
  author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, reply_to_entry_id: null,
  created_at: '2026-10-26T17:00:00Z', delivery: [] };
const anchor = {
  schema_version: 1 as const, anchor_id: 'panel-anchor', source: source.source,
  locator: { kind: 'whole' as const }, content_digest: source.content_digest,
};
const preflight: FeedbackPreflight = { preflight_id: 'panel-review', manifest_hash: 'a'.repeat(64),
  expires_at: '2099-01-01T00:00:00Z', selection: [{ entry_version_id: entry.entry_version_id, expected_entry_version: 1, staging_version: 2 }],
  context_items: [{ entry, anchor, role: 'instruction' }], source_summaries: [source], destination: { kind: 'origin_chat', conversation_id: 'source-panel-conversation' },
  update_mode: 'revise_source', warnings: [], task_consequences: null };
let row: FeedbackStagedItem;
beforeEach(async () => {
  localStorage.clear();
  await loginFixtureAccount();
  row = { entry_id: entry.entry_id, entry_version_id: entry.entry_version_id, staging_version: 1,
    inclusion: 'prepared', subject_id: source.subject_id, anchor_id: 'panel-anchor' };
  calls.staging.mockReset().mockImplementation(async () => ({ conversation_id: 'source-panel-conversation', version: row.staging_version, items: [{ ...row }], receipts: [] }));
  calls.stage.mockReset().mockImplementation(async () => { row = { ...row, staging_version: 2, inclusion: 'included' }; return row; });
  calls.entries.mockReset().mockResolvedValue({ items: [entry] });
  calls.preflight.mockReset().mockResolvedValue(preflight);
  calls.destinations.mockReset().mockResolvedValue({ options: [] });
  calls.receipt.mockReset(); calls.submit.mockReset();
});
afterEach(async () => { cleanup(); await auth.clear(); });
it('includes from the source panel and immediately enables the composer without focus refresh', async () => {
  const controller = new FeedbackController(source.source, source.context);
  controller.patch({ source, threads: [{ thread_id: entry.thread_id, subject_id: source.subject_id, version: 1,
    state: 'open', hidden: false, audience: 'internal', entries: [entry],
    anchor }] });
  const sent = vi.fn();
  render(FeedbackInclusionHarness, { controller, conversationId: 'source-panel-conversation', onSend: sent });
  expect(screen.getByRole('checkbox', { name: /Reviewed source correction/ })).toBeChecked();
  await fireEvent.click(await screen.findByRole('button', { name: 'Include 1 comment in message' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send reviewed feedback' })).toBeEnabled());
  await fireEvent.click(screen.getByRole('button', { name: 'Send reviewed feedback' }));
  expect(sent).toHaveBeenCalledOnce();
  controller.dispose();
});
it.each(['close', 'invalidate'] as const)('does not dispatch after %s while receipt lookup awaits', async (transition) => {
  const frozen = { ...preflight, selection: [], destination: { kind: 'new_followup' as const, agent_id: 'laforge', project_id: null, title: null } };
  await new FeedbackDispatchJournal([source.subject_id]).freeze(frozen, 'retained-standalone-request');
  let rejectReceipt!: (error: unknown) => void;
  calls.receipt.mockImplementation(() => new Promise((_resolve, reject) => { rejectReceipt = reject; }));
  const view = render(FeedbackReview, { props: { entries: [], subjectIds: [source.subject_id], context: { kind: 'standalone' }, onClose: () => {} } });
  await fireEvent.click(await screen.findByRole('button', { name: 'Retry original request' }));
  await waitFor(() => expect(calls.receipt).toHaveBeenCalledOnce());
  if (transition === 'close') view.unmount();
  else await documentCoordination().invalidate();
  rejectReceipt(Object.assign(new Error('Receipt not found'), { status: 404 }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(calls.submit).not.toHaveBeenCalled();
});
