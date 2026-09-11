// @ts-nocheck -- focused live lifecycle tests own lifecycle fixture validation.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ActivityOverviewResponse,
  TimelineScope,
  WorkProjectionResponse,
} from '$lib/chat-v2/types';
import { conversationTimelineScope } from '$lib/chat-v2/types';
import type { StepRun } from '$lib/types/api';
import TaskWorkPanel from './TaskWorkPanel.svelte';
import {
  clearActivityOverview,
  requestActivityOverview,
  setActivityOverview,
} from '$lib/activityOverviewCache';
import {
  clearWorkViewStates,
  invalidateWorkFromSocket,
  invalidateWorkScope,
} from '$lib/work/workViewState';

afterEach(cleanup);
beforeEach(() => {
  clearActivityOverview();
  clearWorkViewStates();
  window.localStorage.clear();
});

const run = {
  task_id: 'task-1',
  step_run_id: 'run-1',
  step_name: 'implement',
  conversation_id: 'conversation-1',
  session_id: 'session-1',
  updated_at: '2026-01-01T00:00:00Z'
} as StepRun;

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

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
    materialization: { state: 'live' },
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
  it('includes the task identity in every task-step Work scope', async () => {
    const loadWork = vi.fn().mockResolvedValue(projection(4));
    const loadOverview = vi.fn().mockResolvedValue(activityOverview(4));

    render(TaskWorkPanel, { stepRuns: [run], loadWork, loadOverview });

    await waitFor(() => expect(loadWork).toHaveBeenCalledWith(
      expect.objectContaining({
        key: 'task_step:run-1',
        kind: 'task_step',
        task_id: 'task-1',
        step_run_id: 'run-1',
      }),
    ));
    await waitFor(() => expect(loadOverview).toHaveBeenCalledWith(
      expect.objectContaining({
        key: 'task_step:run-1',
        kind: 'task_step',
        task_id: 'task-1',
        step_run_id: 'run-1',
      }),
      expect.any(AbortSignal),
    ));
  });

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
    await fireEvent.click(screen.getByRole('button', { name: 'Description' }));
    expect(screen.getByTitle('Run tests')).toBeTruthy();
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

  it('reloads an exact-scope overview only for a strictly newer socket revision', async () => {
    const scope = projection(4).scope;
    setActivityOverview(scope, {
      ...activityOverview(4),
      work_revision: 7,
    });
    const loadOverview = vi.fn().mockResolvedValue({
      ...activityOverview(8),
      work_revision: 8,
    });
    render(TaskWorkPanel, {
      stepRuns: [run],
      loadWork: vi.fn().mockResolvedValue(projection(4)),
      loadOverview,
    });
    await waitFor(() => expect(screen.getAllByText('4 files').length).toBeGreaterThan(0));

    for (const revision of ['7', '6']) {
      invalidateWorkFromSocket({
        type: 'work_invalidated',
        reason: 'work_invalidated',
        revision,
        work_scope_key: scope.key,
      });
      await Promise.resolve();
    }
    expect(loadOverview).not.toHaveBeenCalled();

    invalidateWorkFromSocket({
      type: 'work_invalidated',
      reason: 'work_invalidated',
      revision: '8',
      work_scope_key: scope.key,
    });
    await waitFor(() => expect(loadOverview).toHaveBeenCalledOnce());
  });

  it('aborts an obsolete selected task-step request and starts the queued current scope', async () => {
    const blockerReleases: Array<() => void> = [];
    const blockers = Array.from({ length: 3 }, (_, index) => {
      const blockerScope = conversationTimelineScope(`task-blocker-${index}`);
      return requestActivityOverview(blockerScope, () => new Promise((resolve) => {
        blockerReleases.push(() => resolve({
          ...activityOverview(1),
          scope: blockerScope,
        }));
      }));
    });
    const oldOverview = deferred<ActivityOverviewResponse>();
    const currentOverview = deferred<ActivityOverviewResponse>();
    const runTwo = {
      ...run,
      step_run_id: 'run-2',
      step_name: 'review',
      session_id: 'session-2',
      updated_at: '2025-12-31T00:00:00Z',
    } as StepRun;
    const loadOverview = vi.fn((scope: TimelineScope) => (
      scope.key === 'task_step:run-1'
        ? oldOverview.promise
        : currentOverview.promise
    ));
    const loadWork = vi.fn(async (scope: TimelineScope) => ({
      ...projection(4),
      scope,
    }));
    const rendered = render(TaskWorkPanel, {
      stepRuns: [run, runTwo],
      loadWork,
      loadOverview,
    });
    await waitFor(() => expect(loadOverview).toHaveBeenCalledWith(
      expect.objectContaining({ key: 'task_step:run-1' }),
      expect.any(AbortSignal),
    ));

    await rendered.rerender({
      stepRuns: [
        run,
        { ...runTwo, updated_at: '2026-01-02T00:00:00Z' },
      ],
      loadWork,
      loadOverview,
    });
    await waitFor(() => expect(loadOverview).toHaveBeenCalledWith(
      expect.objectContaining({ key: 'task_step:run-2' }),
      expect.any(AbortSignal),
    ));

    currentOverview.resolve({
      ...activityOverview(9),
      scope: {
        ...activityOverview(9).scope,
        key: 'task_step:run-2',
        step_run_id: 'run-2',
        session_id: 'session-2',
      },
    });
    await waitFor(() => expect(screen.getAllByText('9 files').length).toBeGreaterThan(0));
    oldOverview.resolve(activityOverview(99));
    await Promise.resolve();
    expect(screen.queryByText('99 files')).not.toBeInTheDocument();

    blockerReleases.forEach((release) => release());
    await Promise.all(blockers);
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

  it('loads the canonical task result through its authorized step-run accessor', async () => {
    const work = projection(1);
    work.final_deliverable = {
      deliverable_id: 'deliverable-1',
      format: 'markdown',
      title: 'Canonical result',
      content: '# Result',
      render_metadata: {},
      export_metadata: {},
    };
    const loadDeliverableForStepRun = vi.fn().mockResolvedValue({
      deliverable_id: 'deliverable-1',
      step_run_id: 'run-1',
      version: 1,
      attempt_number: 1,
      content: '# Result\n\nLoaded through the step.',
      format: 'markdown',
      title: 'Canonical result',
      target: 'none',
      outputs: {},
      status: 'ready',
      evaluator_feedback: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    });

    const loadOverview = vi.fn().mockResolvedValue(activityOverview(1));
    render(TaskWorkPanel, {
      stepRuns: [run],
      canonicalDeliverableId: 'deliverable-1',
      loadWork: vi.fn().mockResolvedValue(work),
      loadOverview,
      loadDeliverableForStepRun,
      view: 'deliverable',
    });

    await waitFor(() => expect(loadDeliverableForStepRun).toHaveBeenCalledWith('run-1', 'deliverable-1'));
    await screen.findByTestId('rich-deliverable');
    expect(screen.getByRole('button', { name: 'Expand document' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Explore' })).toBeNull();
    expect(loadOverview).not.toHaveBeenCalled();
    expect(screen.queryByText('Loaded through the step.')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Expand document' }));
    expect(await screen.findByText('Loaded through the step.')).toBeInTheDocument();
  });

  it('auto-expands the canonical deliverable only when explicitly requested', async () => {
    const work = projection(1);
    work.final_deliverable = {
      deliverable_id: 'deliverable-1',
      format: 'markdown',
      title: 'Canonical result',
      content: '# Result',
      render_metadata: {},
      export_metadata: {},
    };
    render(TaskWorkPanel, {
      stepRuns: [run],
      canonicalDeliverableId: 'deliverable-1',
      loadWork: vi.fn().mockResolvedValue(work),
      loadDeliverableForStepRun: vi.fn().mockResolvedValue({
        deliverable_id: 'deliverable-1',
        step_run_id: 'run-1',
        version: 1,
        attempt_number: 1,
        content: '# Result\n\nExpanded dashboard result.',
        format: 'markdown',
        title: 'Canonical result',
        target: 'none',
        outputs: {},
        status: 'ready',
        evaluator_feedback: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      }),
      view: 'deliverable',
      deliverableCollapsedByDefault: false,
    });

    expect(await screen.findByText('Expanded dashboard result.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Expand document' })).not.toBeInTheDocument();
    expect(screen.getByTestId('rich-deliverable-inline-document')).toBeVisible();
  });

  it('keeps final deliverables out of the activity-only task panel', async () => {
    const work = projection(1);
    work.final_deliverable = {
      deliverable_id: 'deliverable-1',
      format: 'markdown',
      title: 'Canonical result',
      content: '# Result',
      render_metadata: {},
      export_metadata: {},
    };
    render(TaskWorkPanel, {
      stepRuns: [run],
      canonicalDeliverableId: 'deliverable-1',
      loadWork: vi.fn().mockResolvedValue(work),
      loadOverview: vi.fn().mockResolvedValue(activityOverview(1)),
      view: 'activity',
    });

    await waitFor(() => expect(screen.getByTestId('activity-summary-strip')).toBeInTheDocument());
    expect(screen.queryByTestId('task-final-result')).toBeNull();
  });

  it('shows a retryable error when the deliverable projection fails', async () => {
    const loadWork = vi.fn().mockRejectedValue(new Error('Projection unavailable'));
    render(TaskWorkPanel, {
      stepRuns: [run],
      canonicalDeliverableId: 'deliverable-1',
      loadWork,
      view: 'deliverable',
    });

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Projection unavailable'));
    expect(screen.queryByTestId('task-final-result-empty')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(loadWork).toHaveBeenCalledTimes(2);
  });

});
