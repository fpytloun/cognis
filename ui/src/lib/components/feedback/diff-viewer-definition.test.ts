import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import type { WorkMutationEvent, TimelineScope } from '$lib/chat-v2/types';
import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
import RecentFileEditRow from '../RecentFileEditRow.svelte';
import ToolCallBlock from '../ToolCallBlock.svelte';
import { feedbackDiffViewer } from './diff-viewer-definition';

beforeAll(async () => {
  // Keep compilation outside DOM query deadlines; definitions still load on actual mount.
  await import('./FeedbackDiffViewer.svelte');
  await import('../FileDiffViewer.svelte');
  await import('../ToolCallDetails.svelte');
});

beforeEach(loginFixtureAccount);
afterEach(async () => { cleanup(); vi.restoreAllMocks(); await auth.clear(); });

const file = { path: 'src/app.ts', diff: '@@ -1 +1 @@\n-old\n+new', additions: 1, deletions: 1 };
const scope: TimelineScope = { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' };
const event: WorkMutationEvent = {
  id: 'edit-1', call_id: 'call-edit-1', tool_name: 'apply_patch', display_name: 'Edit file',
  status: 'complete', file_diffs: [file], file_stats: [], additions: 1, deletions: 1,
  sort_key: 'edit-1', category: 'files', operation_kind: 'edit', arguments: {},
  paths: [file.path], diffs_truncated: false,
};

it('loads the exact recent diff only on expansion and destroys it on collapse/reopen', async () => {
  const load = vi.spyOn(feedbackDiffViewer, 'load');
  render(RecentFileEditRow, { event, file, scope });
  expect(load).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Expand src/app.ts' }));
  expect(await screen.findByText('old')).toBeTruthy();
  expect(screen.getByText('new')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Collapse src/app.ts' }));
  expect(screen.queryByText('old')).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Expand src/app.ts' }));
  expect(await screen.findByText('old')).toBeTruthy();
});

it('drops a pending recent diff completion after collapse', async () => {
  const component = await feedbackDiffViewer.load();
  vi.spyOn(feedbackDiffViewer, 'peek').mockReturnValue(null);
  let release!: (value: typeof component) => void;
  const load = vi.spyOn(feedbackDiffViewer, 'load').mockImplementation(() => new Promise(resolve => { release = resolve; }));
  render(RecentFileEditRow, { event, file, scope });
  await fireEvent.click(screen.getByRole('button', { name: 'Expand src/app.ts' }));
  await waitFor(() => expect(load).toHaveBeenCalledOnce());
  await fireEvent.click(screen.getByRole('button', { name: 'Collapse src/app.ts' }));
  release(component);
  await Promise.resolve();
  expect(screen.queryByText('old')).toBeNull();
});

it('renders the tool diff through the deferred wrapper at the existing disclosure', async () => {
  const item: ToolCallTimelineItem = {
    id: 'tool-edit-1', kind: 'tool_call', callId: 'call-edit-1', toolName: 'apply_patch',
    status: 'completed', timestamp: null, arguments: {}, result: 'Applied patch', fileDiffs: [file],
  };
  const view = render(ToolCallBlock, { item });
  const disclosure = view.container.querySelector<HTMLButtonElement>('button[aria-expanded]')!;
  if (disclosure.getAttribute('aria-expanded') === 'false') await fireEvent.click(disclosure);
  expect(await screen.findByText('old')).toBeTruthy();
  expect(screen.getByText('new')).toBeTruthy();
});
