import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, expect, test, vi } from 'vitest';
import { CommentWorkspace } from '$lib/feedback/comment-workspace';
import FeedbackConversationWorkspace from './FeedbackConversationWorkspace.svelte';
import type { FeedbackThread } from '$lib/feedback/types';

const calls = vi.hoisted(() => ({ staging: vi.fn(), threads: vi.fn() }));

vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({
    capture: () => ({ assertCurrent() {}, signal: new AbortController().signal }),
    onInvalidated: () => () => {},
  }),
}));
vi.mock('$lib/feedback/api', () => ({
  feedbackApi: { staging: (...args: unknown[]) => calls.staging(...args), threads: (...args: unknown[]) => calls.threads(...args) },
}));
vi.mock('$lib/feedback/surface-portal', () => ({
  surfacePortal: (_node: HTMLElement, active: (value: boolean) => void) => { active(true); },
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); calls.staging.mockReset(); calls.threads.mockReset(); });

test('conversation comments follow the transcript viewport above the message input', async () => {
  calls.staging.mockResolvedValue({ items: [] });
  const observers: (() => void)[] = [];
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { observers.push(callback); }
    observe() {}
    disconnect() {}
  });
  const viewport = document.createElement('div');
  let bottom = 600;
  viewport.getBoundingClientRect = () => new DOMRect(100, 50, 400, bottom - 50);
  const workspace = new CommentWorkspace();
  workspace.showComments();
  render(FeedbackConversationWorkspace, { workspace, conversationId: 'conv_work_conformance_files', viewport });
  const panel = await screen.findByRole('region', { name: 'Comments' });
  await waitFor(() => expect(panel.style.bottom).toBe(`${innerHeight - 600 + 8}px`));
  expect(panel.style.right).toBe(`${innerWidth - 500 + 8}px`);
  expect(panel.style.width).toBe('384px');
  bottom = 420;
  observers.forEach(callback => callback());
  await waitFor(() => expect(panel.style.bottom).toBe(`${innerHeight - 420 + 8}px`));
  workspace.dispose();
});

test('fresh clients load resolved sent subjects and an open panel refreshes after agent completion', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const original = {
    entry_id: 'entry-question', entry_version_id: 'entry-question-v1',
    body: 'Keep it simple.', author: { kind: 'user', label: 'Reviewer', is_self: true },
    created_at: '2026-11-02T10:30:00Z', version: 1, delivery: [],
  };
  let thread = {
    thread_id: 'thread-question', subject_id: 'subject-commented-message', version: 1,
    state: 'open', hidden: false, entries: [original],
    anchor: { source: { kind: 'document', content_ref: 'document-reviewed' }, locator: { kind: 'whole' } },
  } as unknown as FeedbackThread;
  calls.staging.mockResolvedValue({ items: [], subject_ids: ['subject-commented-message'] });
  calls.threads.mockImplementation(async () => ({ items: [thread], next_cursor: null }));
  const workspace = new CommentWorkspace();
  workspace.showComments();
  const view = render(FeedbackConversationWorkspace, { workspace, conversationId: 'conv_review' });
  expect(await screen.findByRole('tab', { name: 'Open 1' })).toBeTruthy();
  thread = { ...thread, state: 'resolved', version: 2, entries: [...thread.entries, {
    ...original, entry_id: 'entry-answer', entry_version_id: 'entry-answer-v1',
    body: 'Kept the implementation simple.', author: { ...original.author, kind: 'agent', label: 'Assistant' },
  } as unknown as FeedbackThread['entries'][number]] };
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Resolved 1' })).toBeTruthy());
  await fireEvent.click(screen.getByRole('tab', { name: 'Resolved 1' }));
  expect(screen.getByText('Kept the implementation simple.')).toBeTruthy();
  view.unmount();
  // No in-memory controllers are needed by a second client opening this conversation.
  const fresh = new CommentWorkspace();
  fresh.showComments();
  render(FeedbackConversationWorkspace, { workspace: fresh, conversationId: 'conv_review' });
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Resolved 1' })).toBeTruthy());
  await fireEvent.click(screen.getByRole('tab', { name: 'Resolved 1' }));
  expect(screen.getByText('Kept the implementation simple.')).toBeTruthy();
});

test('the comment panel offers an unsent human reply for the next message on its own', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const entry = (id: string, body: string, delivery: unknown[]) => ({ entry_id: id, entry_version_id: `${id}-v1`, thread_id: 'thread-reply',
    body, author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, created_at: '2026-11-02T10:30:00Z', version: 1, delivery });
  const thread = {
    thread_id: 'thread-reply', subject_id: 'subject-reply', version: 2, state: 'open', hidden: false,
    entries: [entry('entry-root', 'Root comment already sent.', [{ submission_id: 's', manifest_hash: 'a', delivery: 'accepted' }]),
      entry('entry-reply', 'A new follow-up reply.', [])],
    anchor: { anchor_id: 'anchor-reply', source: { kind: 'document', content_ref: 'document-reply' }, locator: { kind: 'whole' } },
  } as unknown as FeedbackThread;
  // Only the reply is staged: the root was delivered earlier.
  calls.staging.mockResolvedValue({ conversation_id: 'conv_reply', version: 1, receipts: [], subject_ids: ['subject-reply'],
    items: [{ entry_id: 'entry-reply', entry_version_id: 'entry-reply-v1', staging_version: 1, inclusion: 'prepared', subject_id: 'subject-reply', anchor_id: 'anchor-reply' }] });
  calls.threads.mockResolvedValue({ items: [thread], next_cursor: null });
  const workspace = new CommentWorkspace();
  workspace.showComments();
  render(FeedbackConversationWorkspace, { workspace, conversationId: 'conv_reply' });
  const reply = await screen.findByRole('checkbox', { name: 'Include in message: A new follow-up reply.' });
  expect(reply).not.toBeChecked();
  expect(screen.queryByRole('checkbox', { name: 'Include in message: Root comment already sent.' })).toBeNull();
  expect(screen.getByText('0 of 1 in the next message')).toBeTruthy();
  workspace.dispose();
});

test('polling does not supersede a slow in-flight refresh', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const interval = vi.spyOn(window, 'setInterval');
  calls.staging.mockResolvedValue({ items: [], subject_ids: ['subject-slow'] });
  let finish!: (value: { items: FeedbackThread[]; next_cursor: null }) => void;
  calls.threads.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const workspace = new CommentWorkspace();
  workspace.showComments();
  render(FeedbackConversationWorkspace, { workspace, conversationId: 'conv_slow' });
  await waitFor(() => expect(calls.threads).toHaveBeenCalledOnce());
  const poll = interval.mock.calls.find(([, delay]) => delay === 5000)?.[0];
  expect(typeof poll).toBe('function');
  if (typeof poll !== 'function') throw new Error('Missing comments poll');
  poll(); poll(); poll();
  expect(calls.threads).toHaveBeenCalledOnce();
  finish({ items: [], next_cursor: null });
  await waitFor(() => expect(screen.queryByText('Loading comments…')).toBeNull());
  calls.threads.mockResolvedValue({ items: [], next_cursor: null });
  poll();
  await waitFor(() => expect(calls.threads).toHaveBeenCalledTimes(2));
});
