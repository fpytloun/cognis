import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { feedbackApi } from '$lib/feedback/api';
import { loadFeedbackController } from '$lib/feedback/controller-loader';
import type { FeedbackThread, ResolvedFeedbackSource } from '$lib/feedback/types';
import type { FeedbackController } from '$lib/feedback/state';
import FeedbackHostLoaderHarness from './FeedbackHostLoaderHarness.svelte';
import FeedbackCountHarness from './FeedbackCountHarness.svelte';

vi.mock('$lib/feedback/controller-loader', () => ({ loadFeedbackController: vi.fn() }));
const source: ResolvedFeedbackSource = {
  source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
  subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
  context: { kind: 'chat', conversation_id: 'loader-conversation' },
  availability: 'available', permitted_locator_kinds: ['whole'], lineage_id: null, revision: 1,
  latest_content_ref: null, title: 'Comment count source', format: 'text', canonical_text: null,
  capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false },
};
function thread(state: 'open' | 'resolved' = 'open', hidden = false): FeedbackThread {
  return { thread_id: crypto.randomUUID(), subject_id: source.subject_id, version: 1, state, hidden,
    audience: 'internal', entries: [], anchor: { schema_version: 1, anchor_id: crypto.randomUUID(),
      source: source.source, content_digest: source.content_digest, locator: { kind: 'whole' } } };
}
beforeEach(async () => {
  await loginFixtureAccount();
  vi.spyOn(feedbackApi, 'resolve').mockResolvedValue(source);
  vi.spyOn(feedbackApi, 'threads').mockResolvedValue({ items: [thread()], next_cursor: null });
});
afterEach(async () => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); await auth.clear(); });
const badge = () => screen.getByRole('button', { name: /^Comments/ });
const entry = (author: 'verified_user' | 'agent', delivery: 'accepted' | null = null): FeedbackThread['entries'][number] => ({
  entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), thread_id: crypto.randomUUID(), version: 1, body: 'Comment',
  // Agent replies come after the reviewer's comment.
  audience: 'internal', created_at: author === 'agent' ? '2026-11-10T08:05:00Z' : '2026-11-10T08:00:00Z', reply_to_entry_id: null,
  author: { kind: author, label: author === 'agent' ? 'Forge' : 'Reviewer', is_self: author !== 'agent' },
  delivery: delivery ? [{ submission_id: crypto.randomUUID(), manifest_hash: 'b'.repeat(64), delivery }] : [],
});

it('counts what waits on the reviewer: unsent and answered comments, not ones the agent is still on', async () => {
  const sent = { ...thread(), entries: [entry('verified_user', 'accepted')] };
  const answered = { ...thread(), entries: [entry('verified_user', 'accepted'), entry('agent')] };
  const unsent = { ...thread(), entries: [entry('verified_user')] };
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [sent, answered, unsent], next_cursor: null });
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('2'));
  expect(badge()).toHaveAccessibleName('Comments, 2 need you');
});

it('loads all open comments before interaction without downloading the annotation controller', async () => {
  vi.mocked(feedbackApi.threads)
    .mockResolvedValueOnce({ items: [thread(), thread('resolved'), thread('open', true)], next_cursor: 'next-page' })
    .mockResolvedValueOnce({ items: [thread()], next_cursor: null });
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('2'));
  expect(feedbackApi.threads).toHaveBeenLastCalledWith(source.subject_id, 'next-page', expect.any(AbortSignal));
  expect(loadFeedbackController).not.toHaveBeenCalled();
  expect(screen.queryByRole('region', { name: 'Feedback panel' })).toBeNull();
});

it('refreshes after feedback changes and withdraws the count when access fails', async () => {
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [thread(), thread()], next_cursor: null });
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(badge()).toHaveTextContent('2'));
  vi.mocked(feedbackApi.resolve).mockRejectedValue(Object.assign(new Error('Forbidden'), { status: 403 }));
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(badge()).not.toHaveTextContent(/\d/));
});

it('does not resurrect a stale source count after replacement', async () => {
  let release!: (page: Awaited<ReturnType<typeof feedbackApi.threads>>) => void;
  vi.mocked(feedbackApi.threads).mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  const view = render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(feedbackApi.threads).toHaveBeenCalledOnce());
  await view.rerender({ source: { kind: 'document', content_ref: `dlv_${crypto.randomUUID().replaceAll('-', '')}` } });
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  release({ items: [thread(), thread()], next_cursor: null });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(badge()).toHaveTextContent('1');
});

