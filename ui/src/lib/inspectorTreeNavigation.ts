import {
  conversationTimelineScope,
  sessionTimelineScope,
  type ActivityOverviewResponse,
  type TimelineScope,
  type WorkstreamRef,
} from '$lib/chat-v2/types';

export function rootOverviewForConversation(
  overview: ActivityOverviewResponse | null,
  conversationId: string | null | undefined,
): ActivityOverviewResponse | null {
  return overview?.scope.kind === 'conversation' && overview.scope.conversation_id === conversationId
    ? overview
    : null;
}

/**
 * Session selection is a subtree scope. Work resolves the selected session
 * and every authorized descendant, not an exact-session-only filter.
 */
export function selectedWorkSubtreeScope(
  conversationId: string,
  sessionId: string | null,
): TimelineScope {
  return sessionId
    ? sessionTimelineScope(sessionId, conversationId)
    : conversationTimelineScope(conversationId);
}

export function workstreamForSession(
  nodes: WorkstreamRef[],
  sessionId: string | null,
): WorkstreamRef | null {
  if (!sessionId) return null;
  return nodes.find((node) => node.session_id === sessionId)
    ?? nodes.find((node) => node.backing_session_ids?.includes(sessionId))
    ?? null;
}

export function canonicalWorkstreamSessionId(
  nodes: WorkstreamRef[],
  sessionId: string | null,
): string | null {
  return workstreamForSession(nodes, sessionId)?.session_id ?? sessionId;
}

export function treeSessionNavigation(presentation: 'closed' | 'pinned' | 'overlay' | 'focus'): {
  openSubSessionViewer: true;
  closeInspectorOverlay: boolean;
} {
  return {
    openSubSessionViewer: true,
    closeInspectorOverlay: false,
  };
}

export function structuralParentSessionId(
  nodes: WorkstreamRef[],
  sessionId: string | null,
): string | null {
  if (!sessionId) return null;
  const current = workstreamForSession(nodes, sessionId);
  if (!current?.parent_key) return null;
  const parent = nodes.find((node) => node.key === current.parent_key);
  return parent?.parent_key ? parent.session_id : null;
}

export interface InspectorTraversalState {
  drawerOpen: boolean;
  activeTab: 'overview' | 'work' | 'session';
  presentation: 'closed' | 'pinned' | 'overlay' | 'focus';
  focusedSessionId: string | null;
  middleSessionId: string | null;
  workSessionId: string | null;
}

export function traverseInspectorSession(
  state: InspectorTraversalState,
  sessionId: string | null,
): InspectorTraversalState {
  return {
    ...state,
    drawerOpen: state.drawerOpen,
    activeTab: state.activeTab,
    presentation: state.presentation,
    focusedSessionId: sessionId,
    middleSessionId: sessionId,
    workSessionId: sessionId,
  };
}
