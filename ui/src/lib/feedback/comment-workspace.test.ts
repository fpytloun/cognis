import { get } from 'svelte/store';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { CommentWorkspace } from './comment-workspace';
import { FeedbackController } from './state';
import type { ResolvedFeedbackSource } from './types';

const owners: FeedbackController[] = [];
let workspace: CommentWorkspace;
beforeEach(async () => { await loginFixtureAccount(); workspace = new CommentWorkspace(); });
afterEach(async () => { workspace.dispose(); for (const owner of owners.splice(0)) owner.dispose(); await auth.clear(); });
function owner() {
  const source: ResolvedFeedbackSource = { subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(),
    source: { kind: 'document', content_ref: `art_${crypto.randomUUID().replaceAll('-', '')}` }, context: { kind: 'chat', conversation_id: crypto.randomUUID() },
    content_digest: 'a'.repeat(64), lineage_id: null, revision: 1, latest_content_ref: null, format: 'markdown', title: 'Review source',
    availability: 'available', canonical_text: null, permitted_locator_kinds: ['whole'],
    capabilities: { read: true, comment: true, dispatch: true, moderate: false, disclose: false } };
  const result = new FeedbackController(source.source, source.context);
  result.patch({ source, loading: false });
  owners.push(result);
  workspace.register(result);
  return result;
}
it('toggles commenting without opening chrome or loading any sources', () => {
  const first = owner(), load = vi.spyOn(first, 'load');
  workspace.setEnabled(true);
  expect(get(first.state).mode).toBe('comment');
  expect(get(workspace.state)).toMatchObject({ enabled: true, active: null, list: false });
  expect(get(first.presenter.state).composer).toBe(false);
  const second = owner();
  expect(get(second.state).mode).toBe('comment');
  workspace.setEnabled(false);
  expect(get(first.state).mode).toBe('view');
  expect(get(second.state).mode).toBe('view');
  expect(load).not.toHaveBeenCalled();
});
it('does not enable editing through another control in a read-only conversation', () => {
  workspace.dispose();
  workspace = new CommentWorkspace(() => false);
  const current = owner();
  workspace.setEnabled(true);
  expect(get(workspace.state).enabled).toBe(false);
  expect(get(current.state).mode).toBe('view');
  workspace.showComments();
  expect(get(workspace.state).list).toBe(true);
});
it('keeps only one contextual editor without discarding either source draft or enabling mode', () => {
  const first = owner(), second = owner();
  first.patch({ draft: 'First retained draft' });
  second.patch({ draft: 'Second retained draft' });
  first.presenter.compose();
  expect(get(workspace.state).active).toBe(first);
  second.presenter.compose();
  expect(get(workspace.state)).toMatchObject({ active: second, enabled: false, list: false });
  expect(get(first.presenter.state).composer).toBe(false);
  expect(get(first.state).draft).toBe('First retained draft');
  workspace.showComments();
  expect(get(second.presenter.state).composer).toBe(false);
  expect(get(workspace.state).list).toBe(true);
  expect(get(second.state).draft).toBe('Second retained draft');
});
it('resumes the active draft in comment mode without replacing its text', () => {
  const current = owner();
  current.patch({ draft: 'Keep this draft.' });
  current.presenter.compose();
  workspace.showComments();
  workspace.resumeDraft();
  expect(get(workspace.state)).toMatchObject({ enabled: true, list: false, active: current });
  expect(get(current.presenter.state).composer).toBe(true);
  expect(get(current.state).draft).toBe('Keep this draft.');
});
it('showing the comments turns on comment mode and closing them turns it off', () => {
  const current = owner();
  workspace.showComments();
  expect(get(workspace.state)).toMatchObject({ enabled: true, list: true });
  expect(get(current.state).mode).toBe('comment');
  workspace.closeComments();
  expect(get(workspace.state)).toMatchObject({ enabled: false, list: false });
  expect(get(current.state).mode).toBe('view');
});
it('unsubscribes once when a source unmounts after the workspace was reset', () => {
  const current = owner();
  workspace.dispose();
  const original = current.state.subscribe;
  const stop = vi.fn();
  vi.spyOn(current.state, 'subscribe').mockImplementation((...args) => {
    const unsubscribe = original(...args);
    return () => { stop(); unsubscribe(); };
  });
  const off = workspace.register(current);
  workspace.dispose(); off(); off();
  expect(stop).toHaveBeenCalledOnce();
  expect(get(workspace.state).active).toBeNull();
});
