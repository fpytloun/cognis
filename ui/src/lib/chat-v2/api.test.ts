import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChatV2ApiClient, ChatV2ApiError } from './api';
import { sessionTimelineScope, taskStepTimelineScope } from './types';
import { selectedWorkSubtreeScope } from '$lib/inspectorTreeNavigation';

const jsonHeaders = { 'Content-Type': 'application/json' };

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200, headers: jsonHeaders });
}

describe('ChatV2ApiClient', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('requests snapshots from the v2 route', async () => {
    const payload = { schema_version: 2, cursor: 'cursor-1' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.snapshot('conv-1')).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/chat/v2/conversations/conv-1/snapshot', {
      credentials: 'include',
      headers: new Headers(),
      body: undefined,
      signal: expect.any(AbortSignal)
    });
  });

  it('returns a warmed cache-only snapshot', async () => {
    const payload = { schema_version: 2, cursor: 'cursor-1' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.snapshotCacheOnly('conv-1')).resolves.toEqual(payload);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/conversations/conv-1/snapshot/cache-only'
    );
  });

  it.each([204, 404, 405, 500])(
    'returns null when the cache-only probe responds with %s',
    async (status) => {
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status }));
      const client = new ChatV2ApiClient({ fetch: fetchMock });

      await expect(client.snapshotCacheOnly('conv-1')).resolves.toBeNull();
    }
  );

  it.each([401, 403])('preserves cache-only authorization failure %s', async (status) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status }));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.snapshotCacheOnly('conv-1')).rejects.toMatchObject({ status });
  });

  it('emits bounded client performance best-effort with keepalive', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.clientPerformance('cached_restore_ms', 25)).resolves.toBeUndefined();

    const [, init] = fetchMock.mock.calls[0]!;
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/chat/v2/client-performance');
    expect(init?.method).toBe('POST');
    expect(init?.credentials).toBe('include');
    expect(init?.keepalive).toBe(true);
    expect(JSON.parse(String(init?.body))).toEqual({
      metric: 'cached_restore_ms',
      duration_ms: 25
    });

    fetchMock.mockRejectedValueOnce(new Error('offline'));
    await expect(client.clientPerformance('timeline_fresh_ms', 50)).resolves.toBeUndefined();
  });

  it('sends idempotent messages with PUT and JSON payload', async () => {
    const payload = {
      status: 'accepted',
      client_txn_id: 'txn-1',
      client_message_id: 'client-1',
      conversation_id: 'conv-1',
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(
      client.sendMessage('conv-1', 'txn-1', {
        client_message_id: 'client-1',
        content: 'hello',
        attachments: [],
        chat_mode: null
      })
    ).resolves.toEqual(payload);

    const [, init] = fetchMock.mock.calls[0]!;
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/chat/v2/conversations/conv-1/messages/txn-1');
    expect(init?.method).toBe('PUT');
    expect(init?.credentials).toBe('include');
    expect((init?.headers as Headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual({
      client_message_id: 'client-1',
      content: 'hello',
      attachments: [],
      chat_mode: null
    });
  });

  it('executes slash commands through the idempotent Chat v2 route', async () => {
    const payload = {
      conversation_id: 'conv-1',
      client_txn_id: 'txn-command',
      status: 'completed',
      result_type: 'conversation_created',
      text: 'Conversation forked.',
      data: { conversation_id: 'conv-2' },
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.executeCommand('conv-1', 'txn-command', '/fork new topic')).resolves.toEqual(payload);

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/chat/v2/conversations/conv-1/commands/txn-command');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({ content: '/fork new topic' });
  });

  it('forks a conversation from a completed assistant message', async () => {
    const payload = {
      conversation_id: 'conv-fork',
      session_id: 'session-fork',
      source_session_id: 'session-source',
      source_seq: 42,
      copied: true,
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(
      client.forkAssistantMessage('conv-1', {
        source_session_id: 'session-source',
        source_seq: 42
      })
    ).resolves.toEqual(payload);

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/chat/v2/conversations/conv-1/assistant-messages/fork');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      source_session_id: 'session-source',
      source_seq: 42
    });
  });

  it('serializes sync query parameters', async () => {
    const payload = {
      schema_version: 2,
      projection_version: 'v',
      conversation_id: 'conv-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [],
      reset_required: false,
      has_more: false,
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.sync('conv-1', 'cursor-1', { limit: 50 })).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/conversations/conv-1/sync?cursor=cursor-1&limit=50'
    );
  });

  it('routes session and task-step scopes to their native endpoints', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({}));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await client.snapshot(sessionTimelineScope('sess-1', 'conv-1'));
    await client.timeline(taskStepTimelineScope('task-1', 'step-1', 'conv-1'), { before: 'older', limit: 25 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/chat/v2/sessions/sess-1/snapshot');
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      '/api/v1/chat/v2/task-steps/step-1/timeline?before=older&limit=25'
    );
  });

  it('serializes an exact selected session separately from its task-step scope', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}));
    const client = new ChatV2ApiClient({ fetch: fetchMock });
    await client.work({
      key: 'task_step:run-1',
      kind: 'task_step',
      step_run_id: 'run-1',
      conversation_id: 'conv-1',
      session_id: 'session-descendant-b',
    }, { category: 'mutations', before: 'cursor-1', sessionId: 'session-a' });
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/task-steps/run-1/work?before=cursor-1&category=mutations&session_id=session-a'
    );
  });

  it('loads a selected session subtree without an exact-session filter', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await client.work(
      selectedWorkSubtreeScope('managed-conversation-1', 'session-child'),
      { category: 'files' },
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/sessions/session-child/work?category=files'
    );
  });

  it('loads tool output from exact conversation, session, and task-step scopes', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockImplementation(async () => jsonResponse({ content: 'full output' }));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await client.toolOutputPage('conv-1', 'call/1', { offset: 2, limit: 20 });
    await client.toolOutputPage(sessionTimelineScope('sess-1', 'conv-1'), 'call/1');
    await client.toolOutputPage(taskStepTimelineScope('task-1', 'step-1', 'conv-1'), 'call/1');

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/chat/v2/conversations/conv-1/tool-outputs/call%2F1?offset=2&limit=20',
      '/api/v1/chat/v2/sessions/sess-1/tool-outputs/call%2F1',
      '/api/v1/chat/v2/task-steps/step-1/tool-outputs/call%2F1',
    ]);
  });

  it('sends queued-message delete transaction id as a query parameter', async () => {
    const payload = {
      conversation_id: 'conv-1',
      client_txn_id: 'delete-1',
      status: 'deleted',
      queue: { messages: [], queued_count: 0 },
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(
      client.deleteQueuedMessage('conv-1', 'queue-1', { client_txn_id: 'delete-1' })
    ).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/conversations/conv-1/queue/queue-1?client_txn_id=delete-1'
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE');
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined();
  });

  it('retries failed turns with POST and no message payload', async () => {
    const payload = {
      conversation_id: 'conv-1',
      client_txn_id: 'retry-1',
      turn_id: 'turn-1',
      status: 'accepted',
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(
      client.retryTurn('conv-1', 'turn-1', { client_txn_id: 'retry-1' })
    ).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/conversations/conv-1/turns/turn-1/retry'
    );
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.method).toBe('POST');
    expect((init?.headers as Headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual({ client_txn_id: 'retry-1' });
  });

  it('updates queued messages with PATCH and JSON payload', async () => {
    const payload = {
      conversation_id: 'conv-1',
      client_txn_id: 'update-1',
      status: 'updated',
      queue: {
        messages: [{ queue_id: 'queue-1', content: 'updated', attachments: [], position: 0 }],
        queued_count: 1
      },
      server_time: '2026-01-01T00:00:00Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(jsonResponse(payload));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(
      client.updateQueuedMessage('conv-1', 'queue-1', {
        client_txn_id: 'update-1',
        content: 'updated'
      })
    ).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/chat/v2/conversations/conv-1/queue/queue-1'
    );
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.method).toBe('PATCH');
    expect((init?.headers as Headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual({
      client_txn_id: 'update-1',
      content: 'updated'
    });
  });

  it('throws structured API errors', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: 'cursor_invalid', message: 'bad cursor' } }), {
        status: 400,
        headers: jsonHeaders
      })
    );
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    await expect(client.snapshot('conv-1')).rejects.toMatchObject({
      code: 'cursor_invalid',
      status: 400,
      message: 'bad cursor'
    } satisfies Partial<ChatV2ApiError>);
  });

  it('turns stalled requests into structured timeout errors', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    }));
    const client = new ChatV2ApiClient({ fetch: fetchMock });

    const promise = client.snapshot('conv-1').catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(30_000);

    await expect(promise).resolves.toMatchObject({
      code: 'request_timeout',
      status: 0
    } satisfies Partial<ChatV2ApiError>);
    vi.useRealTimers();
  });
});
