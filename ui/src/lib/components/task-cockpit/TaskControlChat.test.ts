import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  ChatSnapshot,
  ChatSyncResponse,
  SendMessageV2Request,
  SendMessageV2Response,
  TimelineScope
} from '$lib/chat-v2/types';
import type { TaskControlChatResponse } from '$lib/types/api';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { createIndexedDbChatV2Outbox } from '$lib/chat-v2/outbox';

const api = vi.hoisted(() => ({
  snapshot: vi.fn(),
  sync: vi.fn(),
  timeline: vi.fn(),
  sendMessage: vi.fn(),
  upload: vi.fn(),
  signedUrl: vi.fn()
}));
const slashCommandSuggestions = vi.hoisted(() => vi.fn());

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: api
}));

vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: vi.fn(() => () => undefined),
    acquireChatV2: vi.fn(),
    updateChatV2Cursor: vi.fn(),
    releaseChatV2: vi.fn(),
    consumeCatchUp: vi.fn(() => null),
    reconnectNow: vi.fn()
  },
  wsState: { subscribe: (run: (value: unknown) => void) => { run({ status: 'connected', attempts: 0, lastError: null }); return () => undefined; } }
}));

vi.mock('$lib/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('$lib/api/client')>(),
  api: {
    notifications: { list: vi.fn(async () => []) },
    conversations: {
      slashCommandSuggestions,
      backgroundWork: vi.fn(async () => ({ background_work_changed: true, background_work: { items: [], truncated: false } })),
    },
    artifacts: {
      upload: api.upload,
      signedUrl: api.signedUrl,
    },
  },
}));
vi.mock('$lib/feedback/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/feedback/api')>();
  return { ...original, feedbackApi: { ...original.feedbackApi,
    staging: vi.fn(async (conversation_id: string) => ({ conversation_id, version: 1, items: [], receipts: [] })),
  } };
});
vi.mock('$lib/chat-v2/attachment-uploads', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/chat-v2/attachment-uploads')>();
  return { ...original, attachmentUploads: new original.AttachmentUploadManager(undefined, blob => api.upload(blob)) };
});

import TaskControlChat from './TaskControlChat.svelte';

function chat(taskId: string): TaskControlChatResponse {
  return {
    task_id: taskId,
    conversation_id: `conv-${taskId}`,
    session_id: `session-${taskId}`,
    agent_id: 'laforge',
    agent_profile_id: 'developer',
    task_status: 'running',
    attempt_number: 2
  };
}

function snapshot(scope: TimelineScope): ChatSnapshot {
  const conversationId = scope.conversation_id ?? scope.key;
  return {
    schema_version: 2,
    projection_version: 'task-control-test',
    scope,
    conversation: {
      conversation_id: conversationId,
      agent_id: 'laforge',
      status: 'active'
    },
    timeline: { items: [], has_more_before: false, before_cursor: null },
    state: {
      state_version: 1,
      snapshot_generated_at: '2026-08-01T00:00:00Z',
      capabilities: [],
      active_turn: {},
      pending: {},
      active_session: {},
      task: null
    },
    queue: { messages: [], queued_count: 0 },
    runtime: {
      runtime_epoch: 'epoch-1',
      runtime_revision: 0,
      generated_at: '2026-08-01T00:00:00Z',
      has_active_turn: false,
      active_turn: null,
      volatile_items: []
    },
    cursor: `cursor:${conversationId}:1`,
    server_time: '2026-08-01T00:00:00Z'
  };
}

function syncResponse(scope: TimelineScope): ChatSyncResponse {
  const conversationId = scope.conversation_id ?? scope.key;
  return {
    schema_version: 2,
    projection_version: 'task-control-test',
    scope,
    conversation_id: conversationId,
    cursor_before: `cursor:${conversationId}:1`,
    cursor_after: `cursor:${conversationId}:2`,
    ops: [],
    runtime: null,
    reset_required: false,
    reset_reason: null,
    has_more: false,
    server_time: '2026-08-01T00:00:01Z'
  };
}

