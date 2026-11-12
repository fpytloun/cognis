import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { documentCoordination } from '$lib/document-coordination';
import type { FeedbackThread } from '$lib/feedback/types';
import AgentCommentReplies from './AgentCommentReplies.svelte';

const api = vi.hoisted(() => ({ thread: vi.fn() }));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: api }));
const thread: FeedbackThread = {
  thread_id: crypto.randomUUID(), subject_id: crypto.randomUUID(), version: 1, state: 'open', hidden: false, audience: 'internal',
  anchor: { schema_version: 1, anchor_id: crypto.randomUUID(),
    source: { kind: 'document', content_ref: `dlv_${crypto.randomUUID().replaceAll('-', '')}` },
    content_digest: 'a'.repeat(64), locator: { kind: 'whole' } },
  entries: [],
};
thread.entries.push({ entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), thread_id: thread.thread_id,
  version: 1, body: 'Who owns the rollback?', author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
  audience: 'internal', created_at: '2026-01-01T00:00:00Z', reply_to_entry_id: null, delivery: [] });
const replies = [{ callId: crypto.randomUUID(), commentId: thread.thread_id, body: '**Operations** owns the rollback.', resolved: false }];
beforeEach(async () => {
  api.thread.mockReset().mockResolvedValue(thread);
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('reviewer@cognis.test');
});
afterEach(() => { cleanup(); document.body.innerHTML = ''; });

it('shows original context and does not mistake an unbound document for a located annotation', async () => {
  const source = document.createElement('div');
  source.dataset.deliverableId = thread.anchor.source.kind === 'document' ? thread.anchor.source.content_ref : '';
  source.scrollIntoView = vi.fn();
  document.body.append(source);
  render(AgentCommentReplies, { replies });
  const comment = await screen.findByRole('button', { name: 'Locate comment: Who owns the rollback?' });
  expect(screen.getByText('Reviewer')).toBeVisible();
  expect(screen.getByText('Whole document')).toBeVisible();
  expect(screen.getByText('Operations', { selector: 'strong' })).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open original revision' })).toHaveAttribute('href', expect.stringContaining(source.dataset.deliverableId!));
  expect(screen.queryByText('Comment 1')).not.toBeInTheDocument();
  await fireEvent.click(comment);
  expect(source).not.toHaveAttribute('data-feedback-located');
  expect(await screen.findByRole('status')).toHaveTextContent('The annotation is not available in this view.');
});

it('keeps the answer readable when the original comment cannot be read', async () => {
  api.thread.mockRejectedValue(new Error('Not authorized'));
  render(AgentCommentReplies, { replies });
  expect(await screen.findByText('Original comment unavailable')).toBeVisible();
  expect(screen.getByText('Operations', { selector: 'strong' })).toBeVisible();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});

it('withdraws original comment data on account invalidation, including late reads', async () => {
  let finish!: (value: FeedbackThread) => void;
  api.thread.mockReturnValue(new Promise<FeedbackThread>(resolve => { finish = resolve; }));
  render(AgentCommentReplies, { replies });
  await documentCoordination().beginLogin();
  finish(thread);
  await vi.waitFor(() => expect(screen.queryByText('Who owns the rollback?')).not.toBeInTheDocument());
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});
