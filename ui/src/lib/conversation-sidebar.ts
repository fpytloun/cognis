import {
  isDashboardConversationActive,
  isDashboardConversationWaiting,
} from '$lib/dashboard/dashboard';
import { backgroundWorkItemIsRunning } from '$lib/ongoing-work';
import type { BackgroundWorkItem, Conversation } from '$lib/types/api';

export interface SidebarConversationLanes {
  active: Conversation[];
  waiting: Conversation[];
  recent: Conversation[];
}

/** The canonical Waiting eligibility; session outcomes only affect row color. */
export function isSidebarConversationWaiting(
  conversation: Pick<Conversation, 'has_unread' | 'pending_notification_types' | 'is_readonly_for_caller'>,
  isActive: boolean,
): boolean {
  if (conversation.is_readonly_for_caller) return false;
  return isDashboardConversationWaiting(conversation)
    || (conversation.has_unread && !isActive);
}

export function partitionSidebarConversationLanes(
  conversations: Conversation[],
  backgroundWork: readonly BackgroundWorkItem[] = [],
): SidebarConversationLanes {
  const backgroundActiveIds = new Set(
    backgroundWork.filter(backgroundWorkItemIsRunning).map((item) => item.controller_conversation_id),
  );
  const isActive = (conversation: Conversation) =>
    isDashboardConversationActive(conversation)
    || backgroundActiveIds.has(conversation.conversation_id);
  const waiting = conversations.filter((conversation) =>
    isSidebarConversationWaiting(conversation, isActive(conversation))
  );
  const waitingIds = new Set(waiting.map((conversation) => conversation.conversation_id));
  const active = conversations.filter(
    (conversation) =>
      !waitingIds.has(conversation.conversation_id)
      && isActive(conversation),
  );
  const activeIds = new Set(active.map((conversation) => conversation.conversation_id));
  const recent = conversations.filter(
    (conversation) =>
      !waitingIds.has(conversation.conversation_id)
      && !activeIds.has(conversation.conversation_id),
  );
  return { active, waiting, recent };
}
