import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

  it('sends direct step responses with notification_id', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.respondStepQuestion(
      'notif-1',
      {
        mode: 'structured',
        answers: [
          {
            question_id: 'q1',
            selected_option_ids: [],
            custom_answer: 'Use the main repo'
          }
        ]
      },
      'direct'
    );

    expect(socket.sent.some((payload) => payload.includes('"notification_id":"notif-1"'))).toBe(true);
    expect(socket.sent.some((payload) => payload.includes('"type":"step_response"'))).toBe(true);
    wsClient.disconnect();
  });

  it('sends direct auth challenge responses with response text', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.respondAuthChallenge('auth-1', '123456', 'direct');

    expect(socket.sent.some((payload) => payload.includes('"notification_id":"auth-1"'))).toBe(true);
    expect(socket.sent.some((payload) => payload.includes('"response":"123456"'))).toBe(true);
    wsClient.disconnect();
  });

  it('can replace an advanced replay cursor when resubscribing from cached history', async () => {
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
    expect(reconnects.at(-2)?.last_seq).toBe(42);
    expect(reconnects.at(-1)?.last_seq).toBe(12);
    wsClient.disconnect();
  });

  it('opts into Chat v2 frames with an explicit cursor and preserves it across legacy reconnects', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 7, 'sess-1');
    wsClient.subscribeChatV2Conversation('conv-1', 'cursor-a');
    wsClient.updateConversationSeq('conv-1', 11, 'sess-1');
    wsClient.subscribeConversation('conv-1', 11, 'sess-1', { replaceCursor: true });

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames).toContainEqual({
      type: 'chat_v2_subscribe',
      conversation_id: 'conv-1',
      cursor: 'cursor-a'
    });
    const reconnects = frames.filter((payload) => payload.type === 'reconnect');
    expect(reconnects.at(-1)).toMatchObject({
      conversation_id: 'conv-1',
      last_seq: 11,
      session_id: 'sess-1',
      chat_v2_cursor: 'cursor-a'
    });
    wsClient.disconnect();
  });

  it('clears Chat v2 server opt-in without dropping the legacy conversation subscription', async () => {
    const { wsClient } = await import('./ws/client');

    wsClient.connect();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onopen?.();
    socket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    wsClient.subscribeConversation('conv-1', 4, 'sess-1');
    wsClient.subscribeChatV2Conversation('conv-1', 'cursor-a');
    wsClient.clearChatV2Cursor('conv-1');
    wsClient.subscribeConversation('conv-1', 5, 'sess-1', { replaceCursor: true });

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames).toContainEqual({
      type: 'chat_v2_unsubscribe',
      conversation_id: 'conv-1'
    });
    expect(frames.at(-1)).toMatchObject({
      type: 'reconnect',
      conversation_id: 'conv-1',
      last_seq: 5,
      session_id: 'sess-1',
      chat_v2_cursor: null
    });
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
    wsClient.subscribeChatV2Conversation('conv-1', 'cursor-a');
    wsClient.unsubscribeConversation('conv-1');

    const frames = socket.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>);
    expect(frames).toContainEqual({
      type: 'chat_v2_unsubscribe',
      conversation_id: 'conv-1'
    });
    wsClient.disconnect();
  });
});
