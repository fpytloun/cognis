import {
  conversationTimelineScope,
  sessionTimelineScope,
  type TimelineScope,
  type WorkstreamRef,
} from '$lib/chat-v2/types';
import type { Conversation } from '$lib/types/api';
import { structuralParentSessionId, workstreamForSession } from '$lib/inspectorTreeNavigation';

export type ChildView =
  | { kind: 'delegate'; sessionId: string; controllerRootConversationId: string; nodeKey: string }
  | { kind: 'managed'; conversationId: string; sessionId: string; controllerRootConversationId: string; nodeKey: string };

export function controllerRootConversationId(conversation: Conversation): string {
  return conversation.root_controller_conversation_id ?? conversation.conversation_id;
}

export function childViewForWorkstream(node: WorkstreamRef, controllerRootId: string): ChildView {
  return node.kind === 'managed' && Boolean(node.conversation_id || node.link_id) && node.conversation_id
    ? { kind: 'managed', conversationId: node.conversation_id, sessionId: node.session_id, controllerRootConversationId: controllerRootId, nodeKey: node.key }
    : { kind: 'delegate', sessionId: node.session_id, controllerRootConversationId: controllerRootId, nodeKey: node.key };
}

export function canonicalChildView(nodes: WorkstreamRef[], sessionId: string, controllerRootId: string): ChildView | null {
  const node = workstreamForSession(nodes, sessionId);
  return node ? childViewForWorkstream(node, controllerRootId) : null;
}

export function childViewScope(view: ChildView): TimelineScope {
  return view.kind === 'managed'
    ? conversationTimelineScope(view.conversationId)
    : sessionTimelineScope(view.sessionId, view.controllerRootConversationId);
}

export function childViewWorkstream(nodes: WorkstreamRef[], view: ChildView | null): WorkstreamRef | null {
  if (!view) return null;
  return nodes.find((node) => node.key === view.nodeKey) ?? workstreamForSession(nodes, view.sessionId);
}

export function parentChildView(
  nodes: WorkstreamRef[],
  view: ChildView,
): ChildView | null {
  const parentSessionId = structuralParentSessionId(nodes, view.sessionId);
  if (!parentSessionId) return null;
  return canonicalChildView(nodes, parentSessionId, view.controllerRootConversationId);
}

export function eventNeedsTreeRefresh(event: { type: string } & Record<string, unknown>): boolean {
  if (event.type === 'work_invalidated' || event.type.startsWith('delegation_')) return true;
  if (event.type !== 'chat_v2_frame') return false;
  const serialized = JSON.stringify(event);
  return serialized.includes('"tool_name":"delegate"')
    || serialized.includes('"tool_name":"agent_conversation_create"');
}
