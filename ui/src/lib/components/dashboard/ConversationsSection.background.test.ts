import { cleanup, render, waitFor, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import type { Conversation } from '$lib/types/api';
import ConversationsSection from './ConversationsSection.svelte';

const mocks = vi.hoisted(() => ({ backgroundWork: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ user: { email: 'owner@cognis.test' } }) };
});
vi.mock('$lib/api/client', () => ({ api: { conversations: { backgroundWork: mocks.backgroundWork } } }));
vi.mock('$lib/ws/client', () => ({ wsClient: { subscribe: () => () => {} } }));
afterEach(cleanup);

it('moves an unread Waiting snapshot into Active now while its child is running', async () => {
  mocks.backgroundWork.mockResolvedValue({
    background_work_changed: true,
    background_work: { items: [{
      kind: 'managed_conversation', status: 'running', controller_conversation_id: 'parent',
    }] },
  });
  const parent = {
    conversation_id: 'parent', agent_id: 'riker', title: 'Waiting for child',
    context: { type: 'web' }, has_active_turn: false, has_unread: true,
    pending_notification_types: [],
  } as unknown as Conversation;
  const view = render(ConversationsSection, {
    conversations: [parent], waitingConversations: [parent], agents: [], onOpenConversation: vi.fn(),
  });
  await waitFor(() => {
    const active = within(view.getByTestId('dashboard-conversations-active'));
    expect(active.getByText('Waiting for child')).toBeInTheDocument();
    expect(active.getByLabelText('Background work active')).toBeInTheDocument();
  });
  expect(view.queryByTestId('dashboard-conversations-waiting')).not.toBeInTheDocument();
  await view.rerender({ waitingConversations: [{ ...parent, pending_notification_types: ['step_question'] }] });
  expect(within(view.getByTestId('dashboard-conversations-waiting')).getByText('Waiting for child')).toBeInTheDocument();
});
