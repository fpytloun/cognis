import { getContext, setContext } from 'svelte';
const key = Symbol('chat-message-feedback');
export interface ChatMessageFeedback { readonly eligible: boolean; readonly sourceKey: string }
export function provideChatMessageFeedback(value: ChatMessageFeedback) { setContext(key, value); }
export function getChatMessageFeedback() { return getContext<ChatMessageFeedback | undefined>(key); }
