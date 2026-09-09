import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';

import CompactConversationChat from './CompactConversationChat.svelte';
import { sessionTimelineScope } from '$lib/chat-v2/types';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  dispatch: vi.fn(),
}));
vi.mock('$lib/chat-v2/outbox', () => ({
  MemoryChatV2Outbox: class { list = mocks.list; },
  createIndexedDbChatV2Outbox: () => ({ list: mocks.list }),
}));
vi.mock('$lib/chat-v2/composer-dispatch', () => ({
  dispatchChatComposerMessage: mocks.dispatch,
  normalizeChatComposerInput: vi.fn(),
}));

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
  afterEach(cleanup);
  beforeEach(() => {
    mocks.list.mockReset().mockResolvedValue([]);
    mocks.dispatch.mockReset();
  });
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      disconnect() {}
    });
  });

  it('renders one editable queue inside a bounded panel while preserving timeline and composer', async () => {
    render(CompactConversationChat, {
      conversationId: 'conversation-one',
      embedded: true,
    });

    expect(await screen.findByText('Queued once')).toBeInTheDocument();
    expect(screen.getAllByText('Queued once')).toHaveLength(1);
    expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-show-queue', 'false');
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
      conversation_id: 'conversation-one', content: 'Pending', status: 'pending',
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
      conversation_id: 'conversation-one', content: 'Pending', status: 'pending',
      created_at: '2026-08-26T00:00:00Z', client_txn_id: 'txn-1', client_message_id: 'message-1',
    }]);
    mocks.dispatch.mockResolvedValue({ kind: 'message', response: {} });
    render(CompactConversationChat, { conversationId: 'conversation-one' });
    await waitFor(() => expect(screen.getByTestId('compact-timeline-fixture')).toHaveAttribute('data-admissions', '1'));
  });
});
