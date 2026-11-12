import type { ChatV2ClientState } from './sync-engine';
import type { ActivityOverviewResponse, TimelineItem } from './types';

function unconfirmedCompaction(item: TimelineItem): boolean {
  return item.kind === 'compaction' && item.stable === false && item.source_refs.length === 0;
}

export function unconfirmedCompactionIds(state: ChatV2ClientState): string[] {
  return [...new Set([...state.localItems, ...(state.runtime?.volatile_items ?? [])]
    .filter(unconfirmedCompaction).map(item => item.id))].sort();
}

/** Positive child ownership can retire leaked overlays; absence from a bounded
 * graph cannot prove anything. Canonical history and unknown items stay intact.
 */
export function reconcileCompactionScope(
  state: ChatV2ClientState,
  overview: ActivityOverviewResponse,
): ChatV2ClientState {
  if (state.scope?.kind !== 'conversation' || overview.scope.kind !== 'conversation'
    || overview.scope.key !== state.scopeKey
    || overview.scope.conversation_id !== state.conversationId) return state;
  const childSessions = new Set(overview.workstreams
    .filter(node => node.kind === 'delegate' && node.parent_key
      && node.conversation_id === state.conversationId)
    .flatMap(node => [node.session_id, node.event_store_session_id, ...(node.backing_session_ids ?? [])]));
  if (!childSessions.size) return state;
  const canonical = new Set(state.timelineItems.map(item => item.id));
  const keep = (item: TimelineItem): boolean => !(
    unconfirmedCompaction(item) && !canonical.has(item.id) && item.kind === 'compaction'
    && ((item.session_id && childSessions.has(item.session_id))
      || (item.previous_session_id && childSessions.has(item.previous_session_id)))
  );
  const localItems = state.localItems.filter(keep);
  const runtimeItems = state.runtime?.volatile_items.filter(keep);
  if (localItems.length === state.localItems.length
    && runtimeItems?.length === state.runtime?.volatile_items.length) return state;
  return {
    ...state,
    localItems,
    runtime: state.runtime && runtimeItems ? { ...state.runtime, volatile_items: runtimeItems } : state.runtime,
  };
}
