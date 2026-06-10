import type { Task } from '$lib/types/api';

export const TASK_BOARD_COLUMNS = [
  { id: 'draft', label: 'Draft' },
  { id: 'queued', label: 'Queued' },
  { id: 'running', label: 'Running' },
  { id: 'paused', label: 'Paused' },
  { id: 'done', label: 'Done' }
] as const;

export type TaskBoardColumnId = (typeof TASK_BOARD_COLUMNS)[number]['id'];

export function boardColumnForStatus(status: string): TaskBoardColumnId {
  if (status === 'draft') {
    return 'draft';
  }
  if (status === 'queued' || status === 'ready') {
    return 'queued';
  }
  if (status === 'running') {
    return 'running';
  }
  if (status === 'paused') {
    return 'paused';
  }
  return 'done';
}

export function sortTasks(tasks: Task[]): Task[] {
  const activityValue = (task: Task): string =>
    task.updated_at ?? task.completed_at ?? task.started_at ?? task.created_at ?? '';
  const isDone = (task: Task): boolean => ['completed', 'failed', 'cancelled'].includes(task.status);

  return [...tasks].sort((left, right) => {
    if (isDone(left) !== isDone(right)) {
      return isDone(left) ? 1 : -1;
    }
    if (!isDone(left) && left.priority !== right.priority) {
      return right.priority - left.priority;
    }
    const activityDelta = activityValue(right).localeCompare(activityValue(left));
    if (activityDelta !== 0) {
      return activityDelta;
    }
    if (left.priority !== right.priority) {
      return right.priority - left.priority;
    }
    return right.task_id.localeCompare(left.task_id);
  });
}

export interface TaskFilterState {
  search: string;
  agentId: string;
  workflowId: string;
  projectId: string;
  status: string;
}

export function taskFiltersFromSearchParams(searchParams: URLSearchParams): TaskFilterState {
  return {
    search: searchParams.get('q') ?? searchParams.get('search') ?? '',
    agentId: searchParams.get('agent_id') ?? searchParams.get('agent') ?? '',
    workflowId: searchParams.get('workflow_id') ?? searchParams.get('workflow') ?? '',
    projectId: searchParams.get('project_id') ?? searchParams.get('project') ?? '',
    status: searchParams.get('status') ?? ''
  };
}

export function taskFiltersToSearchParams(filters: TaskFilterState): URLSearchParams {
  const searchParams = new URLSearchParams();
  if (filters.search) searchParams.set('q', filters.search);
  if (filters.agentId) searchParams.set('agent_id', filters.agentId);
  if (filters.workflowId) searchParams.set('workflow_id', filters.workflowId);
  if (filters.projectId) searchParams.set('project_id', filters.projectId);
  if (filters.status) searchParams.set('status', filters.status);
  return searchParams;
}

export function taskBoardProjectUrl(projectId: string): string {
  const searchParams = taskFiltersToSearchParams({
    search: '',
    agentId: '',
    workflowId: '',
    projectId,
    status: ''
  });
  return `/tasks?${searchParams.toString()}`;
}

export function scheduleListProjectUrl(projectId: string): string {
  const searchParams = new URLSearchParams({ project_id: projectId });
  return `/schedules?${searchParams.toString()}`;
}

export function matchesTaskFilters(task: Task, filters: TaskFilterState): boolean {
  if (filters.agentId && task.agent_id !== filters.agentId) {
    return false;
  }

  if (filters.workflowId && task.workflow_id !== filters.workflowId) {
    return false;
  }

  if (filters.projectId && task.project_id !== filters.projectId) {
    return false;
  }

  if (filters.status && task.status !== filters.status) {
    return false;
  }

  if (filters.search) {
    const search = filters.search.toLowerCase();
    return `${task.title} ${task.description}`.toLowerCase().includes(search);
  }

  return true;
}
