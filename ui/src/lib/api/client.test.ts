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

describe('executor API client', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('attaches the observed config generation to concurrent config writes', async () => {
    const executor = {
      executor_id: 'exec-1',
      desired_config_version: 4
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify([executor]), { status: 200, headers: jsonHeaders })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...executor, desired_config_version: 5 }),
          { status: 200, headers: jsonHeaders }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: {
              code: 'executor_config_conflict',
              message: 'Executor configuration changed; reload it before saving'
            }
          }),
          { status: 409, headers: jsonHeaders }
        )
      );
    global.fetch = fetchMock;

    await api.executor.list();
    const results = await Promise.allSettled([
      api.executor.update('exec-1', { config: { lsp_enabled: true } }),
      api.executor.update('exec-1', { config: { local_inference_enabled: false } })
    ]);

    expect(results.map((result) => result.status)).toEqual(['fulfilled', 'rejected']);
    const firstBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    const secondBody = JSON.parse(String(fetchMock.mock.calls[2][1]?.body));
    expect(firstBody.expected_config_version).toBe(4);
    expect(secondBody.expected_config_version).toBe(4);
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

  it('serializes multi-select sidebar filters as repeated query parameters', async () => {
    const payload = {
      agents: [],
      agent_direct_chats: [],
      conversations: { items: [], cursor: null, has_more: false },
      context_types: ['agent_work', 'web']
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
        contextTypes: ['web', 'agent_work'],
        agentIds: ['laforge', 'riker'],
        status: 'active'
      })
    ).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/conversations/sidebar?limit=50&context_types=web&context_types=agent_work&agent_ids=laforge&agent_ids=riker&status=active'
    );
  });

  it('serializes sidebar delta cursor timestamps', async () => {
    const payload = {
      agents: [],
      agent_direct_chats: [],
      conversations: { items: [], cursor: null, has_more: false },
      context_types: [],
      removed_conversation_ids: [],
      full_resync_required: false,
      sync_timestamp: '2026-01-01T00:00:05Z'
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(
      api.conversations.sidebar(null, { contextType: 'web' }, {
        changedSince: '2026-01-01T00:00:00Z'
      })
    ).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/conversations/sidebar?limit=50&changed_since=2026-01-01T00%3A00%3A00Z&context_type=web'
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
        include_state: false,
        candidate_conversation_ids: ['conv-other', 'conv-selected'],
        candidate_conversations: [
          {
            conversation_id: 'conv-selected',
            opened_at: '2026-06-22T10:00:00.000Z',
          }
        ]
      })
    ).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/conversations/open');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({
        agent_id: 'agent-1',
        context_type: 'web',
        include_state: false,
        candidate_conversation_ids: ['conv-other', 'conv-selected'],
        candidate_conversations: [
          {
            conversation_id: 'conv-selected',
            opened_at: '2026-06-22T10:00:00.000Z',
          }
        ]
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

  it('can request lightweight conversation detail without legacy state', async () => {
    const payload = { conversation_id: 'conv-lightweight' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(api.conversations.detail('conv-lightweight', { includeState: false })).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/conversations/conv-lightweight?include_state=false');
  });
});

describe('knowledgebase API client contracts', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('preserves nested relative paths in multipart ingestion', async () => {
    const response = { outcomes: [] };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(response), { status: 200, headers: jsonHeaders })
    );
    global.fetch = fetchMock;
    const files = [new File(['a'], 'a.md'), new File(['b'], 'b.md')];

    await api.knowledgebases.documents.upload(
      'kb_1',
      files,
      ['guides/setup/a.md', 'reference/nested/b.md'],
      'replace'
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/knowledgebases/kb_1/documents');
    const body = fetchMock.mock.calls[0]?.[1]?.body as FormData;
    expect(body.getAll('paths[]')).toEqual(['guides/setup/a.md', 'reference/nested/b.md']);
    expect(body.getAll('files[]')).toHaveLength(2);
    expect(body.get('conflict_policy')).toBe('replace');
  });

  it('uses backend document paging and content query contracts', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ documents: [], next_cursor: null }), { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        kb_artifact_id: 'kba_1', artifact_id: 'art_1', source_path: 'a.md',
        content_mode: 'extracted', mime_type: 'text/markdown', text: 'A',
        size_bytes: 1, extraction_method: 'markdown', diagnostics: {}
      }), { status: 200, headers: jsonHeaders }));
    global.fetch = fetchMock;

    await api.knowledgebases.documents.list('kb_1', { sort: 'updated_at', direction: 'desc', limit: 25 });
    await api.knowledgebases.documents.content('kb_1', 'kba_1', 'source');

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/knowledgebases/kb_1/documents?sort=updated_at&direction=desc&limit=25');
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/knowledgebases/kb_1/documents/kba_1/content?content_mode=source');
  });

  it('uses exact direct-sharing paths and payloads', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('[]', { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response('[]', { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: 'Reader',
        permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
      }), { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response('{"revoked":true}', { status: 200, headers: jsonHeaders }));
    global.fetch = fetchMock;
    await api.knowledgebases.shares('kb_1');
    await api.knowledgebases.shareCandidates('kb_1', 're');
    await api.knowledgebases.grantShare('kb_1', { user_email: 'reader@example.com', permission: 'view' });
    await api.knowledgebases.revokeShare('kb_1', 'reader@example.com');
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/knowledgebases/kb_1/shares',
      '/api/v1/knowledgebases/kb_1/shares/candidates?q=re',
      '/api/v1/knowledgebases/kb_1/shares',
      '/api/v1/knowledgebases/kb_1/shares/reader%40example.com'
    ]);
    expect(fetchMock.mock.calls[2]?.[1]?.method).toBe('PUT');
    expect(fetchMock.mock.calls[2]?.[1]?.body).toBe('{"user_email":"reader@example.com","permission":"view"}');
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe('DELETE');
  });

  it('posts typed facet requests with cancellation support', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify({ fields: [] }), { status: 200, headers: jsonHeaders })
    );
    global.fetch = fetchMock;
    const controller = new AbortController();
    await api.knowledgebases.facets(
      'kb_1',
      {
        fields: ['category'],
        filters: [{ field: 'active', op: 'eq', value: true }],
        limit_per_field: 25
      },
      { signal: controller.signal }
    );
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/knowledgebases/kb_1/facets');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST', signal: controller.signal,
      body: '{"fields":["category"],"filters":[{"field":"active","op":"eq","value":true}],"limit_per_field":25}'
    });
  });
});

describe('long-running API client endpoints', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('does not apply the default UI-load timeout to image generation', async () => {
    const payload = { image_id: 'img_1', url: '/api/v1/images/img_1', prompt_used: 'cat' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: jsonHeaders
      })
    );
    global.fetch = fetchMock;

    await expect(api.images.generate('cat')).resolves.toEqual(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/images/generate');
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBeUndefined();
  });
});

describe('settings API client', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('resets a setting with DELETE and returns the refreshed setting', async () => {
    const payload = {
      key: 'session.timeout',
      value: 30,
      category: 'session',
      section: 'Lifecycle',
      label: 'Session timeout',
      description: 'How long sessions remain active.',
      default_value: 30,
      value_type: 'integer',
      options: null,
      minimum: 5,
      maximum: 120,
      unit: 'minutes',
      is_overridden: false,
      apply_scope: 'new sessions',
      updated_by: null,
      updated_at: null
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(JSON.stringify(payload), { status: 200, headers: jsonHeaders })
    );
    global.fetch = fetchMock;

    await expect(api.settings.reset('session.timeout')).resolves.toEqual(payload);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/settings/session.timeout');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: 'DELETE' });
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined();
  });
});
