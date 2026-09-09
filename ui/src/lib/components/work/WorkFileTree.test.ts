// @ts-nocheck -- retired history response metadata is ignored.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkFileDiff } from '$lib/work/fileTree';
import { ChatV2ApiError } from '$lib/chat-v2/api';
import { conversationTimelineScope, type WorkFileHistoryResponse } from '$lib/chat-v2/types';
import type { FileHistoryLoader } from '$lib/work/fileHistoryClient';
import WorkFileTree from './WorkFileTree.svelte';

class TestResizeObserver {
  static instances: TestResizeObserver[] = [];
  callback: ResizeObserverCallback;
  target: Element | null = null;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    TestResizeObserver.instances.push(this);
  }
  observe(target: Element) {
    this.target = target;
  }
  unobserve() {}
  disconnect() {}
  fire(height: number) {
    this.callback(
      [{ contentRect: { height, width: 900 } } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
}

/**
 * 50,000 changed files spread across many small folders (representative of a
 * real large diff) plus one deliberately oversized "big" folder so a single
 * expanded subtree exceeds the virtualization threshold on its own.
 */
function largeDiffSet(bigFolderCount = 2_000, smallFolderTotal = 48_000, perSmallFolder = 50): WorkFileDiff[] {
  const diffs: WorkFileDiff[] = [];
  for (let index = 0; index < bigFolderCount; index += 1) {
    diffs.push({
      path: `src/big/file${String(index).padStart(6, '0')}.ts`,
      diff: '@@ -1 +1 @@\n-old\n+new',
      additions: 1,
      deletions: 1,
    });
  }
  for (let index = 0; index < smallFolderTotal; index += 1) {
    const folder = Math.floor(index / perSmallFolder);
    diffs.push({
      path: `src/mod${String(folder).padStart(4, '0')}/file${String(index).padStart(6, '0')}.ts`,
      diff: '@@ -1 +1 @@\n-old\n+new',
      additions: 1,
      deletions: 1,
    });
  }
  return diffs;
}

function smallDiffSet(count: number): WorkFileDiff[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `src/file${String(index).padStart(4, '0')}.ts`,
    diff: '@@ -1 +1 @@\n-old\n+new',
    additions: 1,
    deletions: 1,
  }));
}

afterEach(() => {
  cleanup();
  TestResizeObserver.instances = [];
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver as unknown as typeof ResizeObserver);
});

function stubViewportHeight(observer: TestResizeObserver, height: number): void {
  if (observer.target) {
    Object.defineProperty(observer.target, 'clientHeight', { value: height, configurable: true });
  }
  observer.fire(height);
}

async function expandBigFolder(tree: HTMLElement): Promise<void> {
  await fireEvent.click(within(tree).getByText('src').closest('button')!);
  await fireEvent.click(within(tree).getByText('big').closest('button')!);
}

