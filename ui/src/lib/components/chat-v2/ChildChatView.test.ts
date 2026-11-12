import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { WorkstreamRef } from '$lib/chat-v2/types';
import type { ChatSnapshot } from '$lib/chat-v2/types';
import type { Conversation } from '$lib/types/api';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import ChildChatView from './ChildChatView.svelte';

const node: WorkstreamRef = {
  key: 'child', root_key: 'root', parent_key: 'root', kind: 'delegate',
  edge_kind: 'delegate', ordinal: 1, session_id: 'session-child',
  event_store_session_id: 'session-child', title: 'Child session', agent_id: 'worker',
  status: 'completed', current: false, superseded: false, activity_state: 'closed',
};

describe('ChildChatView', () => {
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });
  it('renders header actions, scoped todos, and preserves independent callbacks', async () => {
    const onBack = vi.fn();
    const onClose = vi.fn();
    const onToggleInspector = vi.fn();
    render(ChildChatView, {
      view: { kind: 'delegate', conversationId: 'root', sessionId: 'session-child', controllerRootConversationId: 'root', nodeKey: 'child' },
      node,
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: true,
      timelineApi: {
        snapshot: vi.fn().mockResolvedValue({
          schema_version: 2, projection_version: 'test',
          scope: { key: 'session:session-child', kind: 'session', session_id: 'session-child', conversation_id: 'root' },
          conversation: { conversation_id: 'root' },
          timeline: {
            items: [{
              id: 'todo:child', kind: 'todo_state', sort_key: '0001',
              source_refs: [], stable: true,
              todos: [{ content: 'Inspect child output', status: 'in_progress', priority: 'normal' }],
            }, {
              id: 'message:child', kind: 'message', sort_key: '0002',
              source_refs: [], stable: true, role: 'assistant', content: 'Child content',
            }],
            has_more_before: false, before_cursor: null,
          },
          state: { state_version: 1, snapshot_generated_at: '', capabilities: [], active_turn: {}, pending: {}, active_session: {} },
          queue: { messages: [], queued_count: 0 },
          runtime: {
            has_active_turn: false,
            active_turn: null,
            volatile_items: [],
            cycle_states: [],
          },
          cursor: 'cursor-1', server_time: '',
        } as unknown as ChatSnapshot),
        sync: vi.fn(),
        timeline: vi.fn(),
      },
      timelineRealtime: {
        subscribe: () => () => {},
        acquireChatV2: vi.fn(),
        updateChatV2Cursor: vi.fn(),
        releaseChatV2: vi.fn(),
      },
      onBack,
      onClose,
      onToggleInspector,
      onViewSession: vi.fn(),
    });
    expect(screen.getByRole('heading', { name: 'Child session' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Info' })).toBeNull();
    const info = screen.getByRole('button', { name: 'Toggle session details' });
    expect(info).toHaveAttribute('aria-expanded', 'true');
    expect(info.querySelector('svg')).toHaveClass('lucide-info');
    await fireEvent.click(info);
    expect(onToggleInspector).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole('link', { name: 'Back to parent conversation' }));
    expect(onBack).toHaveBeenCalledOnce();
    expect(screen.getByRole('link', { name: 'Back to parent conversation' })).toHaveAttribute('href', '/chat/root');
    expect(screen.getByRole('link', { name: 'Back to parent conversation' })).not.toHaveTextContent('Parent');
    expect(screen.queryByRole('button', { name: 'Close child conversation' })).not.toBeInTheDocument();
    await screen.findByText('Child content');
    await fireEvent.click(screen.getByRole('button', { name: 'Search in conversation' }));
    const search = await screen.findByPlaceholderText('Search loaded subsession history');
    await fireEvent.input(search, { target: { value: 'Child content' } });
    await fireEvent.keyDown(search, { key: 'Escape' });
    expect(screen.queryByPlaceholderText('Search loaded subsession history')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Search in conversation' })).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
    expect(onBack).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole('button', { name: /Ongoing work/ })).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: /Ongoing work/ }));
    expect(screen.getAllByText('Inspect child output')).toHaveLength(1);
    expect(screen.getByRole('button', { name: /Ongoing work/ }).closest('footer'))
      .toBe(screen.getByTestId('child-footer'));
  });

  it('forwards runtime activity and clears it on unmount', async () => {
    const onRuntimeActiveChange = vi.fn();
    const { unmount } = render(ChildChatView, {
      view: { kind: 'delegate', conversationId: 'root', sessionId: 'session-child', controllerRootConversationId: 'root', nodeKey: 'child' },
      node: { ...node, status: 'active', activity_state: 'active' },
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: true,
      timelineApi: {
        snapshot: vi.fn().mockResolvedValue({
          schema_version: 2, projection_version: 'test',
          scope: { key: 'session:session-child', kind: 'session', session_id: 'session-child', conversation_id: 'root' },
          conversation: { conversation_id: 'root' },
          timeline: { items: [], has_more_before: false, before_cursor: null },
          state: { state_version: 1, snapshot_generated_at: '', capabilities: [], active_turn: {}, pending: {}, active_session: {} },
          queue: { messages: [], queued_count: 0 },
          runtime: { has_active_turn: true, active_turn: { turn_id: 'turn-1' }, volatile_items: [], cycle_states: [] },
          cursor: 'cursor-active', server_time: '',
        } as unknown as ChatSnapshot),
        sync: vi.fn(),
        timeline: vi.fn(),
      },
      timelineRealtime: {
        subscribe: () => () => {},
        acquireChatV2: vi.fn(),
        updateChatV2Cursor: vi.fn(),
        releaseChatV2: vi.fn(),
      },
      onBack: vi.fn(),
      onClose: vi.fn(),
      onToggleInspector: vi.fn(),
      onViewSession: vi.fn(),
      onRuntimeActiveChange,
    });
    await waitFor(() => expect(onRuntimeActiveChange).toHaveBeenCalledWith(true));
    unmount();
    expect(onRuntimeActiveChange).toHaveBeenLastCalledWith(false);
  });

  it.each([
    ['running', 'Running'],
    ['queued', 'Queued'],
    ['waiting', 'Waiting'],
    ['recovering', 'Recovering'],
    ['idle', 'Idle'],
    ['completed', 'Completed'],
  ] as const)('uses canonical %s state instead of old session completion', (executionState, label) => {
    render(ChildChatView, {
      view: { kind: 'delegate', conversationId: 'root', sessionId: 'session-child', controllerRootConversationId: 'root', nodeKey: 'child' },
      node: { ...node, execution_state: executionState },
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: true,
      timelineApi: { snapshot: vi.fn(() => new Promise<ChatSnapshot>(() => {})), sync: vi.fn(), timeline: vi.fn() },
      timelineRealtime: {
        subscribe: () => () => {},
        acquireChatV2: vi.fn(), updateChatV2Cursor: vi.fn(), releaseChatV2: vi.fn(),
      },
      onBack: vi.fn(), onClose: vi.fn(), onToggleInspector: vi.fn(), onViewSession: vi.fn(),
    });
    const header = screen.getByTestId('child-header');
    expect(header).toHaveTextContent(label);
    expect(header).not.toHaveTextContent('Closed');
    expect(header).not.toHaveTextContent('Following latest');
    expect(header).not.toHaveTextContent('Live follow paused');
    expect(header.querySelectorAll('.animate-bounce')).toHaveLength(0);
  });

  it('renders terminated as closed without live-follow state', () => {
    render(ChildChatView, {
      view: { kind: 'delegate', conversationId: 'root', sessionId: 'session-child', controllerRootConversationId: 'root', nodeKey: 'child' },
      node: { ...node, status: 'terminated', activity_state: 'ongoing' },
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: true,
      onBack: vi.fn(),
      onClose: vi.fn(),
      onToggleInspector: vi.fn(),
      onViewSession: vi.fn(),
    });
    expect(screen.getByText('Closed')).toBeTruthy();
    expect(screen.queryByText('Running')).toBeNull();
    expect(screen.queryByText('Following latest')).toBeNull();
  });

  it('shows the conversation title, inline agent identity and only Search/Info actions', () => {
    const onViewSession = vi.fn();
    render(ChildChatView, {
      view: {
        kind: 'managed',
        conversationId: 'managed/conversation',
        sessionId: 'session-child',
        controllerRootConversationId: 'root',
        nodeKey: 'child',
      },
      node: { ...node, kind: 'managed', conversation_id: 'managed/conversation' },
      managedConversation: { conversation_id: 'managed/conversation', title: 'Actual target conversation title' } as Conversation,
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: true,
      onBack: vi.fn(),
      onClose: vi.fn(),
      onToggleInspector: vi.fn(),
      onViewSession,
    });
    expect(screen.getByRole('heading', { name: 'Actual target conversation title' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /in new window/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy canonical session ID' })).not.toBeInTheDocument();
    expect(screen.getByTestId('child-header').querySelectorAll('button')).toHaveLength(2);
    expect(onViewSession).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Toggle session details' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'Search in conversation' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Close child conversation' })).not.toBeInTheDocument();
  });

  it('restores the root parent through the host and keeps running progress in the footer', async () => {
    const onClose = vi.fn();
    const onBack = vi.fn();
    render(ChildChatView, {
      view: {
        kind: 'managed', conversationId: 'managed-child', sessionId: 'session-child',
        controllerRootConversationId: 'root', nodeKey: 'child',
      },
      node: { ...node, kind: 'managed', conversation_id: 'managed-child', execution_state: 'running' },
      managedConversation: {
        conversation_id: 'managed-child', has_active_turn: true,
        managed_agent: { controller_conversation_id: 'root', conversation_state: 'open', turn_state: 'running' },
      } as Conversation,
      preferences: DEFAULT_USER_PREFERENCES, inspectorOpen: false,
      onBack, onClose, onToggleInspector: vi.fn(), onViewSession: vi.fn(),
      onManagedStop: vi.fn(), onManagedSend: vi.fn(), onManagedTakeControl: vi.fn(),
    });
    expect(screen.getByTestId('managed-running-indicator')).toHaveTextContent(/^running$/);
    expect(screen.queryByTestId('timeline-running-indicator')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('link', { name: 'Parent conversation' }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(onBack).not.toHaveBeenCalled();
  });

  it('does not render a new-window link for a delegate child', () => {
    render(ChildChatView, {
      view: {
        kind: 'delegate',
        conversationId: 'root',
        sessionId: 'session-child',
        controllerRootConversationId: 'root',
        nodeKey: 'child',
      },
      node,
      preferences: DEFAULT_USER_PREFERENCES,
      inspectorOpen: false,
      onBack: vi.fn(),
      onClose: vi.fn(),
      onToggleInspector: vi.fn(),
      onViewSession: vi.fn(),
    });
    expect(screen.queryByRole('link', { name: 'Open Child session in new window' })).toBeNull();
  });
});
