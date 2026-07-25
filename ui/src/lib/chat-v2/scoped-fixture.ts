import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  ChatSyncResponse,
  TimelineBackfillResponse,
  TimelineItem,
  MessageTimelineItem,
  TimelineScope,
} from './types';
import { conversationTimelineScope, sessionTimelineScope, taskStepTimelineScope } from './types';
import { CognisWebSocketClient } from '$lib/ws/client';

class FixtureWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FixtureWebSocket[] = [];
  static handler: ((socket: FixtureWebSocket, payload: Record<string, unknown>) => void) | null = null;
  readyState = FixtureWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor() {
    FixtureWebSocket.instances.push(this);
    queueMicrotask(() => {
      this.readyState = FixtureWebSocket.OPEN;
      this.onopen?.();
      this.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) } as MessageEvent<string>);
    });
  }
  send(payload: string): void {
    FixtureWebSocket.handler?.(this, JSON.parse(payload) as Record<string, unknown>);
  }
  close(): void {
    this.readyState = 3;
    queueMicrotask(() => this.onclose?.({ code: 1000 } as CloseEvent));
  }
  receive(payload: ChatRealtimeFrame): void {
    if (this.readyState === FixtureWebSocket.OPEN) {
      this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
    }
  }
}

const NOW = '2026-01-01T00:00:00.000Z';

function item(id: string, sortKey: string, content: string, role: 'user' | 'assistant' | 'system' = 'assistant'): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    sort_key: sortKey,
    source_refs: [],
    created_at: NOW,
    updated_at: NOW,
    stable: true,
    status: 'complete',
    role,
    content,
    message_id: id,
    attachments: [],
    partial: false,
  };
}

function scopeItem(scope: TimelineScope, index: number): TimelineItem {
  const prefix = scope.key.replace(/[^a-z0-9]+/gi, '-');
  const label = `${scope.label ?? scope.key} event ${index}`;
  const content = index === 20
    ? `## ${label}\n\n**Scoped markdown**\n\n[Unsafe link](javascript:alert("unsafe"))`
    : label;
  return item(`${prefix}:item-${index}`, `${String(index).padStart(4, '0')}:item-${index}`, content);
}

function fixtureTool(scope: TimelineScope, index: number, toolName: string, arguments_: Record<string, unknown>, output: string): TimelineItem {
  return {
    id: `${scope.key}:tool:${index}`,
    kind: 'tool_call',
    sort_key: `000${index}:tool`,
    source_refs: [{ store: 'fixture', session_id: scope.session_id ?? 'fixture-parent', seq: index, event_type: 'tool_call' }],
    created_at: NOW,
    updated_at: NOW,
    stable: true,
    status: 'complete',
    call_id: `call_fixture_${index}`,
    tool_name: toolName,
    arguments: arguments_,
    result_preview: output,
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: toolName === 'web_search',
    has_full_output: toolName === 'web_search',
  };
}

