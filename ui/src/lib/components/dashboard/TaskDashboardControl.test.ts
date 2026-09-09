import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { PendingPause, TaskDetail } from '$lib/types/api';
import TaskDashboardControl from './TaskDashboardControl.svelte';

const { pause, resume, cancel, rerun, gateResponse, stepResponse, confirmAction } = vi.hoisted(() => ({
  pause: vi.fn(),
  resume: vi.fn(),
  cancel: vi.fn(),
  rerun: vi.fn(),
  gateResponse: vi.fn(),
  stepResponse: vi.fn(),
  confirmAction: vi.fn()
}));

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/stores/confirm', () => ({ confirmAction }));
vi.mock('$lib/api/client', () => ({
  api: { tasks: { pause, resume, cancel, rerun, gateResponse, stepResponse } },
  asApiError: (error: unknown) => error instanceof Error
    ? error
    : error as { message: string; status: number }
}));
vi.mock('$lib/components/tasks/TaskComments.svelte', async () => (
  import('./TaskComments.test-fixture.svelte')
));

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  confirmAction.mockResolvedValue(true);
  pause.mockResolvedValue({ ok: true, task_id: 'task-1', status: 'paused' });
  resume.mockResolvedValue({ ok: true, task_id: 'task-1', status: 'ready' });
  cancel.mockResolvedValue({ ok: true, task_id: 'task-1', status: 'cancelled' });
  rerun.mockResolvedValue({ ok: true, source_task_id: 'task-1', task_id: 'task-1', status: 'queued', created_new: false });
  gateResponse.mockResolvedValue({ ok: true, task_id: 'task-1', status: 'ready' });
  stepResponse.mockResolvedValue({ ok: true, task_id: 'task-1', status: 'ready' });
});

function task(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    task_id: 'task-1',
    title: 'Controlled task',
    description: 'Task description.',
    expected_output: null,
    status: 'running',
    priority: 0,
    created_by: 'owner@example.com',
    agent_id: 'riker',
    created_by_agent_id: null,
    source_type: 'manual',
    source_ref: null,
    delivery: { mode: 'same_conversation', target: null },
    completion_mode_family: 'default',
    allow_silent_completion: false,
    interaction_mode_override: null,
    session_policy: null,
    workflow_id: 'workflow-1',
    project_id: null,
    attempt_number: 1,
    workspace_root: null,
    working_directory: null,
    workflow_state: null,
    queue_name: 'default',
    scheduled_for: null,
    created_at: null,
    started_at: null,
    completed_at: null,
    updated_at: null,
    result_summary: null,
    result_data: null,
    applied_completion_mode: null,
    applied_completion_reason: null,
    dependencies: [],
    step_runs: [],
    workflow_run: {
      task_id: 'task-1',
      workflow_id: 'workflow-1',
      project_id: null,
      attempt_number: 1,
      workflow_state: null,
      current_step_name: 'implement',
      pending_pause: null
    },
    pending_pause: null,
    workflow_projection: null,
    progress: null,
    ...overrides
  } as TaskDetail;
}

function gatePause(): PendingPause {
  return {
    pause_id: 'pause-1',
    task_id: 'task-1',
    step_run_id: 'run-1',
    session_id: 'session-1',
    pause_type: 'gate',
    step_name: 'review',
    question: 'Approve the result?',
    questions: [],
    options: [{ action: 'continue', label: 'Approve' }],
    context: {}
  };
}

function refreshResult(result: 'refreshed' | 'superseded' | 'not-found' | 'failed' = 'refreshed') {
  return vi.fn().mockResolvedValue(result);
}

function apiFailure(status: number, message: string): { status: number; message: string } {
  return { status, message };
}

