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

    wsClient.respondStepQuestion('notif-1', 'Use the main repo', 'direct');

    expect(socket.sent.some((payload) => payload.includes('"notification_id":"notif-1"'))).toBe(true);
    expect(socket.sent.some((payload) => payload.includes('"type":"step_response"'))).toBe(true);
    wsClient.disconnect();
  });
});
