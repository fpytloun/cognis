import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActivityOverviewResponse, WorkProjectionResponse } from '$lib/chat-v2/types';
import type { StepRun } from '$lib/types/api';
import TaskWorkPanel from './TaskWorkPanel.svelte';
import { clearActivityOverview } from '$lib/activityOverviewCache';
import { invalidateWorkScope } from '$lib/work/workViewState';

afterEach(cleanup);
beforeEach(() => clearActivityOverview());

const run = {
  step_run_id: 'run-1',
  step_name: 'implement',
  conversation_id: 'conversation-1',
  session_id: 'session-1',
  updated_at: '2026-01-01T00:00:00Z'
} as StepRun;

function projection(changedFiles: number): WorkProjectionResponse {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope: {
      key: 'task_step:run-1',
      kind: 'task_step',
      step_run_id: 'run-1',
      conversation_id: 'conversation-1',
      session_id: 'session-1'
    },
    final_deliverable: null,
    mutations: [],
    commands: [],
    artifacts: [],
    summary: { mutations: 2, commands: 3, changed_files: changedFiles, artifacts: 0 },
    has_more_before: false,
    server_time: '2026-01-01T00:00:00Z'
  };
}

function activityOverview(changedFiles: number): ActivityOverviewResponse {
  return {
    schema_version: 2, projection_version: 'test', scope: projection(changedFiles).scope,
    summary: { mutations: 2, commands: 3, changed_files: changedFiles, artifacts: 1 },
    materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
    recent: { commands: [{ id: 'recent-1', category: 'commands', session_id: 'session-1', occurred_at: '2026-01-01T00:00:00Z', title: 'Run tests' }] },
    recent_work: {
      commands: [{ id: 'command-1', call_id: 'call-1', sort_key: '1', command: 'npm test', description: 'Run tests', status: 'complete', preview: 'passed', preview_truncated: false, has_full_output: false }],
      files: [], mutations: [], artifacts: [], deliverables: [],
    },
    workstreams: [{ key: 'root', kind: 'task', root_key: 'root', edge_kind: 'contains', ordinal: 0, conversation_id: 'managed-conversation-1', session_id: 'session-a', event_store_session_id: 'session-a', title: 'Implement', agent_id: 'agent', status: 'complete', current: true, superseded: false, activity_state: 'closed', summary: { changed_files: changedFiles, commands: 3, mutations: 2, artifacts: 1 } }],
    graph_fingerprint: 'graph', graph_truncated: false,
  };
}

describe('TaskWorkPanel', () => {
  it('does not render false zero evidence when the initial projection fails', async () => {
    render(TaskWorkPanel, {
      stepRuns: [run],
      loadWork: vi.fn().mockRejectedValue(new Error('Projection unavailable'))
    });

    await waitFor(() => expect(screen.getByText(/Projection unavailable/)).toBeInTheDocument());
    expect(screen.queryByLabelText('Task work summary')).not.toBeInTheDocument();
    expect(screen.queryByText('No persisted task work yet.')).not.toBeInTheDocument();
  });

  it('reuses activity summary, recent activity, and execution tree with one overview request', async () => {
    const overview = activityOverview(4);
    const loadOverview = vi.fn().mockResolvedValue(overview);
    render(TaskWorkPanel, { stepRuns: [run], loadWork: vi.fn().mockResolvedValue(projection(4)), loadOverview });
    await waitFor(() => expect(screen.getByTitle('npm test')).toBeTruthy());
    expect(screen.getByText('Execution sessions')).toBeTruthy();
    expect(loadOverview).toHaveBeenCalledOnce();
  });

  it('wires shared recent and tree actions to Task navigation callbacks', async () => {
    const onViewSession = vi.fn();
    const onViewWork = vi.fn();
    render(TaskWorkPanel, {
      stepRuns: [run],
      loadWork: vi.fn().mockResolvedValue(projection(4)),
      loadOverview: vi.fn().mockResolvedValue(activityOverview(4)),
      onViewSession,
      onViewWork,
    });
    await waitFor(() => expect(screen.getByText('See all commands')).toBeTruthy());
    await fireEvent.click(screen.getByText('See all commands'));
    expect(onViewWork).toHaveBeenCalledWith(expect.objectContaining({ step_run_id: 'run-1' }), 'commands');
    await fireEvent.click(screen.getByText('Execution sessions'));
    await fireEvent.click(screen.getByRole('button', { name: 'View session Implement' }));
    expect(onViewSession).toHaveBeenCalledWith('session-a');
    await fireEvent.click(screen.getByRole('button', { name: 'View Work for Implement' }));
    expect(onViewWork).toHaveBeenCalledWith({
      key: 'session:session-a',
      kind: 'session',
      session_id: 'session-a',
      conversation_id: 'managed-conversation-1',
    }, 'files');
  });

  it('reloads mounted local overview state for the matching task-step invalidation', async () => {
    const updatedOverview = activityOverview(7);
    updatedOverview.recent = {
      commands: [{
        id: 'recent-2',
        category: 'commands',
        session_id: 'session-2',
        occurred_at: '2026-01-02T00:00:00Z',
        title: 'Run updated tests',
      }],
    };
    updatedOverview.recent_work = {
      ...(updatedOverview.recent_work!),
      commands: [{ ...(updatedOverview.recent_work!.commands[0]), command: 'npm run updated-tests' }],
    };
    updatedOverview.workstreams = [{
      ...updatedOverview.workstreams[0],
      key: 'updated-root',
      root_key: 'updated-root',
      title: 'Updated execution',
    }];
    const loadOverview = vi.fn()
      .mockResolvedValueOnce(activityOverview(4))
      .mockResolvedValueOnce(updatedOverview);
    render(TaskWorkPanel, {
      stepRuns: [run],
      loadWork: vi.fn().mockResolvedValue(projection(4)),
      loadOverview,
    });

    await waitFor(() => expect(screen.getByTitle('npm test')).toBeTruthy());
    expect(screen.getAllByText('4 files').length).toBeGreaterThan(0);
    invalidateWorkScope('task_step:run-1', { workRevision: 2 });

    await waitFor(() => expect(screen.getByTitle('npm run updated-tests')).toBeTruthy());
    expect(screen.getAllByText('7 files').length).toBeGreaterThan(0);
    expect(screen.getByText('Updated execution')).toBeTruthy();
    expect(loadOverview).toHaveBeenCalledTimes(2);
  });

  it('preserves the prior projection and offers retry after a refresh failure', async () => {
    const loadWork = vi.fn()
      .mockResolvedValueOnce(projection(4))
      .mockRejectedValueOnce(new Error('Temporary failure'))
      .mockResolvedValueOnce(projection(5));
    const loadOverview = vi.fn()
      .mockResolvedValueOnce(activityOverview(4))
      .mockResolvedValueOnce(activityOverview(4))
      .mockResolvedValueOnce(activityOverview(5));
    const { rerender } = render(TaskWorkPanel, { stepRuns: [run], loadWork, loadOverview });

    await waitFor(() => expect(screen.getAllByText('4 files').length).toBeGreaterThan(0));
    await rerender({ stepRuns: [{ ...run, updated_at: '2026-01-02T00:00:00Z' }], loadWork, loadOverview });
    await waitFor(() => expect(screen.getByText(/Temporary failure/)).toBeInTheDocument());
    expect(screen.getAllByText('4 files').length).toBeGreaterThan(0);

    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(loadWork).toHaveBeenCalledTimes(3));
    expect(screen.getAllByText('4 files').length).toBeGreaterThan(0);
  });
});
