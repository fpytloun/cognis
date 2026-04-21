import { describe, expect, it } from 'vitest';

import { sortTasks } from '$lib/tasks';
import type { Task } from '$lib/types/api';

function makeTask(overrides: Partial<Task>): Task {
  return {
    task_id: overrides.task_id ?? 'task_1',
    title: overrides.title ?? 'Task',
    description: overrides.description ?? '',
    expected_output: overrides.expected_output ?? null,
    status: overrides.status ?? 'completed',
    priority: overrides.priority ?? 0,
    created_by: overrides.created_by ?? 'user@example.com',
    agent_id: overrides.agent_id ?? 'agent-1',
    source_type: overrides.source_type ?? 'chat',
    source_ref: overrides.source_ref ?? 'conv_1',
    delivery: overrides.delivery ?? { mode: 'same_conversation', target: null },
    completion_mode_family: overrides.completion_mode_family ?? 'default',
    allow_silent_completion: overrides.allow_silent_completion ?? false,
    workflow_id: overrides.workflow_id ?? null,
    workspace_root: overrides.workspace_root ?? null,
    working_directory: overrides.working_directory ?? null,
    workflow_state: overrides.workflow_state ?? null,
    queue_name: overrides.queue_name ?? 'default',
    scheduled_for: overrides.scheduled_for ?? null,
    created_at: overrides.created_at ?? '2026-04-20T10:00:00Z',
    started_at: overrides.started_at ?? null,
    completed_at: overrides.completed_at ?? null,
    updated_at: overrides.updated_at ?? null,
    result_summary: overrides.result_summary ?? null,
    result_data: overrides.result_data ?? null,
    applied_completion_mode: overrides.applied_completion_mode ?? null,
    applied_completion_reason: overrides.applied_completion_reason ?? null,
  };
}

describe('sortTasks', () => {
  it('orders tasks by last activity before priority', () => {
    const tasks = sortTasks([
      makeTask({ task_id: 'task_old_high', priority: 10, updated_at: '2026-04-20T10:00:00Z' }),
      makeTask({ task_id: 'task_new_low', priority: 1, updated_at: '2026-04-21T12:00:00Z' }),
      makeTask({ task_id: 'task_mid', priority: 5, updated_at: '2026-04-21T09:00:00Z' }),
    ]);

    expect(tasks.map((task) => task.task_id)).toEqual(['task_new_low', 'task_mid', 'task_old_high']);
  });

  it('preserves priority-first ordering for active board columns', () => {
    const tasks = sortTasks([
      makeTask({ task_id: 'task_old_high', status: 'running', priority: 10, updated_at: '2026-04-20T10:00:00Z' }),
      makeTask({ task_id: 'task_new_low', status: 'running', priority: 1, updated_at: '2026-04-21T12:00:00Z' }),
    ]);

    expect(tasks.map((task) => task.task_id)).toEqual(['task_old_high', 'task_new_low']);
  });
});
