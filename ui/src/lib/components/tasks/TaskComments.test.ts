import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TaskDetail } from '$lib/types/api';

const { addComment } = vi.hoisted(() => ({ addComment: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api: {
    tasks: {
      comments: vi.fn().mockResolvedValue([]),
      addComment
    }
  },
  asApiError: (error: unknown) => ({ message: String(error) })
}));

import TaskComments from './TaskComments.svelte';

afterEach(() => {
  cleanup();
  addComment.mockReset();
});

function task(status: string): TaskDetail {
  return {
    task_id: 'task-39',
    status,
    workflow_id: 'workflow-39',
    pending_pause: status === 'paused' ? {
      pause_type: 'step_input',
      task_id: 'task-39',
      step_name: 'verify',
      step_run_id: 'run-39',
      session_id: 'session-39',
      question: 'Approve?',
      questions: [],
      options: null,
      context: null
    } : null
  } as TaskDetail;
}

describe('TaskComments collaboration intents', () => {
  it('explains next-cycle guidance and terminal revision semantics', async () => {
    render(TaskComments, {
      task: task('completed'),
      stepOptions: [{ name: 'verify', label: 'Verify result' }],
      initialTargetStep: 'verify'
    });

    await fireEvent.click(screen.getByRole('radio', { name: 'Guide next agent cycle' }));
    expect(screen.getByText(/Sent to the agent at the next model boundary/)).toBeTruthy();

    await fireEvent.click(screen.getByRole('radio', { name: 'Revise result' }));
    expect(screen.getByText(/reopens it from the selected step/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Revise result' })).toBeTruthy();
  });

  it('exposes answer pause only when the existing pause contract applies', async () => {
    render(TaskComments, { task: task('paused'), stepOptions: [{ name: 'verify', label: 'Verify result' }] });
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Answer pause' })).toBeEnabled());
  });
});
