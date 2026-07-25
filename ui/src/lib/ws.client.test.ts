import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatV2ConversationLifecycle } from './chat-page';
import { sessionTimelineScope } from './chat-v2/types';

vi.mock('$lib/config', () => ({
  getWebSocketUrl: () => 'ws://localhost/api/ws'
}));

vi.mock('$lib/stores/auth', () => ({
  auth: {
    getSnapshot: () => ({ status: 'authenticated', initialized: true, user: { email: 'user@example.com' } }),
    clear: vi.fn()
  }
}));

vi.mock('$lib/errors', () => ({
  reportError: vi.fn()
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;

  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.closed = true;
    this.onclose?.({ code: 1000 } as CloseEvent);
  }
}

describe('ws client heartbeat', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    FakeWebSocket.instances = [];
  });

  afterEach(async () => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it('sends ping after authentication', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    vi.advanceTimersByTime(30_000);

    expect(socket.sent.some((payload) => payload.includes('"type":"ping"'))).toBe(true);
    wsClient.disconnect();
  });

  it('stops sending ping after disconnect', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);
    wsClient.disconnect();

    const sentBefore = socket.sent.length;
    vi.advanceTimersByTime(35_000);

    expect(socket.sent.length).toBe(sentBefore);
  });

  it('marks the socket stalled when pong is missing', async () => {
    const { getWebSocketState, wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    vi.advanceTimersByTime(40_000);

    expect(socket.closed).toBe(true);
    expect(['stalled', 'reconnecting']).toContain(getWebSocketState().status);
    wsClient.disconnect();
  });

  it('does not emit legacy replay frames when conversation sequence state changes', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 10, 'sess-1');
    wsClient.updateConversationSeq('conv-1', 42, 'sess-1');
    wsClient.subscribeConversation('conv-1', 12, 'sess-1');
    wsClient.subscribeConversation('conv-1', 12, 'sess-1', { replaceCursor: true });

    const reconnects = socket.sent
      .map((payload) => JSON.parse(payload) as { type?: string; last_seq?: number })
      .filter((payload) => payload.type === 'reconnect');
    expect(reconnects).toEqual([]);
    wsClient.disconnect();
  });

  it('subscribes conversation scopes through the Chat v2 protocol', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 7, 'sess-1');
    wsClient.acquireChatV2('conv-1', 'cursor-a');
    wsClient.updateConversationSeq('conv-1', 11, 'sess-1');
    wsClient.subscribeConversation('conv-1', 11, 'sess-1', { replaceCursor: true });

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_subscribe')).toEqual([{
      type: 'chat_v2_subscribe',
      scope: { key: 'conversation:conv-1', kind: 'conversation', conversation_id: 'conv-1' },
      cursor: 'cursor-a'
    }]);
    expect(frames.filter((payload) => payload.type === 'reconnect')).toEqual([]);
    wsClient.disconnect();
  });

  it('clears the Chat v2 cursor without dropping mounted ownership', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 4, 'sess-1');
    wsClient.acquireChatV2('conv-1', 'cursor-a');
    wsClient.clearChatV2Cursor('conv-1');
    wsClient.subscribeConversation('conv-1', 5, 'sess-1', { replaceCursor: true });
    wsClient.releaseChatV2('conversation:conv-1');

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'reconnect')).toEqual([]);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toEqual([
      { type: 'chat_v2_unsubscribe', scope_key: 'conversation:conv-1' }
    ]);
    expect((wsClient as unknown as {
      chatV2Subscriptions: Map<string, unknown>
    }).chatV2Subscriptions.size).toBe(0);
    wsClient.disconnect();
  });

  it('acquires once per mounted view and releases once after repeated refresh/reset/recovery updates', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    const scope = sessionTimelineScope('session-1', 'conv-1');
    const lifecycle = new ChatV2ConversationLifecycle(wsClient);
    lifecycle.acceptSnapshot(scope, 'cursor-1');
    lifecycle.acceptSnapshot(scope, 'cursor-2');
    lifecycle.acceptSnapshot(scope, 'cursor-3');
    wsClient.clearChatV2Cursor(scope);
    lifecycle.acceptSnapshot(scope, 'cursor-4');
    lifecycle.acceptSnapshot(scope, 'cursor-5');
    lifecycle.release();
    lifecycle.release();

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_subscribe')).toHaveLength(1);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toHaveLength(1);
    expect((wsClient as unknown as { chatV2Subscriptions: Map<string, unknown> }).chatV2Subscriptions.size).toBe(0);
    wsClient.disconnect();
  });

  it('reference-counts two separate mounted views of one scope', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    const scope = { key: 'session:session-2', kind: 'session', session_id: 'session-2', conversation_id: 'conv-2' } as const;
    wsClient.acquireChatV2(scope, 'cursor-1');
    wsClient.acquireChatV2(scope, 'cursor-1');
    wsClient.releaseChatV2(scope.key);

    let frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toHaveLength(0);
    expect((wsClient as unknown as { chatV2Subscriptions: Map<string, { refCount: number }> }).chatV2Subscriptions.get(scope.key)?.refCount).toBe(1);

    wsClient.releaseChatV2(scope.key);
    frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toHaveLength(1);
    wsClient.disconnect();
  });

  it('re-registers a cleared scope after re-authentication without incrementing refs', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    const scope = { key: 'session:session-reconnect', kind: 'session', session_id: 'session-reconnect', conversation_id: 'conv-reconnect' } as const;
    wsClient.acquireChatV2(scope, 'cursor-a');
    wsClient.clearChatV2Cursor(scope);
    wsClient.reAuthenticate();
    await Promise.resolve();
    const replacement = FakeWebSocket.instances.at(-1);
    replacement?.onopen?.();
    replacement?.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);
    expect((wsClient as unknown as {
      chatV2Subscriptions: Map<string, { wireRegistered: boolean }>
    }).chatV2Subscriptions.get(scope.key)?.wireRegistered).toBe(false);
    wsClient.updateChatV2Cursor(scope, 'cursor-b');
    wsClient.updateChatV2Cursor(scope, 'cursor-c');

    const frames = replacement?.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>) ?? [];
    expect(frames.filter((payload) => payload.type === 'chat_v2_subscribe')).toEqual([
      { type: 'chat_v2_subscribe', scope, cursor: 'cursor-b' }
    ]);
    expect((wsClient as unknown as { chatV2Subscriptions: Map<string, { cursor: string | null }> })
      .chatV2Subscriptions.get(scope.key)?.cursor).toBe('cursor-c');
    expect((wsClient as unknown as { chatV2Subscriptions: Map<string, { refCount: number; wireRegistered: boolean }> })
      .chatV2Subscriptions.get(scope.key)).toMatchObject({ refCount: 1, wireRegistered: true });
    wsClient.disconnect();
  });

  it('restores one unwired existing scope on a second acquire and releases both owners once', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const first = FakeWebSocket.instances[0];
    first.onopen?.();
    first.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    const scope = sessionTimelineScope('session-two-views', 'conv-two-views');
    wsClient.acquireChatV2(scope, 'cursor-a');
    wsClient.clearChatV2Cursor(scope);
    wsClient.reAuthenticate();
    await Promise.resolve();
    const replacement = FakeWebSocket.instances.at(-1)!;
    replacement.onopen?.();
    replacement.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.acquireChatV2(scope, 'cursor-b');
    let frames = replacement.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_subscribe')).toEqual([
      { type: 'chat_v2_subscribe', scope, cursor: 'cursor-b' }
    ]);
    expect((wsClient as unknown as {
      chatV2Subscriptions: Map<string, { refCount: number; wireRegistered: boolean }>
    }).chatV2Subscriptions.get(scope.key)).toMatchObject({
      refCount: 2,
      wireRegistered: true
    });

    wsClient.releaseChatV2(scope.key);
    frames = replacement.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toHaveLength(0);

    wsClient.releaseChatV2(scope.key);
    frames = replacement.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toEqual([
      { type: 'chat_v2_unsubscribe', scope_key: scope.key }
    ]);
    expect((wsClient as unknown as {
      chatV2Subscriptions: Map<string, unknown>
    }).chatV2Subscriptions.size).toBe(0);
    wsClient.disconnect();
  });

  it('unsubscribes Chat v2 on full conversation unsubscribe when v2 was active', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 1, 'sess-1');
    wsClient.acquireChatV2('conv-1', 'cursor-a');
    wsClient.unsubscribeConversation('conv-1');

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames).toContainEqual({
      type: 'chat_v2_unsubscribe',
      scope_key: 'conversation:conv-1'
    });
    wsClient.disconnect();
  });

  it('unsubscribes a conversation after its cursor is cleared on the current socket', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-clear', 1, 'sess-clear');
    wsClient.acquireChatV2('conv-clear', 'cursor-a');
    wsClient.clearChatV2Cursor('conv-clear');
    wsClient.unsubscribeConversation('conv-clear');

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames.filter((payload) => payload.type === 'chat_v2_unsubscribe')).toEqual([
      { type: 'chat_v2_unsubscribe', scope_key: 'conversation:conv-clear' }
    ]);
    expect((wsClient as unknown as {
      chatV2Subscriptions: Map<string, unknown>
    }).chatV2Subscriptions.size).toBe(0);
    wsClient.disconnect();
  });

  it('evicts old Chat v2 subscriptions when many conversations were visited', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    for (let i = 1; i <= 13; i += 1) {
      wsClient.acquireChatV2(`conv-${i}`, `cursor-${i}`);
    }

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames).toContainEqual({
      type: 'chat_v2_unsubscribe',
      scope_key: 'conversation:conv-1'
    });
    wsClient.disconnect();
  });
});
