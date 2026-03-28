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
  return [...tasks].sort((left, right) => {
    if (left.priority !== right.priority) {
      return right.priority - left.priority;
    }
    return (right.completed_at ?? right.started_at ?? right.created_at ?? '').localeCompare(
      left.completed_at ?? left.started_at ?? left.created_at ?? ''
    );
  });
}

export interface TaskFilterState {
  search: string;
  agentId: string;
  workflowId: string;
  status: string;
}

export function matchesTaskFilters(task: Task, filters: TaskFilterState): boolean {
  if (filters.agentId && task.agent_id !== filters.agentId) {
    return false;
  }

  if (filters.workflowId && task.workflow_id !== filters.workflowId) {
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
