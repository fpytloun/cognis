import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { LibraryItem } from '$lib/library';
import DocumentConversation from './DocumentConversation.svelte';
import { auth } from '$lib/stores/auth';

const mocks = vi.hoisted(() => ({
  detail: vi.fn(), background: vi.fn(), agent: vi.fn(), listener: null as null | ((event: any) => void),
}));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ status: 'authenticated', user: { email: 'owner@cognis.test' } }) };
});
const actor = auth as unknown as Writable<{ status: string; user: { email: string } }>;
vi.mock('$lib/api/client', () => ({ api: {
  conversations: { detail: mocks.detail, backgroundWork: mocks.background },
  agents: { detail: mocks.agent },
} }));
vi.mock('$lib/ws/client', async () => ({ wsState: (await import('svelte/store')).writable({ status: 'disconnected' }),
  wsClient: { subscribe: (listener: (event: any) => void) => {
  mocks.listener = listener; return () => { mocks.listener = null; };
} } }));
const item = { conversation_id: 'conv_review', conversation_title: 'Release review' } as LibraryItem;
const conversation = { conversation_id: 'conv_review', agent_id: 'forge', title: 'Release review',
  context: { type: 'web' }, has_active_turn: true, has_unread: false, pending_notification_types: [] };
beforeEach(() => {
  vi.resetAllMocks();
  actor.set({ status: 'authenticated', user: { email: 'owner@cognis.test' } });
  mocks.detail.mockResolvedValue(conversation);
  mocks.agent.mockResolvedValue({ agent_id: 'forge', name: 'forge', display_name: 'Forge', avatar_url: null });
  mocks.background.mockResolvedValue({ background_work: { items: [] } });
});
afterEach(cleanup);

it('shows live work and waiting states beside the conversation identity', async () => {
  render(DocumentConversation, { item });
  expect(await screen.findByText('Forge', { exact: true })).toBeVisible();
  expect(screen.getByText('Control chat is working', { exact: true })).toBeVisible();
  expect(screen.getByTestId('activity-avatar-orbit')).toBeVisible();
  mocks.detail.mockResolvedValue({ ...conversation, has_active_turn: false, pending_notification_types: ['step_question'] });
  mocks.listener!({ type: 'conversation_updated', conversation_id: 'conv_review',
    has_active_turn: false, pending_notification_types: ['step_question'] });
  expect(await screen.findByText('Waiting for answer', { exact: true })).toBeVisible();
  expect(screen.queryByTestId('activity-avatar-orbit')).not.toBeInTheDocument();
  expect(screen.getByTestId('activity-avatar-attention')).toBeVisible();
});

it('shows background work after the source chat settles and refreshes after invalidation', async () => {
  mocks.detail.mockResolvedValue({ ...conversation, has_active_turn: false });
  mocks.background.mockResolvedValue({ background_work: { items: [{
    controller_conversation_id: 'conv_review', status: 'running', kind: 'task',
  }] } });
  render(DocumentConversation, { item });
  expect(await screen.findByText('Background work active', { exact: true })).toBeVisible();
  mocks.background.mockResolvedValue({ background_work: { items: [] } });
  mocks.listener!({ type: 'work_invalidated' });
  expect(await screen.findByText('No new activity', { exact: true })).toBeVisible();
});

it('clears stale identity on authority change and does not revive a late detail response', async () => {
  let complete!: (value: typeof conversation) => void;
  mocks.detail.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  render(DocumentConversation, { item });
  await waitFor(() => expect(mocks.listener).not.toBeNull());
  actor.set({ status: 'unauthenticated', user: { email: '' } });
  await waitFor(() => expect(mocks.listener).toBeNull());
  complete(conversation);
  await Promise.resolve();
  expect(screen.queryByText('Forge', { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByTestId('activity-avatar')).not.toBeInTheDocument();
});

it('shows an honest unavailable state rather than treating inaccessible work as idle', async () => {
  mocks.detail.mockRejectedValue(new Error('Conversation is unavailable.'));
  render(DocumentConversation, { item });
  expect(await screen.findByRole('alert')).toHaveTextContent('Conversation is unavailable.');
  expect(screen.queryByText('No new activity')).not.toBeInTheDocument();
  expect(screen.getByRole('link')).toHaveAttribute('href', '/chat/conv_review');
});

it('retains newer activity and agent patches across overlapping invalidations and stale reads', async () => {
  render(DocumentConversation, { item });
  await screen.findByText('Forge', { exact: true });
  const reads: Array<(value: typeof conversation) => void> = [];
  mocks.detail.mockImplementation(() => new Promise(resolve => { reads.push(resolve); }));
  mocks.agent.mockImplementation(async (id: string) => ({ agent_id: id, name: id, display_name: id === 'mira' ? 'Mira' : 'Forge' }));
  mocks.listener!({ type: 'conversation_updated', conversation_id: 'conv_review',
    has_active_turn: false, pending_notification_types: ['step_question'], agent_id: 'mira' });
  mocks.listener!({ type: 'scope_invalidated', reason: 'notification_state_changed', conversation_id: 'conv_review' });
  reads[1](conversation);
  expect(await screen.findByText('Mira', { exact: true })).toBeVisible();
  expect(screen.getByText('Waiting for answer', { exact: true })).toBeVisible();
  reads[0](conversation);
  await Promise.resolve();
  expect(screen.getByText('Mira', { exact: true })).toBeVisible();
  expect(screen.queryByText('Control chat is working', { exact: true })).not.toBeInTheDocument();
});
