import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { IDBFactory } from 'fake-indexeddb';
import { DocumentCoordination, documentCoordination } from '$lib/document-coordination';
import { TestWebLocks } from '../../../test/web-locks';
import { FeedbackController } from '$lib/feedback/state';
import { presentationAuthority } from '$lib/feedback/presentation';
import type { FeedbackThread, ResolvedFeedbackSource } from '$lib/feedback/types';
import FeedbackInline from './FeedbackInline.svelte';
import FeedbackComments from './FeedbackComments.svelte';

vi.mock('$lib/document-coordination', async importOriginal => ({
  ...await importOriginal<typeof import('$lib/document-coordination')>(),
  documentCoordination: vi.fn(),
}));

const disposers: (() => void)[] = [];
afterEach(() => { cleanup(); disposers.splice(0).reverse().forEach(dispose => dispose()); vi.restoreAllMocks(); });

async function fixture() {
  const coordination = new DocumentCoordination({
    indexedDB: new IDBFactory(), locks: new TestWebLocks() as unknown as LockManager,
    profile: () => location.origin, generation: () => 'feedback-hydration',
    uuid: () => crypto.randomUUID(), subscribe: () => () => {}, publish: () => {},
  });
  await coordination.initialize();
  await coordination.verified('feedback-hydration@example.test');
  vi.mocked(documentCoordination).mockReturnValue(coordination);
  disposers.push(() => coordination.dispose());
  const source: ResolvedFeedbackSource = {
    source: { kind: 'document', content_ref: crypto.randomUUID() }, subject_id: crypto.randomUUID(),
    source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64), context: { kind: 'standalone' },
    availability: 'available', permitted_locator_kinds: ['whole'], lineage_id: null, revision: 1,
    latest_content_ref: null, title: 'Hydration source', format: 'text', canonical_text: null,
    capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false },
  };
  const thread: FeedbackThread = {
    thread_id: crypto.randomUUID(), subject_id: source.subject_id, version: 1, state: 'open',
    hidden: false, audience: 'internal', anchor: { schema_version: 1, anchor_id: crypto.randomUUID(),
      source: source.source, content_digest: source.content_digest, locator: { kind: 'whole' } },
    entries: [],
  };
  thread.entries.push({
    entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), thread_id: thread.thread_id,
    version: 1, body: 'Saved comment', author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
    audience: 'internal', reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [],
  });
  const controller = new FeedbackController(source.source, source.context);
  disposers.push(() => controller.dispose());
  controller.patch({ source, threads: [thread], selected: thread.thread_id, mode: 'comment' });
  const port = controller.presentation();
  port.session.authorize(presentationAuthority(port, get(port.state)));
  return { coordination, controller, port, source, thread };
}

const editButton = () => screen.getByRole('button', { name: /^Edit comment/ });

it('does not reset an open edit on unrelated entry copies, but closes on an actual new version', async () => {
  const f = await fixture();
  render(FeedbackComments, { port: f.port });
  await fireEvent.click(editButton());
  const input = screen.getByRole('textbox', { name: 'Edit comment' });
  await fireEvent.input(input, { target: { value: '' } });
  input.focus();
  f.port.binding()!.onInteractionChange?.(false);
  f.controller.patch({ threads: [{ ...f.thread, entries: f.thread.entries.map(entry => ({ ...entry })) }] });
  await tick();
  expect(screen.getByRole('textbox', { name: 'Edit comment' })).toBe(input);
  expect(input).toHaveFocus();
  expect(input).toHaveValue('');
  f.controller.patch({ threads: [{ ...f.thread, entries: [{ ...f.thread.entries[0],
    entry_version_id: crypto.randomUUID(), version: 2, body: 'Updated saved comment' }] }] });
  await tick();
  expect(screen.queryByRole('textbox', { name: 'Edit comment' })).toBeNull();
  await fireEvent.click(editButton());
  expect(screen.getByRole('textbox', { name: 'Edit comment' })).toHaveValue('Updated saved comment');
});

it('clears private edit input on real account-lease revocation with the same thread ID', async () => {
  const f = await fixture(), lease = f.coordination.capture();
  render(FeedbackComments, { port: f.port });
  const article = screen.getByRole('article', { name: 'Open comment' });
  await fireEvent.click(editButton());
  await fireEvent.input(screen.getByRole('textbox', { name: 'Edit comment' }), { target: { value: 'Private edit' } });
  await f.coordination.invalidate();
  await tick();
  expect(lease.signal.aborted).toBe(true);
  expect(screen.getByRole('article', { name: 'Open comment' })).toBe(article);
  expect(get(f.controller.state).threads[0].thread_id).toBe(f.thread.thread_id);
  expect(screen.queryByRole('textbox', { name: 'Edit comment' })).toBeNull();
});

for (const change of ['source', 'context', 'grant'] as const) {
  it(`clears private edit input on ${change} replacement even when the thread ID is reused`, async () => {
    const f = await fixture();
    f.port.openRail();
    const view = render(FeedbackInline, { port: f.port });
    await fireEvent.click(editButton());
    await fireEvent.input(screen.getByRole('textbox', { name: 'Edit comment' }), { target: { value: 'Private scoped edit' } });
    if (change === 'context') await view.rerender({ port: { ...f.port, context: { kind: 'chat', conversation_id: crypto.randomUUID() } } });
    else f.controller.patch({ source: change === 'source' ? { ...f.source, source_token: crypto.randomUUID() }
      : { ...f.source, capabilities: { ...f.source.capabilities, comment: false } } });
    await tick();
    expect(get(f.controller.state).threads[0].thread_id).toBe(f.thread.thread_id);
    expect(screen.queryByRole('textbox', { name: 'Edit comment' })).toBeNull();
  });
}

for (const status of ['loading', 'error'] as const) for (const dismiss of ['Escape', 'Close'] as const) {
  it(`dismisses source ${status} with ${dismiss} through the same draft-preserving transition`, async () => {
    const f = await fixture();
    const retained = { draft: 'Retained controller draft', pending_body: 'Original uncertain save',
      candidate: { locator: { kind: 'whole' as const } } };
    f.controller.patch({ ...retained, source: null, loading: status === 'loading',
      error: status === 'error' ? 'Source resolution failed' : '', open: true });
    const changeMode = vi.spyOn(f.port, 'setMode');
    render(FeedbackInline, { controller: f.controller });
    expect(screen.getByRole('region', { name: 'Comments' })).toBeVisible();
    if (dismiss === 'Escape') await fireEvent.keyDown(window, { key: 'Escape' });
    else await fireEvent.click(screen.getByRole('button', { name: 'Close comments' }));
    expect(changeMode).toHaveBeenCalledExactlyOnceWith('view');
    expect(get(f.controller.state)).toMatchObject({ ...retained, mode: 'view', open: false });
    expect(screen.queryByRole('region', { name: 'Comments' })).toBeNull();
    expect(get(f.port.session.state)).toMatchObject({ surface: null, composer: false, target: null });
  });
}
