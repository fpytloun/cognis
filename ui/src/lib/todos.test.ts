import { describe, expect, it } from 'vitest';
import { incompleteTodos, summarizeTodoProgress, visibleTodos, type TodoSnapshotItem } from './todos';

function todo(status: string): TodoSnapshotItem {
  return { content: status, status, priority: 'normal' };
}

describe('todo progress', () => {
  it('counts an in-progress todo as half done across the full visible todo list', () => {
    const progress = summarizeTodoProgress([
      todo('in_progress'),
      todo('pending'),
      todo('pending')
    ]);

    expect(progress).toMatchObject({
      total: 3,
      completed: 0,
      inProgress: 1,
      weightedDone: 0.5
    });
    expect(progress.progress).toBeCloseTo(1 / 6);
  });

  it('excludes cancelled todos from the progress denominator', () => {
    const progress = summarizeTodoProgress([
      todo('completed'),
      todo('in_progress'),
      todo('pending'),
      todo('cancelled')
    ]);

    expect(progress.total).toBe(3);
    expect(progress.progress).toBeCloseTo(0.5);
  });

  it('reuses the same visible todo filtering for UI callers', () => {
    expect(visibleTodos([todo('pending'), todo('cancelled')])).toEqual([todo('pending')]);
    expect(visibleTodos(undefined)).toEqual([]);
  });

  it('reuses the same incomplete todo filtering for active count UI callers', () => {
    expect(incompleteTodos([todo('completed'), todo('in_progress'), todo('pending'), todo('cancelled')])).toEqual([
      todo('in_progress'),
      todo('pending')
    ]);
  });
});