function admission(
  conversationId: string,
  clientTxnId: string,
  payload: SendMessageV2Request,
  status: 'accepted' | 'queued'
): SendMessageV2Response {
  return {
    status,
    client_txn_id: clientTxnId,
    client_message_id: payload.client_message_id,
    conversation_id: conversationId,
    message_id: status === 'accepted' ? 'message-1' : null,
    queue_id: status === 'queued' ? 'queue-1' : null,
    cursor: `cursor:${conversationId}:1`,
    server_time: '2026-08-01T00:00:01Z'
  };
}

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});
beforeEach(loginFixtureAccount);
afterEach(async () => { await auth.clear(); });

beforeEach(() => {
  localStorage.clear();
  api.snapshot.mockImplementation(async (scope: TimelineScope) => snapshot(scope));
  api.sync.mockImplementation(async (scope: TimelineScope) => syncResponse(scope));
  api.timeline.mockResolvedValue({ items: [], has_more_before: false, before_cursor: null });
  api.sendMessage.mockImplementation(
    async (conversationId: string, clientTxnId: string, payload: SendMessageV2Request) =>
      admission(conversationId, clientTxnId, payload, 'accepted')
  );
  api.upload.mockResolvedValue({
    artifact_id: 'artifact-pasted',
    filename: 'pasted.png',
    mime_type: 'image/png',
    size_bytes: 5,
  });
  slashCommandSuggestions.mockResolvedValue({ items: [] });
  api.signedUrl.mockResolvedValue({ url: 'https://example.test/pasted.png' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('TaskControlChat', () => {
  it('scopes dynamic slash suggestions to the task conversation', async () => {
    render(TaskControlChat, { chat: chat('task-a') });
    const composer = await screen.findByTestId('task-control-composer');

    await fireEvent.input(composer, { target: { value: '/profile ' } });

    await waitFor(() => {
      expect(slashCommandSuggestions).toHaveBeenCalledWith(
        'conv-task-a',
        '/profile ',
        12,
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
    });
  });

  it('embeds the native Chat v2 timeline and composer without an iframe shell', async () => {
    render(TaskControlChat, { chat: chat('task-39') });

    expect(screen.getByTestId('task-control-native-chat')).toBeInTheDocument();
    expect(screen.getByTestId('task-control-composer')).toBeVisible();
    expect(document.querySelector('iframe')).not.toBeInTheDocument();
    await waitFor(() => expect(api.snapshot).toHaveBeenCalled());
  });

  it.each(['accepted', 'queued'] as const)(
    'reconciles an %s admission and clears the composer without a WebSocket frame',
    async (status) => {
      api.sendMessage.mockImplementation(
        async (conversationId: string, clientTxnId: string, payload: SendMessageV2Request) =>
          admission(conversationId, clientTxnId, payload, status)
      );
      const onSent = vi.fn();
      render(TaskControlChat, { chat: chat('task-a'), onSent });
      await waitFor(() =>
        expect(document.querySelector('[data-cursor="cursor:conv-task-a:1"]')).toBeInTheDocument()
      );
      const composer = screen.getByTestId('task-control-composer');
      await fireEvent.input(composer, { target: { value: 'Status update' } });
      await fireEvent.click(screen.getByRole('button', { name: 'Send task control message' }));

      await waitFor(() => expect(api.sync).toHaveBeenCalled());
      await waitFor(() => expect(composer).toHaveValue(''));
      await waitFor(() => expect(onSent).toHaveBeenCalledOnce());
    }
  );

  it('retains network-failed admission under the original durable transaction', async () => {
    api.sendMessage.mockRejectedValue(new Error('Controller unavailable'));
    render(TaskControlChat, { chat: chat('task-a') });
    const composer = screen.getByTestId('task-control-composer');
    await fireEvent.input(composer, { target: { value: 'Keep this draft' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send task control message' }));

    expect(await screen.findByText('Message saved. It will be sent automatically when the connection recovers.')).toBeVisible();
    const rows = (await createIndexedDbChatV2Outbox().list('conv-task-a'))
      .filter(row => row.client_message_id === api.sendMessage.mock.calls[0][2].client_message_id);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ content: 'Keep this draft', client_txn_id: api.sendMessage.mock.calls[0][1] });
    expect(composer).toHaveValue('');
  });

  it('keeps a failed post-admission cursor sync unreconciled until explicit snapshot recovery', async () => {
    api.sync.mockRejectedValueOnce(new Error('WebSocket disconnected'));
    render(TaskControlChat, { chat: chat('task-a') });
    await waitFor(() => expect(document.querySelector('[data-cursor="cursor:conv-task-a:1"]'))
      .toHaveAttribute('data-freshness', 'current'));
    const composer = screen.getByTestId('task-control-composer');
    await fireEvent.input(composer, { target: { value: 'Reconnect safely' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send task control message' }));

    await waitFor(() => expect(api.sync).toHaveBeenCalledOnce());
    expect(api.sync).toHaveBeenCalledWith(
      expect.objectContaining({ key: 'conversation:conv-task-a' }),
      'cursor:conv-task-a:1', expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('WebSocket disconnected');
    expect(document.querySelector('[data-cursor="cursor:conv-task-a:1"]'))
      .toHaveAttribute('data-freshness', 'reconciling');
    expect(api.snapshot).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(api.snapshot).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelector('[data-cursor="cursor:conv-task-a:1"]'))
      .toHaveAttribute('data-freshness', 'current'));
    expect(screen.queryByRole('alert')).toBeNull();
    await waitFor(() => expect(composer).toHaveValue(''));
  });

  it('ignores a late task A admission after rebinding to task B', async () => {
    let resolveAdmission: ((response: SendMessageV2Response) => void) | undefined;
    let captured: [string, string, SendMessageV2Request] | undefined;
    api.sendMessage.mockImplementation(
      (conversationId: string, clientTxnId: string, payload: SendMessageV2Request) => {
        captured = [conversationId, clientTxnId, payload];
        return new Promise<SendMessageV2Response>((resolve) => {
          resolveAdmission = resolve;
        });
      }
    );
    const props = { chat: chat('task-a') };
    const { rerender } = render(TaskControlChat, props);
    const composer = screen.getByTestId('task-control-composer');
    await fireEvent.input(composer, { target: { value: 'Task A message' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send task control message' }));
    await waitFor(() => expect(captured).toBeDefined());
    await rerender({ chat: chat('task-b') });
    await fireEvent.input(composer, { target: { value: 'Task B draft' } });

    if (!captured || !resolveAdmission) throw new Error('Admission was not captured');
    resolveAdmission(admission(...captured, 'accepted'));
    await Promise.resolve();

    expect(composer).toHaveValue('Task B draft');
    expect(api.sync).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('uploads a pasted file, renders its preview, and sends the attachment', async () => {
    render(TaskControlChat, { chat: chat('task-a') });
    const composer = screen.getByTestId('task-control-composer');
    const file = new File(['image'], 'pasted.png', { type: 'image/png' });
    const clipboardData = {
      files: [file],
      items: [],
    } as unknown as DataTransfer;

    await fireEvent.paste(composer, { clipboardData });

    await waitFor(() => expect(api.upload).toHaveBeenCalledWith(file));
    expect(await screen.findByRole('img', { name: 'pasted.png' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Send task control message' }));

    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledWith(
      'conv-task-a',
      expect.any(String),
      expect.objectContaining({
        content: '',
        attachments: [expect.objectContaining({ artifact_id: 'artifact-pasted' })],
      }),
    ));
  });
});
