import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '$lib/api/client';
import {
  isTaskRerunnable,
  loadTaskPageData,
  refreshTaskPageData,
  shouldClearTaskFromError
} from '$lib/task-detail';

const taskDetail = {
  task_id: 'task-1',
  title: 'Task',
  description: 'desc',
  expected_output: null,
  priority: 1,
  status: 'running',
  created_by: 'user@example.com',
  agent_id: 'agent-1',
  created_by_agent_id: null,
  source_type: 'scheduler',
  source_ref: 'sched-1',
  delivery: { mode: 'same_conversation', target: null },
  completion_mode_family: 'default',
  allow_silent_completion: false,
  workflow_id: 'wf-1',
  workspace_root: null,
  working_directory: null,
  workflow_state: null,
  queue_name: null,
  scheduled_for: null,
  created_at: null,
  started_at: null,
  completed_at: null,
  result_summary: null,
  result_data: null,
  applied_completion_mode: null,
  applied_completion_reason: null,
  dependencies: [],
  step_runs: [],
  pending_pause: null,
  workflow_run: null
} as const;

function buildApi(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    tasks: {
      detail: vi.fn().mockResolvedValue(taskDetail),
      summary: vi.fn().mockResolvedValue(taskDetail),
      stepSummaries: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
    },
    agents: {
      listAll: vi.fn().mockResolvedValue([{ agent_id: 'agent-1', name: 'Agent' }])
    },
    workflows: {
      listAll: vi.fn().mockResolvedValue([{ workflow_id: 'wf-1', name: 'Workflow' }]),
      detail: vi.fn().mockResolvedValue({ workflow_id: 'wf-1', name: 'Workflow' })
    },
    conversations: {
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
      detail: vi.fn().mockResolvedValue({ conversation_id: 'conv-1', agent_id: 'agent-1' })
    },
    ...overrides
  };
}

describe('task detail helpers', () => {
  it('keeps task detail when auxiliary loads fail', async () => {
    const api = buildApi({
      workflows: {
        listAll: vi.fn().mockRejectedValue(new ApiError('Workflow registry unavailable', { status: 503 })),
        detail: vi.fn().mockResolvedValue({ workflow_id: 'wf-1', name: 'Workflow' })
      }
    });

    const data = await loadTaskPageData(api as never, 'task-1');

    expect(data.task.task_id).toBe('task-1');
    expect(data.workflows).toEqual([{ workflow_id: 'wf-1', name: 'Workflow' }]);
    expect(data.auxiliaryError).toBe('Workflow registry unavailable');
  });

  it('fails the whole load when task detail is missing', async () => {
    const api = buildApi({
      tasks: {
        detail: vi.fn().mockResolvedValue(taskDetail),
        summary: vi.fn().mockRejectedValue(new ApiError('Task not found', { status: 404 })),
        stepSummaries: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
        list: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
      }
    });

    await expect(loadTaskPageData(api as never, 'task-1')).rejects.toMatchObject({
      message: 'Task not found',
      status: 404
    });
  });

  it('preserves the currently rendered task list during refresh', async () => {
    const api = buildApi({
      tasks: {
        detail: vi.fn().mockResolvedValue(taskDetail),
        summary: vi.fn().mockResolvedValue(taskDetail),
        stepSummaries: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
        list: vi.fn().mockResolvedValue({ items: [], next_cursor: null, has_more: false }),
      }
    });

    const data = await refreshTaskPageData(api as never, 'task-1', [
      { task_id: 'existing-task', title: 'Existing' } as never
    ]);

    expect(data.task.task_id).toBe('task-1');
    expect(data.allTasks).toEqual([{ task_id: 'existing-task', title: 'Existing' }]);
    expect(data.auxiliaryError).toBe('');
  });

  it('only clears the task view for actual not-found detail errors', () => {
    expect(shouldClearTaskFromError(new ApiError('Task not found', { status: 404 }))).toBe(true);
    expect(shouldClearTaskFromError(new ApiError('Temporary failure', { status: 503 }))).toBe(false);
  });

  it('only exposes rerun for paused and terminal tasks', () => {
    expect(isTaskRerunnable({ status: 'paused' })).toBe(true);
    expect(isTaskRerunnable({ status: 'completed' })).toBe(true);
    expect(isTaskRerunnable({ status: 'failed' })).toBe(true);
    expect(isTaskRerunnable({ status: 'cancelled' })).toBe(true);
    expect(isTaskRerunnable({ status: 'running' })).toBe(false);
    expect(isTaskRerunnable({ status: 'queued' })).toBe(false);
    expect(isTaskRerunnable({ status: 'draft' })).toBe(false);
    expect(isTaskRerunnable(null)).toBe(false);
  });
});
