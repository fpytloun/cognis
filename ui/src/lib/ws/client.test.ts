import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

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

import { CognisWebSocketClient } from './client';
import { conversationTimelineScope, sessionTimelineScope, taskStepTimelineScope, type ChatRealtimeFrame } from '$lib/chat-v2/types';

function frame(conversationId: string, cursor: string): ChatRealtimeFrame {
  return {
    type: 'chat_v2_frame',
    conversation_id: conversationId,
    schema_version: 2,
    projection_version: 'test',
    cursor_before: cursor,
    cursor_after: `${cursor}:next`,
    ops: [],
    cycle_states: [],
    runtime: null,
    server_time: '2026-07-07T00:00:00Z',
  };
}

describe('CognisWebSocketClient', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('drops queued Chat v2 frames for an unsubscribed conversation before flush', () => {
    const callbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callbacks.push(callback);
      return callbacks.length;
    });
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    const client = new CognisWebSocketClient();
    const received: string[] = [];
    client.subscribe((event) => {
      if (event.type === 'chat_v2_frame') {
        received.push(event.conversation_id);
      }
    });
    const handleMessage = (client as unknown as { handleMessage(raw: string): void }).handleMessage.bind(client);

    handleMessage(JSON.stringify(frame('source-conv', 'cursor-1')));
    handleMessage(JSON.stringify(frame('target-conv', 'cursor-2')));
    client.unsubscribeConversation('source-conv');

    callbacks[0]?.(0);

    expect(received).toEqual(['target-conv']);
  });

  it('re-subscribes exact conversation, session, and task-step cursors through Chat v2', () => {
    class TestWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      static instances: TestWebSocket[] = [];
      readyState = TestWebSocket.OPEN;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      sent: string[] = [];
      close = vi.fn();
      constructor() {
        TestWebSocket.instances.push(this);
      }
      send(payload: string): void {
        this.sent.push(payload);
      }
    }
    vi.stubGlobal('WebSocket', TestWebSocket);
    const client = new CognisWebSocketClient();
    const conversation = conversationTimelineScope('conv-1');
    const session = sessionTimelineScope('session-1', 'conv-1');
    const taskStep = taskStepTimelineScope('task-1', 'step-1', 'conv-1');
    client.subscribeConversation('conv-1', 4, 'session-1');
    client.acquireChatV2(conversation, 'conversation-cursor');
    client.acquireChatV2(session, 'session-cursor');
    client.acquireChatV2(taskStep, 'task-cursor');
    client.updateChatV2Cursor(conversation, 'conversation-cursor-2');
    client.updateChatV2Cursor(session, 'session-cursor-2');
    client.updateChatV2Cursor(taskStep, 'task-cursor-2');

    const firstSocket = TestWebSocket.instances[0];
    expect(firstSocket).toBeDefined();
    const authenticate = (socket: TestWebSocket): void => {
      socket.onmessage?.(
        { data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>
      );
    };
    authenticate(firstSocket);
    client.disconnect();
    client.reAuthenticate();
    const secondSocket = TestWebSocket.instances[1];
    expect(secondSocket).toBeDefined();
    authenticate(secondSocket);

    expect(secondSocket.sent.map((payload) => JSON.parse(payload))).toEqual([
      { type: 'chat_v2_subscribe', scope: conversation, cursor: 'conversation-cursor-2' },
      { type: 'chat_v2_subscribe', scope: session, cursor: 'session-cursor-2' },
      { type: 'chat_v2_subscribe', scope: taskStep, cursor: 'task-cursor-2' }
    ]);
    client.disconnect();
  });

  it('ignores a stale socket close after replacement authentication', () => {
    class TestWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      static instances: TestWebSocket[] = [];
      readyState = TestWebSocket.OPEN;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      sent: string[] = [];
      close = vi.fn();
      constructor() {
        TestWebSocket.instances.push(this);
      }
      send(payload: string): void {
        this.sent.push(payload);
      }
    }
    vi.stubGlobal('WebSocket', TestWebSocket);

    const client = new CognisWebSocketClient();
    client.subscribeConversation('conv-1', 7, 'session-1');
    const firstSocket = TestWebSocket.instances[0];
    expect(firstSocket).toBeDefined();
    firstSocket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    client.reAuthenticate();
    const secondSocket = TestWebSocket.instances[1];
    expect(secondSocket).toBeDefined();
    secondSocket.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);

    firstSocket.onclose?.({ code: 1006 } as CloseEvent);

    expect(get(client.state).status).toBe('connected');
    expect(secondSocket.sent.map((payload) => JSON.parse(payload))).toEqual([]);
    expect(firstSocket.close).toHaveBeenCalledOnce();
    expect(secondSocket.close).not.toHaveBeenCalled();
    client.disconnect();
  });
});
