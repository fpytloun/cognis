export interface VirtualWindow {
  startIndex: number;
  endIndex: number;
  paddingTop: number;
  paddingBottom: number;
}

/** Below this row count, mounting every row is cheaper than windowing. */
export const VIRTUALIZE_THRESHOLD = 300;

export interface VirtualWindowParams {
  itemCount: number;
  itemHeight: number;
  scrollTop: number;
  viewportHeight: number;
  overscan?: number;
}

/**
 * Fixed-row-height virtualization window. Pure function so it is testable
 * at 50,000 rows without mounting a real DOM.
 */
export function computeVirtualWindow(params: VirtualWindowParams): VirtualWindow {
  const { itemCount, itemHeight, scrollTop, viewportHeight, overscan = 8 } = params;
  if (itemCount <= 0 || itemHeight <= 0) {
    return { startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 };
  }
  const safeScrollTop = Math.max(0, scrollTop);
  const safeViewportHeight = Math.max(0, viewportHeight);
  const firstVisible = Math.floor(safeScrollTop / itemHeight);
  const visibleCount = Math.ceil(safeViewportHeight / itemHeight) + 1;
  const startIndex = Math.max(0, firstVisible - overscan);
  const endIndex = Math.min(itemCount, firstVisible + visibleCount + overscan);
  return {
    startIndex,
    endIndex,
    paddingTop: startIndex * itemHeight,
    paddingBottom: Math.max(0, (itemCount - endIndex) * itemHeight),
  };
}

/** Whether virtualization should apply for the given row count. */
export function shouldVirtualize(itemCount: number, threshold = VIRTUALIZE_THRESHOLD): boolean {
  return itemCount > threshold;
}

/**
 * Scroll offset (in px) that brings `index` into view within the current
 * window, or null when it is already visible and no scroll is needed.
 */
export function scrollOffsetForIndex(params: {
  index: number;
  itemHeight: number;
  scrollTop: number;
  viewportHeight: number;
}): number | null {
  const { index, itemHeight, scrollTop, viewportHeight } = params;
  const top = index * itemHeight;
  const bottom = top + itemHeight;
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewportHeight) return bottom - viewportHeight;
  return null;
}
