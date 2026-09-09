// @ts-nocheck -- retired history response metadata is ignored.
import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import RecentWorkActivity from './RecentWorkActivity.svelte';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';
import { chatV2Api } from '$lib/chat-v2/api';

describe('RecentWorkActivity', () => {
  beforeEach(() => window.localStorage.clear());

  it('reuses Work renderers and exposes category See all links', async () => {
    const onSeeAll = vi.fn();
    const overview = {
      schema_version: 2, projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 1, mutations: 1, artifacts: 1, deliverables: 1 },
      materialization: { state: 'live' },
      workstreams: [],
      recent: {
        commands: [{
          id: 'c1',
          category: 'commands',
          session_id: 'session-1',
          occurred_at: '2026-01-01T00:00:00Z',
          title: 'Run tests',
        }],
      },
      graph_fingerprint: 'g',
      graph_truncated: false,
      recent_work: {
        files: [{ id: 'f1', call_id: 'file-call-1', tool_name: 'apply_patch', display_name: 'Edit file', status: 'complete', file_diffs: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@\n-old\n+new' }], file_stats: [], additions: 1, deletions: 1 }],
        mutations: [{ id: 'm1', call_id: 'mutation-call-1', tool_name: 'write_config', display_name: 'Update config', status: 'complete', file_diffs: [], file_stats: [], additions: 0, deletions: 0 }],
        commands: [{ id: 'c1', call_id: 'call-1', command: 'npm test', description: 'Run tests', status: 'completed', duration_ms: 4200, output: 'passed' }],
        artifacts: [{ id: 'a1', artifact_id: 'art_1', filename: 'report.txt', mime_type: 'text/plain', size_bytes: 10 }],
        deliverables: [{ deliverable_id: 'd1', title: 'Final report', format: 'markdown', content: '# Done' }],
      },
    } as unknown as ActivityOverviewResponse;

    render(RecentWorkActivity, { overview, scope: overview.scope, onSeeAll });
    expect(screen.getByText('npm test')).toBeTruthy();
    expect(screen.getByText('4.2s')).toBeTruthy();
    await fireEvent.click(screen.getByText('npm test').closest('button')!);
    expect(screen.getByTestId('tool-execution-status')).toHaveTextContent('Success');
    expect(screen.getByText('src/app.ts')).toBeTruthy();
    expect(screen.queryByText('old')).toBeNull();
    const fileDisclosure = screen.getByText('src/app.ts').closest('button')!;
    expect(fileDisclosure).toHaveAttribute('aria-expanded', 'false');
    expect(fileDisclosure).toHaveAttribute('aria-controls');
    await fireEvent.click(fileDisclosure);
    expect(fileDisclosure).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('old')).toBeTruthy();
    expect(screen.getByText('report.txt')).toBeTruthy();
    expect(screen.getByText('See all deliverables')).toBeTruthy();
    expect(document.querySelector('.assistant-deliverable-wrapper')).toHaveAttribute('data-collapsed-by-default', 'true');
    await fireEvent.click(screen.getByText('See all commands'));
    expect(onSeeAll).toHaveBeenCalledWith('commands');
    await fireEvent.click(screen.getByText('See all files'));
    expect(onSeeAll).toHaveBeenCalledWith('files');
    await fireEvent.click(screen.getByText('See all mutations'));
    expect(onSeeAll).toHaveBeenCalledWith('mutations');
  });

  it('renders lightweight apply_patch activity as file edits instead of tool calls', async () => {
    const onSeeAll = vi.fn();
    const overview = {
      schema_version: 2, projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 0, mutations: 1, artifacts: 0, deliverables: 0 },
      materialization: { state: 'live' },
      workstreams: [], recent: {}, graph_fingerprint: 'g', graph_truncated: false,
      recent_work: {
        files: [{
          id: 'f1', call_id: 'file-call-1', tool_name: 'apply_patch',
          display_name: 'Apply patch', status: 'complete',
          file_diffs: [{
            path: '/repo/src/app.ts', relative_path: 'src/app.ts',
            diff: '', additions: 3, deletions: 1, content_truncated: true,
          }],
          file_stats: [
            {
              path: '/repo/src/app.ts', path_id: 'root:src/app.ts',
              relative_path: 'src/app.ts', additions: 3, deletions: 1,
              preview_available: false,
            },
            {
              path: '/repo/src/other.ts', path_id: 'root:src/other.ts',
              relative_path: 'src/other.ts', additions: 2, deletions: 0,
              preview_available: false,
            },
          ],
          paths: ['/repo/src/app.ts', '/repo/src/other.ts', 'src/third.ts'],
          additions: 5, deletions: 1,
        }],
        mutations: [], commands: [], artifacts: [], deliverables: [],
      },
    } as unknown as ActivityOverviewResponse;

    render(RecentWorkActivity, { overview, scope: overview.scope, onSeeAll });

    expect(screen.queryByText('apply_patch')).toBeNull();
    expect(screen.getByText('src/app.ts')).toBeTruthy();
    expect(screen.getByText('src/other.ts')).toBeTruthy();
    expect(screen.getByText('src/third.ts')).toBeTruthy();
    expect(screen.getByText('+3')).toBeTruthy();
    expect(screen.getByText('−1')).toBeTruthy();
    const expand = screen.getByRole('button', { name: 'Expand src/app.ts' });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    await fireEvent.click(expand);
    expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(onSeeAll).not.toHaveBeenCalled();
  });

  it('shows file type, preserved execution time, a tail-scrolled path, and an icon-only Work action', async () => {
    const timestamp = '2026-01-01T12:34:00Z';
    const overview = {
      schema_version: 2,
      projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 0, mutations: 1, artifacts: 0, deliverables: 0 },
      workstreams: [],
      recent: {
        files: [{
          id: 'timed-edit',
          category: 'files',
          session_id: 'session-1',
          occurred_at: timestamp,
        }],
      },
      recent_work: {
        files: [{
          id: 'timed-edit',
          call_id: 'timed-call',
          tool_name: 'apply_patch',
          status: 'complete',
          paths: [],
          file_stats: [],
          file_diffs: [{
            path: '/long/repository/cognis/api/chat_v2/work_projection.py',
            diff: '+change',
          }],
        }],
        mutations: [],
        commands: [],
        artifacts: [],
        deliverables: [],
      },
    } as unknown as ActivityOverviewResponse;
    render(RecentWorkActivity, { overview, scope: overview.scope });

    expect(screen.getByTestId('recent-file-type')).toHaveTextContent('Py');
    expect(screen.queryByText('✓')).toBeNull();
    expect(screen.queryByText('Open in Work')).toBeNull();
    expect(screen.getByRole('button', { name: /Open .*work_projection\.py in Work/ }))
      .toHaveAttribute('title', 'Open in Work');
    await fireEvent.click(screen.getByRole('button', { name: /Expand .*work_projection\.py/ }));
    expect(screen.getByTestId('recent-file-time')).toHaveAttribute('title', timestamp);

    const path = screen.getByTestId('recent-file-path');
    Object.defineProperties(path, {
      scrollWidth: { configurable: true, value: 600 },
      clientWidth: { configurable: true, value: 180 },
    });
    window.dispatchEvent(new Event('resize'));
    await Promise.resolve();
    expect(path.scrollLeft).toBe(420);
  });

  it('toggles compact command labels between raw commands and descriptions', async () => {
    const overview = {
      schema_version: 2,
      projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 0, commands: 1, mutations: 0, artifacts: 0, deliverables: 0 },
      workstreams: [],
      recent: {},
      recent_work: {
        files: [],
        mutations: [],
        commands: [{
          id: 'c1',
          call_id: 'call-1',
          command: 'npm test',
          description: 'Run focused tests',
          status: 'completed',
          output: 'passed',
        }],
        artifacts: [],
        deliverables: [],
      },
    } as unknown as ActivityOverviewResponse;

    render(RecentWorkActivity, { overview, scope: overview.scope });
    const label = screen.getByTestId('tool-command-summary-scroll');
    expect(label).toHaveTextContent('npm test');

    await fireEvent.click(screen.getByRole('button', { name: 'Description' }));
    expect(label).toHaveTextContent('Run focused tests');
    expect(window.localStorage.getItem('cognis:work-command-label-mode')).toBe('description');

    await fireEvent.click(screen.getByRole('button', { name: 'Command' }));
    expect(label).toHaveTextContent('npm test');
  });

  it('preserves newest-first deliverable order from the Overview projection', () => {
    const overview = {
      schema_version: 2,
      projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 0, commands: 0, mutations: 0, artifacts: 0, deliverables: 2 },
      workstreams: [],
      recent: {},
      recent_work: {
        files: [], mutations: [], commands: [], artifacts: [],
        deliverables: [
          { deliverable_id: 'newest', title: 'Newest', format: 'markdown', sort_key: '0010' },
          { deliverable_id: 'older', title: 'Older', format: 'markdown', sort_key: '0009' },
        ],
      },
    } as unknown as ActivityOverviewResponse;

    render(RecentWorkActivity, { overview, scope: overview.scope });
    expect([...document.querySelectorAll('.assistant-deliverable-wrapper')].map(
      (element) => element.getAttribute('data-deliverable-id'),
    )).toEqual(['newest', 'older']);
  });

  it('keeps two separate edit calls on the same file as two compact event rows', () => {
    const overview = {
      schema_version: 2, projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 0, mutations: 2, artifacts: 0, deliverables: 0 },
      workstreams: [], recent: {},
      recent_work: {
        files: [
          { id: 'edit-1', call_id: 'call-1', tool_name: 'apply_patch', status: 'complete', duration_ms: 318, paths: ['src/shared.ts'], file_stats: [], file_diffs: [{ path: 'src/shared.ts', diff: '+one', additions: 42, deletions: 8 }], additions: 42, deletions: 8 },
          { id: 'edit-2', call_id: 'call-2', tool_name: 'apply_patch', status: 'complete', duration_ms: 544, paths: ['src/shared.ts'], file_stats: [], file_diffs: [{ path: 'src/shared.ts', diff: '+two', additions: 61, deletions: 13 }], additions: 61, deletions: 13 },
        ],
        mutations: [], commands: [], artifacts: [], deliverables: [],
      },
    } as unknown as ActivityOverviewResponse;
    render(RecentWorkActivity, { overview, scope: overview.scope });
    expect(screen.getByTestId('recent-file-edit-edit-1-src/shared.ts')).toHaveTextContent('shared.ts');
    expect(screen.getByTestId('recent-file-edit-edit-1-src/shared.ts')).toHaveTextContent('+42');
    expect(screen.getByTestId('recent-file-edit-edit-1-src/shared.ts')).toHaveTextContent('−8');
    expect(screen.getByTestId('recent-file-edit-edit-2-src/shared.ts')).toHaveTextContent('+61');
    expect(screen.getAllByRole('button', { name: 'Expand src/shared.ts' })).toHaveLength(2);
  });

  it('loads and filters exact lazy history independently from Open in Work', async () => {
    const source = {
      key: 'managed:child', root_key: 'conversation:c1', kind: 'managed', edge_kind: 'managed',
      ordinal: 1, session_id: 'child-session', event_store_session_id: 'child-session',
      conversation_id: 'c1', title: 'Child', agent_id: 'worker', status: 'completed',
      current: false, superseded: false,
    };
    const overview = {
      schema_version: 2, projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 0, mutations: 1, artifacts: 0, deliverables: 0 },
      workstreams: [source], recent: {},
      recent_work: {
        files: [{ id: 'exact-edit', call_id: 'call-exact', tool_name: 'apply_patch', status: 'complete', duration_ms: 12, paths: ['src/exact.ts'], file_diffs: [], file_stats: [{ path: 'src/exact.ts', path_id: 'path-exact', path_generation_id: 'generation-exact', additions: 2, deletions: 1, preview_available: false }], additions: 2, deletions: 1, source_workstream: source }],
        mutations: [], commands: [], artifacts: [], deliverables: [],
      },
    } as unknown as ActivityOverviewResponse;
    vi.spyOn(chatV2Api, 'fileHistory').mockResolvedValue({
      scope: overview.scope,
      cache_epoch: 'epoch', core_projector_version: 'core', files_projector_version: 'files',
      path_generation_id: 'generation-exact',
      items: [
        { path: 'src/exact.ts', path_generation_id: 'generation-exact', source_item_id: 'other-edit', diff: '+wrong' },
        { path: 'src/exact.ts', path_generation_id: 'generation-exact', source_item_id: 'exact-edit', diff: '+right' },
      ],
      before_cursor: null, has_more_before: false,
    });
    const onOpenWork = vi.fn();
    render(RecentWorkActivity, { overview, scope: overview.scope, onOpenWork });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand src/exact.ts' }));
    expect(await screen.findByText('right')).toBeTruthy();
    expect(screen.queryByText('wrong')).toBeNull();
    expect(onOpenWork).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Open src/exact.ts in Work' }));
    expect(onOpenWork).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'session', session_id: 'child-session' }),
      expect.objectContaining({
        workItemId: 'exact-edit',
        files: [expect.objectContaining({ pathGenerationId: 'generation-exact' })],
      }),
    );
  });
});
