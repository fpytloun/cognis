import { describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { FeedbackPresenterSession, registerPresenterAdapter } from './presenter-session';
import type { FeedbackAnnotationAdapter, FeedbackEntryVersion, FeedbackTargetView, FeedbackThread } from './types';

function selectedEntry(): FeedbackEntryVersion {
  return { entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), version: 2, thread_id: crypto.randomUUID(),
    body: 'Selected immutable version', author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
    audience: 'internal', reply_to_entry_id: null, created_at: '', delivery: [] };
}

describe('ephemeral feedback presentation', () => {
  it('pins canonical source proof with the checked version and clears it with selection/authority', () => {
    const session = new FeedbackPresenterSession(), entry = selectedEntry();
    const originalRef = `dlv_${crypto.randomUUID().replace(/-/g, '')}`;
    const thread: Pick<FeedbackThread, 'thread_id' | 'anchor'> = {
      thread_id: entry.thread_id, anchor: { schema_version: 1, anchor_id: crypto.randomUUID(),
        source: { kind: 'document', content_ref: originalRef }, locator: { kind: 'whole' }, content_digest: 'a'.repeat(64) },
    };
    session.check(entry, true, thread);
    thread.anchor.source = { kind: 'document', content_ref: `dlv_${crypto.randomUUID().replace(/-/g, '')}` };
    session.check(entry, true, thread);
    session.review();
    const reviewed = get(session.state);
    expect(reviewed.reviewedSources[0]).toMatchObject({ entry_version_id: entry.entry_version_id,
      version: 2, thread_id: entry.thread_id, source: { kind: 'document', content_ref: originalRef } });
    session.check(entry, false);
    expect(get(session.state).reviewedSources).toEqual(reviewed.reviewedSources);
    session.review();
    expect(get(session.state).reviewedSources).toEqual([]);
    session.check(entry, true, thread);
    session.invalidate();
    session.check(entry, true);
    session.review();
    expect(get(session.state).reviewedSources).toEqual([]);
  });
  it('does not retain proof from a thread that does not own the selected entry', () => {
    const session = new FeedbackPresenterSession(), entry = selectedEntry();
    session.check(entry, true, { thread_id: crypto.randomUUID(), anchor: { schema_version: 1,
      anchor_id: crypto.randomUUID(), source: { kind: 'document', content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}` },
      locator: { kind: 'whole' }, content_digest: 'a'.repeat(64) } });
    session.review();
    expect(get(session.state).reviewedSources).toEqual([]);
  });
  it('returns focus on dismissal without reopening a focus-driven preview', () => {
    const session = new FeedbackPresenterSession();
    const avatar = document.createElement('button'), close = document.createElement('button');
    document.body.append(avatar, close);
    avatar.addEventListener('focus', () => session.preview('thread', false));
    avatar.focus(); session.open('thread'); close.focus(); session.close();
    expect(document.activeElement).toBe(avatar);
    expect(get(session.state)).toMatchObject({ surface: null, preview: null });
    avatar.remove(); close.remove();
  });
  it('keeps one floating view and scopes transient buffers and checked versions to authority', () => {
    const session = new FeedbackPresenterSession();
    const entry: FeedbackEntryVersion = { entry_id: 'entry', entry_version_id: 'version-1', version: 1, thread_id: 'thread',
      body: 'Original version', author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
      audience: 'internal', reply_to_entry_id: null, created_at: '', delivery: [] };
    session.authorize('source/context/author/lease-1');
    session.setBuffer('reply:thread', 'Retained reply');
    session.check(entry, true);
    session.open('thread'); session.compose();
    expect(get(session.state)).toMatchObject({ surface: 'compose', composer: true, preview: null });
    session.open('comments');
    expect(get(session.state)).toMatchObject({ surface: 'comments', composer: false });
    session.review();
    entry.body = 'Later mutation';
    expect(get(session.state).reviewed[0].body).toBe('Original version');
    session.close();
    expect(session.buffer('reply:thread')).toBe('Retained reply');
    expect(get(session.state).checked.map(entry => entry.entry_version_id)).toEqual(['version-1']);
    session.authorize('source/context/author/lease-2');
    expect(session.buffer('reply:thread')).toBe('');
    expect(get(session.state)).toMatchObject({ checked: [], reviewed: [], surface: null });
  });
  it('does not allow hover preview to replace composing, a persistent thread or active native selection', () => {
    const session = new FeedbackPresenterSession();
    session.preview('thread', false);
    expect(get(session.state).preview).toBe('thread');
    session.compose(); session.preview('other', false);
    expect(get(session.state)).toMatchObject({ surface: 'compose', preview: null });
    session.open('thread'); session.preview('other', false);
    expect(get(session.state)).toMatchObject({ surface: 'thread', preview: null });
    session.close(); session.preview('other', true);
    expect(get(session.state).preview).toBeNull();
  });
  it('requests narrow source focus once per intent, with cancellation and deferred bounds', () => {
    const session = new FeedbackPresenterSession(), focus = vi.fn();
    session.requestSourceFocus(focus);
    session.setBounds({ left: 800, top: 40, width: 340, height: 600 });
    expect(focus).toHaveBeenCalledTimes(1);
    session.setBounds({ left: 800, top: 40, width: 340, height: 600 });
    expect(focus).toHaveBeenCalledTimes(1);
    session.invalidate(); session.requestSourceFocus(focus); session.close();
    session.setBounds({ left: 800, top: 40, width: 340, height: 600 });
    expect(focus).toHaveBeenCalledTimes(1);
    session.setBounds({ left: 200, top: 40, width: 900, height: 600 });
    session.requestSourceFocus(focus);
    expect(focus).toHaveBeenCalledTimes(1);
  });
  it('owns only measured layout geometry and releases it on close and authority invalidation', () => {
    const session = new FeedbackPresenterSession();
    const layout = { visibleBottom: 220, viewportHeight: 844 };
    session.setLayout(layout); expect(get(session.state).layout).toBeNull();
    session.compose(); session.setLayout(layout);
    expect(get(session.state).layout).toEqual(layout);
    const snapshot = get(session.state); session.setLayout({ ...layout });
    expect(get(session.state)).toBe(snapshot);
    session.close(); expect(get(session.state).layout).toBeNull();
    session.compose(); session.setLayout(layout); session.invalidate();
    expect(get(session.state)).toMatchObject({ layout: null, composer: false, target: null });
  });
  it('survives binding replacement but revokes a target when its adapter unmounts', async () => {
    const session = new FeedbackPresenterSession(), adapters = new Map<string, FeedbackAnnotationAdapter>();
    const adapter: FeedbackAnnotationAdapter = { locate: () => ({ status: 'exact' }), clearSelection: () => {} };
    const target: FeedbackTargetView = { candidate: { locator: { kind: 'whole' } }, label: 'Source', surface_id: 'mounted-source', instance_id: 'mounted-adapter', geometry: () => null };
    const first = registerPresenterAdapter(adapters, session, target.instance_id, adapter); session.publish(target);
    first(); const second = registerPresenterAdapter(adapters, session, target.instance_id, adapter);
    await Promise.resolve(); expect(get(session.state).target).toBe(target);
    second(); await Promise.resolve(); expect(get(session.state).target).toBeNull();
  });
  it('never accepts or retains a candidate when closing or switching tools', () => {
    const session = new FeedbackPresenterSession();
    const target: FeedbackTargetView = { candidate: { locator: { kind: 'whole' } }, label: 'Source',
      surface_id: 'mounted-source', instance_id: 'mounted-adapter', geometry: () => null };
    session.publish(target);
    expect(get(session.state).target).toBe(target);
    session.compose();
    expect(get(session.state)).toMatchObject({ target: null, composer: true });
    session.publish(target);
    expect(get(session.state).target).toBeNull();
    session.close();
    expect(get(session.state)).toMatchObject({ target: null, composer: false });
    session.publish(target); session.setTool('element');
    expect(get(session.state)).toMatchObject({ target: null, tool: 'element' });
  });
});
