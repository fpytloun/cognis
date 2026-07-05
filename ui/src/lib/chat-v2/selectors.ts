import type { ChatV2ClientState } from './sync-engine';
import type { TimelineItem } from './types';
import { visibleTimelineItems } from './sync-engine';
import { toRenderItems } from './render-adapter';
import type { TimelineItem as RenderTimelineItem } from '$lib/chat';

export function selectVisibleTimeline(state: ChatV2ClientState): TimelineItem[] {
  return visibleTimelineItems(state);
}

/**
 * The visible timeline converted to the legacy leaf render shape. Read-only
 * consumers (search, todos, retry-tail detection, pending step-input lookup)
 * reuse the existing $lib/chat helpers against this projection. This is a
 * pure derivation from canonical state, not a mutable bridge store.
 */
export function selectRenderItems(items: TimelineItem[]): RenderTimelineItem[] {
  return toRenderItems(items);
}

export function selectHasActiveTurn(state: ChatV2ClientState): boolean {
  return state.runtime?.has_active_turn === true;
}

export function selectActiveTurnId(state: ChatV2ClientState): string | null {
  return state.runtime?.active_turn?.turn_id ?? null;
}

export function selectNeedsRecovery(state: ChatV2ClientState): boolean {
  return state.syncStatus === 'gapped';
}

export function selectQueuedCount(state: ChatV2ClientState): number {
  return state.queue?.queued_count ?? 0;
}