it('discards a count requested for the previous context', async () => {
  let release!: (page: Awaited<ReturnType<typeof feedbackApi.threads>>) => void;
  vi.mocked(feedbackApi.threads).mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  const view = render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(feedbackApi.threads).toHaveBeenCalledOnce());
  await view.rerender({ context: { kind: 'chat', conversation_id: crypto.randomUUID() } });
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  release({ items: [thread(), thread()], next_cursor: null });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(badge()).toHaveTextContent('1');
});

it('clears counts on account invalidation', async () => {
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  await documentCoordination().invalidate();
  await waitFor(() => expect(badge()).not.toHaveTextContent(/\d/));
});

it('does not publish a partial or late total after invalidation during pagination', async () => {
  let release!: (page: Awaited<ReturnType<typeof feedbackApi.threads>>) => void;
  vi.mocked(feedbackApi.threads)
    .mockResolvedValueOnce({ items: [thread()], next_cursor: 'next-page' })
    .mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(feedbackApi.threads).toHaveBeenCalledTimes(2));
  expect(badge()).not.toHaveTextContent(/\d/);
  await documentCoordination().invalidate();
  release({ items: [thread()], next_cursor: null });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(badge()).not.toHaveTextContent(/\d/);
});

it('does not list threads when source read capability is absent', async () => {
  vi.mocked(feedbackApi.resolve).mockResolvedValue({ ...source, capabilities: { ...source.capabilities, read: false } });
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(feedbackApi.resolve).toHaveBeenCalledOnce());
  expect(feedbackApi.threads).not.toHaveBeenCalled();
  expect(badge()).not.toHaveTextContent(/\d/);
});

it('does not fall back to loaded controller highlights when the document count is unavailable', async () => {
  const view = render(FeedbackCountHarness, { commentCount: 2 });
  expect(badge()).toHaveTextContent('2');
  await view.rerender({ commentCount: null });
  expect(badge()).not.toHaveTextContent(/\d/);
  await view.rerender({ commentCount: undefined });
  expect(badge()).toHaveTextContent('1');
});

it('ignores an older refresh response after a newer refresh has completed', async () => {
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  let release!: (page: Awaited<ReturnType<typeof feedbackApi.threads>>) => void;
  vi.mocked(feedbackApi.threads).mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(feedbackApi.threads).toHaveBeenCalledTimes(2));
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [thread(), thread()], next_cursor: null });
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(badge()).toHaveTextContent('2'));
  release({ items: [], next_cursor: null });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(badge()).toHaveTextContent('2');
});

it('refreshes after controller-only resolve/reopen mutations and clears counts after denied revalidation', async () => {
  const state = await import('$lib/feedback/state');
  let controller!: FeedbackController;
  vi.mocked(loadFeedbackController).mockResolvedValue({
    ...state,
    FeedbackController: class extends state.FeedbackController {
      constructor(...args: ConstructorParameters<typeof state.FeedbackController>) {
        super(...args); controller = this;
      }
    },
  });
  vi.spyOn(feedbackApi, 'draft').mockRejectedValue(Object.assign(new Error('No draft'), { status: 404 }));
  const item = thread();
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [item], next_cursor: null });
  render(FeedbackHostLoaderHarness);
  await waitFor(() => expect(badge()).toHaveTextContent('1'));
  await fireEvent.click(badge());
  await waitFor(() => expect(controller).toBeDefined());
  await waitFor(() => expect(screen.getByRole('button', { name: 'Hide comments' })).toHaveTextContent('1'));
  const activeBadge = () => screen.getByRole('button', { name: 'Hide comments' });
  vi.spyOn(feedbackApi, 'changeThread').mockResolvedValue({ ...item, state: 'resolved' });
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [{ ...item, state: 'resolved' }], next_cursor: null });
  await controller.resolve(item);
  await waitFor(() => expect(activeBadge()).not.toHaveTextContent(/\d/));
  vi.mocked(feedbackApi.threads).mockResolvedValue({ items: [item], next_cursor: null });
  await controller.resolve({ ...item, state: 'resolved' });
  await waitFor(() => expect(activeBadge()).toHaveTextContent('1'));
  vi.mocked(feedbackApi.resolve).mockRejectedValue(Object.assign(new Error('Forbidden'), { status: 403 }));
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(activeBadge()).not.toHaveTextContent(/\d/));
});
