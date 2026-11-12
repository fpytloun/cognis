/** Recipient-visible transport messages, never the sender's execution session. */
import type { Conversation } from '$lib/types/api';

export function appContactBindingId(conversation: Pick<Conversation, 'context'> | null): string | null {
  const value = conversation?.context?.platform_data?.app_contact_binding_id;
  return typeof value === 'string' && value ? value : null;
}

export function shouldReplaceAgentSidebarConversation(existing: Conversation, incoming: Conversation): boolean {
  const existingContact = appContactBindingId(existing);
  const incomingContact = appContactBindingId(incoming);
  if (!incomingContact) return true;
  if (!existingContact) return false;
  if (existing.conversation_id === incoming.conversation_id) return true;
  // Reading an older contact must not replace a newer contact or a normal main chat.
  const activity = (conversation: Conversation) =>
    Date.parse(conversation.last_message_at ?? conversation.created_at ?? '') || 0;
  return activity(incoming) > activity(existing);
}

export interface AppContactAttachment {
  artifact_id: string;
  filename: string;
  href: string;
}

export interface AppContactMessage {
  id: string;
  direction: 'incoming' | 'outgoing';
  content: string;
  occurred_at: string;
  attachments: AppContactAttachment[];
}

export interface AppContactView {
  id: string;
  conversation_id?: string | null;
  agent_name: string;
  sender_email: string;
  title: string;
  state: string;
  expires_at: string | null;
  can_reply: boolean;
  blocked: boolean;
  main_conversation_id: string | null;
  messages: AppContactMessage[];
}
