export interface FeedbackOverlayEdges { left: number; top: number; right: number; bottom: number }
export interface FeedbackOverlayBounds extends FeedbackOverlayEdges { width: number; height: number }

/** Intersect app-owned CSS chrome/safe-area bounds with the live visual viewport. */
export function feedbackOverlayBounds(shell: FeedbackOverlayEdges, visual: FeedbackOverlayEdges): FeedbackOverlayBounds {
  const left = Math.max(shell.left, visual.left + 8), top = Math.max(shell.top, visual.top + 8);
  const right = Math.max(left, Math.min(shell.right, visual.right - 8));
  const bottom = Math.max(top, Math.min(shell.bottom, visual.bottom - 8));
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}
