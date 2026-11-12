import { writable } from 'svelte/store';
/** Versioned capability boundary; browsers never acquire native capabilities. */
export interface NativeConfiguration {
  version: 1;
  generation: string;
  server: string;
  surface?: 'main' | 'pinned' | 'rail' | 'conversation' | 'resource';
}

declare global {
  interface Window {
    __cognisNative?: NativeConfiguration;
    __cognisNativeReceive?: (command: unknown) => void;
    webkit?: { messageHandlers?: { cognis?: { postMessage: (value: unknown) => void } } };
  }
}

export function nativeConfiguration(): NativeConfiguration | null {
  if (typeof window === 'undefined' || window.top !== window) return null;
  const config = window.__cognisNative;
  if (config?.version !== 1 || typeof config.generation !== 'string'
    || !/^[0-9a-f-]{36}$/i.test(config.generation)) return null;
  try {
    const url = new URL(config.server);
    const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && local))
      || url.username || url.password || url.origin !== window.location.origin
      || url.pathname !== '/' || url.search || url.hash) return null;
    return config;
  } catch { return null; }
}

export function isNativeHost(): boolean { return nativeConfiguration() !== null; }
export function isWorkspaceReplica(): boolean {
  const config = nativeConfiguration();
  return !!config?.surface && config.surface !== 'main';
}

export function postNative(type: 'snapshot' | 'route' | 'dismiss' | 'avatar' | 'appearance' | 'open-main' | 'open-window' | 'open-resource-window' | 'notification-scope' | 'notification' | 'notification-review' | 'notification-settings' | 'workspace' | 'rail' | 'panel-state' | 'save-diagnostic' | 'open-reference', payload: unknown): boolean {
  const config = nativeConfiguration();
  if (!config) return false;
  const handler = window.webkit?.messageHandlers?.cognis;
  if (!handler) return false;
  const message = { version: 1, generation: config.generation, type, payload };
  const json = JSON.stringify(message);
  if (type === 'save-diagnostic') {
    if (new TextEncoder().encode(json).length > 16 * 1024 * 1024) return false;
  } else if (json.length > (['workspace', 'snapshot'].includes(type) ? 524_288 : 32_768)) return false;
  handler.postMessage(message);
  return true;
}

/** A Mac chat window is an identifier-only native capability, not a URL opener. */
export function openNativeConversationWindow(conversationId: string): boolean {
  if (!isNativeHost()) return false;
  if (/^[A-Za-z0-9_-]{1,160}$/.test(conversationId)) postNative('open-window', { conversationId });
  return true;
}

/** Native resource windows accept only a kind and an identifier, never a web URL. */
export function openNativeResourceWindow(kind: 'project' | 'deliverable', id: string): boolean {
  if (!isNativeHost()) return false;
  if (['project', 'deliverable'].includes(kind) && /^[A-Za-z0-9_-]{1,160}$/.test(id)) {
    postNative('open-resource-window', { kind, id });
  }
  // A rejected native command must not fall through to a browser popup.
  return true;
}

let nativeVisible = false;
let nativeFocused = false;
export const nativeVisibility = writable({ visible: false, focused: false });
/** Retain a request across asynchronous route/data loading; the native host supplies an open identity. */
export const nativeComposerFocus = writable<{ agentId: string; request: string } | null>(null);

/** The existing bridge-v1 navigate envelope, not the application's route grammar. */
export function isNativeRoute(path: unknown): path is string {
  return typeof path === 'string' && path.length <= 512
    && !path.startsWith('//') && !/%(?:2f|5c)/i.test(path)
    && /^\/(?:$|[a-zA-Z0-9/_%-]+(?:\?[a-zA-Z0-9_=&%-]*)?)$/.test(path);
}

/** Hand SPA link navigation to the same native router as WebKit/new-window links.
 * Programmatic goto (including an unsupported-link reply) never enters this path. */
export function forwardNativeReference(url: URL): boolean {
  const configuration = nativeConfiguration();
  if (!configuration || url.origin !== new URL(configuration.server).origin
    || url.username || url.password || url.hash) return false;
  const path = url.pathname + url.search;
  if (!isNativeRoute(path)) return false;
  return postNative('open-reference', { path });
}

/** Read receipts require a visible, focused native surface, not a resident document. */
export function hostAllowsObservation(): boolean {
  return !isNativeHost() || (nativeVisible && nativeFocused);
}

export function receiveNativeVisibility(visible: boolean, focused: boolean): void {
  const wasVisible = nativeVisible, wasFocused = nativeFocused;
  nativeVisible = visible;
  nativeFocused = visible && focused;
  nativeVisibility.set({ visible: nativeVisible, focused: nativeFocused });
  if (nativeVisible !== wasVisible) document.dispatchEvent(new Event('visibilitychange'));
  if (nativeFocused !== wasFocused) window.dispatchEvent(new Event(nativeFocused ? 'focus' : 'blur'));
}
