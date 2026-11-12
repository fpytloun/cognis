import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';
import 'fake-indexeddb/auto';
import type { Conversation } from '$lib/types/api';

const mocks = vi.hoisted(() => ({ bridge: vi.fn(), resolve: vi.fn(), read: vi.fn(), detail: vi.fn() }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  return { page: writable({ url: new URL('https://fixture.invalid/native-pinned?agent=agent_a&conversation=conv_a') }) };
});
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ status: 'authenticated', user: { email: 'native@fixture.invalid' } }) };
});
vi.mock('$lib/offline/session', async () => {
  const { writable } = await import('svelte/store');
  return { deviceDataReady: writable(true) };
});
vi.mock('$lib/native/host', async () => {
  const { writable } = await import('svelte/store');
  return { isNativeHost: () => true, isWorkspaceReplica: () => false, nativeConfiguration: () => null,
    hostAllowsObservation: () => true, postNative: mocks.bridge,
    nativeVisibility: writable({ visible: true, focused: true }), nativeComposerFocus: writable(null) };
});
vi.mock('$lib/api/client', async (original) => {
  const module = await original<typeof import('$lib/api/client')>();
  return { ...module, api: { ...module.api, agents: { ...module.api.agents,
    detail: async (id: string) => ({ agent_id: id, name: id }) },
    conversations: { ...module.api.conversations, resolve: mocks.resolve, markRead: mocks.read, detail: mocks.detail } } };
});
// Real route, product panel/controller, direct controller and read tracker.
// Only the expensive chat renderer is a lifecycle fixture.
vi.mock('$lib/components/chat-v2/CompactConversationChat.svelte', () => import('./RouteChatFixture.svelte'));
import Page from './+page.svelte';
import { conversationWorkspace } from '$lib/dashboard/conversation-workspace';
import { pinnedConversations, pinnedAgents } from '$lib/dashboard/conversation-workspace-runtime';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { documentCoordination } from '$lib/document-coordination';
import { page } from '$app/stores';
import { auth } from '$lib/stores/auth';
import { nativeVisibility, nativeComposerFocus } from '$lib/native/host';

const route = page as unknown as Writable<{ url: URL }>;
const session = auth as unknown as Writable<{ status: string; user: { email: string } }>;
const visible = nativeVisibility as Writable<{ visible: boolean; focused: boolean }>;

