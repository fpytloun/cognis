import { describe, expect, it, vi } from 'vitest';

import { loadCompletePausedColumn, reconcileTaskBoard } from './task-board';
import type { TaskBoard, TaskBoardColumn, TaskBoardItem } from '$lib/types/api';

function item(taskId: string): TaskBoardItem {
  return {
    task_id: taskId,
    title: taskId,
    status: 'paused',
    priority: 1,
    agent_id: 'laforge',
    workflow_id: null,
    project_id: null,
    source_type: 'manual',
    source_ref: null,
    created_at: '2026-08-30T20:00:00Z',
    started_at: null,
    completed_at: null,
    updated_at: `2026-08-30T20:00:0${taskId.slice(-1)}Z`,
    result_summary: null,
    attention_type: null,
  };
}

function page(items: TaskBoardItem[], cursor: string | null, hasMore: boolean): TaskBoardColumn {
  return { items, groups: [], cursor, has_more: hasMore, total_count: 7 };
}

describe('loadCompletePausedColumn', () => {
  it('loads more than five paused tasks and deduplicates overlapping pages in first-seen order', async () => {
    const controller = new AbortController();
    const loader = vi.fn()
      .mockResolvedValueOnce(page([item('task-5'), item('task-6')], 'cursor-2', true))
      .mockResolvedValueOnce(page([item('task-7')], null, false));

    const result = await loadCompletePausedColumn(
      page([1, 2, 3, 4, 5].map((value) => item(`task-${value}`)), 'cursor-1', true),
      loader,
      controller.signal,
      () => true,
    );

    expect(result?.items.map((task) => task.task_id)).toEqual([
      'task-1', 'task-2', 'task-3', 'task-4', 'task-5', 'task-6', 'task-7',
    ]);
    expect(loader).toHaveBeenNthCalledWith(1, 'cursor-1', controller.signal);
    expect(loader).toHaveBeenNthCalledWith(2, 'cursor-2', controller.signal);
  });

  it('stops when the server repeats a cursor', async () => {
    const controller = new AbortController();
    const loader = vi.fn().mockResolvedValue(page([item('task-6')], 'cursor-1', true));

    const result = await loadCompletePausedColumn(
      page([item('task-1')], 'cursor-1', true),
      loader,
      controller.signal,
      () => true,
    );

    expect(result?.items.map((task) => task.task_id)).toEqual(['task-1', 'task-6']);
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('rejects an aborted or stale paginated response', async () => {
    const controller = new AbortController();
    let current = true;
    const loader = vi.fn().mockImplementation(async () => {
      current = false;
      return page([item('task-6')], null, false);
    });

    await expect(loadCompletePausedColumn(
      page([item('task-1')], 'cursor-1', true),
      loader,
      controller.signal,
      () => current,
    )).resolves.toBeNull();

    controller.abort();
    expect(await loadCompletePausedColumn(
      page([item('task-1')], 'cursor-1', true),
      loader,
      controller.signal,
      () => true,
    )).toBeNull();
  });
});

describe('reconcileTaskBoard', () => {
  it('moves an invalidated cancelled task to Recent and preserves the loaded tail', () => {
    const current: TaskBoard = {
      columns: {
        running: page([item('task-1')], null, false),
        done: page([item('task-older'), item('task-tail')], 'cursor-tail', true),
      },
    };
    const cancelled = { ...item('task-1'), status: 'cancelled', completed_at: '2026-08-30T21:00:00Z' };
    const refreshed: TaskBoard = {
      columns: {
        running: page([], null, false),
        done: page([cancelled, item('task-older')], 'cursor-head', true),
      },
    };

    const result = reconcileTaskBoard(current, refreshed, new Set(['task-1']));

    expect(result.columns.running.items).toEqual([]);
    expect(result.columns.done.items.map((task) => task.task_id)).toEqual([
      'task-1',
      'task-older',
      'task-tail',
    ]);
    expect(result.columns.done.cursor).toBe('cursor-tail');
    expect(result.columns.done.has_more).toBe(true);
  });
});
