import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { conversationTimelineScope, type WorkProjectionResponse } from '$lib/chat-v2/types';
import { ChatV2ApiError } from '$lib/chat-v2/api';
import {
  clearWorkViewStates,
  invalidateWorkScope,
  saveWorkViewState,
} from '$lib/work/workViewState';
import WorkView from './WorkView.svelte';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
beforeEach(clearWorkViewStates);

let intersectionCallback: IntersectionObserverCallback | null = null;

class TestIntersectionObserver {
  constructor(callback: IntersectionObserverCallback) {
    intersectionCallback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] { return []; }
  root = null;
  rootMargin = '';
  thresholds = [];
}

function intersectSentinel(): void {
  setSentinelIntersection(true);
}

function setSentinelIntersection(isIntersecting: boolean): void {
  intersectionCallback?.(
    [{ isIntersecting } as IntersectionObserverEntry],
    {} as IntersectionObserver,
  );
}

function projection(): WorkProjectionResponse {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope: conversationTimelineScope('conversation-1'),
    mutations: [
      {
        id: 'mutation-1',
        call_id: 'call-1',
        sort_key: '1',
        tool_name: 'write',
        category: 'filesystem',
        operation_kind: 'file_write',
        status: 'complete',
        arguments: { path: 'src/app.ts' },
        paths: ['src/app.ts'],
        file_diffs: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@\n-old\n+new' }],
        diffs_truncated: false,
      },
      {
        id: 'mutation-2',
        call_id: 'call-2',
        sort_key: '2',
        tool_name: 'memory_add',
        display_name: 'Store memory',
        category: 'memory',
        operation_kind: 'create',
        status: 'complete',
        arguments: {},
        paths: [],
        file_diffs: [],
        diffs_truncated: false,
      },
    ],
    commands: [
      {
        id: 'command-1',
        call_id: 'call-3',
        sort_key: '3',
        command: 'npm test',
        workdir: 'repo',
        status: 'complete',
        preview: '12 tests passed',
        preview_truncated: false,
        has_full_output: true,
      },
    ],
    artifacts: [],
    summary: { mutations: 2, commands: 1, changed_files: 1, artifacts: 0 },
    has_more_before: false,
    server_time: '2026-01-01T00:00:00Z',
  };
}

