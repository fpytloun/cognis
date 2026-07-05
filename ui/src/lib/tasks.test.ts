import { describe, expect, it } from 'vitest';

import {
  matchesTaskFilters,
  scheduleListProjectUrl,
  sortTasks,
  taskBoardColumnFromSearchParams,
  taskBoardProjectUrl,
  taskBoardUrlForState,
  taskFiltersFromSearchParams,
  taskFiltersToSearchParams
} from '$lib/tasks';
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
    created_by_agent_id: overrides.created_by_agent_id ?? null,
    source_type: overrides.source_type ?? 'chat',
    source_ref: overrides.source_ref ?? 'conv_1',
    delivery: overrides.delivery ?? { mode: 'same_conversation', target: null },
    completion_mode_family: overrides.completion_mode_family ?? 'default',
    allow_silent_completion: overrides.allow_silent_completion ?? false,
    interaction_mode_override: overrides.interaction_mode_override ?? null,
    session_policy: overrides.session_policy ?? null,
    workflow_id: overrides.workflow_id ?? null,
    project_id: overrides.project_id ?? null,
    attempt_number: overrides.attempt_number ?? 1,
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

describe('task filters', () => {
  it('matches tasks against agent, workflow, project, status, and search filters', () => {
    const task = makeTask({
      title: 'Refresh docs',
      description: 'Update task board guide',
      status: 'running',
      agent_id: 'agent-2',
      workflow_id: 'workflow-1',
      project_id: 'project-1'
    });

    expect(matchesTaskFilters(task, { search: 'board guide', agentId: 'agent-2', workflowId: 'workflow-1', projectId: 'project-1', status: 'running' })).toBe(true);
    expect(matchesTaskFilters(task, { search: '', agentId: 'agent-1', workflowId: '', projectId: '', status: '' })).toBe(false);
    expect(matchesTaskFilters(task, { search: '', agentId: '', workflowId: 'workflow-2', projectId: '', status: '' })).toBe(false);
    expect(matchesTaskFilters(task, { search: '', agentId: '', workflowId: '', projectId: 'project-2', status: '' })).toBe(false);
    expect(matchesTaskFilters(task, { search: '', agentId: '', workflowId: '', projectId: '', status: 'paused' })).toBe(false);
    expect(matchesTaskFilters(task, { search: 'missing', agentId: '', workflowId: '', projectId: '', status: '' })).toBe(false);
  });

  it('normalizes legacy and canonical URL params', () => {
    expect(taskFiltersFromSearchParams(new URLSearchParams('q=docs&agent=agent-1&workflow=workflow-1&project=project-1&status=running'))).toEqual({
      search: 'docs',
      agentId: 'agent-1',
      workflowId: 'workflow-1',
      projectId: 'project-1',
      status: 'running'
    });
    expect(taskFiltersFromSearchParams(new URLSearchParams('search=docs&agent_id=agent-2&workflow_id=workflow-2&project_id=project-2&status=paused'))).toEqual({
      search: 'docs',
      agentId: 'agent-2',
      workflowId: 'workflow-2',
      projectId: 'project-2',
      status: 'paused'
    });
  });

  it('serializes task filters using canonical API-style query params', () => {
    const params = taskFiltersToSearchParams({
      search: 'docs',
      agentId: 'agent-1',
      workflowId: 'workflow-1',
      projectId: 'project-1',
      status: 'running'
    });

    expect(params.toString()).toBe('q=docs&agent_id=agent-1&workflow_id=workflow-1&project_id=project-1&status=running');
    expect(taskBoardProjectUrl('project-1')).toBe('/tasks?project_id=project-1');
    expect(scheduleListProjectUrl('project-1')).toBe('/schedules?project_id=project-1');
  });

  it('normalizes the mobile task board column query param', () => {
    expect(taskBoardColumnFromSearchParams(new URLSearchParams('col=paused'))).toBe('paused');
    expect(taskBoardColumnFromSearchParams(new URLSearchParams('col=missing'))).toBe('running');
    expect(taskBoardColumnFromSearchParams(new URLSearchParams())).toBe('running');
  });

  it('serializes the mobile task board column only when it changes from the default', () => {
    const emptyFilters = { search: '', agentId: '', workflowId: '', projectId: '', status: '' };

    expect(taskBoardUrlForState(emptyFilters, 'running')).toBe('/tasks');
    expect(taskBoardUrlForState(emptyFilters, 'paused')).toBe('/tasks?col=paused');
    expect(taskBoardUrlForState({ ...emptyFilters, status: 'paused' }, 'paused')).toBe('/tasks?status=paused&col=paused');
  });
});