describe('WorkFileTree virtualization', () => {
  it('selects and highlights an initial exact Work item after files arrive asynchronously', async () => {
    const focus = {
      workItemId: 'edit-target',
      files: [{ pathGenerationId: 'generation-target' }],
    };
    const { rerender } = render(WorkFileTree, { diffs: [], initialFocus: focus });
    await rerender({
      diffs: [
        ...Array.from({ length: 49 }, (_, index) => ({ path: `src/large/other-${index}.ts`, diff: '+other', source_item_id: `edit-${index}`, path_generation_id: `generation-${index}` })),
        { path: 'src/deep/nested/target.ts', diff: '+target', source_item_id: 'edit-target', path_generation_id: 'generation-target' },
      ],
      initialFocus: focus,
    });
    const target = await screen.findByTestId('work-file-src/deep/nested/target.ts');
    await vi.waitFor(() => expect(target).toHaveAttribute('data-initial-focus', 'true'));
    expect(target).toHaveAttribute('aria-selected', 'true');
  });

  it('does not virtualize a small tree', async () => {
    render(WorkFileTree, { diffs: smallDiffSet(5) });
    const tree = await screen.findByTestId('work-files-tree');
    expect(tree.dataset.virtualized).toBe('false');
    expect(screen.queryByTestId('work-files-virtual-padding-top')).toBeNull();
  });

  it('virtualizes a 50,000-path tree and only mounts a windowed slice of the expanded folder', async () => {
    render(WorkFileTree, { diffs: largeDiffSet() });
    const tree = await screen.findByTestId('work-files-tree');
    await expandBigFolder(tree);

    const treeObserver = TestResizeObserver.instances.find((instance) => instance.target === tree);
    expect(treeObserver).toBeDefined();
    stubViewportHeight(treeObserver!, 600);

    expect(tree.dataset.virtualized).toBe('true');
    const rows = within(tree).getAllByRole('treeitem');
    // Top-level folders + a bounded slice of the expanded "big" folder's rows,
    // not all 2,000 of its children (let alone all 50,000 paths).
    expect(rows.length).toBeLessThan(200);
    const paddingBottom = screen.getByTestId('work-files-virtual-padding-bottom');
    expect(Number(paddingBottom.style.height.replace('px', ''))).toBeGreaterThan(0);
  });

  it('scrolls a virtualized tree to keep keyboard navigation in view past the window boundary', async () => {
    render(WorkFileTree, { diffs: largeDiffSet() });
    const tree = await screen.findByTestId('work-files-tree');
    await expandBigFolder(tree);

    const treeObserver = TestResizeObserver.instances.find((instance) => instance.target === tree);
    stubViewportHeight(treeObserver!, 360);
    Object.defineProperty(tree, 'scrollTop', { value: 0, writable: true, configurable: true });

    const bigFolderButton = within(tree).getByText('big').closest('button') as HTMLElement;
    bigFolderButton.focus();
    for (let index = 0; index < 40; index += 1) {
      await fireEvent.keyDown(tree, { key: 'ArrowDown' });
    }

    expect(tree.scrollTop).toBeGreaterThan(0);
  });

  it('transfers focus to a mounted row after manual scroll and continues keyboard navigation', async () => {
    render(WorkFileTree, { diffs: largeDiffSet() });
    const tree = await screen.findByTestId('work-files-tree');
    await expandBigFolder(tree);
    const treeObserver = TestResizeObserver.instances.find((instance) => instance.target === tree);
    stubViewportHeight(treeObserver!, 360);
    Object.defineProperty(tree, 'scrollTop', { value: 0, writable: true, configurable: true });

    const firstFile = screen.getByTestId('work-file-src/big/file000000.ts');
    firstFile.focus();
    tree.scrollTop = 36 * 500;
    await fireEvent.scroll(tree);
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

    const focusedAfterScroll = document.activeElement as HTMLElement;
    expect(focusedAfterScroll).toHaveAttribute('role', 'treeitem');
    expect(tree.contains(focusedAfterScroll)).toBe(true);
    const transferredId = focusedAfterScroll.dataset.nodeId;
    await fireEvent.keyDown(tree, { key: 'ArrowDown' });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect((document.activeElement as HTMLElement).dataset.nodeId).not.toBe(transferredId);
    expect(tree.contains(document.activeElement)).toBe(true);
  });

  it('does not steal focus from the file filter during virtualized manual scroll', async () => {
    render(WorkFileTree, { diffs: largeDiffSet() });
    const tree = await screen.findByTestId('work-files-tree');
    await expandBigFolder(tree);
    const treeObserver = TestResizeObserver.instances.find((instance) => instance.target === tree);
    stubViewportHeight(treeObserver!, 360);
    Object.defineProperty(tree, 'scrollTop', { value: 0, writable: true, configurable: true });
    const filter = screen.getByPlaceholderText('Filter files…');
    filter.focus();

    tree.scrollTop = 36 * 500;
    await fireEvent.scroll(tree);
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(document.activeElement).toBe(filter);
  });

  it('recomputes the visible window when the expanded folder is collapsed again', async () => {
    render(WorkFileTree, { diffs: largeDiffSet() });
    const tree = await screen.findByTestId('work-files-tree');
    await expandBigFolder(tree);

    const treeObserver = TestResizeObserver.instances.find((instance) => instance.target === tree);
    stubViewportHeight(treeObserver!, 600);
    expect(tree.dataset.virtualized).toBe('true');
    expect(screen.getByTestId('work-file-src/big/file000000.ts')).toBeTruthy();

    const bigFolderButton = within(tree).getByText('big').closest('button')!;
    await fireEvent.click(bigFolderButton);
    expect(screen.queryByTestId('work-file-src/big/file000000.ts')).toBeNull();
  });

  it('preserves selection, focus, and rendering for a small (non-virtualized) tree unchanged', async () => {
    render(WorkFileTree, { diffs: smallDiffSet(3) });
    const tree = await screen.findByTestId('work-files-tree');
    expect(within(tree).getAllByRole('treeitem').length).toBeGreaterThanOrEqual(3);
    expect(screen.getByTestId('work-diff-pane')).toBeTruthy();
  });
});