describe('TaskDashboardControl', () => {
  it('resumes an unstructured paused task through the canonical API', async () => {
    const onRefresh = refreshResult();
    render(TaskDashboardControl, {
      task: task({ status: 'paused' }),
      canMutate: true,
      onRefresh
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Resume task' }));

    await waitFor(() => expect(resume).toHaveBeenCalledWith('task-1'));
    expect(onRefresh).toHaveBeenCalled();
    expect(await screen.findByRole('status')).toHaveTextContent('Task resumed.');
  });

  it('shows the canonical resolver instead of blind resume for a gate pause', async () => {
    const onRefresh = refreshResult();
    render(TaskDashboardControl, {
      task: task({
        status: 'paused',
        pending_pause: gatePause()
      }),
      canMutate: true,
      onRefresh
    });

    expect(screen.queryByRole('button', { name: 'Resume task' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(gateResponse).toHaveBeenCalledWith('task-1', {
      step_name: 'review',
      action: 'continue',
      feedback: null
    }));
    expect(onRefresh).toHaveBeenCalled();
  });

  it('confirms and cancels a running task', async () => {
    const onMutationSettled = vi.fn();
    render(TaskDashboardControl, {
      task: task(),
      canMutate: true,
      onRefresh: refreshResult(),
      onMutationSettled,
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));

    await waitFor(() => expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({
      title: 'Cancel task?',
      variant: 'danger'
    })));
    expect(cancel).toHaveBeenCalledWith('task-1');
    await waitFor(() => expect(onMutationSettled).toHaveBeenCalledWith('task-1', true));
  });

  it('pauses a running task through the canonical API', async () => {
    render(TaskDashboardControl, {
      task: task(),
      canMutate: true,
      onRefresh: refreshResult()
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Pause task' }));

    await waitFor(() => expect(pause).toHaveBeenCalledWith('task-1'));
  });

  it('submits a pending structured step question', async () => {
    render(TaskDashboardControl, {
      task: task({
        status: 'paused',
        pending_pause: {
          ...gatePause(),
          pause_type: 'step_question',
          question: 'Choose a target.',
          options: [],
          questions: [{
            id: 'target',
            header: null,
            question: 'Which target?',
            options: [{ id: 'safe', label: 'Safe target', description: null }],
            required: true,
            multiple: false,
            allow_custom: false
          }]
        }
      }),
      canMutate: true,
      onRefresh: refreshResult()
    });

    await fireEvent.click(screen.getByText('Safe target'));
    await fireEvent.click(screen.getByRole('button', { name: 'Send response' }));

    await waitFor(() => expect(stepResponse).toHaveBeenCalledWith('task-1', {
      mode: 'structured',
      answers: [{
        question_id: 'target',
        selected_option_ids: ['safe'],
        custom_answer: null
      }]
    }));
  });

  it('re-runs a failed task through the canonical idempotent API', async () => {
    render(TaskDashboardControl, {
      task: task({ status: 'failed' }),
      canMutate: true,
      onRefresh: refreshResult()
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Re-run task' }));

    await waitFor(() => expect(rerun).toHaveBeenCalledWith('task-1'));
  });

  it('offers the canonical re-run action for completed tasks', async () => {
    const onMutationSettled = vi.fn();
    rerun.mockResolvedValueOnce({
      ok: true,
      source_task_id: 'task-1',
      task_id: 'task-rerun',
      status: 'queued',
      created_new: true
    });
    render(TaskDashboardControl, {
      task: task({ status: 'completed' }),
      canMutate: true,
      onRefresh: refreshResult(),
      onMutationSettled
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Re-run task' }));

    await waitFor(() => expect(rerun).toHaveBeenCalledWith('task-1'));
    expect(onMutationSettled).toHaveBeenCalledWith('task-1', false);
  });

  it('reports a successful mutation with stale details when refresh fails', async () => {
    render(TaskDashboardControl, {
      task: task({ status: 'paused' }),
      canMutate: true,
      onRefresh: refreshResult('failed')
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Resume task' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Task resumed. Current task details could not be refreshed.'
    );
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('refreshes canonical details after a stale cancel conflict and preserves the action error', async () => {
    const onRefresh = refreshResult();
    const onMutationSettled = vi.fn();
    cancel.mockRejectedValueOnce(apiFailure(409, 'Task is already cancelled'));
    render(TaskDashboardControl, {
      task: task(),
      canMutate: true,
      onRefresh,
      onMutationSettled
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Task is already cancelled');
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onMutationSettled).toHaveBeenCalledWith('task-1', false);
  });

  it('refreshes canonical details after a stale resume conflict', async () => {
    const onRefresh = refreshResult();
    resume.mockRejectedValueOnce(apiFailure(409, 'Task is no longer paused'));
    render(TaskDashboardControl, {
      task: task({ status: 'paused' }),
      canMutate: true,
      onRefresh
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Resume task' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Task is no longer paused');
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('renders an unavailable state when lifecycle recovery confirms that the task is gone', async () => {
    cancel.mockRejectedValueOnce(apiFailure(404, 'Task not found'));
    render(TaskDashboardControl, {
      task: task(),
      canMutate: true,
      onRefresh: refreshResult('not-found')
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));

    expect(await screen.findByTestId('dashboard-task-control-unavailable')).toHaveTextContent('Task unavailable');
    expect(screen.getByRole('alert')).toHaveTextContent('Task not found');
    expect(screen.queryByRole('button', { name: 'Cancel task' })).not.toBeInTheDocument();
  });

  it('preserves the lifecycle action error when canonical recovery refresh fails', async () => {
    const onRefresh = refreshResult('failed');
    resume.mockRejectedValueOnce(apiFailure(409, 'Resume conflicts with current state'));
    render(TaskDashboardControl, {
      task: task({ status: 'paused' }),
      canMutate: true,
      onRefresh
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Resume task' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Resume conflicts with current state');
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('does not duplicate recovery refresh when realtime delivers a newer task revision first', async () => {
    let rejectResume!: (reason: unknown) => void;
    resume.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectResume = reject; }));
    const onRefresh = refreshResult();
    const view = render(TaskDashboardControl, {
      task: task({ status: 'paused', updated_at: '2026-09-01T10:00:00Z' }),
      canMutate: true,
      onRefresh
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Resume task' }));
    await view.rerender({
      task: task({ status: 'running', updated_at: '2026-09-01T10:00:01Z' }),
      canMutate: true,
      onRefresh
    });
    rejectResume(apiFailure(409, 'Resume already applied'));

    expect(await screen.findByRole('alert')).toHaveTextContent('Resume already applied');
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Pause task' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume task' })).not.toBeInTheDocument();
  });

  it('shows viewer denial and does not render mutation controls', () => {
    render(TaskDashboardControl, {
      task: task(),
      canMutate: false,
      onRefresh: refreshResult()
    });

    expect(screen.getByTestId('dashboard-task-control-readonly')).toHaveTextContent('View-only access');
    expect(screen.queryByRole('button', { name: 'Cancel task' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-comments-fixture')).not.toBeInTheDocument();
  });

  it('refreshes after a stale pause conflict so the resolved form can close', async () => {
    const onRefresh = refreshResult();
    gateResponse.mockRejectedValueOnce(new Error('Pause has already been resolved'));
    render(TaskDashboardControl, {
      task: task({
        status: 'paused',
        pending_pause: gatePause()
      }),
      canMutate: true,
      onRefresh
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Pause has already been resolved');
    expect(onRefresh).toHaveBeenCalled();
  });
});
