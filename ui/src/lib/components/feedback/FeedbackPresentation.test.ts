import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';
import { tick } from 'svelte';
import FeedbackInline from './FeedbackInline.svelte';
import FeedbackThreadItem from './FeedbackThreadItem.svelte';
import FeedbackComments from './FeedbackComments.svelte';
import CommentReplies from './CommentReplies.svelte';
import { FeedbackPresenterSession } from '$lib/feedback/presenter-session';
import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
import type { FeedbackPresentationPort, FeedbackPresentationState } from '$lib/feedback/presentation';
import type { FeedbackSurfaceBinding, FeedbackTargetView, FeedbackThread, ResolvedFeedbackSource } from '$lib/feedback/types';

afterEach(cleanup);
it('shows author avatars and agent identity in both full threads and folded replies', async () => {
  const f = fixture(), first = f.thread.entries[0];
  f.thread.entries.push({ ...first, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(),
    author: { kind: 'agent', label: 'Release Engineer', is_self: false }, body: 'Rollback is documented.',
    reply_to_entry_id: first.entry_id, delivery: [] });
  const full = render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn() });
  expect(screen.getByText('RE')).toBeVisible();
  expect(screen.getByText('R')).toBeVisible();
  expect(screen.getByText('Release Engineer')).toBeVisible();
  expect(screen.getByText('Agent')).toBeVisible();
  expect(screen.getByText('Rollback is documented.')).toBeVisible();
  full.unmount();
  render(CommentReplies, { thread: f.thread });
  expect(screen.getByText('RE')).toBeVisible();
  expect(screen.getByText('Release Engineer')).toBeVisible();
  expect(screen.getByText('Agent')).toBeVisible();
  expect(screen.getByText('Rollback is documented.')).toBeVisible();
});

it('names each inclusion checkbox by author and specific body without changing exact-version selection', async () => {
  const f = fixture(), select = vi.fn();
  const first = f.thread.entries[0];
  const second = { ...first, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), body: 'Check the staging evidence.' };
  f.thread.entries.push(second);
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [first.entry_version_id], onSelect: select, selectable: true });
  const firstCheckbox = screen.getByRole('checkbox', { name: `Include in message: ${first.body}` });
  const secondCheckbox = screen.getByRole('checkbox', { name: `Include in message: ${second.body}` });
  expect(firstCheckbox).toBeChecked();
  expect(secondCheckbox).not.toBeChecked();
  await fireEvent.click(secondCheckbox);
  expect(select).toHaveBeenCalledExactlyOnceWith(second.entry_version_id, true);
});