function visualFixtureItems(scope: TimelineScope): TimelineItem[] {
  return [
    item(`${scope.key}:visual-user`, '0000:user', 'Please inspect the native presentation.', 'user'),
    {
      id: `${scope.key}:visual-todo`,
      kind: 'todo_state',
      sort_key: '0000:todo',
      source_refs: [],
      created_at: NOW,
      updated_at: NOW,
      stable: true,
      status: 'complete',
      todos: [{ content: 'Canonical state only', status: 'completed', priority: 'medium' }],
    },
    item(`${scope.key}:visual-assistant`, '0001:assistant', '## Native response\n\nThe semantic cards below are canonical.'),
    {
      ...item(`${scope.key}:visual-system`, '0002:system', 'Recovery completed; the canonical stream is healthy.', 'system'),
      notice_kind: 'model_recovery',
      attempt: 1,
      max_attempts: 2,
    },
    fixtureTool(scope, 3, 'web_search', { query: 'canonical Chat v2' }, '{"count":2,"results":["Selectors","Presentation"]}'),
    fixtureTool(scope, 4, 'bash', { description: 'Run focused tests', command: 'npm test' }, '32 tests passed'),
    fixtureTool(scope, 5, 'read', { file_path: 'ui/src/lib/chat-v2/types.ts' }, '1: export type TimelineItem = ...'),
    fixtureTool(scope, 6, 'memory_search', { query: 'native presentation' }, '{"count":1}'),
    fixtureTool(scope, 7, 'write_deliverable', { title: 'Review report', content: 'Approved' }, '{"deliverable_id":"dlv_fixture"}'),
    fixtureTool(scope, 8, 'artifact_read', { artifact_id: 'art_fixture', prompt: 'Inspect' }, '{"summary":"valid"}'),
    fixtureTool(scope, 9, 'read_tool_output', { call_id: 'call_fixture_5' }, 'Referenced file output'),
    fixtureTool(scope, 10, 'agent_conversation_create', { title: 'Managed review', agent_id: 'reviewer' }, '{"conversation_id":"conv_review"}'),
    fixtureTool(scope, 11, 'delegate', { title: 'Delegate review', task: 'Inspect parity' }, '{"session_id":"sess_review"}'),
    fixtureTool(scope, 12, 'skill_load', { skill_id: 'cognis-coding' }, '{"name":"Cognis Coding"}'),
    fixtureTool(scope, 13, 'request_user_input', { title: 'Release input', questions: [] }, '{"status":"waiting"}'),
    fixtureTool(scope, 14, 'mcp:unknown-provider:custom_operation', { query: 'generic integration' }, '{"ok":true}'),
    fixtureTool(scope, 15, 'mcp:slack-lumilens:conversations_search_messages', { query: 'release' }, '{"matches":2}'),
    {
      id: `${scope.key}:artifact`,
      kind: 'artifact',
      sort_key: '0007:artifact',
      source_refs: [],
      created_at: NOW,
      updated_at: NOW,
      stable: true,
      status: 'complete',
      artifact_id: 'art_fixture',
      filename: 'review.md',
      mime_type: 'text/markdown',
      size_bytes: 512,
      title: 'Independent review',
    },
    {
      id: `${scope.key}:delegation`,
      kind: 'delegation',
      sort_key: '0008:delegation',
      source_refs: [],
      created_at: NOW,
      updated_at: NOW,
      stable: true,
      status: 'complete',
      child_session_id: 'sess_fixture_child',
      agent_id: 'reviewer',
      title: 'Independent review',
      summary: 'Semantic and visual parity verified.',
      todos: [],
    },
    {
      id: `${scope.key}:question`,
      kind: 'question_set',
      sort_key: '0009:question',
      source_refs: [],
      created_at: NOW,
      updated_at: NOW,
      stable: true,
      status: 'waiting',
      request_id: 'req_fixture',
      title: 'Release decision',
      questions: [{ id: 'approve', question: 'Approve this presentation?', options: [{ id: 'yes', label: 'Approve' }], multiple: false, allow_custom: true, required: true }],
    },
  ];
}

function scopeFor(id: string): TimelineScope {
  if (id === 'task-step') return taskStepTimelineScope('fixture-task', 'fixture-step', 'fixture-conversation');
  if (id === 'child') return sessionTimelineScope('fixture-child', 'fixture-conversation');
  if (id === 'grandchild') return sessionTimelineScope('fixture-grandchild', 'fixture-conversation');
  return conversationTimelineScope('fixture-conversation');
}

export const fixtureScopes: Record<string, TimelineScope> = {
  parent: { ...scopeFor('parent'), label: 'Parent conversation' },
  child: { ...scopeFor('child'), label: 'Child delegated session', parent_session_id: 'fixture-parent' },
  grandchild: { ...scopeFor('grandchild'), label: 'Grandchild delegated session', parent_session_id: 'fixture-child' },
  'task-step': { ...scopeFor('task-step'), label: 'Task step' },
  missing: { ...taskStepTimelineScope('fixture-task', 'missing-step', 'fixture-conversation'), label: 'Missing task stream', missing_stream: true },
};

export class ScopedFixtureController {
  private client: CognisWebSocketClient;
  private pageByScope = new Map<string, { page: number; before: string }>();
  private serverCursorByScope = new Map<string, string>();
  private subscriptions = new Map<string, { cursor: string; count: number }>();
  private previousCursorByScope = new Map<string, string>();
  private readonly operations: string[] = [];
  private snapshotRevision = 0;
  private resetNextSync = false;
  private holdNextSnapshotFor: string | null = null;
  private heldSnapshot: { response: ChatSnapshot; resolve: (response: ChatSnapshot) => void } | null = null;
  constructor() {
    (globalThis as typeof globalThis & { WebSocket: unknown }).WebSocket = FixtureWebSocket as unknown as typeof WebSocket;
    FixtureWebSocket.handler = (socket, payload) => this.handleClientPayload(socket, payload);
    this.client = new CognisWebSocketClient({
      getSnapshot: () => ({ status: 'authenticated', initialized: true, expiresAt: null, error: null, user: { email: 'fixture@example.com', name: 'Fixture', role: 'user' } }),
      clear: () => undefined,
    });
  }

