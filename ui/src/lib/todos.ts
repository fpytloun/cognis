export interface TodoSnapshotItem {
  content: string;
  status: string;
  priority: string;
}

export interface TodoProgressSummary {
  total: number;
  completed: number;
  inProgress: number;
  weightedDone: number;
  progress: number;
}

export function parseTodoSnapshot(value: unknown): TodoSnapshotItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const content = typeof record.content === 'string' ? record.content.trim() : '';
      if (!content) return null;
      return {
        content,
        status: typeof record.status === 'string' ? record.status : 'pending',
        priority: typeof record.priority === 'string' ? record.priority : 'medium'
      } satisfies TodoSnapshotItem;
    })
    .filter((item): item is TodoSnapshotItem => item !== null);
}

export function visibleTodos(items: TodoSnapshotItem[] | undefined): TodoSnapshotItem[] {
  return (items ?? []).filter((todo) => todo.status !== 'cancelled');
}

export function incompleteTodos(items: TodoSnapshotItem[] | undefined): TodoSnapshotItem[] {
  return visibleTodos(items).filter((todo) => todo.status !== 'completed');
}

export function summarizeTodoProgress(items: TodoSnapshotItem[] | undefined): TodoProgressSummary {
  const activeTodos = visibleTodos(items);
  const total = activeTodos.length;
  const completed = activeTodos.filter((todo) => todo.status === 'completed').length;
  const inProgress = activeTodos.filter((todo) => todo.status === 'in_progress').length;
  const weightedDone = completed + inProgress * 0.5;

  return {
    total,
    completed,
    inProgress,
    weightedDone,
    progress: total > 0 ? Math.max(0, Math.min(weightedDone / total, 1)) : 0
  };
}
