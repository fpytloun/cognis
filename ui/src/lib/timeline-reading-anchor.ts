import type { TimelineWindow } from './chat-page';

interface ReadingPoint {
  ids: string[];
  top: number;
  leaf: boolean;
  visible?: boolean;
}

export interface TimelineReadingAnchor {
  /** Row-key fields also support the existing view-position cache. */
  key: string;
  top: number;
  scrollTop: number;
  points: ReadingPoint[];
}

function members(row: HTMLElement): string[] {
  try { return JSON.parse(row.dataset.timelineItemIds ?? '[]'); }
  catch { return []; }
}

/** Capture actual visible canonical content, not a mutable group's first member. */
export function captureTimelineReadingAnchor(viewport: HTMLElement): TimelineReadingAnchor | null {
  const rect = viewport.getBoundingClientRect();
  const rows = Array.from(viewport.querySelectorAll<HTMLElement>('[data-timeline-row-key]'));
  const points: ReadingPoint[] = [];
  let first: HTMLElement | undefined;
  for (const row of rows) {
    const bounds = row.getBoundingClientRect();
    if (bounds.bottom <= rect.top) continue;
    first ??= row;
    // Include one following row as a fallback, without measuring the entire DOM.
    if (bounds.top >= rect.bottom) {
      points.push({ ids: members(row), top: bounds.top - rect.top, leaf: false, visible: false });
      break;
    }
    const leaves = Array.from(row.querySelectorAll<HTMLElement>('[data-timeline-item-id]'));
    let found = false;
    for (const leaf of leaves) {
      const child = leaf.getBoundingClientRect();
      if (child.height > 0 && child.bottom > rect.top && child.top < rect.bottom) {
        points.push({ ids: [leaf.dataset.timelineItemId!], top: child.top - rect.top, leaf: true });
        found = true;
      }
    }
    if (!found) points.push({ ids: members(row), top: bounds.top - rect.top, leaf: false });
  }
  if (!first || !points.length) return null;
  return {
    key: first.dataset.timelineRowKey!,
    top: first.getBoundingClientRect().top - rect.top,
    scrollTop: viewport.scrollTop,
    points,
  };
}

/** Protect leaves and one surviving member per visible collapsed group, not whole groups. */
export function protectTimelineReadingWindow(
  target: TimelineWindow,
  items: readonly { id: string }[],
  anchor: TimelineReadingAnchor | null,
): TimelineWindow {
  if (!anchor) return target;
  const visible = anchor.points.filter(point => point.visible !== false);
  const wanted = new Set(visible.flatMap(point => point.ids));
  if (!wanted.size) return target;
  const positions = new Map<string, number>();
  for (let index = 0; index < items.length && wanted.size; index++) {
    if (wanted.delete(items[index].id)) positions.set(items[index].id, index);
  }
  const protectedIndices = visible.filter(point => point.leaf)
    .flatMap(point => point.ids.flatMap(id => positions.has(id) ? [positions.get(id)!] : []));
  function containingWindow(): TimelineWindow {
    if (!protectedIndices.length) return target;
    const first = Math.min(...protectedIndices);
    const last = Math.max(...protectedIndices) + 1;
    const span = Math.max((target.end ?? items.length) - target.start, last - first);
    const start = Math.max(0, last - span, Math.min(target.start, first));
    const end = Math.min(items.length, Math.max(last, start + span));
    return { start, end: target.end === null && end === items.length ? null : end };
  }
  for (const point of visible) {
    if (point.leaf) continue;
    const indices = point.ids.flatMap(id => {
      const index = positions.get(id);
      return index === undefined ? [] : [index];
    });
    if (!indices.length) continue;
    const preferred = containingWindow();
    const inside = indices.find(index => index >= preferred.start && index < (preferred.end ?? items.length));
    protectedIndices.push(inside ?? (indices[0] >= (preferred.end ?? items.length) ? indices[0] : indices.at(-1)!));
  }
  return containingWindow();
}

export function timelineReadingScrollTop(
  viewport: HTMLElement,
  anchor: TimelineReadingAnchor | null,
): number | null {
  if (!anchor) return null;
  const top = viewport.getBoundingClientRect().top;
  const leaves = new Map(Array.from(viewport.querySelectorAll<HTMLElement>('[data-timeline-item-id]'))
    .map(node => [node.dataset.timelineItemId!, node]));
  const rows = Array.from(viewport.querySelectorAll<HTMLElement>('[data-timeline-row-key]'));
  for (const point of anchor.points) {
    if (point.leaf) {
      const leaf = leaves.get(point.ids[0]);
      if (leaf && leaf.getBoundingClientRect().height > 0) {
        const target = viewport.scrollTop + leaf.getBoundingClientRect().top - top - point.top;
        return Math.abs(target - viewport.scrollTop) < 1 ? null : target;
      }
    } else {
      const ids = new Set(point.ids);
      const row = rows.find(row => members(row).some(id => ids.has(id)));
      if (row) {
        const target = viewport.scrollTop + row.getBoundingClientRect().top - top - point.top;
        return Math.abs(target - viewport.scrollTop) < 1 ? null : target;
      }
    }
  }
  return null;
}

export function timelinePagingDirection(current: number, previous: number, maximum: number): number {
  const clamp = (value: number) => Math.max(0, Math.min(Math.max(0, maximum), value));
  return Math.sign(clamp(current) - clamp(previous));
}