  readonly api = {
    snapshot: async (scope: TimelineScope): Promise<ChatSnapshot> => this.snapshot(scope),
    sync: async (scope: TimelineScope, cursor: string): Promise<ChatSyncResponse> => this.sync(scope, cursor),
    timeline: async (scope: TimelineScope, options: { before?: string | null } = {}): Promise<TimelineBackfillResponse> =>
      this.timeline(scope, options.before ?? null),
  };

  readonly realtime = {
    subscribe: (listener: (frame: ChatRealtimeFrame) => void): (() => void) => this.client.subscribe((event) => {
      if (event.type === 'chat_v2_frame') listener(event);
    }),
      acquireChatV2: (scope: TimelineScope, cursor: string): void => {
        this.client.acquireChatV2(scope, cursor);
     },
      updateChatV2Cursor: (scope: TimelineScope, cursor: string): void => {
        this.client.updateChatV2Cursor(scope, cursor);
     },
      releaseChatV2: (scopeKey: string): void => {
        this.client.releaseChatV2(scopeKey);
      },
      emitFrame: (frame: ChatRealtimeFrame): void => {
        const socket = FixtureWebSocket.instances.at(-1);
        socket?.receive(frame);
      },
      disconnect: (): void => {
        this.client.disconnect();
      },
      reAuthenticate: (): void => {
        this.client.reAuthenticate();
      },
  };

