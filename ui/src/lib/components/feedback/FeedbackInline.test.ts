import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';
import { tick } from 'svelte';
import FeedbackInline from './FeedbackInline.svelte';
import { FeedbackPresenterSession } from '$lib/feedback/presenter-session';
import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
import type { FeedbackPresentationPort, FeedbackPresentationState } from '$lib/feedback/presentation';
import type { FeedbackSurfaceBinding, FeedbackThread, ResolvedFeedbackSource } from '$lib/feedback/types';

afterEach(cleanup);
it.each([false, true])('preserves a cold-mounted diff target with native gutter slot=%s', async (gutter) => {
  const source: ResolvedFeedbackSource = {
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(),
    source: { kind: 'work_diff', activity_scope_id: crypto.randomUUID(), source_item_id: crypto.randomUUID(),
      source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 1, event_type: 'tool_result' },
      file_ordinal: 0, root_id: null, path: 'src/handler.py', old_path: null },
    content_digest: 'f'.repeat(64), context: { kind: 'chat', conversation_id: crypto.randomUUID() },
    availability: 'available', permitted_locator_kinds: ['whole', 'diff'], lineage_id: null,
    revision: 1, latest_content_ref: null, title: 'Handler', format: 'diff', canonical_text: null,
    capabilities: { read: true, comment: true, dispatch: true, moderate: false, disclose: false },
  };
  const state = writable<FeedbackPresentationState>({ source, threads: [], selected: '', mode: 'view',
    candidate: null, draft: '', saving: false, pending_body: null, error: '', loading: false,
    next_cursor: null, open: false, view: 'threads' });
  const session = new FeedbackPresenterSession();
  const binding: FeedbackSurfaceBinding = { source, context: source.context, mode: 'view',
    supported_locator_kinds: source.permitted_locator_kinds, completeness: 'available', highlights: [],
    selected_thread_id: '', onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(),
    registerAdapter: () => () => {}, resolveTextScope: vi.fn() };
  const geometry = () => ({ space: 'client' as const, rects: [{ left: 100, top: 200, width: 130, height: 20 }] });
  const target = { candidate: { locator: { kind: 'diff' as const, scope: 'lines' as const, side: 'new' as const,
    start_line: 1, end_line: 1, hunk_digest: 'b'.repeat(64), quote: 'def handler():' } },
    surface_id: surfaceIdentity(binding), instance_id: crypto.randomUUID(), label: 'New line 1', geometry,
    controlGeometry: gutter ? () => ({ space: 'client' as const, rects: [{ left: 120, top: 200, width: 40, height: 20 }] }) : undefined };
  const port: FeedbackPresentationPort = {
    state, session, context: source.context, binding: () => binding, accept: vi.fn(), setMode: vi.fn(),
    setDraft: vi.fn(), discard: vi.fn(), save: vi.fn(), closeRail: vi.fn(), openRail: vi.fn(), select: vi.fn(),
    geometry, expose: vi.fn(), reveal: vi.fn(), locate: vi.fn(), more: vi.fn(), reload: vi.fn(), reply: vi.fn(), edit: vi.fn(),
  };
  session.publish(target);
  render(FeedbackInline, { port });
  await tick();
  expect(get(session.state).target).toBe(target);
  expect(screen.getByRole('button', { name: 'Add comment' })).toBeVisible();
  if (gutter) {
    const control = screen.getByRole('region', { name: 'Selected target' });
    // Diff lines keep the add-comment icon in their gutter slot.
    expect(control.style.left).toBe('120px');
    expect(control.style.top).toBe('200px');
    expect(control.style.width).toBe('28px');
    expect(control.style.maxHeight).toBe('20px');
  }
});
it('gives a new target exclusive overlay ownership without clearing the saved thread or draft', async () => {
  const source: ResolvedFeedbackSource = {
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), source: { kind: 'document', content_ref: crypto.randomUUID() },
    content_digest: 'f'.repeat(64), context: { kind: 'standalone' }, availability: 'available',
    permitted_locator_kinds: ['whole', 'html'], lineage_id: null, revision: 1, latest_content_ref: null,
    title: 'Overlay source', format: 'html', canonical_text: null,
    capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false },
  };
  const thread: FeedbackThread = {
    thread_id: crypto.randomUUID(), subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
    anchor: { schema_version: 1, anchor_id: crypto.randomUUID(), source: source.source, content_digest: source.content_digest, locator: { kind: 'whole' } },
    entries: [],
  };
  thread.entries = [{ entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), version: 1, thread_id: thread.thread_id,
    body: 'Saved discussion', author: { kind: 'verified_user', label: 'Saved author', is_self: true }, audience: 'internal',
    reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }];
  const state = writable<FeedbackPresentationState>({ source, threads: [thread], selected: thread.thread_id,
    mode: 'comment', candidate: null, draft: 'Preserved input', saving: false, pending_body: null,
    error: '', loading: false, next_cursor: null, open: false, view: 'threads' });
  const session = new FeedbackPresenterSession();
  const geometry = () => ({ space: 'client' as const, rects: [
    { left: 80, top: -100, width: 140, height: 28 }, { left: 80, top: 100, width: 140, height: 28 },
  ] });
  const binding: FeedbackSurfaceBinding = { source, context: source.context, mode: 'comment', supported_locator_kinds: source.permitted_locator_kinds,
    completeness: 'available', highlights: [], selected_thread_id: thread.thread_id, onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(),
    onInteractionChange: vi.fn(), registerAdapter: () => () => {}, resolveTextScope: async () => { throw new Error('No canonical text'); } };
  const port: FeedbackPresentationPort = {
    state, session, context: source.context, binding: () => binding,
    accept: vi.fn(candidate => { state.update(value => ({ ...value, candidate })); session.compose(); }),
    setMode: mode => state.update(value => ({ ...value, mode })), setDraft: draft => state.update(value => ({ ...value, draft })),
    discard: vi.fn(async () => {}), save: vi.fn(async () => {}),
    closeRail: vi.fn(), openRail: vi.fn(), select: selected => state.update(value => ({ ...value, selected })),
    geometry, expose: vi.fn(async () => {}), reveal: vi.fn(async () => {}), locate: vi.fn(async () => {}),
    more: vi.fn(async () => {}), reload: vi.fn(async () => {}), reply: vi.fn(async () => {}), edit: vi.fn(async () => {}),
  };
  session.preview(thread.thread_id);
  render(FeedbackInline, { port });
  expect(screen.getByRole('region', { name: 'Comment preview' })).toHaveTextContent('Saved discussion');
  expect(screen.getByRole('button', { name: 'Saved author: Saved discussion' })).toBeVisible();
  session.setTool('element');
  session.publish({ candidate: { locator: { kind: 'whole' } }, label: 'New source target',
    surface_id: surfaceIdentity(binding), instance_id: crypto.randomUUID(), geometry });
  await tick();
  expect(screen.queryByRole('region', { name: 'Comment preview' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Add comment' }));
  expect(screen.getByRole('region', { name: 'Add comment' })).toBeVisible();
  expect(screen.queryByRole('region', { name: 'Comment preview' })).toBeNull();
  expect(get(state)).toMatchObject({ selected: thread.thread_id, threads: [thread], draft: 'Preserved input' });
  const editor = screen.getByRole('region', { name: 'Add comment' });
  expect(within(editor).getAllByRole('button').map(button => button.textContent?.trim())).toEqual(['Cancel', 'Send']);
  expect(editor).not.toHaveTextContent('saving never sends');
  await fireEvent.click(within(editor).getByRole('button', { name: 'Cancel' }));
  // Cancelling a comment closes the composer; it only closes the comment list when nothing was composed.
  expect(screen.queryByRole('region', { name: 'Add comment' })).toBeNull();
  expect(port.closeRail).not.toHaveBeenCalled();
  expect(get(state).draft).toBe('Preserved input');
  state.update(value => ({ ...value, source: { ...source, source_token: crypto.randomUUID() } }));
  await tick();
  expect(get(session.state)).toMatchObject({ target: null, composer: false, layout: null });
  expect(get(state).draft).toBe('Preserved input');
  cleanup();
  expect(document.querySelector('[data-feedback-overlay]')).toBeNull();
});
