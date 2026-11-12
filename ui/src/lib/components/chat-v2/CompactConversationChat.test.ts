import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
import { sendOutboxEntry } from '$lib/chat-v2/outbox-send';

import CompactConversationChat from './CompactConversationChat.svelte';
import { sessionTimelineScope } from '$lib/chat-v2/types';
import { dispatchChatComposerMessage, normalizeChatComposerInput } from '$lib/chat-v2/composer-dispatch';
import { chatV2Api } from '$lib/chat-v2/api';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  dispatch: vi.fn(),
  backgroundWork: vi.fn(),
  cancel: vi.fn(),
}));
vi.mock('$lib/api/client', () => ({
  api: {
    artifacts: { upload: vi.fn() },
    conversations: { backgroundWork: mocks.backgroundWork },
  },
}));
vi.mock('$lib/chat-v2/outbox', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/chat-v2/outbox')>();
  const outbox = () => {
    const rows = new Map<string, OutboxEntry>();
    return {
    list: async (...args: unknown[]) => {
      const entries: OutboxEntry[] = await mocks.list(...args);
      entries.forEach((entry) => {
        if (!rows.has(entry.client_txn_id)) rows.set(entry.client_txn_id, entry);
      });
      return entries.map((entry) => rows.get(entry.client_txn_id)!);
    },
    get: async (id: string) => rows.get(id) ?? null,
    update: vi.fn(async (id: string, changes: Partial<OutboxEntry>) => {
      const entry = rows.get(id);
      if (entry) rows.set(id, { ...entry, ...changes });
    }),
    delete: vi.fn(async () => undefined),
    isDurable: () => true,
  };
  };
  return {
    ...original,
    MemoryChatV2Outbox: class { constructor() { return outbox(); } },
    createMemoryChatV2Outbox: outbox,
    createIndexedDbChatV2Outbox: outbox,
  };
});
vi.mock('$lib/chat-v2/composer-dispatch', () => ({
  dispatchChatComposerMessage: vi.fn(),
  normalizeChatComposerInput: vi.fn(),
}));
// Queued messages are sent by the outbox drain straight to the message API.
vi.mock('$lib/chat-v2/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/chat-v2/api')>();
  return {
    ...original,
    chatV2Api: {
      ...original.chatV2Api,
      cancelTurn: mocks.cancel,
      sendMessage: (...args: unknown[]) => mocks.dispatch(...args).then(
        (result: { response?: unknown } | undefined) => result?.response ?? {},
      ),
    },
  };
});

vi.mock('$lib/components/chat-v2/ScopedChatV2Timeline.svelte', async () => (
  import('./CompactConversationChat.timeline-test-fixture.svelte')
));
vi.mock('$lib/components/chat-v2/ConversationPendingInteractions.svelte', async () => (
  import('./CompactConversationChat.interactions-test-fixture.svelte')
));
vi.mock('$lib/components/chat-v2/QueuedMessagesPanel.svelte', async () => (
  import('./CompactConversationChat.queue-test-fixture.svelte')
));
vi.mock('$lib/components/chat-v2/ChatV2Composer.svelte', async () => (
  import('./CompactConversationChat.composer-test-fixture.svelte')
));