function fixture() {
  const source: ResolvedFeedbackSource = { source: { kind: 'document', content_ref: crypto.randomUUID() }, content_digest: 'a'.repeat(64),
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), availability: 'available', permitted_locator_kinds: ['whole', 'text'],
    context: { kind: 'standalone' }, lineage_id: null, revision: 1, latest_content_ref: null, capabilities: { read: true, comment: true, dispatch: true, moderate: false, disclose: false }, title: 'Current release', format: 'text', canonical_text: null };
  const thread: FeedbackThread = { thread_id: crypto.randomUUID(), subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
    anchor: { schema_version: 1, anchor_id: crypto.randomUUID(), source: source.source, content_digest: source.content_digest, locator: { kind: 'text', range: { representation: 'feedback-text-v1', text_digest: 'b'.repeat(64), exact: 'Ship safely', start: 0, end: 11, prefix: '', suffix: '' } } }, entries: [] };
  thread.entries = [{ entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), version: 2, thread_id: thread.thread_id,
    body: 'Check the rollback owner.', author: { kind: 'guest_unverified', label: 'Reviewer', is_self: true }, audience: 'internal',
    reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [{ submission_id: crypto.randomUUID(), manifest_hash: 'c'.repeat(64), delivery: 'uncertain' }] }];
  const state = writable<FeedbackPresentationState>({ source, threads: [thread], selected: thread.thread_id, mode: 'comment', candidate: null,
    draft: '', saving: false, pending_body: null, error: '', loading: false, next_cursor: null, open: false, view: 'threads' });
  const session = new FeedbackPresenterSession();
  const binding: FeedbackSurfaceBinding = { source, context: source.context, mode: 'comment', supported_locator_kinds: source.permitted_locator_kinds,
    completeness: 'available', highlights: [], selected_thread_id: thread.thread_id, onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(),
    onInteractionChange: vi.fn(), registerAdapter: () => () => {}, resolveTextScope: vi.fn() };
  const port: FeedbackPresentationPort = { state, session, context: source.context, binding: () => binding,
    accept: vi.fn(candidate => { state.update(value => ({ ...value, candidate })); session.compose(); }),
    setMode: vi.fn(), setDraft: vi.fn(), discard: vi.fn(), save: vi.fn(), closeRail: vi.fn(), openRail: vi.fn(), select: vi.fn(),
    geometry: () => null, expose: vi.fn(), reveal: vi.fn(), locate: vi.fn(), more: vi.fn(), reload: vi.fn(), reply: vi.fn(), edit: vi.fn(),
    resolve: vi.fn(), moderate: vi.fn(), reattach: vi.fn() };
  return { source, thread, state, session, binding, port };
}

it('shows target and guest trust in preview and visible context while typing', async () => {
  const f = fixture();
  f.session.preview(f.thread.thread_id);
  render(FeedbackInline, { port: f.port });
  const preview = screen.getByRole('region', { name: 'Comment preview' });
  expect(preview).toHaveTextContent('“Ship safely”');
  expect(preview).toHaveTextContent('Guest');
  expect(preview).toHaveTextContent('Edited');
  f.state.update(value => ({ ...value, candidate: { locator: f.thread.anchor.locator }, draft: 'Keep input' }));
  f.session.compose(); await tick();
  const input = screen.getByRole('textbox');
  input.focus();
  expect(screen.getByText('“Ship safely”')).toBeVisible();
  expect(input).toHaveValue('Keep input');
});

it('accepts ordinary multi-rect targets once without a shared contextual scope palette', async () => {
  const f = fixture(); render(FeedbackInline, { port: f.port });
  const scope = vi.fn();
  const target: FeedbackTargetView = { candidate: { locator: { kind: 'whole' } }, label: 'Cell', surface_id: surfaceIdentity(f.binding),
    instance_id: crypto.randomUUID(), geometry: () => ({ space: 'client', rects: [
      { left: 20, top: 100, width: 80, height: 30 }, { left: 200, top: 100, width: 80, height: 30 },
    ] }) };
  target.scopes = ['Cell', 'Row', 'Table'].map(label => ({ label, target: () => { scope(label); return { ...target, label }; } }));
  f.session.publish(target); await tick();
  for (const name of ['Cell', 'Row', 'Table']) expect(screen.queryByRole('button', { name })).toBeNull();
  expect(scope).not.toHaveBeenCalled();
  expect(get(f.session.state).target?.geometry()?.rects).toHaveLength(2);
  await fireEvent.click(screen.getByRole('button', { name: 'Add comment' }));
  expect(f.port.accept).toHaveBeenCalledExactlyOnceWith(target.candidate);
});

it('shows body, trust and delivery with only simple actions', async () => {
  const f = fixture(), select = vi.fn();
  f.port.canEdit = () => false;
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: select, selectable: true });
  expect(screen.getByText('Check the rollback owner.')).toBeVisible();
  expect(screen.getByText('Guest', { exact: true })).toBeVisible();
  expect(screen.getByText("Checking delivery")).toBeVisible();
  for (const name of ['More', 'Reply', 'Hide thread', 'Edit']) expect(screen.queryByRole('button', { name })).toBeNull();
  expect(screen.queryByText(/Version \d/)).toBeNull();
  await fireEvent.click(screen.getByRole('checkbox'));
  expect(select).toHaveBeenCalledWith(f.thread.entries[0].entry_version_id, true);
  await fireEvent.click(screen.getByRole('button', { name: 'Resolve' }));
  expect(f.port.resolve).toHaveBeenCalledWith(f.thread);
});

