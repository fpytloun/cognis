import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  hostAllowsObservation, nativeConfiguration, openNativeConversationWindow, openNativeResourceWindow, postNative, receiveNativeVisibility, forwardNativeReference,
} from './host';
import { saveNativeChatDiagnostic } from './diagnostic-export';

const generation = '8d6feafb-2aac-4b41-ac82-42b2314563bb';
afterEach(() => {
  delete window.__cognisNative;
  delete window.webkit;
  vi.restoreAllMocks();
});

describe('native host capability boundary', () => {
  it('forwards only same-server SPA references and leaves browsers, foreign links and fragments unchanged', () => {
    const reference = new URL('/chat/conv_link', window.location.origin);
    expect(forwardNativeReference(reference)).toBe(false);
    window.__cognisNative = { version: 1, generation, server: window.location.origin + '/', surface: 'pinned' };
    expect(forwardNativeReference(reference)).toBe(false);
    const send = vi.fn();
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    expect(forwardNativeReference(reference)).toBe(true);
    expect(send).toHaveBeenCalledWith({
      version: 1, generation, type: 'open-reference', payload: { path: '/chat/conv_link' },
    });
    expect(forwardNativeReference(new URL('https://other.test/chat/conv_link'))).toBe(false);
    const credentialLink = new URL('/chat/conv_link', window.location.origin);
    credentialLink.username = 'account';
    credentialLink.password = 'credential';
    expect(forwardNativeReference(credentialLink)).toBe(false);
    expect(forwardNativeReference(new URL('/chat/conv_link#message', window.location.origin))).toBe(false);
    expect(forwardNativeReference(new URL('/settings?filter=alice%40mail.test+active', window.location.origin))).toBe(false);
    expect(forwardNativeReference(new URL('/settings?filter=' + 'a'.repeat(512), window.location.origin))).toBe(false);
    for (const query of ['src%2Fmain', 'src%2fmain', 'src%5Cmain', 'src%5cmain']) {
      expect(forwardNativeReference(new URL('/chat?search=' + query, window.location.origin))).toBe(false);
    }
  });
  it('sends bounded diagnostic JSON instead of a blob URL and keeps other limits intact', () => {
    const send = vi.fn();
    const filename = 'cognis-chat-diagnostic-conv_native.json';
    const json = JSON.stringify({ schema: 'cognis-chat-client-diagnostic-v1', metadata_padding: 'x'.repeat(100_000) });
    expect(saveNativeChatDiagnostic(filename, json)).toBe(false);
    window.__cognisNative = { version: 1, generation, server: window.location.origin, surface: 'main' };
    expect(saveNativeChatDiagnostic(filename, json)).toBe(false);
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    expect(saveNativeChatDiagnostic(filename, json)).toBe(true);
    expect(send).toHaveBeenCalledExactlyOnceWith({
      version: 1, generation, type: 'save-diagnostic', payload: { filename, json },
    });
    expect(postNative('notification', { text: 'x'.repeat(33_000) })).toBe(false);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('rejects diagnostic paths, oversized UTF-8, missing handlers and the rail surface', () => {
    const send = vi.fn();
    window.__cognisNative = { version: 1, generation, server: window.location.origin, surface: 'main' };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    for (const filename of ['../cognis-chat-diagnostic.json', 'cognis-chat-diagnostic.json\n', 'diagnostic.html']) {
      expect(saveNativeChatDiagnostic(filename, '{}')).toBe(false);
    }
    expect(saveNativeChatDiagnostic('cognis-chat-diagnostic.json', 'é'.repeat(4 * 1024 * 1024 + 1))).toBe(false);
    expect(saveNativeChatDiagnostic('cognis-chat-diagnostic.json', '{}')).toBe(false);
    expect(saveNativeChatDiagnostic('cognis-chat-diagnostic.json', 'not JSON')).toBe(false);
    window.__cognisNative.surface = 'rail';
    expect(saveNativeChatDiagnostic('cognis-chat-diagnostic.json', '{}')).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it('keeps the browser path inert and observable', () => {
    expect(nativeConfiguration()).toBeNull();
    expect(hostAllowsObservation()).toBe(true);
    expect(() => postNative('dismiss', {})).not.toThrow();
  });

  it('requires matching trusted origin, protocol and generation shape', () => {
    for (const server of ['http://remote.test', 'https://remote.test', 'file:///tmp/ui', 'http://user:pass@localhost']) {
      window.__cognisNative = { version: 1, generation, server };
      expect(nativeConfiguration()).toBeNull();
    }
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    expect(nativeConfiguration()).not.toBeNull();
    window.__cognisNative = { version: 1, generation: 'old', server: window.location.origin };
    expect(nativeConfiguration()).toBeNull();
  });

  it('scopes messages and rejects oversized state', () => {
    const send = vi.fn();
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    postNative('dismiss', {});
    expect(send).toHaveBeenCalledExactlyOnceWith({ version: 1, generation, type: 'dismiss', payload: {} });
    postNative('snapshot', { text: 'x'.repeat(525_000) });
    expect(send).toHaveBeenCalledTimes(1);
    postNative('notification', { text: 'x'.repeat(33_000) });
    expect(send).toHaveBeenCalledTimes(1);
    postNative('snapshot', { agents: Array.from({ length: 500 }, (_, index) => ({
      agentId: `agent_${index}`, name: 'Synthetic catalogue agent',
    })) });
    expect(send).toHaveBeenCalledTimes(2);
  });

  it('never treats a hidden or unfocused resident document as observed', () => {
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    receiveNativeVisibility(false, false);
    expect(hostAllowsObservation()).toBe(false);
    receiveNativeVisibility(true, false);
    expect(hostAllowsObservation()).toBe(false);
    receiveNativeVisibility(true, true);
    expect(hostAllowsObservation()).toBe(true);
    receiveNativeVisibility(false, true);
    expect(hostAllowsObservation()).toBe(false);
  });
  it('uses the scoped Mac command without offering a browser fallback, even for a rejected ID', () => {
    const send = vi.fn(), open = vi.spyOn(window, 'open');
    expect(openNativeConversationWindow('conv_window')).toBe(false);
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    expect(openNativeConversationWindow('conv_window')).toBe(true);
    expect(send).toHaveBeenCalledExactlyOnceWith({
      version: 1, generation, type: 'open-window', payload: { conversationId: 'conv_window' }
    });
    expect(openNativeConversationWindow('https://untrusted.test')).toBe(true);
    expect(send).toHaveBeenCalledTimes(1);
    expect(open).not.toHaveBeenCalled();
  });
  it('sends typed resource identifiers and never falls back in native documents', () => {
    expect(openNativeResourceWindow('project', 'project_window')).toBe(false);
    const send = vi.fn();
    window.__cognisNative = { version: 1, generation, server: window.location.origin, surface: 'resource' };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    for (const kind of ['project', 'deliverable'] as const) {
      expect(openNativeResourceWindow(kind, 'shared_id')).toBe(true);
      expect(send).toHaveBeenLastCalledWith({
        version: 1, generation, type: 'open-resource-window', payload: { kind, id: 'shared_id' }
      });
    }
    for (const id of ['../chat', 'id?window=1', 'https://untrusted.test', '', 'a'.repeat(161)]) {
      expect(openNativeResourceWindow('project', id)).toBe(true);
    }
    expect(send).toHaveBeenCalledTimes(2);
    delete window.webkit;
    expect(openNativeResourceWindow('project', 'project_window')).toBe(true);
  });
});
