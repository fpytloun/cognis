import { writable } from 'svelte/store';

export const shortcutHelpOpen = writable(false);

export const CHAT_COMPOSER_FOCUS_EVENT = 'cognis:focus-chat-composer';
export const CANCEL_ACTIVE_TURN_EVENT = 'cognis:cancel-active-turn';

export function openShortcutHelp(): void {
  shortcutHelpOpen.set(true);
}

export function closeShortcutHelp(): void {
  shortcutHelpOpen.set(false);
}

export function requestChatComposerFocus(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.dispatchEvent(new CustomEvent(CHAT_COMPOSER_FOCUS_EVENT));
}

export function onChatComposerFocusRequest(handler: () => void): () => void {
  if (typeof window === 'undefined') {
    return () => undefined;
  }
  const listener = () => handler();
  window.addEventListener(CHAT_COMPOSER_FOCUS_EVENT, listener);
  return () => {
    window.removeEventListener(CHAT_COMPOSER_FOCUS_EVENT, listener);
  };
}

export function requestCancelActiveTurn(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.dispatchEvent(new CustomEvent(CANCEL_ACTIVE_TURN_EVENT));
}

export function onCancelActiveTurnRequest(handler: () => void): () => void {
  if (typeof window === 'undefined') {
    return () => undefined;
  }
  const listener = () => handler();
  window.addEventListener(CANCEL_ACTIVE_TURN_EVENT, listener);
  return () => {
    window.removeEventListener(CANCEL_ACTIVE_TURN_EVENT, listener);
  };
}
