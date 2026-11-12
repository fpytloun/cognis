import type { FeedbackTargetGeometry, FeedbackVisibleArea } from '../types';

/** Scroll only the source's owning ancestors; never move or blur the composer. */
export function exposeGeometry(root: HTMLElement | undefined, geometry: () => FeedbackTargetGeometry | null, visible: FeedbackVisibleArea): void {
  function delta(rect: FeedbackVisibleArea, visible: FeedbackVisibleArea): number {
    const height = Math.min(rect.height, visible.height);
    if (rect.top < visible.top) return rect.top - visible.top;
    if (rect.top + height > visible.top + visible.height) return rect.top + height - visible.top - visible.height;
    return 0;
  }
  function target(): FeedbackVisibleArea | null {
    const rects = geometry()?.rects;
    if (!rects?.length) return null;
    const left = Math.min(...rects.map(rect => rect.left)), top = Math.min(...rects.map(rect => rect.top));
    return { left, top, width: Math.max(...rects.map(rect => rect.left + rect.width)) - left,
      height: Math.max(...rects.map(rect => rect.top + rect.height)) - top };
  }
  if (!root?.isConnected || visible.height < 24) return;
  let node: HTMLElement | null = root;
  for (; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
    if (!/(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY) || node.scrollHeight <= node.clientHeight) continue;
    const bounds = node.getBoundingClientRect(), top = Math.max(visible.top, bounds.top);
    const height = Math.min(visible.top + visible.height, bounds.bottom) - top;
    const rect = target();
    if (rect && height >= 24) {
      const amount = delta(rect, { ...visible, top, height });
      if (Math.abs(amount) > 1) node.scrollBy({ top: amount, behavior: 'instant' });
    }
  }
  const rect = target();
  if (rect) {
    const amount = delta(rect, visible);
    if (Math.abs(amount) > 1) window.scrollBy({ top: amount, behavior: 'instant' });
  }
}