it('removes an own comment after one confirmation', async () => {
  const f = fixture();
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn() });
  await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  expect(f.port.moderate).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm remove' }));
  expect(f.port.moderate).toHaveBeenCalledWith(f.thread, 'Removed');
});

it('locates a comment from its whole author/body entry without turning edit or inclusion into navigation', async () => {
  const f = fixture();
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn(), selectable: true });
  await fireEvent.click(screen.getByRole('button', { name: 'Locate comment: Check the rollback owner.' }));
  expect(f.port.locate).toHaveBeenCalledWith(f.thread);
  vi.mocked(f.port.locate).mockClear();
  await fireEvent.click(screen.getByRole('button', { name: 'Edit comment: Check the rollback owner.' }));
  expect(screen.getByRole('textbox', { name: 'Edit comment' })).toHaveValue('Check the rollback owner.');
  expect(f.port.locate).not.toHaveBeenCalled();
});

it('links a comment on another revision to it, without a warning or a borrowed title', async () => {
  const f = fixture();
  f.thread.anchor = { ...f.thread.anchor, locator: { kind: 'whole' }, source: { kind: 'document', content_ref: crypto.randomUUID() } };
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn() });
  expect(screen.getByRole('link', { name: 'View original revision' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Whole document' })).toBeVisible();
  expect(screen.queryByText(/another revision\.|source unavailable/)).toBeNull();
  f.state.update(value => ({ ...value, source: null })); await tick();
  expect(screen.queryByText(/another revision\.|source unavailable/)).toBeNull();
});

it('does not invent stale selection evidence from paginated or filtered comments', async () => {
  const f = fixture();
  f.state.update(value => ({ ...value, next_cursor: crypto.randomUUID(), error: 'Exact selected version changed. Review again.' }));
  render(FeedbackComments, { port: f.port });
  expect(screen.getByRole('alert')).toHaveTextContent('Exact selected version changed. Review again.');
  expect(screen.queryByText(/Some selected versions/)).toBeNull();
  expect(screen.getByRole('button', { name: 'Load more comments' })).toBeVisible();
});

it('edits only the permitted entry inline', async () => {
  const f = fixture();
  const original = f.thread.entries[0];
  const other = { ...original, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), body: 'Second comment',
    author: { ...original.author, label: 'Another reviewer', is_self: false } };
  f.thread.entries.push(other);
  f.port.canEdit = entry => entry.entry_id === other.entry_id;
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn() });
  const edits = screen.getAllByRole('button', { name: /^Edit comment/ });
  expect(edits).toHaveLength(1);
  await fireEvent.click(edits[0]);
  await fireEvent.input(screen.getByRole('textbox', { name: 'Edit comment' }), { target: { value: 'Updated second comment' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(f.port.edit).toHaveBeenCalledExactlyOnceWith(other, 'Updated second comment');
});

it('shows a saved HTML capture on demand', async () => {
  const f = fixture();
  f.thread.anchor.locator = { kind: 'html', element_key: null, selector_path: null, range: null,
    viewport: { width_css_px: 800, height_css_px: 600, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: 1 },
    render_state_digest: 'd'.repeat(64), capture_id: crypto.randomUUID(), shapes: [] };
  render(FeedbackThreadItem, { thread: f.thread, port: f.port, selected: [], onSelect: vi.fn() });
  await fireEvent.click(screen.getByRole('button', { name: 'Show capture' }));
  expect(screen.getByRole('button', { name: 'Hide capture' })).toBeVisible();
});
