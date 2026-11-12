import type { FeedbackVisibleArea } from './types';
import type { FeedbackSourceLayout } from './presenter-session';

/** Column edges belong to the source; vertical limits belong to stable scroll viewports. */
export function sourceBounds(node: HTMLElement): FeedbackVisibleArea | null {
  if (!node.isConnected) return null;
  const box = node.getBoundingClientRect();
  if (!box.width) return null;
  let left = box.left, right = box.right, top = 0, bottom = window.innerHeight;
  for (let owner = node.parentElement; owner && owner !== document.body; owner = owner.parentElement) {
    const style = getComputedStyle(owner), rect = owner.getBoundingClientRect();
    if (/(auto|scroll|hidden|clip|overlay)/.test(style.overflowX)) {
      left = Math.max(left, rect.left + owner.clientLeft);
      right = Math.min(right, rect.left + owner.clientLeft + owner.clientWidth);
    }
    if (/(auto|scroll|hidden|clip|overlay)/.test(style.overflowY)) {
      top = Math.max(top, rect.top + owner.clientTop);
      bottom = Math.min(bottom, rect.top + owner.clientTop + owner.clientHeight);
    }
  }
  return { left, top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
}

/** The workspace owns both emitted geometry and its source-height accommodation. */
export function observeSourceWorkspace(
  node: HTMLElement,
  report: ((bounds: FeedbackVisibleArea | null) => void) | undefined,
  layout: FeedbackSourceLayout | null | undefined,
  onHeight: (height: number | null) => void,
): () => void {
  let stopped = false, frame = 0;
  const measure = () => {
    if (stopped) return;
    report?.(sourceBounds(node));
    onHeight(layout ? Math.max(24, Math.min(layout.viewportHeight, layout.visibleBottom - node.getBoundingClientRect().top)) : null);
  };
  // Publishing bounds can mount feedback chrome and its observers. Do not do
  // that inside ResizeObserver's delivery cycle for the source viewport.
  const schedule = () => {
    if (!stopped && !frame) frame = requestAnimationFrame(() => { frame = 0; measure(); });
  };
  measure();
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  observer?.observe(node);
  window.addEventListener('resize', measure);
  window.addEventListener('scroll', measure, true);
  return () => { stopped = true; cancelAnimationFrame(frame); observer?.disconnect(); window.removeEventListener('resize', measure); window.removeEventListener('scroll', measure, true); };
}
