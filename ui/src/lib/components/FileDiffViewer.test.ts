import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import FileDiffViewer from './FileDiffViewer.svelte';
import type { FeedbackAnnotationAdapter, FeedbackLocator, FeedbackSurfaceBinding, ResolvedFeedbackSource } from '$lib/feedback/types';

function diffSource(): ResolvedFeedbackSource {
  return {
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'b'.repeat(64),
    source: { kind: 'work_diff', activity_scope_id: crypto.randomUUID(), source_item_id: crypto.randomUUID(), file_ordinal: 0, root_id: null, path: 'src/file.ts', old_path: null,
      source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 1, event_type: 'tool_result' } },
    context: { kind: 'chat', conversation_id: crypto.randomUUID() }, availability: 'available', format: 'diff',
    lineage_id: null, revision: null, latest_content_ref: null, title: 'src/file.ts', canonical_text: null,
    permitted_locator_kinds: ['diff'], capabilities: { read: true, comment: true, moderate: false, dispatch: true, disclose: false },
  };
}

describe('FileDiffViewer', () => {
  it('hosts line editors immediately after the selected range, on the correct diff side', async () => {
    const source = diffSource();
    const onTargetChange = vi.fn();
    const onAnchorCandidate = vi.fn();
    let adapter: FeedbackAnnotationAdapter | undefined;
    const binding: FeedbackSurfaceBinding = { source, context: source.context, mode: 'comment', highlights: [], selected_thread_id: null,
      supported_locator_kinds: ['diff'], completeness: 'available', onTargetChange, onAnchorCandidate,
      onSelectThread: vi.fn(), onInteractionChange: vi.fn(), resolveTextScope: vi.fn(),
      registerAdapter: (_id, value) => { adapter = value; return () => {}; } };
    render(FileDiffViewer, { diffs: [{ path: 'src/file.ts', diff: '@@ -1,4 +1,4 @@\n one\n-old two\n+new two\n three\n four' }],
      collapsible: false, annotationBindings: [binding] });
    await fireEvent.click(screen.getByRole('button', { name: 'Select new line 2' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Select new line 3' }), { shiftKey: true });
    await waitFor(() => expect(onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({
      candidate: { locator: expect.objectContaining({ side: 'new', start_line: 2, end_line: 3 }) },
    })));
    const range: FeedbackLocator = onTargetChange.mock.lastCall![0].candidate.locator;
    const host = adapter!.editorHost!(range)!;
    const row = host.closest('tr')!;
    expect(row.previousElementSibling).toHaveTextContent('three');
    expect(row.nextElementSibling).toHaveTextContent('four');
    expect(host.closest('td')).toHaveAttribute('colspan', '4');
    await fireEvent.click(screen.getByRole('button', { name: 'Select old line 2' }));
    await waitFor(() => expect(onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({
      candidate: { locator: expect.objectContaining({ side: 'old', start_line: 2, end_line: 2 }) },
    })));
    const oldHost = adapter!.editorHost!(onTargetChange.mock.lastCall![0].candidate.locator)!;
    expect(oldHost.closest('tr')!.previousElementSibling).toHaveTextContent('old two');
    // Selection controls are always present; there is no separate comment-mode action strip.
    expect(screen.queryByRole('button', { name: 'Comment on file' })).toBeNull();
  });
  it('offers a line comment from highlighted code in chat without enabling comment mode', async () => {
    const source: ResolvedFeedbackSource = {
      subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'b'.repeat(64),
      source: { kind: 'work_diff', activity_scope_id: crypto.randomUUID(), source_item_id: crypto.randomUUID(), file_ordinal: 0, root_id: null, path: 'src/file.ts', old_path: null,
        source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 1, event_type: 'tool_result' } },
      context: { kind: 'chat', conversation_id: crypto.randomUUID() }, availability: 'available', format: 'diff',
      lineage_id: null, revision: null, latest_content_ref: null, title: 'src/file.ts', canonical_text: null,
      permitted_locator_kinds: ['diff'], capabilities: { read: true, comment: true, moderate: false, dispatch: true, disclose: false },
    };
    const onTargetChange = vi.fn();
    const binding: FeedbackSurfaceBinding = { source, context: source.context, mode: 'view', highlights: [], selected_thread_id: null,
      supported_locator_kinds: ['diff'], completeness: 'available', onTargetChange,
      onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(), registerAdapter: () => () => {},
      resolveTextScope: vi.fn() };
    render(FileDiffViewer, { diffs: [{ path: 'src/file.ts', diff: '@@ -1 +1 @@\n-old\n+new' }], collapsible: false, annotationBindings: [binding] });
    await fireEvent.click(screen.getByText('new'));
    await waitFor(() => expect(onTargetChange).toHaveBeenCalledWith(expect.objectContaining({
      candidate: { locator: expect.objectContaining({ kind: 'diff', scope: 'lines', side: 'new', start_line: 1, end_line: 1 }) },
      controlGeometry: expect.any(Function),
    })));
    expect(screen.getByText('new').closest('tr')).toHaveClass('selected-diff-line');
    expect(binding.mode).toBe('view');
  });
  it('keeps a controlled selected diff open and exposes expand', async () => {
    const onExpand = vi.fn();
    render(FileDiffViewer, {
      diffs: [{ path: 'src/file.ts', diff: '@@ -1 +1 @@\n-old\n+new' }],
      collapsible: false,
      onExpand,
    });
    expect(screen.getByText('Expand diff')).toBeTruthy();
    expect(screen.getByText('Expand diff')).toHaveClass('hidden');
    expect(screen.getByText('Expand diff')).toHaveClass('sm:inline-flex');
    expect(screen.getByText('old')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button'));
    expect(onExpand).toHaveBeenCalledOnce();
    expect(screen.getByText('old')).toBeTruthy();
  });

  it('never displays truncation badges but retains omitted rows', () => {
    render(FileDiffViewer, {
      diffs: [{
        path: '',
        diff: '',
        omitted_count: 12,
      }],
    });
    expect(screen.queryByText(/Truncated/i)).toBeNull();
    expect(screen.getByText(/12 additional file diffs omitted/)).toBeTruthy();
  });
});
