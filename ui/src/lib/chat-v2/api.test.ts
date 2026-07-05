import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChatV2ApiClient, ChatV2ApiError } from './api';

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
      body: undefined
    });
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
});