const conversation = (id: string, agent = 'agent_a') => ({
  conversation_id: id, agent_id: agent, title: id, status: 'completed', has_unread: true,
  last_message_at: '2026-10-18T12:00:00Z',
} as Conversation);
const go = async (query: string) => {
  route.set({ url: new URL(`https://fixture.invalid/native-pinned?${query}`) }); await tick();
};
beforeEach(async () => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.resetAllMocks(); localStorage.clear();
  session.set({ status: 'authenticated', user: { email: 'native@fixture.invalid' } });
  visible.set({ visible: true, focused: true });
  nativeComposerFocus.set(null);
  conversationWorkspace.setCommand((intent) => conversationWorkspace.reduce(intent,
    async (id) => conversation(id)));
  conversationWorkspace.setOwner('native@fixture.invalid', null);
  await documentCoordination().beginLogin();
  await documentCoordination().verified('native@fixture.invalid');
  const a = conversation('conv_a');
  await conversationWorkspace.pin(a);
  pinnedConversations.set({ conv_a: a });
  pinnedAgents.set([{ agent_id: 'agent_a', name: 'Agent A' } as never]);
  mocks.resolve.mockImplementation(async ({ agent_id }) => conversation(`conv_${agent_id.slice(-1)}`, agent_id));
  mocks.detail.mockImplementation(async (id) => conversation(id));
  mocks.read.mockResolvedValue({});
  await go('agent=agent_a&conversation=conv_a');
});
afterEach(() => {
  cleanup(); conversationWorkspace.setOwner(null, null); pinnedConversations.set({});
  vi.unstubAllGlobals();
});
it('retires real pinned A before direct B, ignores A activity/unpin, and preserves drafts in both directions', async () => {
  const drafts = new DocumentDrafts();
  await drafts.write('cognis:chat-v2-draft:conv_a', 'unsent A');
  await drafts.write('cognis:chat-v2-draft:conv_b', 'unsent B');
  const view = render(Page);
  await waitFor(() => expect(mocks.read).toHaveBeenCalledWith('conv_a'));
  expect(await screen.findByTestId('fixture-conv_a')).toBeInTheDocument();
  await go('direct=1&agent=agent_b');
  expect(await screen.findByTestId('fixture-conv_b')).toBeInTheDocument();
  mocks.bridge.mockClear(); mocks.read.mockClear();
  pinnedConversations.update((state) => ({ ...state, conv_a: {
    ...conversation('conv_a'), last_message_at: '2026-10-18T12:01:00Z',
  } }));
  await conversationWorkspace.unpinAgent('agent_a');
  visible.set({ visible: false, focused: false }); await tick();
  visible.set({ visible: true, focused: true });
  document.dispatchEvent(new Event('visibilitychange')); await tick();
  expect(mocks.read).not.toHaveBeenCalledWith('conv_a');
  expect(mocks.read).toHaveBeenCalledWith('conv_b');
  expect(mocks.bridge).not.toHaveBeenCalledWith('dismiss', {});
  expect(mocks.bridge.mock.calls.filter(([type, value]) => type === 'panel-state' && value.agentId === 'agent_a')).toEqual([]);
  expect(screen.getByTestId('fixture-conv_b')).toBeInTheDocument();
  expect(drafts.read('cognis:chat-v2-draft:conv_a')).toBe('unsent A');
  expect(drafts.read('cognis:chat-v2-draft:conv_b')).toBe('unsent B');
  await conversationWorkspace.pin(conversation('conv_a'));
  await go('agent=agent_a&conversation=conv_a');
  expect(await screen.findByTestId('fixture-conv_a')).toBeInTheDocument();
  expect(screen.queryByTestId('fixture-conv_b')).not.toBeInTheDocument();
  mocks.read.mockClear();
  visible.set({ visible: false, focused: false }); await tick();
  visible.set({ visible: true, focused: true }); await tick();
  expect(mocks.read).not.toHaveBeenCalledWith('conv_b');
  expect(drafts.read('cognis:chat-v2-draft:conv_b')).toBe('unsent B');
  await fireEvent.click(screen.getByTestId('pinned-chat-minimize'));
  expect(mocks.bridge).toHaveBeenCalledWith('dismiss', {});
  mocks.bridge.mockClear(); view.unmount(); await tick();
  expect(mocks.bridge).not.toHaveBeenCalledWith('dismiss', {});
});
it('retains focus intent before load and gates it by agent and native visibility', async () => {
  await conversationWorkspace.unpinAgent('agent_a');
  await conversationWorkspace.pinAgent('agent_a');
  await go('agent=agent_a');
  const home: Conversation = {
    ...conversation('conv_a'),
    context: { ...conversation('conv_a').context, type: 'web', platform_data: { kind: 'agent_direct' } },
  };
  mocks.detail.mockImplementation(async (id) => id === home.conversation_id ? home : conversation(id));
  conversationWorkspace.setCommand((intent) => conversationWorkspace.reduce(intent, (id) => mocks.detail(id)));
  let finish!: (value: Conversation) => void;
  mocks.resolve.mockImplementationOnce(() => new Promise((done) => { finish = done; }));
  nativeComposerFocus.set({ agentId: 'agent_a', request: 'open-a' });
  visible.set({ visible: false, focused: false });
  render(Page);
  await waitFor(() => expect(mocks.resolve).toHaveBeenCalled());
  expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument();
  finish(home);
  await waitFor(() => expect(conversationWorkspace.snapshot().agents.find((agent) => agent.agentId === 'agent_a')?.homeConversationId).toBe('conv_a'));
  const composer = await screen.findByRole('textbox', { name: 'Message' });
  expect(composer).not.toHaveFocus();
  visible.set({ visible: true, focused: true });
  await waitFor(() => expect(composer).toHaveFocus());
  composer.blur();
  nativeComposerFocus.set({ agentId: 'agent_b', request: 'open-b' });
  await tick();
  expect(composer).not.toHaveFocus();
  await go('direct=1&agent=agent_b');
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus());
});
it('does not focus a read-only panel', async () => {
  pinnedConversations.set({ conv_a: { ...conversation('conv_a'), is_readonly_for_caller: true } });
  nativeComposerFocus.set({ agentId: 'agent_a', request: 'open-readonly' });
  render(Page);
  expect(await screen.findByTestId('fixture-conv_a')).toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument();
});
it('invalidates deferred pinned Home resolution on switch and never auto-pins direct agents', async () => {
  await conversationWorkspace.unpinAgent('agent_a'); await conversationWorkspace.pinAgent('agent_a');
  let finish!: (value: Conversation) => void;
  mocks.resolve.mockImplementationOnce(() => new Promise((done) => { finish = done; }));
  await go('agent=agent_a');
  render(Page); await waitFor(() => expect(mocks.resolve).toHaveBeenCalled());
  await go('direct=1&agent=agent_b');
  expect(await screen.findByTestId('fixture-conv_b')).toBeInTheDocument();
  mocks.bridge.mockClear();
  finish(conversation('conv_late')); await tick(); await Promise.resolve();
  expect(conversationWorkspace.snapshot().agents.find((agent) => agent.agentId === 'agent_a')?.homeConversationId).toBeUndefined();
  expect(conversationWorkspace.snapshot().agents.some((agent) => agent.agentId === 'agent_b')).toBe(false);
  expect(mocks.bridge).not.toHaveBeenCalledWith('dismiss', {});
  expect(mocks.bridge).not.toHaveBeenCalledWith('panel-state', expect.objectContaining({ conversationId: 'conv_late' }));
});
it('keeps one owner on direct-agent changes, logout/account replacement and disposal', async () => {
  await go('direct=1&agent=agent_b'); const view = render(Page);
  expect(await screen.findByTestId('fixture-conv_b')).toBeInTheDocument();
  await go('direct=1&agent=agent_c');
  expect(await screen.findByTestId('fixture-conv_c')).toBeInTheDocument();
  expect(screen.queryByTestId('fixture-conv_b')).not.toBeInTheDocument();
  mocks.bridge.mockClear(); mocks.read.mockClear();
  session.set({ status: 'anonymous', user: { email: 'native@fixture.invalid' } }); await tick();
  visible.set({ visible: true, focused: true }); document.dispatchEvent(new Event('visibilitychange')); await tick();
  expect(mocks.read).not.toHaveBeenCalled();
  expect(mocks.bridge).not.toHaveBeenCalledWith('dismiss', {});
  session.set({ status: 'authenticated', user: { email: 'replacement@fixture.invalid' } });
  conversationWorkspace.setOwner('replacement@fixture.invalid', null);
  expect(await screen.findByTestId('fixture-conv_c')).toBeInTheDocument();
  mocks.bridge.mockClear(); view.unmount(); await tick();
  expect(mocks.bridge).not.toHaveBeenCalledWith('dismiss', {});
});
