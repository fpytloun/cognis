import { getContext, setContext, type Component } from 'svelte';

/**
 * The signed-in app provides a chat picker for "Send to → Another chat…". Hosted standalone and
 * public viewers never set it: the picker depends on the authenticated app client, which must not
 * enter their bundle.
 */
export interface ConversationChooserProps {
  selected?: string | null;
  onSelect: (conversation: { conversation_id: string; title?: string | null }) => void;
}
export type ConversationChooserLoader = () => Promise<{ default: Component<ConversationChooserProps> }>;

const key = Symbol('feedback-conversation-chooser');

export function provideConversationChooser(loader: ConversationChooserLoader): void {
  setContext(key, loader);
}

export function conversationChooser(): ConversationChooserLoader | null {
  return getContext<ConversationChooserLoader | undefined>(key) ?? null;
}
