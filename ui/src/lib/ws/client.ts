import { writable, get } from 'svelte/store';

import { getWebSocketUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import { auth } from '$lib/stores/auth';
import type { ChatRealtimeFrame } from '$lib/chat-v2/types';
import type { AttachmentRef, CognisWebSocketEvent, QuestionSetReply } from '$lib/types/api';
import { clamp } from '$lib/utils';

type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'stalled';
type CognisWebSocketClientEvent = CognisWebSocketEvent | ChatRealtimeFrame;
type EventListener = (event: CognisWebSocketClientEvent) => void;

interface WebSocketState {
  status: ConnectionStatus;
  attempts: number;
  lastError: string | null;
}

interface ConversationSubscription {
  lastSeq: number;
  sessionId: string | null;
  chatV2Cursor: string | null;
}

interface SubscribeConversationOptions {
  replaceCursor?: boolean;
}

const initialState: WebSocketState = {
  status: 'idle',
  attempts: 0,
  lastError: null
};

class CognisWebSocketClient {
  private socket: WebSocket | null = null;
  private listeners = new Set<EventListener>();
  private subscriptions = new Map<string, ConversationSubscription>();
  private queuedMessages: string[] = [];
  private reconnectTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private pongTimeout: number | null = null;
  private chatV2FrameFlushHandle: number | null = null;
  private pendingChatV2Frames: ChatRealtimeFrame[] = [];
  private reconnectAttempts = 0;
  private authenticated = false;
  private manualDisconnect = false;

  readonly state = writable<WebSocketState>(initialState);

  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  connect(): void {
    void this.connectInternal();
  }

  private async connectInternal(): Promise<void> {
    if (typeof window === 'undefined') {
      return;
    }

    const authState = auth.getSnapshot();
    if (authState.status !== 'authenticated') {
      this.state.set({ status: 'stalled', attempts: this.reconnectAttempts, lastError: 'Authentication required' });
      return;
    }

    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    this.manualDisconnect = false;
    this.authenticated = false;
    this.state.update(() => ({
      status: this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting',
      attempts: this.reconnectAttempts,
      lastError: null
    }));

    this.socket = new WebSocket(getWebSocketUrl());
    this.socket.onopen = () => {};
    this.socket.onmessage = (event) => {
      this.handleMessage(event.data);
    };
    this.socket.onerror = () => {
      this.state.update((state) => ({ ...state, lastError: 'WebSocket connection error' }));
    };
    this.socket.onclose = (event) => {
      this.socket = null;
      this.authenticated = false;
      if (this.manualDisconnect) {
        this.state.set({ status: 'idle', attempts: 0, lastError: null });
        return;
      }
      if (event.code === 4401) {
        auth.clear('Session expired. Please log in again.');
        this.state.set({
          status: 'stalled',
          attempts: this.reconnectAttempts,
          lastError: 'Session expired. Please log in again.'
        });
        return;
      }
      this.scheduleReconnect();
    };
  }

  disconnect(): void {
    this.manualDisconnect = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.clearHeartbeat();
    this.clearPongTimeout();
    this.clearChatV2FrameFlush();
    this.socket?.close();
    this.socket = null;
    this.authenticated = false;
    this.state.set(initialState);
  }

  subscribeConversation(
    conversationId: string,
    lastSeq = 0,
    sessionId: string | null = null,
    options: SubscribeConversationOptions = {}
  ): void {
    const previous = this.subscriptions.get(conversationId);
    const normalizedSessionId = typeof sessionId === 'string' && sessionId.trim() ? sessionId : null;
    const shouldReplaceCursor = options.replaceCursor === true;
    const next: ConversationSubscription = previous && previous.sessionId === normalizedSessionId && !shouldReplaceCursor
      ? { ...previous, lastSeq: Math.max(previous.lastSeq, lastSeq), sessionId: normalizedSessionId }
      : { lastSeq, sessionId: normalizedSessionId, chatV2Cursor: previous?.chatV2Cursor ?? null };
    this.subscriptions.set(conversationId, next);
    if (this.authenticated) {
      this.sendRaw({
        type: 'reconnect',
        conversation_id: conversationId,
        last_seq: next.lastSeq,
        session_id: next.sessionId,
        chat_v2_cursor: next.chatV2Cursor,
      });
      return;
    }

    this.connect();
  }

  unsubscribeConversation(conversationId: string): void {
    const previous = this.subscriptions.get(conversationId);
    if (this.authenticated && previous?.chatV2Cursor) {
      this.sendRaw({ type: 'chat_v2_unsubscribe', conversation_id: conversationId });
    }
    this.subscriptions.delete(conversationId);
  }

  subscribeChatV2Conversation(conversationId: string, cursor: string): void {
    const previous = this.subscriptions.get(conversationId);
    const next: ConversationSubscription = {
      lastSeq: previous?.lastSeq ?? 0,
      sessionId: previous?.sessionId ?? null,
      chatV2Cursor: cursor
    };
    this.subscriptions.set(conversationId, next);
    if (this.authenticated) {
      this.sendRaw({
        type: 'chat_v2_subscribe',
        conversation_id: conversationId,
        cursor
      });
      return;
    }
    this.connect();
  }

  updateChatV2Cursor(conversationId: string, cursor: string): void {
    const previous = this.subscriptions.get(conversationId);
    if (!previous) {
      this.subscriptions.set(conversationId, {
        lastSeq: 0,
        sessionId: null,
        chatV2Cursor: cursor
      });
      return;
    }
    this.subscriptions.set(conversationId, { ...previous, chatV2Cursor: cursor });
  }

  clearChatV2Cursor(conversationId: string): void {
    const previous = this.subscriptions.get(conversationId);
    if (!previous) return;
    this.subscriptions.set(conversationId, { ...previous, chatV2Cursor: null });
    this.sendRaw({ type: 'chat_v2_unsubscribe', conversation_id: conversationId });
  }

  updateConversationSeq(conversationId: string, lastSeq: number, sessionId: string | null = null): void {
    const previous = this.subscriptions.get(conversationId);
    const normalizedSessionId = typeof sessionId === 'string' && sessionId.trim() ? sessionId : null;
    if (!previous || previous.sessionId !== normalizedSessionId) {
      this.subscriptions.set(conversationId, {
        lastSeq,
        sessionId: normalizedSessionId,
        chatV2Cursor: previous?.chatV2Cursor ?? null
      });
      return;
    }
    this.subscriptions.set(conversationId, {
      lastSeq: Math.max(previous.lastSeq, lastSeq),
      sessionId: previous.sessionId,
      chatV2Cursor: previous.chatV2Cursor
    });
  }

  sendMessage(conversationId: string, content: string, attachments: AttachmentRef[] = [], clientMessageId: string | null = null): string {
    const resolvedClientMessageId = clientMessageId ?? `cmsg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
    // Only subscribe if not already subscribed — avoids sending a redundant
    // reconnect frame before every message which triggers session_recovered.
    if (!this.subscriptions.has(conversationId)) {
      this.subscribeConversation(conversationId, 0);
    }
    this.sendRaw({ type: 'message', conversation_id: conversationId, content, attachments, client_message_id: resolvedClientMessageId });
    return resolvedClientMessageId;
  }

  resolveEscalation(callId: string, decision: string, note?: string): void {
    this.sendRaw({ type: 'resolve_escalation', call_id: callId, decision, note });
  }

  respondGate(taskId: string, action: string, stepName?: string, feedback?: string): void {
    this.sendRaw({ type: 'gate_response', task_id: taskId, action, step_name: stepName, feedback });
  }

  respondStep(taskId: string, reply: QuestionSetReply, stepName?: string): void {
    this.sendRaw({ type: 'step_response', task_id: taskId, ...reply, step_name: stepName });
  }

  respondStepQuestion(notificationId: string, reply: QuestionSetReply, stepName?: string): void {
    this.sendRaw({ type: 'step_response', notification_id: notificationId, ...reply, step_name: stepName });
  }

  respondAuthChallenge(notificationId: string, response: string, stepName?: string): void {
    this.sendRaw({ type: 'step_response', notification_id: notificationId, response, step_name: stepName });
  }

  ping(): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN || !this.authenticated) {
      return;
    }
    this.socket.send(JSON.stringify({ type: 'ping' }));
    this.startPongTimeout();
  }

  enableTts(voice: string | null = null): void {
    this.sendRaw({ type: 'enable_tts', voice });
  }

  disableTts(): void {
    this.sendRaw({ type: 'disable_tts' });
  }

  private sendRaw(payload: Record<string, unknown>): void {
    const serialized = JSON.stringify(payload);
    if (
      !this.socket ||
      this.socket.readyState !== WebSocket.OPEN ||
      !this.authenticated
    ) {
      this.queuedMessages.push(serialized);
      this.connect();
      return;
    }

    this.socket.send(serialized);
  }

  private flushQueue(): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN || !this.authenticated) {
      return;
    }

    const queued = [...this.queuedMessages];
    this.queuedMessages = [];
    for (const message of queued) {
      this.socket.send(message);
    }
  }

  private handleMessage(raw: string): void {
    try {
      const payload = JSON.parse(raw) as CognisWebSocketClientEvent;
      if (payload.type === 'authenticated') {
        this.authenticated = true;
        this.reconnectAttempts = 0;
        this.clearPongTimeout();
        this.state.set({ status: 'connected', attempts: 0, lastError: null });
        this.startHeartbeat();
        for (const [conversationId, subscription] of this.subscriptions.entries()) {
          this.sendRaw({
            type: 'reconnect',
            conversation_id: conversationId,
            last_seq: subscription.lastSeq,
            session_id: subscription.sessionId,
            chat_v2_cursor: subscription.chatV2Cursor,
          });
        }
        this.flushQueue();
        return;
      }

      if (
        'conversation_id' in payload
        && typeof payload.conversation_id === 'string'
        && 'seq' in payload
        && typeof payload.seq === 'number'
        && payload.seq > 0
      ) {
        const sessionId = 'session_id' in payload && typeof payload.session_id === 'string'
          ? payload.session_id
          : null;
        this.updateConversationSeq(payload.conversation_id, payload.seq, sessionId);
      }

      if (payload.type === 'error') {
        this.state.update((state) => ({ ...state, lastError: payload.message }));
      }

      if (payload.type === 'pong') {
        this.clearPongTimeout();
      }

      if (payload.type === 'chat_v2_frame') {
        this.enqueueChatV2Frame(payload);
        return;
      }

      this.flushChatV2Frames();
      this.dispatch(payload);
    } catch (error) {
      reportError('Failed to process WebSocket payload', error);
    }
  }

  private dispatch(payload: CognisWebSocketClientEvent): void {
    for (const listener of this.listeners) {
      listener(payload);
    }
  }

  private enqueueChatV2Frame(frame: ChatRealtimeFrame): void {
    this.pendingChatV2Frames.push(frame);
    if (typeof window === 'undefined') {
      this.flushChatV2Frames();
      return;
    }
    if (this.chatV2FrameFlushHandle !== null) {
      return;
    }
    this.chatV2FrameFlushHandle = window.requestAnimationFrame(() => {
      this.chatV2FrameFlushHandle = null;
      this.flushChatV2Frames();
    });
  }

  private flushChatV2Frames(): void {
    if (this.pendingChatV2Frames.length === 0) return;
    if (this.chatV2FrameFlushHandle !== null && typeof window !== 'undefined') {
      window.cancelAnimationFrame(this.chatV2FrameFlushHandle);
      this.chatV2FrameFlushHandle = null;
    }
    const frames = this.pendingChatV2Frames;
    this.pendingChatV2Frames = [];
    try {
      for (const pendingFrame of frames) {
        this.dispatch(pendingFrame);
      }
    } catch (error) {
      reportError('Failed to dispatch Chat v2 frame batch', error);
    }
  }

  private clearChatV2FrameFlush(): void {
    if (this.chatV2FrameFlushHandle !== null && typeof window !== 'undefined') {
      window.cancelAnimationFrame(this.chatV2FrameFlushHandle);
    }
    this.chatV2FrameFlushHandle = null;
    this.pendingChatV2Frames = [];
  }

  private scheduleReconnect(): void {
    this.clearHeartbeat();
    this.clearPongTimeout();
    if (typeof window === 'undefined') {
      return;
    }

    if (this.reconnectAttempts >= 10) {
      this.state.set({
        status: 'stalled',
        attempts: this.reconnectAttempts,
        lastError: 'Connection lost. Use reconnect to try again.'
      });
      return;
    }

    const delay = clamp(1000 * 2 ** this.reconnectAttempts, 1000, 30_000);
    this.reconnectAttempts += 1;
    this.state.set({
      status: 'reconnecting',
      attempts: this.reconnectAttempts,
      lastError: 'Reconnecting to Cognis…'
    });

    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startHeartbeat(): void {
    if (typeof window === 'undefined') {
      return;
    }
    this.clearHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      this.ping();
    }, 30_000);
  }

  private clearHeartbeat(): void {
    if (typeof window !== 'undefined' && this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
    }
    this.heartbeatTimer = null;
  }

  private startPongTimeout(): void {
    if (typeof window === 'undefined') {
      return;
    }
    this.clearPongTimeout();
    this.pongTimeout = window.setTimeout(() => {
      this.state.update((state) => ({
        ...state,
        status: 'stalled',
        lastError: 'Heartbeat timed out. Reconnecting…'
      }));
      this.socket?.close();
    }, 10_000);
  }

  private clearPongTimeout(): void {
    if (typeof window !== 'undefined' && this.pongTimeout !== null) {
      window.clearTimeout(this.pongTimeout);
    }
    this.pongTimeout = null;
  }
}

export const wsClient = new CognisWebSocketClient();
export const wsState = wsClient.state;

export function getWebSocketState(): WebSocketState {
  return get(wsState);
}

// ---------------------------------------------------------------------------
// Dev-only WS frame recorder
//
// Activated by ?recordWs=1 in the URL (or window.__cognisWsRecorder.start()).
// Records every incoming WS event to an in-memory buffer and exposes a
// download() method that saves a JSONL file for use as a golden replay input.
//
// Usage (browser console):
//   window.__cognisWsRecorder.start()   // begin recording
//   window.__cognisWsRecorder.stop()    // stop recording
//   window.__cognisWsRecorder.download() // save as ws-recording-<timestamp>.jsonl
//   window.__cognisWsRecorder.clear()   // clear buffer
//   window.__cognisWsRecorder.count     // number of events recorded
// ---------------------------------------------------------------------------

class WsFrameRecorder {
  private _recording = false;
  private _frames: CognisWebSocketClientEvent[] = [];
  private _unsubscribe: (() => void) | null = null;

  get recording(): boolean { return this._recording; }
  get count(): number { return this._frames.length; }

  start(): void {
    if (this._recording) return;
    this._recording = true;
    this._frames = [];
    this._unsubscribe = wsClient.subscribe((event) => {
      if (this._recording) {
        this._frames.push(event);
      }
    });
    console.info('[WsRecorder] Recording started. Call window.__cognisWsRecorder.download() to save.');
  }

  stop(): void {
    this._recording = false;
    this._unsubscribe?.();
    this._unsubscribe = null;
    console.info(`[WsRecorder] Recording stopped. ${this._frames.length} events captured.`);
  }

  clear(): void {
    this._frames = [];
    console.info('[WsRecorder] Buffer cleared.');
  }

  download(filename?: string): void {
    if (this._frames.length === 0) {
      console.warn('[WsRecorder] No events recorded.');
      return;
    }
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const name = filename ?? `ws-recording-${ts}.jsonl`;
    const content = this._frames.map((e) => JSON.stringify(e)).join('\n') + '\n';
    const blob = new Blob([content], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
    console.info(`[WsRecorder] Downloaded ${this._frames.length} events as ${name}`);
  }

  /** Return the raw frames array (for programmatic use). */
  frames(): CognisWebSocketClientEvent[] {
    return [...this._frames];
  }
}

export const wsRecorder = new WsFrameRecorder();

// Auto-start recording if ?recordWs=1 is in the URL (dev/debug only).
if (typeof window !== 'undefined') {
  const params = new URLSearchParams(window.location.search);
  if (params.get('recordWs') === '1') {
    wsRecorder.start();
  }
  // Expose on window for console access.
  (window as unknown as Record<string, unknown>)['__cognisWsRecorder'] = wsRecorder;
}
