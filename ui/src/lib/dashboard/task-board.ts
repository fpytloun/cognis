import type { TaskBoard, TaskBoardColumn, TaskBoardItem } from '$lib/types/api';

const MAX_PAUSED_COLUMN_PAGES = 1_000;

export type TaskBoardColumnPageLoader = (
  cursor: string,
  signal: AbortSignal,
) => Promise<TaskBoardColumn>;

export async function loadCompletePausedColumn(
  initial: TaskBoardColumn,
  loadPage: TaskBoardColumnPageLoader,
  signal: AbortSignal,
  isCurrent: () => boolean,
): Promise<TaskBoardColumn | null> {
  const itemsById = new Map<string, TaskBoardItem>();
  for (const item of initial.items) itemsById.set(item.task_id, item);

  let page = initial;
  const seenCursors = new Set<string>();
  for (let pageCount = 1; page.has_more && pageCount < MAX_PAUSED_COLUMN_PAGES; pageCount += 1) {
    if (signal.aborted || !isCurrent()) return null;
    const cursor = page.cursor;
    if (!cursor || seenCursors.has(cursor)) break;
    seenCursors.add(cursor);

    page = await loadPage(cursor, signal);
    if (signal.aborted || !isCurrent()) return null;
    for (const item of page.items) itemsById.set(item.task_id, item);
  }

  return {
    ...initial,
    items: [...itemsById.values()],
    cursor: page.cursor,
    has_more: page.has_more,
    total_count: Math.max(initial.total_count, itemsById.size),
  };
}

export function reconcileTaskBoard(
  current: TaskBoard,
  refreshed: TaskBoard,
  changedTaskIds: Set<string>,
): TaskBoard {
  const currentDone = current.columns.done;
  const refreshedDone = refreshed.columns.done;
  if (!currentDone || !refreshedDone) return refreshed;

  const canonicalIds = new Set(
    Object.values(refreshed.columns).flatMap((column) => column.items.map((item) => item.task_id)),
  );
  const retainedItems = currentDone.items.filter((item) => (
    !canonicalIds.has(item.task_id) && !changedTaskIds.has(item.task_id)
  ));
  const refreshedGroupKeys = new Set(refreshedDone.groups.map((group) => group.key));
  const retainedGroups = currentDone.groups.filter((group) => (
    !refreshedGroupKeys.has(group.key)
    && !canonicalIds.has(group.latest.task_id)
    && !changedTaskIds.has(group.latest.task_id)
  ));

  return {
    ...refreshed,
    columns: {
      ...refreshed.columns,
      done: {
        ...refreshedDone,
        items: [...refreshedDone.items, ...retainedItems],
        groups: [...refreshedDone.groups, ...retainedGroups],
        cursor: currentDone.cursor,
        has_more: currentDone.has_more,
        total_count: Math.max(refreshedDone.total_count, currentDone.total_count),
      },
    },
  };
}
