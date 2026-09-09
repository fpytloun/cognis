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

export function workInvalidationTouchesTree(
  invalidatedScopeKey: string | null | undefined,
  inspectorScopeKey: string,
  rootScopeKey: string | null,
  nodes: WorkstreamRef[],
): boolean {
  if (!invalidatedScopeKey) return true;
  if (invalidatedScopeKey === inspectorScopeKey || invalidatedScopeKey === rootScopeKey) {
    return true;
  }
  const separator = invalidatedScopeKey.indexOf(':');
  if (separator < 1) return false;
  const kind = invalidatedScopeKey.slice(0, separator);
  const identifier = invalidatedScopeKey.slice(separator + 1);
  if (!identifier) return false;
  if (kind === 'conversation') {
    return nodes.some((node) => node.conversation_id === identifier);
  }
  if (kind === 'session') {
    return nodes.some((node) => (
      node.session_id === identifier || node.backing_session_ids?.includes(identifier)
    ));
  }
  if (kind === 'task_step') {
    return nodes.some((node) => node.step_run_id === identifier);
  }
  return false;
}

export function activityOverviewInvalidationScopeKeys(
  inspectorScopeKey: string,
  rootScopeKey: string | null,
  fallbackScopeKey: string | null,
): string[] {
  return [...new Set(
    [inspectorScopeKey, rootScopeKey, fallbackScopeKey]
      .filter((scopeKey): scopeKey is string => Boolean(scopeKey)),
  )];
}

export function treeSessionNavigation(presentation: 'closed' | 'pinned' | 'overlay' | 'focus'): {
  openSubSessionViewer: true;
  closeInspectorOverlay: boolean;
} {
  return {
    openSubSessionViewer: true,
    closeInspectorOverlay: presentation === 'overlay' || presentation === 'focus',
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
  const dismissTransientInspector = state.presentation === 'overlay' || state.presentation === 'focus';
  return {
    ...state,
    drawerOpen: dismissTransientInspector ? false : state.drawerOpen,
    activeTab: state.activeTab,
    presentation: dismissTransientInspector ? 'closed' : state.presentation,
    focusedSessionId: sessionId,
    middleSessionId: sessionId,
    workSessionId: sessionId,
  };
}
