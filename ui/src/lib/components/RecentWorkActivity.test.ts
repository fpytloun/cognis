import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import RecentWorkActivity from './RecentWorkActivity.svelte';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';

describe('RecentWorkActivity', () => {
  it('reuses Work renderers and exposes category See all links', async () => {
    const onSeeAll = vi.fn();
    const overview = {
      schema_version: 2, projection_version: 'test',
      scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
      summary: { changed_files: 1, commands: 1, mutations: 1, artifacts: 1, deliverables: 1 },
      materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
      workstreams: [], recent: {}, graph_fingerprint: 'g', graph_truncated: false,
      recent_work: {
        files: [{ id: 'f1', call_id: 'file-call-1', tool_name: 'apply_patch', display_name: 'Edit file', status: 'complete', file_diffs: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@\n-old\n+new' }], file_stats: [], additions: 1, deletions: 1 }],
        mutations: [{ id: 'm1', call_id: 'mutation-call-1', tool_name: 'write_config', display_name: 'Update config', status: 'complete', file_diffs: [], file_stats: [], additions: 0, deletions: 0 }],
        commands: [{ id: 'c1', call_id: 'call-1', command: 'npm test', description: 'Run tests', status: 'completed', output: 'passed' }],
        artifacts: [{ id: 'a1', artifact_id: 'art_1', filename: 'report.txt', mime_type: 'text/plain', size_bytes: 10 }],
        deliverables: [{ deliverable_id: 'd1', title: 'Final report', format: 'markdown', content: '# Done' }],
      },
    } as unknown as ActivityOverviewResponse;

    render(RecentWorkActivity, { overview, scope: overview.scope, onSeeAll });
    expect(screen.getByText('npm test')).toBeTruthy();
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
      materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
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

    expect(screen.getByText('src/app.ts')).toBeTruthy();
    expect(screen.getByText('src/other.ts')).toBeTruthy();
    expect(screen.getByText('src/third.ts')).toBeTruthy();
    expect(screen.getAllByText('src/app.ts')).toHaveLength(1);
    expect(screen.getByText('+3')).toBeTruthy();
    expect(screen.getByText('-1')).toBeTruthy();
    expect(screen.queryByText('apply_patch')).toBeNull();
    await fireEvent.click(screen.getByText('src/app.ts').closest('button')!);
    expect(onSeeAll).toHaveBeenCalledWith('files');
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
});
