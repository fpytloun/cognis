import { acquireTimeline } from './timeline-controller.svelte';
import type { TimelineScope } from './types';

/** Explicit, read-only recovery of this device's derived history. */
export async function forceRefreshTimeline(scope: TimelineScope, signal: AbortSignal): Promise<void> {
  const { controller, release } = acquireTimeline(scope);
  try {
    const snapshot = await controller.load({ discardCachedHistory: true, signal });
    signal.throwIfAborted();
    if (!snapshot) throw new Error('History changed during refresh. Try again.');
  } finally {
    release();
  }
}