describe('WorkView', () => {
  it('hides scroll status when the first page is already complete', async () => {
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(projection()),
      refreshIntervalMs: 0,
    });

    await waitFor(() => expect(screen.getByTestId('work-tab-files')).toBeTruthy());
    expect(screen.queryByTestId('work-scroll-sentinel')).toBeNull();
    expect(screen.queryByText('All Work history loaded.')).toBeNull();
    expect(screen.queryByText('More evidence loads as you scroll.')).toBeNull();
  });

  it('preserves a restored active tab through initial load and A to B to A', async () => {
    const scopeA = conversationTimelineScope('conversation-a');
    const scopeB = conversationTimelineScope('conversation-b');
    saveWorkViewState(scopeA, {
      activeTab: 'commands',
      workstreamFilter: 'all',
      agentFilter: 'all',
      statusFilter: 'all',
      workstreamSearch: '',
    });
    const loadWork = vi.fn().mockImplementation((scope) => Promise.resolve({
      ...projection(),
      scope,
    }));
    const { rerender } = render(WorkView, { scope: scopeA, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(screen.getByTestId('work-tab-commands')).toHaveAttribute('aria-selected', 'true'));

    await rerender({ scope: scopeB, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(screen.getByTestId('work-tab-files')).toHaveAttribute('aria-selected', 'true'));
    await fireEvent.click(screen.getByTestId('work-tab-results'));
    await rerender({ scope: scopeA, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(screen.getByTestId('work-tab-commands')).toHaveAttribute('aria-selected', 'true'));
  });

  it('keeps rendered Work stable when the parent recreates the same logical scope', async () => {
    const loadWork = vi.fn().mockResolvedValue(projection());
    const { rerender } = render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 0,
    });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('work-file-explorer')).toBeTruthy();

    await rerender({
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 0,
    });

    await Promise.resolve();
    expect(loadWork).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('work-file-explorer')).toBeTruthy();
  });

  it('refreshes for matching typed Work invalidation and ignores foreign scope', async () => {
    const scope = conversationTimelineScope('conversation-a');
    const first = { ...projection(), scope, work_revision: 1, graph_revision: 1 };
    const second = { ...projection(), scope, work_revision: 2, graph_revision: 1 };
    const loadWork = vi.fn().mockResolvedValueOnce(first).mockResolvedValue(second);
    render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));

    invalidateWorkScope('conversation:conversation-b', {
      workRevision: 2,
      graphRevision: 1,
    });
    await Promise.resolve();
    expect(loadWork).toHaveBeenCalledTimes(1);

    invalidateWorkScope(scope.key, {
      workRevision: 2,
      graphRevision: 1,
    });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2));
  });

  it('hides lineage filters for one source and filters all tabs with provenance for multiple sources', async () => {
    const next = projection();
    const root = {
      key: 'session:root',
      kind: 'root',
      root_key: 'session:root',
      edge_kind: 'root',
      ordinal: 0,
      conversation_id: 'conversation-1',
      session_id: 'root',
      event_store_session_id: 'store-root',
      title: 'Root implementation',
      agent_id: 'architect',
      status: 'completed',
      current: false,
      superseded: false,
    };
    const child = {
      ...root,
      key: 'session:child',
      kind: 'delegate',
      parent_key: root.key,
      edge_kind: 'delegate',
      ordinal: 1,
      session_id: 'child',
      event_store_session_id: 'store-child',
      title: 'UI implementation',
      agent_id: 'worker',
      status: 'running',
      current: true,
    };
    next.workstreams = [root, child];
    next.mutations[0].source_workstream = child;
    next.mutations[1].source_workstream = root;
    next.commands[0].source_workstream = child;
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });

    await waitFor(() => expect(screen.getByTestId('workstream-filters')).toBeTruthy());
    await fireEvent.click(screen.getByTestId('work-filter-toggle'));
    expect(screen.getByRole('option', { name: /↳ UI implementation/ })).toBeTruthy();
    await fireEvent.change(screen.getByLabelText('Workstream'), {
      target: { value: child.key },
    });
    expect(screen.getByRole('tab', { name: /Files 1/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Commands 1/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Mutations 2/ })).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    expect(screen.queryByText('worker · UI implementation · running')).toBeNull();
    await fireEvent.click(screen.getByText('npm test'));
    expect(screen.getByText('worker · UI implementation · running')).toBeTruthy();
    await fireEvent.change(screen.getByLabelText('Status'), {
      target: { value: 'completed' },
    });
    expect(screen.getByRole('tab', { name: /Commands 1/ })).toBeTruthy();
  });

  it('hides lineage filter chrome when the graph has one source', async () => {
    const next = projection();
    next.workstreams = [{
      key: 'session:root',
      kind: 'root',
      root_key: 'session:root',
      edge_kind: 'root',
      ordinal: 0,
      conversation_id: 'conversation-1',
      session_id: 'root',
      event_store_session_id: 'store-root',
      title: 'Root implementation',
      agent_id: 'architect',
      status: 'completed',
      current: true,
      superseded: false,
    }];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });
    await waitFor(() => expect(screen.getByTestId('work-view')).toBeTruthy());
    expect(screen.queryByTestId('workstream-filters')).toBeNull();
  });

  it('clears only invalid lineage filters when a selected child disappears', async () => {
    const first = projection();
    const root = {
      key: 'session:root',
      kind: 'root',
      root_key: 'session:root',
      edge_kind: 'root',
      ordinal: 0,
      session_id: 'root',
      event_store_session_id: 'store-root',
      title: 'Root stream',
      agent_id: 'architect',
      status: 'completed',
      current: true,
      superseded: false,
    };
    const child = {
      ...root,
      key: 'session:child',
      kind: 'delegate',
      parent_key: root.key,
      edge_kind: 'delegate',
      ordinal: 1,
      session_id: 'child',
      event_store_session_id: 'store-child',
      title: 'Child stream',
      agent_id: 'worker',
      status: 'running',
    };
    first.workstreams = [root, child];
    first.graph_fingerprint = 'graph-a';
    const second = { ...first, workstreams: [root], graph_fingerprint: 'graph-b' };
    const loadWork = vi.fn().mockResolvedValueOnce(first).mockResolvedValue(second);
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 0,
    });
    await waitFor(() => expect(screen.getByTestId('workstream-filters')).toBeTruthy());
    await fireEvent.change(screen.getByLabelText('Workstream'), {
      target: { value: child.key },
    });
    await fireEvent.change(screen.getByLabelText('Agent'), {
      target: { value: child.agent_id },
    });
    await fireEvent.change(screen.getByLabelText('Status'), {
      target: { value: child.status },
    });
    await fireEvent.input(screen.getByLabelText('Search'), {
      target: { value: 'useful query' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh work' }));
    await waitFor(() => expect(screen.queryByTestId('workstream-filters')).toBeNull());
    expect(screen.getByRole('tab', { name: /Files 1/ })).toBeTruthy();
  });

  it('uses counted tabs and canonical compact tool rows without duplicate file trees', async () => {
    const loadWork = vi.fn().mockResolvedValue(projection());
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-panel-files')).toBeTruthy());
    expect(screen.getAllByText('src/app.ts').length).toBeGreaterThan(0);
    expect(screen.getAllByRole('tree')).toHaveLength(1);
    expect(screen.getByRole('tab', { name: /Files 1/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Commands 1/ })).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    const command = screen.getByText('npm test');
    expect(command.className).toContain('font-mono');
    expect(screen.queryByRole('button', { name: 'Full output' })).toBeNull();
    await fireEvent.click(command);
    expect(screen.getByText('12 tests passed')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-mutations'));
    expect(screen.getByText('Store memory')).toBeTruthy();
  });

  it('selects files, supports keyboard navigation, filtering, and mobile back navigation', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [
      { path: 'src/client/index.ts', diff: '@@ -1 +1 @@\n-old\n+client' },
      { path: 'src/server/index.ts', diff: '@@ -1 +1 @@\n-old\n+server' },
    ];
    next.summary.changed_files = 2;
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-file-src/client/index.ts')).toBeTruthy());
    expect(screen.getByTestId('work-files-tree-pane').getAttribute('style')).toBeNull();
    const client = screen.getByTestId('work-file-src/client/index.ts');
    const server = screen.getByTestId('work-file-src/server/index.ts');
    await fireEvent.click(server);
    expect(server.getAttribute('aria-selected')).toBe('true');

    await fireEvent.focus(client);
    await fireEvent.keyDown(client, { key: 'ArrowDown' });
    expect(document.activeElement).not.toBe(client);
    await fireEvent.keyDown(screen.getByTestId('work-files-tree'), { key: '/' });
    expect(document.activeElement).toBe(screen.getAllByRole('textbox', { name: 'Filter changed files' })[0]);

    await fireEvent.input(screen.getAllByRole('textbox', { name: 'Filter changed files' })[0], { target: { value: 'missing' } });
    expect(screen.getByTestId('work-files-empty-filter')).toBeTruthy();
    await fireEvent.input(screen.getAllByRole('textbox', { name: 'Filter changed files' })[0], { target: { value: '' } });

    await fireEvent.click(screen.getByTestId('work-file-src/server/index.ts'));
    await fireEvent.click(screen.getByRole('button', { name: 'Back to files' }));
    expect(screen.getByTestId('work-files-tree-pane').className).toContain('flex');
  });

  it('resizes and collapses the desktop tree', async () => {
    const OriginalResizeObserver = globalThis.ResizeObserver;
    globalThis.ResizeObserver = class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) {
        this.callback([{ target, contentRect: { width: 1000 } } as ResizeObserverEntry], this);
      }
      unobserve() {}
      disconnect() {}
    };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(projection()),
      refreshIntervalMs: 60_000,
    });
    await waitFor(() => expect(screen.getByTestId('work-files-resizer')).toBeTruthy());

    await fireEvent.pointerDown(screen.getByTestId('work-files-resizer'), { clientX: 280 });
    await fireEvent.pointerMove(window, { clientX: 360 });
    await fireEvent.pointerUp(window);
    expect(screen.getByTestId('work-files-tree-pane').getAttribute('style')).toContain('360px');
    await fireEvent.keyDown(screen.getByTestId('work-files-resizer'), { key: 'ArrowRight' });
    expect(screen.getByTestId('work-files-tree-pane').getAttribute('style')).toContain('370px');
    expect(screen.getByTestId('work-files-resizer')).toHaveAttribute('aria-valuenow', '370');
    await fireEvent.click(screen.getByTestId('work-file-src/app.ts'));
    expect(screen.getByTestId('work-files-tree-pane').getAttribute('style')).toContain('370px');
    expect(window.localStorage.getItem('cognis.work.fileTreeWidth')).toBe('370');

    await fireEvent.click(screen.getByRole('button', { name: 'Hide files tree' }));
    expect(screen.getByRole('button', { name: 'Show files tree' })).toBeTruthy();
    globalThis.ResizeObserver = OriginalResizeObserver;
  });

  it('shows explicit binary and truncated states without overflowing the page shell', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [
      { path: 'assets/logo.png', diff: 'Binary files differ', binary: true },
      { path: 'src/large.ts', diff: '@@ -1 +1 @@\n-old\n+partial', truncated: true },
    ];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-diff-no-preview')).toBeTruthy());
    expect(screen.getAllByText('No text preview is available.').length).toBeGreaterThan(0);
    await fireEvent.click(screen.getByTestId('work-file-src/large.ts'));
    expect(screen.getByText(/The combined diff contains content-truncated previews/)).toBeTruthy();
    expect(screen.getByTestId('work-diff-pane').className).toContain('min-w-0');
  });

  it('collapses folders for a 40-plus file projection and reveals filtered matches', async () => {
    const next = projection();
    next.mutations[0].file_diffs = Array.from({ length: 41 }, (_, index) => ({
      path: `src/group/file-${index}.ts`,
      diff: '@@ -1 +1 @@\n-old\n+new',
    }));
    next.summary.changed_files = 41;
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-files-tree')).toBeTruthy());
    expect(screen.queryByTestId('work-file-src/group/file-40.ts')).toBeNull();
    await fireEvent.input(screen.getAllByRole('textbox', { name: 'Filter changed files' })[0], {
      target: { value: 'file-40' },
    });
    expect(screen.getByTestId('work-file-src/group/file-40.ts')).toBeTruthy();
    expect(screen.getByRole('treeitem', { name: /src/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('offers one responsive status filter and combines every repeated-path patch', async () => {
    const next = projection();
    const fileMutation = next.mutations[0];
    next.mutations = [
      {
        ...fileMutation,
        id: 'newer-edit',
        call_id: 'newer-call',
        sort_key: '2',
        file_diffs: [
          { path: 'src/app.ts', diff: '@@ -1 +1 @@\n-middle\n+new', status: 'modified' },
        ],
      },
      {
        ...fileMutation,
        id: 'older-edit',
        call_id: 'older-call',
        sort_key: '1',
        file_diffs: [
          { path: 'src/app.ts', diff: '@@ -1 +1 @@\n-old\n+middle', status: 'modified' },
          { path: 'src/new.ts', diff: '--- /dev/null\n+++ b/src/new.ts\n+new', status: 'added' },
        ],
      },
    ];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-file-src/app.ts')).toBeTruthy());
    expect(screen.getAllByText('@@ -1 +1 @@')).toHaveLength(2);
    const combined = screen.getByTestId('work-combined-diff');
    expect(combined.querySelectorAll('section')).toHaveLength(1);
    expect(combined.textContent?.indexOf('old')).toBeLessThan(combined.textContent?.indexOf('new') ?? 0);
    await fireEvent.change(screen.getByRole('combobox', { name: 'Filter by file status' }), {
      target: { value: 'added' },
    });
    expect(screen.queryByTestId('work-file-src/app.ts')).toBeNull();
    expect(screen.getByTestId('work-file-src/new.ts')).toBeTruthy();
  });

  it('shows a real authorized root, aggregate stats, artifacts, and every deliverable', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [{
      path: 'cognis/src/app.ts',
      diff: '@@ -1 +1,2 @@\n-old\n+new\n+extra',
    }];
    next.summary = {
      mutations: 1,
      commands: 1,
      changed_files: 1,
      artifacts: 1,
      deliverables: 2,
      additions: 2,
      deletions: 1,
    };
    next.artifacts = [{
      artifact_id: 'artifact-1',
      filename: 'report.pdf',
      mime_type: 'application/pdf',
      size_bytes: 42,
    }];
    next.deliverables = [
      { deliverable_id: 'result-1', format: 'markdown', title: 'First result', content: 'First body' },
      { deliverable_id: 'result-2', format: 'markdown', title: 'Second result', content: 'Second body' },
    ];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByRole('treeitem', { name: /cognis/ })).toBeTruthy());
    expect(screen.queryByText('…')).toBeNull();
    expect(screen.getByRole('tab', { name: /Files 1.*\+2.*-1/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Deliverables 2/ })).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-artifacts'));
    expect(screen.getByText('report.pdf')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-results'));
    expect(screen.getByTestId('work-deliverable-result-1')).toBeTruthy();
    expect(screen.getByTestId('work-deliverable-result-2')).toBeTruthy();
    expect(screen.getByTestId('work-deliverable-result-1').querySelector('.assistant-deliverable-wrapper')).toHaveAttribute('data-collapsed-by-default', 'true');
  });

  it('renders deliverables newest-first without promoting an older primary', async () => {
    const next = projection();
    next.mutations = [];
    next.commands = [];
    next.deliverables = [
      { deliverable_id: 'newest', format: 'markdown', title: 'Newest', sort_key: '0010' },
      { deliverable_id: 'primary', format: 'markdown', title: 'Older primary', sort_key: '0008' },
      { deliverable_id: 'middle', format: 'markdown', title: 'Middle', sort_key: '0009' },
      { deliverable_id: 'middle', format: 'markdown', title: 'Duplicate middle', sort_key: '0009' },
    ];
    next.final_deliverable = next.deliverables[1];
    next.summary = { mutations: 0, commands: 0, changed_files: 0, artifacts: 0, deliverables: 3 };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
      initialTab: 'results',
      forceInitialTab: true,
    });
    await waitFor(() => expect(screen.getByTestId('work-deliverable-newest')).toBeTruthy());
    const ids = [...screen.getByRole('tabpanel').querySelectorAll('[data-testid^="work-deliverable-"]')]
      .map((element) => element.getAttribute('data-testid'));
    expect(ids).toEqual([
      'work-deliverable-newest',
      'work-deliverable-middle',
      'work-deliverable-primary',
    ]);
    expect(screen.getByTestId('work-deliverable-primary')).toHaveAttribute('data-primary', 'true');
    expect(screen.getByTestId('work-deliverable-newest')).not.toHaveAttribute('data-primary');
  });

  it('uses explicit exact stats when a preview is truncated and marks omitted file previews separately', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [{
      path: 'repo/src/large.ts',
      path_id: 'root:src/large.ts',
      diff: '+partial',
      additions: 900,
      deletions: 400,
      content_truncated: true,
    }];
    next.mutations[0].file_stats = [
      { path: 'repo/src/large.ts', path_id: 'root:src/large.ts', additions: 900, deletions: 400, preview_available: true },
      { path: 'repo/src/omitted.ts', path_id: 'root:src/omitted.ts', additions: 7, deletions: 3, preview_available: false },
    ];
    next.mutations[0].omitted_file_count = 1;
    next.summary = { mutations: 0, commands: 1, changed_files: 2, artifacts: 0, additions: 907, deletions: 403, omitted_files: 1 };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
    });

    await waitFor(() => expect(screen.getAllByText('+907').length).toBeGreaterThan(0));
    expect(screen.getAllByText('-403').length).toBeGreaterThan(0);
    await fireEvent.click(screen.getByTestId('work-file-root:src/omitted.ts'));
    expect(screen.getByTestId('work-diff-preview-omitted')).toBeTruthy();
    expect(screen.getByText(/1 changed files do not have diff previews/)).toBeTruthy();
  });

  it('loads Files once with its category, has no sentinel, and keeps it on a tab round-trip', async () => {
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options) => {
      const next = projection();
      next.commands = [];
      next.mutations = [];
      next.summary = { mutations: 8, commands: 12, changed_files: 1, artifacts: 3, deliverables: 4 };
      return Promise.resolve(next);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    expect(loadWork.mock.calls[0][3]).toMatchObject({ category: 'files', from: null, to: null });
    expect(screen.queryByTestId('work-scroll-sentinel')).toBeNull();
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByTestId('work-tab-files'));
    expect(loadWork).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('tab', { name: /Commands 12/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Mutations 8/ })).toBeTruthy();
  });

  it('passes the exact session filter and exposes a clear chip', async () => {
    const loadWork = vi.fn().mockResolvedValue(projection());
    const onClearSessionFilter = vi.fn();
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      sessionId: 'session-child',
      onClearSessionFilter,
      loadWork,
      refreshIntervalMs: 0,
    });
    await waitFor(() => expect(loadWork).toHaveBeenCalledOnce());
    expect(loadWork.mock.calls[0][3]).toMatchObject({ category: 'files', sessionId: 'session-child' });
    expect(screen.getByTestId('work-session-filter')).toHaveTextContent('session-child');
    await fireEvent.click(screen.getByRole('button', { name: 'Clear session filter' }));
    expect(onClearSessionFilter).toHaveBeenCalledOnce();
  });

  it('isolates cached A, B, and all responses and restores cached B without a request', async () => {
    const scope = conversationTimelineScope('conversation-session-cache');
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options) => {
      const next = projection();
      next.mutations[0].file_diffs = [{
        path: `${options?.sessionId ?? 'all'}.ts`,
        diff: `+${options?.sessionId ?? 'all'}`,
      }];
      return Promise.resolve(next);
    });

    const view = render(WorkView, { scope, sessionId: 'session-a', loadWork, refreshIntervalMs: 0 });
    await screen.findAllByText('session-a.ts');
    await view.rerender({ scope, sessionId: 'session-b', loadWork, refreshIntervalMs: 0 });
    await screen.findAllByText('session-b.ts');
    expect(screen.queryAllByText('session-a.ts')).toHaveLength(0);
    expect(loadWork.mock.calls[1][3]).toMatchObject({ sessionId: 'session-b' });
    await view.rerender({ scope, sessionId: undefined, loadWork, refreshIntervalMs: 0 });
    await screen.findAllByText('all.ts');
    expect(loadWork.mock.calls[2][3].sessionId).toBeUndefined();
    expect(loadWork).toHaveBeenCalledTimes(3);
    view.unmount();

    render(WorkView, { scope, sessionId: 'session-b', loadWork, refreshIntervalMs: 0 });
    expect(screen.getAllByText('session-b.ts').length).toBeGreaterThan(0);
    expect(loadWork).toHaveBeenCalledTimes(3);
  });

  it('does not let a late A refresh overwrite B after the exact-session filter changes', async () => {
    const scope = conversationTimelineScope('conversation-session-race');
    let resolveLateA!: (value: WorkProjectionResponse) => void;
    let aRequests = 0;
    const response = (sessionId: string) => {
      const next = projection();
      next.mutations[0].file_diffs = [{ path: `${sessionId}.ts`, diff: `+${sessionId}` }];
      return next;
    };
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options) => {
      if (options?.sessionId === 'session-a') {
        aRequests += 1;
        if (aRequests > 1) {
          return new Promise<WorkProjectionResponse>((resolve) => { resolveLateA = resolve; });
        }
      }
      return Promise.resolve(response(options?.sessionId ?? 'all'));
    });
    const view = render(WorkView, { scope, sessionId: 'session-a', loadWork, refreshIntervalMs: 0 });
    await screen.findAllByText('session-a.ts');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh work' }));
    await waitFor(() => expect(aRequests).toBe(2));
    await view.rerender({ scope, sessionId: 'session-b', loadWork, refreshIntervalMs: 0 });
    await screen.findAllByText('session-b.ts');
    resolveLateA(response('stale-a'));
    await Promise.resolve();
    expect(screen.queryAllByText('stale-a.ts')).toHaveLength(0);
    expect(screen.getAllByText('session-b.ts').length).toBeGreaterThan(0);
  });

  it('keeps command and mutation cursors independent and advances only the active category', async () => {
    const OriginalIntersectionObserver = globalThis.IntersectionObserver;
    globalThis.IntersectionObserver = TestIntersectionObserver as unknown as typeof IntersectionObserver;
    const loadWork = vi.fn().mockImplementation((_scope, _signal, before?: string, options?: { category: string }) => {
      const next = projection();
      next.mutations = [];
      next.commands = [];
      next.artifacts = [];
      next.deliverables = [];
      next.summary = { mutations: 2, commands: 2, changed_files: 0, artifacts: 0 };
      next.has_more_before = !before;
      next.before_cursor = before ? null : `${options?.category}-older`;
      if (options?.category === 'commands') next.commands = [{ ...projection().commands[0], id: before ? 'command-old' : 'command-new', call_id: before ? 'old' : 'new' }];
      if (options?.category === 'mutations') next.mutations = [{ ...projection().mutations[1], id: before ? 'mutation-old' : 'mutation-new', call_id: before ? 'mutation-old' : 'mutation-new' }];
      return Promise.resolve(next);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await screen.findByTestId('work-scroll-sentinel');
    intersectSentinel();
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
    expect(loadWork.mock.calls.slice(1).map((call) => [call[2], call[3].category])).toEqual([
      [undefined, 'commands'], ['commands-older', 'commands'],
    ]);
    await fireEvent.click(screen.getByTestId('work-tab-mutations'));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(4));
    expect(loadWork.mock.calls[3][2]).toBeUndefined();
    expect(loadWork.mock.calls[3][3].category).toBe('mutations');
    globalThis.IntersectionObserver = OriginalIntersectionObserver;
  });

  it('resets every category and reloads Files when the Work range changes', async () => {
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      next.commands = options?.category === 'commands' ? next.commands : [];
      next.mutations = options?.category === 'mutations' ? next.mutations : [];
      next.artifacts = options?.category === 'artifacts' ? next.artifacts : [];
      next.deliverables = options?.category === 'deliverables' ? next.deliverables : [];
      return Promise.resolve(next);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByTestId('work-time-range-picker'));
    await fireEvent.click(screen.getByRole('button', { name: 'Last 1h' }));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
    expect(loadWork.mock.calls[2][3]).toMatchObject({ category: 'files' });
    expect(loadWork.mock.calls[2][3].from).toMatch(/Z$/);
  });

  it('drops a deferred refresh when the requested category is no longer active', async () => {
    let resolveRefresh!: (value: WorkProjectionResponse) => void;
    let commandRequests = 0;
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      next.commands = options?.category === 'commands' ? next.commands : [];
      next.mutations = options?.category === 'mutations' ? next.mutations : [];
      if (options?.category !== 'commands') return Promise.resolve(next);
      commandRequests += 1;
      if (commandRequests === 1) return Promise.resolve(next);
      return new Promise<WorkProjectionResponse>((resolve) => { resolveRefresh = resolve; });
    });
    const scope = conversationTimelineScope('conversation-1');
    render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await screen.findByText('npm test');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh work' }));
    await waitFor(() => expect(commandRequests).toBe(2));
    await fireEvent.click(screen.getByTestId('work-tab-files'));
    resolveRefresh({
      ...projection(),
      commands: [{ ...projection().commands[0], command: 'stale command response' }],
    });
    await Promise.resolve();
    expect(screen.queryByText('stale command response')).toBeNull();
    expect(screen.getByTestId('work-panel-files')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Refresh work' })).not.toBeDisabled();
    invalidateWorkScope(scope.key, { workRevision: 2 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(4));
  });

  it('marks cached inactive categories stale after invalidation and refreshes on selection', async () => {
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      next.commands = options?.category === 'commands' ? next.commands : [];
      next.mutations = options?.category === 'mutations' ? next.mutations : [];
      return Promise.resolve(next);
    });
    const scope = conversationTimelineScope('conversation-1');
    render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByTestId('work-tab-files'));
    invalidateWorkScope(scope.key, { workRevision: 2 });
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(4));
    expect(loadWork.mock.calls[3][3].category).toBe('commands');
  });

  it('does not append an older Files page after applying a new range', async () => {
    const OriginalIntersectionObserver = globalThis.IntersectionObserver;
    globalThis.IntersectionObserver = TestIntersectionObserver as unknown as typeof IntersectionObserver;
    let resolveOlder!: (value: WorkProjectionResponse) => void;
    const first = projection();
    first.has_more_before = true;
    first.before_cursor = 'old-range';
    const loadWork = vi.fn().mockImplementation((_scope, _signal, before?: string) => {
      if (before === 'old-range') {
        return new Promise<WorkProjectionResponse>((resolve) => { resolveOlder = resolve; });
      }
      return Promise.resolve(first);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-scroll-sentinel');
    intersectSentinel();
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByTestId('work-time-range-picker'));
    await fireEvent.click(screen.getByRole('button', { name: 'Last 1h' }));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
    const older = projection();
    older.mutations[0].file_diffs = [{ path: 'old-range-only.ts', diff: '+old' }];
    resolveOlder(older);
    await Promise.resolve();
    expect(screen.queryByText('old-range-only.ts')).toBeNull();
    globalThis.IntersectionObserver = OriginalIntersectionObserver;
  });

  it('selects a populated legacy category when Files is empty', async () => {
    const legacy = projection();
    legacy.mutations = [];
    legacy.summary.changed_files = 0;
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(legacy),
      refreshIntervalMs: 0,
    });
    expect(await screen.findByText('npm test')).toBeTruthy();
    expect(screen.getByTestId('work-panel-commands')).toBeTruthy();
    expect(screen.queryByText('Loading Work…')).toBeNull();
  });

  it('keeps the Work shell visible while an uncached tab loads, then shows a panel error and retry', async () => {
    let rejectCommands!: (error: Error) => void;
    let commandAttempts = 0;
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      if (options?.category !== 'commands') {
        next.commands = [];
        return Promise.resolve(next);
      }
      commandAttempts += 1;
      if (commandAttempts === 1) {
        return new Promise<WorkProjectionResponse>((_resolve, reject) => { rejectCommands = reject; });
      }
      return Promise.resolve(next);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-panel-files');
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    expect(screen.getByTestId('work-tab-files')).toBeTruthy();
    expect(screen.getByTestId('work-panel-loading')).toBeTruthy();
    rejectCommands(new Error('Command request failed'));
    expect(await screen.findByTestId('work-panel-error')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry loading newest evidence' }));
    expect(await screen.findByText('npm test')).toBeTruthy();
  });

  it('keeps cached A visible during rapid A to B to A switching', async () => {
    let resolveCommands!: (value: WorkProjectionResponse) => void;
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      if (options?.category === 'files') {
        next.commands = [];
        return Promise.resolve(next);
      }
      if (options?.category === 'commands') {
        return new Promise<WorkProjectionResponse>((resolve) => { resolveCommands = resolve; });
      }
      return Promise.resolve(next);
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-panel-files');
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    expect(screen.getByTestId('work-panel-loading')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-files'));
    expect(screen.getByTestId('work-file-explorer')).toBeTruthy();
    resolveCommands(projection());
    await Promise.resolve();
    expect(screen.getByTestId('work-file-explorer')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    expect(screen.getByTestId('work-panel-loading')).toBeTruthy();
  });

  it('preserves the shell and hides stale panel data while a new range loads', async () => {
    let resolveRange!: (value: WorkProjectionResponse) => void;
    let filesRequests = 0;
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      if (options?.category === 'files') {
        filesRequests += 1;
        if (filesRequests > 1) return new Promise<WorkProjectionResponse>((resolve) => { resolveRange = resolve; });
      }
      return Promise.resolve(projection());
    });
    render(WorkView, { scope: conversationTimelineScope('conversation-1'), loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-file-explorer');
    await fireEvent.click(screen.getByTestId('work-time-range-picker'));
    await fireEvent.click(screen.getByRole('button', { name: 'Last 1h' }));
    expect(screen.getByTestId('work-tab-files')).toBeTruthy();
    expect(screen.getByTestId('work-panel-loading')).toBeTruthy();
    expect(screen.queryByTestId('work-file-explorer')).toBeNull();
    resolveRange(projection());
    expect(await screen.findByTestId('work-file-explorer')).toBeTruthy();
  });

  it('exposes one collapsed narrow filter control with active status and clear action', async () => {
    const next = projection();
    const root = {
      key: 'session:root', kind: 'root', root_key: 'session:root', edge_kind: 'root',
      ordinal: 0, session_id: 'root', event_store_session_id: 'store-root',
      title: 'Root', agent_id: 'architect', status: 'completed', current: true, superseded: false,
    };
    next.workstreams = [
      root,
      { ...root, key: 'session:child', kind: 'delegate', parent_key: root.key, ordinal: 1, title: 'Child', agent_id: 'worker' },
    ];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });
    const toggle = await screen.findByTestId('work-filter-toggle');
    expect(screen.getByTestId('work-toolbar')).toContainElement(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByTestId('workstream-filters')).toHaveAttribute('hidden');
    await fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('workstream-filters')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('workstream-filters')).toHaveClass('w-full');
    expect(screen.getByTestId('workstream-filters')).not.toHaveClass('absolute');
    await fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'worker' } });
    expect(toggle).toHaveTextContent('1 active');
    await fireEvent.click(toggle);
    expect(screen.getByLabelText('Agent')).toHaveValue('worker');
    await fireEvent.click(toggle);
    await fireEvent.click(screen.getByTestId('work-filter-clear'));
    expect(toggle).not.toHaveTextContent('active');
    expect(screen.getByLabelText('Agent')).toHaveValue('all');
  });

  it('selects exactly one path identity when same-label roots share a display path', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [
      {
        path: 'repo/src/app.ts',
        path_id: 'path-one',
        root_name: 'repo',
        root_id: 'root-one',
        diff: '+one',
      },
      {
        path: 'repo/src/app.ts',
        path_id: 'path-two',
        root_name: 'repo',
        root_id: 'root-two',
        diff: '+two',
      },
    ];
    next.summary.changed_files = 2;
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });

    await waitFor(() => expect(screen.getByTestId('work-file-path-one')).toBeTruthy());
    expect(screen.getAllByRole('treeitem', { selected: true })).toHaveLength(1);
    await fireEvent.click(screen.getByTestId('work-file-path-two'));
    expect(screen.getAllByRole('treeitem', { selected: true })).toHaveLength(1);
    expect(screen.getByTestId('work-file-path-two')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('work-file-path-one')).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByText('repo · ot-one')).toBeTruthy();
  });

  it('combines text patches and summarizes unavailable binary and generated history', async () => {
    const next = projection();
    next.mutations[0].file_diffs = [
      { path: 'src/binary-text.ts', diff: 'Binary files differ', binary: true },
      { path: 'src/binary-text.ts', diff: '@@ -1 +1 @@\n-before\n+after' },
      { path: 'src/generated-text.ts', diff: '', generated: true },
      { path: 'src/generated-text.ts', diff: '@@ -1 +1 @@\n-before\n+after' },
      { path: 'src/text-binary.ts', diff: '@@ -1 +1 @@\n-before\n+after' },
      { path: 'src/text-binary.ts', diff: 'Binary files differ', binary: true },
      { path: 'src/text-generated.ts', diff: '@@ -1 +1 @@\n-before\n+after' },
      { path: 'src/text-generated.ts', diff: '', generated: true },
    ];
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByTestId('work-file-src/binary-text.ts')).toBeTruthy());
    for (const path of ['src/binary-text.ts', 'src/generated-text.ts', 'src/text-binary.ts', 'src/text-generated.ts']) {
      await fireEvent.click(screen.getByTestId(`work-file-${path}`));
      expect(screen.getByText('@@ -1 +1 @@')).toBeTruthy();
      expect(screen.getByTestId('work-diff-partial-history')).toHaveTextContent(
        '1 change without a text preview is not included in the combined diff.'
      );
      expect(screen.getByTestId('work-combined-diff').querySelectorAll('section')).toHaveLength(1);
    }
  });

  it('shows an error with a retry action', async () => {
    const loadWork = vi.fn()
      .mockRejectedValueOnce(new Error('Projection unavailable'))
      .mockResolvedValueOnce({ ...projection(), mutations: [], commands: [], summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0 } });
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByText('Projection unavailable')).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: 'Retry loading newest evidence' }));
    await waitFor(() => expect(screen.getByText('No persisted work yet.')).toBeTruthy());
    expect(loadWork).toHaveBeenCalledTimes(2);
  });

  it('shows hydration progress and retries a temporary event-store failure', async () => {
    const loadWork = vi.fn()
      .mockRejectedValueOnce(new ChatV2ApiError('Session event store is temporarily unavailable', {
        code: 'event_store_unavailable',
        status: 503,
      }))
      .mockResolvedValueOnce(projection());
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork,
      refreshIntervalMs: 60_000,
    });

    await waitFor(() => expect(screen.getByText('Loading Work history from cache…')).toBeTruthy());
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(2), { timeout: 2_000 });
    await waitFor(() => expect(screen.getAllByText('src/app.ts').length).toBeGreaterThan(0));
  });

  it('cancels a retryable hydration when returning to cached Files and permits a later refresh', async () => {
    vi.useFakeTimers();
    const scope = conversationTimelineScope('conversation-1');
    const retryable = new ChatV2ApiError('Session event store is temporarily unavailable', {
      code: 'event_store_unavailable',
      status: 503,
    });
    const loadWork = vi.fn().mockImplementation((_scope, _signal, _before, options?: { category: string }) => {
      const next = projection();
      next.commands = options?.category === 'commands' ? next.commands : [];
      if (options?.category === 'files') return Promise.resolve(next);
      if (options?.category === 'commands') return Promise.reject(retryable);
      return Promise.resolve(next);
    });
    render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await vi.waitFor(() => expect(loadWork).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByTestId('work-tab-commands')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await vi.waitFor(() => expect(screen.getByTestId('work-panel-loading')).toBeTruthy());
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Refresh work' })).toBeDisabled());
    await fireEvent.click(screen.getByTestId('work-tab-files'));
    expect(screen.getByTestId('work-file-explorer')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Refresh work' })).not.toBeDisabled();
    await vi.advanceTimersByTimeAsync(3_500);
    expect(loadWork).toHaveBeenCalledTimes(2);
    invalidateWorkScope(scope.key, { workRevision: 2 });
    await vi.waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
  });

  it('shows partial materialization without a false empty or exhausted state', async () => {
    const next = projection();
    next.mutations = [];
    next.commands = [];
    next.artifacts = [];
    next.deliverables = [];
    next.materialization = {
      state: 'materializing',
      completed_streams: 17,
      total_streams: 195,
      covered_events: 400,
      target_events: 2_000,
      failed_streams: 0,
    };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });

    expect(await screen.findByText('Building Work history — 17 of 195 streams')).toBeTruthy();
    expect(screen.getAllByText('Results below are partial.').length).toBeGreaterThan(0);
    expect(screen.queryByText('No persisted work yet.')).toBeNull();
    expect(screen.queryByText('All Work history loaded.')).toBeNull();
  });

  it('shows explicit repair counts', async () => {
    const next = projection();
    next.materialization = {
      state: 'repair',
      completed_streams: 3,
      total_streams: 5,
      covered_events: 40,
      target_events: 60,
      failed_streams: 1,
      retry_after_ms: 1_000,
    };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });

    expect(
      await screen.findByText('Work history is incomplete — 3 of 5 streams are ready.')
    ).toBeTruthy();
    expect(screen.getByText('1 streams failed. Cognis will retry the background repair.')).toBeTruthy();
  });

  it.each([
    ['suppresses a one-event live tail', 178, 179, 0, false],
    ['shows a six-event backlog', 173, 179, 0, true],
    ['shows a failed stream', 178, 179, 1, true],
  ])('%s', async (_label, coveredEvents, targetEvents, failedStreams, visible) => {
    const next = projection();
    next.materialization = {
      state: 'repair',
      completed_streams: 178,
      total_streams: 179,
      covered_events: coveredEvents,
      target_events: targetEvents,
      failed_streams: failedStreams,
    };
    render(WorkView, {
      scope: conversationTimelineScope('conversation-1'),
      loadWork: vi.fn().mockResolvedValue(next),
      refreshIntervalMs: 0,
    });
    await screen.findByTestId('work-panel-files');
    if (visible) {
      expect(screen.getByTestId('work-repair')).toBeTruthy();
    } else {
      expect(screen.queryByTestId('work-repair')).toBeNull();
      expect(screen.queryByText('Results below are partial.')).toBeNull();
    }
  });

  it('restores a fresh same-scope response without another request', async () => {
    const scope = conversationTimelineScope('conversation-cache-remount');
    const loadWork = vi.fn().mockResolvedValue(projection());
    const first = render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-panel-files');
    expect(loadWork).toHaveBeenCalledOnce();
    first.unmount();
    render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    expect(screen.getByTestId('work-panel-files')).toBeTruthy();
    expect(loadWork).toHaveBeenCalledOnce();
  });

  it('persists the command label mode globally', async () => {
    window.localStorage.removeItem('cognis:work-command-label-mode');
    const scope = conversationTimelineScope('conversation-command-mode');
    const loadWork = vi.fn().mockResolvedValue(projection());
    const first = render(WorkView, { scope, loadWork, refreshIntervalMs: 0 });
    await screen.findByTestId('work-panel-files');
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    await screen.findByTestId('work-command-label-mode');
    await fireEvent.click(screen.getByRole('button', { name: 'Description' }));
    expect(window.localStorage.getItem('cognis:work-command-label-mode')).toBe('description');
    first.unmount();
    render(WorkView, {
      scope: conversationTimelineScope('conversation-command-mode-2'),
      loadWork,
      refreshIntervalMs: 0,
    });
    await screen.findByTestId('work-panel-files');
    await fireEvent.click(screen.getByTestId('work-tab-commands'));
    expect(await screen.findByRole('button', { name: 'Description' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('expands and minimizes the selected non-collapsible diff without losing selection', async () => {
    render(WorkView, {
      scope: conversationTimelineScope('conversation-diff-overlay'),
      loadWork: vi.fn().mockResolvedValue(projection()),
      refreshIntervalMs: 0,
    });
    const selected = await screen.findByTestId('work-file-src/app.ts');
    expect(selected).toHaveAttribute('aria-selected', 'true');
    await fireEvent.click(screen.getByText('Expand diff'));
    expect(screen.getByTestId('work-diff-overlay')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Minimize diff' }));
    expect(screen.queryByTestId('work-diff-overlay')).toBeNull();
    expect(screen.getByTestId('work-file-src/app.ts')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Expand diff')).toBeTruthy();
  });
});