describe('CompactConversationChat auxiliary content', () => {
  it('does not report execution stopped when its active view is destroyed', async () => {
    const observed = vi.fn();
    const view = render(CompactConversationChat, { conversationId: 'runtime-retained-conversation', onRuntimeActiveChange: observed });
    await fireEvent.click(screen.getByRole('button', { name: 'Activate fixture turn' }));
    expect(observed).toHaveBeenLastCalledWith(true);
    observed.mockClear();
    view.unmount();
    expect(observed).not.toHaveBeenCalled();
  });
  afterEach(cleanup);
  beforeEach(loginFixtureAccount);
  afterEach(async () => { await auth.clear(); });
  beforeEach(() => {
    vi.mocked(dispatchChatComposerMessage).mockReset();
    // Composite autosave also calls normalization; unrelated UI tests need
    // its real total function rather than an undefined-returning mock.
    vi.mocked(normalizeChatComposerInput).mockReset().mockImplementation(({ content }) => ({
      kind: 'message', content, chatMode: undefined,
    }));
    mocks.list.mockReset().mockResolvedValue([]);
    mocks.dispatch.mockReset();
    mocks.cancel.mockReset().mockResolvedValue({});
    mocks.backgroundWork.mockReset().mockResolvedValue({
      background_work_changed: true,
      background_work: {
        items: [{
          kind: 'task',
          work_id: 'task-one',
          task_id: 'task-one',
          controller_conversation_id: 'conversation-one',
          controller_session_id: 'session-root',
          title: 'Investigate task activity',
          agent_id: 'riker',
          agent_profile_id: 'smart',
          status: 'running',
          todos: [],
        }, {
          kind: 'managed_conversation',
          work_id: 'managed-one',
          controller_conversation_id: 'conversation-one',
          controller_session_id: 'session-root',
          target_conversation_id: 'conversation-child',
          title: 'Managed follow-up',
          agent_id: 'riker',
          status: 'running',
          todos: [],
        }, {
          kind: 'background_command',
          work_id: 'command-one',
          controller_conversation_id: 'conversation-one',
          controller_session_id: 'session-root',
          title: 'Build frontend',
          agent_id: 'riker',
          status: 'running',
          todos: [],
        }, {
          kind: 'delegated_session',
          work_id: 'session-child',
          controller_conversation_id: 'conversation-one',
          parent_session_id: 'session-root',
          session_id: 'session-child',
          title: 'Stale projected title',
          agent_id: 'riker',
          status: 'running',
          todos: [],
        }, {
          kind: 'background_command',
          work_id: 'other-root-command',
          controller_conversation_id: 'conversation-one',
          controller_session_id: 'session-other-root',
          title: 'Other root command',
          agent_id: 'riker',
          status: 'running',
          todos: [],
        }, {
          kind: 'background_command',
          work_id: 'other-command',
          controller_conversation_id: 'conversation-other',
          title: 'Other conversation command',
          agent_id: 'riker',
          status: 'running',
          todos: [],
        }],
        active_count: 6,
        truncated: false,
        generated_at: '2026-09-10T07:51:00Z',
      },
    });
  });
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });

  it('clears a completed command when changing timeline scope without changing conversation', async () => {
    vi.mocked(normalizeChatComposerInput).mockReturnValue({ kind: 'command', content: '/status', chatMode: undefined });
    vi.mocked(dispatchChatComposerMessage).mockResolvedValue({
      kind: 'command',
      response: { status: 'completed', text: 'Parent conversation status' },
    } as Awaited<ReturnType<typeof dispatchChatComposerMessage>>);
    const view = render(CompactConversationChat, { conversationId: 'conversation-one' });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: '/status' } });
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    expect(await screen.findByTestId('compact-command-result')).toHaveTextContent('Parent conversation status');
    await view.rerender({
      conversationId: 'conversation-one',
      timelineScope: sessionTimelineScope('session-child', 'conversation-one'),
    });
    expect(screen.queryByTestId('compact-command-result')).not.toBeInTheDocument();
  });

  it('does not publish a pending parent command into a newly selected session view', async () => {
    let resolveCommand!: (result: Awaited<ReturnType<typeof dispatchChatComposerMessage>>) => void;
    vi.mocked(normalizeChatComposerInput).mockReturnValue({ kind: 'command', content: '/status', chatMode: undefined });
    vi.mocked(dispatchChatComposerMessage).mockReturnValue(new Promise(resolve => { resolveCommand = resolve; }));
    const onCommandResponse = vi.fn();
    const view = render(CompactConversationChat, { conversationId: 'conversation-one', onCommandResponse });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: '/status' } });
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await view.rerender({
      conversationId: 'conversation-one',
      timelineScope: sessionTimelineScope('session-child', 'conversation-one'),
    });
    resolveCommand({
      kind: 'command',
      response: { status: 'completed', text: 'Late parent conversation status' },
    } as Awaited<ReturnType<typeof dispatchChatComposerMessage>>);
    await tick();
    expect(screen.queryByTestId('compact-command-result')).not.toBeInTheDocument();
    expect(onCommandResponse).not.toHaveBeenCalled();
  });

  it('ignores a late stop error from a previous conversation and releases its busy state', async () => {
    let rejectStop!: (error: Error) => void;
    mocks.cancel.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectStop = reject; }));
    const view = render(CompactConversationChat, { conversationId: 'conversation-one' });
    await fireEvent.click(screen.getByRole('button', { name: 'Activate fixture turn' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Stop fixture turn' }));
    expect(screen.getByRole('button', { name: 'Stop fixture turn' })).toBeDisabled();
    expect(chatV2Api.cancelTurn).toHaveBeenCalledWith('conversation-one', expect.any(Object));
    await view.rerender({ conversationId: 'conversation-two' });
    expect(screen.getByRole('button', { name: 'Stop fixture turn' })).not.toBeDisabled();
    rejectStop(new Error('Previous conversation cancellation failed'));
    await tick();
    expect(screen.queryByText('Previous conversation cancellation failed')).not.toBeInTheDocument();
  });

  it('renders one editable queue inside a bounded panel while preserving timeline and composer', async () => {
    render(CompactConversationChat, {
      conversationId: 'conversation-one',
      controllerSessionIds: ['session-root'],
      embedded: true,
    });

    expect(await screen.findByText('Queued once')).toBeInTheDocument();
    expect(screen.getAllByText('Queued once')).toHaveLength(1);
    expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-show-queue', 'false');
    expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-show-todo-drawer', 'false');
    expect(screen.getByTestId('pending-interactions-fixture')).toBeInTheDocument();
    expect(screen.getByTestId('pending-interactions-fixture')).toHaveAttribute(
      'data-presentation',
      'floating',
    );
    const overlay = screen.getByTestId('compact-chat-pending-overlay');
    expect(overlay).toHaveClass('absolute', 'pointer-events-none', 'z-30');
    expect(overlay).toContainElement(screen.getByTestId('pending-interactions-fixture'));
    expect(screen.getByTestId('editable-queue-fixture')).toBeInTheDocument();
    expect(screen.getByTestId('compact-chat-auxiliary'))
      .not.toContainElement(screen.getByTestId('pending-interactions-fixture'));
    expect(screen.getByTestId('compact-chat-auxiliary')).toHaveClass(
      'shrink-0',
      'overflow-y-auto',
    );
    expect(screen.getByTestId('compact-timeline-fixture')).toHaveClass('min-h-0', 'flex-1');
    expect(screen.getByTestId('compact-composer-fixture')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Ongoing work/ })).toBeInTheDocument();
    const ongoing = await screen.findByRole('button', { name: /Ongoing work · 4 running · 1 task · 2 sessions · 1 command · 1 todo/ });
    expect(ongoing).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Ongoing work/ })).toHaveLength(1);
    await fireEvent.click(ongoing);
    expect(screen.getByText('Investigate task activity')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open Investigate task activity' })).toHaveAttribute(
      'href',
      '/tasks/task-one',
    );
  });

  it('does not rotate the replacement tab when a destroyed chat command completes late', async () => {
    let resolve!: (result: Awaited<ReturnType<typeof dispatchChatComposerMessage>>) => void;
    vi.mocked(normalizeChatComposerInput).mockReturnValue({ kind: 'command', content: '/new', chatMode: undefined });
    vi.mocked(dispatchChatComposerMessage).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const onRotation = vi.fn();
    const chatA = render(CompactConversationChat, { conversationId: 'conversation-a', onCommandResponse: onRotation });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: '/new' } });
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(dispatchChatComposerMessage).toHaveBeenCalledOnce());
    chatA.unmount();
    render(CompactConversationChat, { conversationId: 'conversation-b', onCommandResponse: onRotation });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: 'Draft in B' } });
    resolve({ kind: 'command', clientTxnId: 'txn-rotate-a', response: {
      conversation_id: 'conversation-a', client_txn_id: 'txn-rotate-a', status: 'completed',
      result_type: 'new_conversation', text: 'New conversation created',
      data: { conversation_id: 'conversation-new' }, server_time: '2026-10-14T08:00:00Z',
    } });
    await tick();
    await tick();
    expect(onRotation).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Composer text')).toHaveValue('Draft in B');
  });

  it('does not expose parent conversation controls while viewing a child session', async () => {
    render(CompactConversationChat, {
      conversationId: 'conversation-one',
      timelineScope: sessionTimelineScope('session-child', 'conversation-one'),
      embedded: true,
    });

    expect(await screen.findByTestId('compact-timeline-fixture')).toBeInTheDocument();
    expect(screen.queryByTestId('compact-composer-fixture')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pending-interactions-fixture')).not.toBeInTheDocument();
    expect(screen.queryByTestId('editable-queue-fixture')).not.toBeInTheDocument();
    window.dispatchEvent(new Event('online'));
    await tick();
    expect(mocks.list).not.toHaveBeenCalled();
    expect(mocks.dispatch).not.toHaveBeenCalled();
  });

  it.each(['list', 'dispatch'] as const)('isolates a scope switch during outbox %s', async (phase) => {
    const entries = [1, 2].map((id) => ({
      conversation_id: 'conversation-one',
      owner: documentCoordination().capture().owner,
      content: `Pending ${id}`,
      status: 'pending',
      created_at: '2026-08-26T00:00:00Z',
      client_txn_id: `txn-${id}`,
      client_message_id: `message-${id}`,
      attachments: [],
    }));
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    mocks.list.mockImplementation(async () => {
      if (phase === 'list') await pending;
      return entries;
    });
    mocks.dispatch.mockImplementation(async () => {
      if (phase === 'dispatch') await pending;
      return { kind: 'message', response: {} };
    });
    const view = render(CompactConversationChat, { conversationId: 'conversation-one' });
    await waitFor(() => expect(phase === 'list' ? mocks.list : mocks.dispatch).toHaveBeenCalledOnce());
    await view.rerender({
      conversationId: 'conversation-one',
      timelineScope: sessionTimelineScope('session-child', 'conversation-one'),
    });
    release();
    await pending;
    await tick();
    expect(mocks.dispatch).toHaveBeenCalledTimes(phase === 'list' ? 0 : 1);
    expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-admissions', '0');
    expect(screen.queryByTestId('editable-queue-fixture')).not.toBeInTheDocument();
  });

  it('restarts a superseded drain after switching away and back during list', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const entry = {
      conversation_id: 'conversation-one', owner: documentCoordination().capture().owner, content: 'Pending', status: 'pending',
      created_at: '2026-08-26T00:00:00Z', client_txn_id: 'txn-1', client_message_id: 'message-1',
    };
    mocks.list.mockImplementationOnce(async () => {
      await pending;
      return [entry];
    }).mockResolvedValue([entry]);
    mocks.dispatch.mockResolvedValue({ kind: 'message', response: {} });
    const view = render(CompactConversationChat, { conversationId: 'conversation-one' });
    await waitFor(() => expect(mocks.list).toHaveBeenCalledOnce());
    await view.rerender({
      conversationId: 'conversation-one',
      timelineScope: sessionTimelineScope('session-child', 'conversation-one'),
    });
    await view.rerender({ conversationId: 'conversation-one', timelineScope: undefined });
    expect(mocks.list).toHaveBeenCalledOnce();
    release();
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-admissions', '1'));
    expect(mocks.dispatch).toHaveBeenCalledOnce();
  });

  it('reconciles pending messages in the editable conversation scope', async () => {
    mocks.list.mockResolvedValue([{
      conversation_id: 'conversation-one', owner: documentCoordination().capture().owner, content: 'Pending', status: 'pending',
      created_at: '2026-08-26T00:00:00Z', client_txn_id: 'txn-1', client_message_id: 'message-1',
    }]);
    mocks.dispatch.mockResolvedValue({ kind: 'message', response: {} });
    render(CompactConversationChat, { conversationId: 'conversation-one' });
    await waitFor(() => expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-admissions', '1'));
  });

  it('retains admission receipts across repeated mock lists and sends only once', async () => {
    const entry: OutboxEntry = {
      conversation_id: 'conversation-one', owner: documentCoordination().capture().owner!,
      content: 'Pending', status: 'pending', created_at: '2026-08-26T00:00:00Z',
      updated_at: '2026-08-26T00:00:00Z', client_txn_id: 'txn-repeat',
      client_message_id: 'message-repeat', attachments: [],
    };
    mocks.list.mockResolvedValue([entry]);
    mocks.dispatch.mockResolvedValue({ response: {} });
    const outbox = createIndexedDbChatV2Outbox();
    await outbox.list('conversation-one');
    await sendOutboxEntry(outbox, entry);
    const [reloaded] = await outbox.list('conversation-one');
    expect(reloaded.status).toBe('acked');
    expect(reloaded.admitted).toBeDefined();
    await sendOutboxEntry(outbox, reloaded);
    expect(mocks.dispatch).toHaveBeenCalledOnce();
  });
});