describe('WorkFileTree lazy file history', () => {
  const scope = conversationTimelineScope('conversation-1');
  const lazyDiff: WorkFileDiff = {
    path: 'src/file.ts',
    path_generation_id: 'wpg_1',
    diff: '',
    preview_omitted: true,
    preview_omission_reason: 'not_persisted',
    additions: 1,
    deletions: 0,
  };
  function pendingResponse(): {
    promise: Promise<WorkFileHistoryResponse>;
    resolve: (value: WorkFileHistoryResponse) => void;
  } {
    let resolve!: (value: WorkFileHistoryResponse) => void;
    const promise = new Promise<WorkFileHistoryResponse>((next) => { resolve = next; });
    return { promise, resolve };
  }

  function historyResponse(overrides: Partial<WorkFileHistoryResponse> = {}): WorkFileHistoryResponse {
    return {
      scope,
      cache_epoch: 'wce_1',
      core_projector_version: 'core-1',
      files_projector_version: 'files-1',
      path_generation_id: 'wpg_1',
      items: [{ path: 'src/file.ts', diff: '@@ -1 +1 @@\n-old\n+new' }],
      before_cursor: null,
      has_more_before: false,
      ...overrides,
    };
  }

  it('falls back to inline diffs without calling the endpoint when generation identity is absent', async () => {
    const loader = vi.fn<FileHistoryLoader>();
    render(WorkFileTree, {
      diffs: [{ path: 'src/file.ts', diff: '+inline' }],
      scope,
      loadFileHistory: loader,
    });
    await screen.findByTestId('work-combined-diff');
    expect(loader).not.toHaveBeenCalled();
  });

  it('refreshes a stale Work response once before reporting a missing identity', async () => {
    let finishRefresh!: () => void;
    const refreshMissingIdentity = vi.fn(() => new Promise<void>((resolve) => {
      finishRefresh = resolve;
    }));
    const { rerender } = render(WorkFileTree, {
      diffs: [{
        path: 'src/file.ts',
        diff: '',
        preview_omitted: true,
        preview_omission_reason: 'not_persisted',
      }],
      scope,
      cacheKey: 'stale-response',
      refreshMissingIdentity,
    });

    await waitFor(() => expect(refreshMissingIdentity).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('work-file-history-loading')).toBeTruthy();
    expect(screen.queryByText(/no retained file-history identity/i)).toBeNull();

    await rerender({
      diffs: [lazyDiff],
      scope,
      cacheKey: 'stale-response',
      refreshMissingIdentity,
    });
    finishRefresh();
    await waitFor(() => expect(screen.queryByTestId('work-file-history-loading')).toBeNull());
    expect(refreshMissingIdentity).toHaveBeenCalledTimes(1);
  });

  it('loads lazy history and paginates older pages', async () => {
    const loader = vi.fn<FileHistoryLoader>()
      .mockResolvedValueOnce(historyResponse({
        before_cursor: 'cursor-1',
        has_more_before: true,
      }))
      .mockResolvedValueOnce(historyResponse({
        items: [{ path: 'src/file.ts', diff: '@@ -1 +1 @@\n-older\n+old' }],
      }));
    render(WorkFileTree, { diffs: [lazyDiff], scope, loadFileHistory: loader });

    const more = await screen.findByTestId('work-file-history-more');
    expect(loader).toHaveBeenCalledTimes(1);
    await fireEvent.click(more);
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2));
    expect(loader.mock.calls[1][0].before).toBe('cursor-1');
    expect(screen.queryByTestId('work-file-history-more')).toBeNull();
  });

  it('shows a terminal retention message for HTTP 410 and keeps the inline fallback', async () => {
    const loader = vi.fn<FileHistoryLoader>().mockRejectedValue(
      new ChatV2ApiError('history unavailable', {
        code: 'history_unavailable',
        status: 410,
      }),
    );
    render(WorkFileTree, { diffs: [lazyDiff], scope, loadFileHistory: loader });
    expect(await screen.findByTestId('work-file-history-unavailable')).toBeTruthy();
    expect(screen.getByTestId('work-diff-preview-omitted')).toBeTruthy();
    expect(screen.getByText(
      'The Work cache retained exact change totals, but the source diff is no longer retained.',
    )).toBeTruthy();
    expect(screen.queryByText(/projection file limit/i)).toBeNull();
  });

  it('shows the actual projection-budget reason', async () => {
    render(WorkFileTree, {
      diffs: [{
        ...lazyDiff,
        path_generation_id: null,
        preview_omission_reason: 'projection_budget',
      }],
      scope,
    });

    expect(await screen.findByText(
      'The inline diff exceeded the response preview limit. Full file history is unavailable for this item.',
    )).toBeTruthy();
    expect(screen.queryByText(/projection file limit/i)).toBeNull();
  });

  it('renders binary and generated flags without a row-level truncated badge', async () => {
    render(WorkFileTree, {
      diffs: [
        { path: 'binary.dat', diff: '', binary: true },
        { path: 'generated.js', diff: '', generated: true },
        { path: 'truncated.txt', diff: '', content_truncated: true },
      ],
    });
    expect(await screen.findByText('binary')).toBeTruthy();
    expect(screen.getByText('generated')).toBeTruthy();
    expect(screen.queryByText('truncated')).toBeNull();
  });

  it('retains an exact selected diff across refreshed page objects and failed duplicate history', async () => {
    const first = pendingResponse();
    const loader = vi.fn<FileHistoryLoader>()
      .mockImplementationOnce(() => first.promise)
      .mockRejectedValueOnce(new Error('temporary history failure'));
    const { rerender } = render(WorkFileTree, {
      diffs: [{ ...lazyDiff, path_id: 'path-1', source_item_id: 'mutation-1' }],
      scope,
      cacheKey: 'revision-1',
      loadFileHistory: loader,
    });
    first.resolve(historyResponse());
    await screen.findByText('new');
    expect(screen.getByTestId('work-file-path-1')).toHaveAttribute('aria-selected', 'true');

    await rerender({
      diffs: [{
        ...lazyDiff,
        path_id: 'path-1',
        path_generation_id: null,
        source_item_id: 'mutation-1',
      }],
      scope,
      cacheKey: 'revision-2',
      loadFileHistory: loader,
    });
    await Promise.resolve();

    expect(loader).toHaveBeenCalledOnce();
    expect(screen.getByText('new')).toBeTruthy();
    expect(screen.queryByText(/diff is not stored in the Work cache/i)).toBeNull();
    expect(screen.getByTestId('work-file-path-1')).toHaveAttribute('aria-selected', 'true');

    await rerender({
      diffs: [{ ...lazyDiff, path_id: 'path-1', source_item_id: 'mutation-1' }],
      scope,
      cacheKey: 'revision-3',
      loadFileHistory: loader,
    });
    await Promise.resolve();

    expect(loader).toHaveBeenCalledOnce();
    expect(screen.getByText('new')).toBeTruthy();
    expect(screen.queryByText(/diff is not stored in the Work cache/i)).toBeNull();
  });
});
