import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { WorkstreamRef } from '$lib/chat-v2/types';
import type { ChatSnapshot } from '$lib/chat-v2/types';
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
      view: { kind: 'delegate', sessionId: 'session-child', controllerRootConversationId: 'root', nodeKey: 'child' },
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
    await fireEvent.click(screen.getByRole('button', { name: 'Collapse conversation inspector' }));
    expect(onToggleInspector).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole('button', { name: 'Back to parent conversation' }));
    expect(onBack).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole('button', { name: 'Close child conversation' }));
    expect(onClose).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole('button', { name: /Ongoing work/ })).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: /Ongoing work/ }));
    expect(screen.getAllByText('Inspect child output').length).toBeGreaterThan(0);
  });
});
