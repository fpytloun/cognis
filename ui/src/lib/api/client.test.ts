import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';

const jsonHeaders = { 'Content-Type': 'application/json' };

describe('api client session handling', () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    auth.clear();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('clears auth state when a protected request returns 401', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            user: { email: 'user@example.com', name: 'User', role: 'user' },
            expires_at: new Date(Date.now() + 3_600_000).toISOString()
          }),
          { status: 200, headers: jsonHeaders }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: 'unauthorized', message: 'expired' } }), {
          status: 401,
          headers: jsonHeaders
        })
      );

    global.fetch = fetchMock;

    await auth.login('user@example.com', 'password123');
    await expect(api.auth.me()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(auth.getSnapshot().status).toBe('anonymous');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('conversation API client', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('requests projected conversation context types without loading all conversations', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(['signal', 'web']), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(api.conversations.contextTypes({ status: 'archived' })).resolves.toEqual([
      'signal',
      'web'
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/conversations/context-types?status=archived');
  });

  it('requests a backend-shaped sidebar projection', async () => {
    const payload = {
      agents: [],
      agent_direct_chats: [],
      conversations: { items: [], cursor: null, has_more: false },
      context_types: ['web']
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(
      api.conversations.sidebar(null, {
        contextType: 'web',
        agentId: 'agent-1',
        status: 'active'
      })
    ).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/conversations/sidebar?limit=50&context_type=web&agent_id=agent-1&status=active'
    );
  });

  it('requests projected conversation timeline pages', async () => {
    const payload = {
      items: [],
      timeline_items: [],
      last_seq: 0,
      has_more: false,
      has_active_turn: false
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(api.conversations.timelinePage('conv_1', 100, 'cursor-old')).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/conversations/conv_1/timeline?after_seq=0&limit=100&anchor=latest&before=cursor-old'
    );
  });

  it('requests backend chat open resolution with ordered candidates', async () => {
    const payload = {
      conversation_id: 'conv-selected',
      user_email: 'user@example.test',
      agent_id: 'agent-1',
      project_id: null,
      title: 'Selected',
      title_source: 'manual',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
      active_session_id: null,
      active_executor_id: null,
      active_executor_assigned_at: null,
      active_executor_expires_at: null,
      active_executor_source: null,
      active_session_status: null,
      active_session_completion_reason: null,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      pending_notification_types: [],
      starred_at: null,
      status: 'active',
      last_message_at: null,
      last_read_at: null,
      has_unread: false,
      has_active_turn: false,
      created_at: null,
      updated_at: null,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(
      api.conversations.open({
        agent_id: 'agent-1',
        context_type: 'web',
        candidate_conversation_ids: ['conv-other', 'conv-selected']
      })
    ).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/conversations/open');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({
        agent_id: 'agent-1',
        context_type: 'web',
        candidate_conversation_ids: ['conv-other', 'conv-selected']
      })
    });
  });

  it('marks a conversation as opened through the backend', async () => {
    const payload = { conversation_id: 'conv-opened' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(api.conversations.rememberOpened('conv-opened')).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/conversations/conv-opened/opened');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: 'POST' });
  });
});
