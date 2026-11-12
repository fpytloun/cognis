import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { loginFixtureAccount } from '../../test/verified-account';
import { auth } from '$lib/stores/auth';
import type { FeedbackContext, FeedbackSourceRef } from './types';
import { FeedbackDiffRegistry } from './diff-registry';

const loading = vi.hoisted(() => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  return { release, gate, requested: vi.fn(), constructed: vi.fn() };
});
vi.mock('./state', async () => {
  loading.requested();
  await loading.gate;
  return { FeedbackController: class { constructor() { loading.constructed(); } } };
});
beforeEach(loginFixtureAccount);
afterEach(async () => { await auth.clear(); });

it('does not construct a controller after cold scope replacement or disposal', async () => {
  let context: FeedbackContext = { kind: 'chat', conversation_id: 'registry-origin' };
  const source: Extract<FeedbackSourceRef, { kind: 'work_diff' }> = {
    kind: 'work_diff', activity_scope_id: 'registry-activity', source_item_id: 'registry-item',
    source_event: { store: 'intaris', session_id: 'registry-session', seq: 1, event_type: 'tool_result' },
    file_ordinal: 0, root_id: null, path: 'src/loading.ts', old_path: null,
  };
  const changed = new FeedbackDiffRegistry(context, () => {}, () => context);
  const closed = new FeedbackDiffRegistry(context, () => {});
  changed.binding(source);
  closed.binding(source);
  await waitFor(() => expect(loading.requested).toHaveBeenCalledOnce());
  context = { kind: 'chat', conversation_id: 'replaced-registry-origin' };
  closed.dispose();
  loading.release();
  await vi.dynamicImportSettled();
  expect(loading.constructed).not.toHaveBeenCalled();
  changed.dispose();
});