  triggerActiveFrame(scope: TimelineScope): void {
    const cursor = this.serverCursorByScope.get(scope.key) ?? `fixture-${scope.key}-cursor-${this.snapshotRevision}`;
    const nextCursor = `${cursor}:frame`;
    this.previousCursorByScope.set(scope.key, cursor);
    this.serverCursorByScope.set(scope.key, nextCursor);
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'fixture-projection-v1',
      scope,
      conversation_id: 'fixture-conversation',
      cursor_before: cursor,
      cursor_after: nextCursor,
      ops: [{ op: 'upsert_item', item: item(`${scope.key}:live-${Date.now()}`, '9999:live', `${scope.label} live frame`) }],
      runtime: null,
      reset_required: false,
      server_time: NOW,
    } as ChatRealtimeFrame;
    FixtureWebSocket.instances.at(-1)?.receive(frame);
  }

  triggerStaleFrame(scope: TimelineScope): void {
    const cursor = this.previousCursorByScope.get(scope.key) ?? `stale:${scope.key}`;
    this.emitFrame(scope, cursor, `${cursor}:stale`, 'stale frame');
  }

  triggerCrossScopeFrame(scope: TimelineScope): void {
    const other = scope.key === fixtureScopes.parent.key ? fixtureScopes.child : fixtureScopes.parent;
    this.emitFrame(other, this.serverCursorByScope.get(other.key) ?? `cross:${other.key}`, `cross:${other.key}`, 'cross-scope frame');
  }

  private emitFrame(scope: TimelineScope, cursor: string, nextCursor: string, label: string): void {
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'fixture-projection-v1',
      scope,
      conversation_id: 'fixture-conversation',
      cursor_before: cursor,
      cursor_after: nextCursor,
      ops: [{ op: 'upsert_item', item: item(`${scope.key}:${label}:${Date.now()}`, '9999:live', `${scope.label} ${label}`) }],
      runtime: null,
      server_time: NOW,
    } as ChatRealtimeFrame;
    FixtureWebSocket.instances.at(-1)?.receive(frame);
  }

  triggerCursorReset(): void {
    this.resetNextSync = true;
    this.snapshotRevision = 1;
    const scope = fixtureScopes.parent;
    const frame: ChatRealtimeFrame = {
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'fixture-projection-v1',
      scope,
      conversation_id: 'fixture-conversation',
      cursor_before: 'stale-cursor',
      cursor_after: 'stale-next',
      ops: [],
      runtime: null,
      server_time: NOW,
    };
    FixtureWebSocket.instances.at(-1)?.receive(frame);
  }

  holdNextRefreshSnapshot(): void {
    this.holdNextSnapshotFor = fixtureScopes.parent.key;
  }

  resolveHeldRefreshSnapshot(): void {
    const held = this.heldSnapshot;
    this.heldSnapshot = null;
    held?.resolve(held.response);
  }

  private snapshot(scope: TimelineScope): Promise<ChatSnapshot> {
    const missing = scope.missing_stream === true;
    const items = missing ? [] : [
      ...visualFixtureItems(scope),
      ...Array.from({ length: 70 }, (_, index) => scopeItem(scope, index + 20)),
    ];
    const cursor = `fixture-${scope.key}-cursor-${this.snapshotRevision}`;
    this.serverCursorByScope.set(scope.key, cursor);
    const response: ChatSnapshot = {
      schema_version: 2,
      projection_version: 'fixture-projection-v1',
      scope,
      conversation: { conversation_id: 'fixture-conversation', agent_id: 'fixture-agent', title: scope.label, status: 'active' },
      timeline: { items, has_more_before: !missing, before_cursor: missing ? null : `${cursor}:page:1` },
      state: { state_version: 1, snapshot_generated_at: NOW, capabilities: [], active_turn: {}, pending: {}, active_session: {} },
      queue: { messages: [], queued_count: 0 },
      runtime: { runtime_epoch: 'fixture-epoch', runtime_revision: this.snapshotRevision, generated_at: NOW, has_active_turn: false, volatile_items: [] },
      cursor,
      server_time: NOW,
    };
    // Different response latencies make rapid scope changes exercise stale
    // response rejection rather than relying on a keyed component remount.
    if (this.holdNextSnapshotFor === scope.key) {
      this.holdNextSnapshotFor = null;
      return new Promise<ChatSnapshot>((resolve) => {
        this.heldSnapshot = { response, resolve };
      });
    }
    const delay = scope.key === fixtureScopes.parent.key ? 35 : scope.key === fixtureScopes.child.key ? 5 : 12;
    return new Promise<ChatSnapshot>((resolve) => window.setTimeout(() => resolve(response), delay));
  }

  private sync(scope: TimelineScope, cursor: string): ChatSyncResponse {
    if (this.resetNextSync) {
      this.resetNextSync = false;
      return {
        schema_version: 2, projection_version: 'fixture-projection-v1', scope, conversation_id: 'fixture-conversation',
        cursor_before: cursor, cursor_after: cursor, ops: [], reset_required: true, reset_reason: 'cursor_invalid',
        has_more: false, server_time: NOW,
      };
    }
    return {
      schema_version: 2, projection_version: 'fixture-projection-v1', scope, conversation_id: 'fixture-conversation',
      cursor_before: cursor, cursor_after: `${cursor}-synced`, ops: [{ op: 'upsert_item', item: scopeItem(scope, 999) }],
      reset_required: false, has_more: false, server_time: NOW,
    };
  }

  private handleClientPayload(_socket: FixtureWebSocket, payload: Record<string, unknown>): void {
    const scope = payload.scope as TimelineScope | undefined;
    const scopeKey = typeof payload.scope_key === 'string'
      ? payload.scope_key
      : scope?.key ?? (typeof payload.conversation_id === 'string' ? `conversation:${payload.conversation_id}` : null);
    if (!scopeKey) return;
    if (payload.type === 'chat_v2_subscribe' && typeof payload.cursor === 'string') {
      const current = this.subscriptions.get(scopeKey);
      this.subscriptions.set(scopeKey, { cursor: payload.cursor, count: (current?.count ?? 0) + 1 });
      this.operations.push(`subscribe:${scopeKey}:${payload.cursor}`);
    } else if (payload.type === 'reconnect' && typeof payload.chat_v2_cursor === 'string') {
      this.subscriptions.set(scopeKey, { cursor: payload.chat_v2_cursor, count: 1 });
      this.operations.push(`reconnect:${scopeKey}:${payload.chat_v2_cursor}`);
    } else if (payload.type === 'chat_v2_unsubscribe') {
      const current = this.subscriptions.get(scopeKey);
      if (current && current.count > 1) this.subscriptions.set(scopeKey, { ...current, count: current.count - 1 });
      else this.subscriptions.delete(scopeKey);
      this.operations.push(`unsubscribe:${scopeKey}`);
    } else if (payload.type === 'pong') {
      return;
    }
  }

  private timeline(scope: TimelineScope, before: string | null): TimelineBackfillResponse {
    const current = this.pageByScope.get(scope.key);
    const expectedBefore = current?.before ?? `${this.serverCursorByScope.get(scope.key) ?? `fixture-${scope.key}-cursor-${this.snapshotRevision}`}:page:1`;
    if (before !== expectedBefore) throw new Error(`cross-scope or stale backfill cursor for ${scope.key}`);
    const page = (current?.page ?? 1) + 1;
    const hasMore = page < 3;
    const nextBefore = hasMore ? `opaque:${scope.key}:history:${page + 1}` : null;
    this.pageByScope.set(scope.key, { page, before: nextBefore ?? expectedBefore });
    return {
      schema_version: 2, projection_version: 'fixture-projection-v1', scope, conversation_id: 'fixture-conversation',
       items: Array.from({ length: 8 }, (_, index) => scopeItem(scope, Math.max(0, 20 - page * 8 + index))),
       has_more_before: hasMore, before_cursor: nextBefore, server_time: NOW,
    };
  }

  get activeSubscriptions(): string[] {
    return Array.from(this.subscriptions.keys()).sort();
  }

  get operationLog(): string[] {
    return [...this.operations];
  }
}
