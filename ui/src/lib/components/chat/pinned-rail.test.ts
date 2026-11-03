import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable, type Writable } from 'svelte/store';
import { conversationWorkspace } from '$lib/dashboard/conversation-workspace';
import { pinnedAgents, pinnedConversations } from '$lib/dashboard/conversation-workspace-runtime';
import type { Agent, Conversation, UserPreferences } from '$lib/types/api';
import PinnedConversationDock from './PinnedConversationDock.svelte';
import { page } from '$app/stores';
import { flushSync } from 'svelte';
import { userPreferences } from '$lib/stores/userPreferences';
const testPage = page as unknown as Writable<{ url: URL; params: Record<string, string> }>;
const testPreferences = userPreferences as Writable<UserPreferences>;

vi.mock('$lib/components/chat-v2/CompactConversationChat.svelte', async () => ({
  default: (await import('./PinnedDockChatStub.svelte')).default
}));

const bridge = vi.hoisted(() => vi.fn());
vi.mock('$lib/native/host', () => ({ postNative: bridge, hostAllowsObservation: () => false, isNativeHost: () => false, isWorkspaceReplica: () => false }));
vi.mock('$app/stores', () => ({ page: writable({ url: new URL('https://rail.invalid/chat/first'), params: {} }) }));
vi.mock('$lib/stores/userPreferences', () => ({
  loadUserPreferences: async () => {},
  userPreferences: writable({ display: { pinned_agents_collapsed: false, pinned_agents_order_by_activity: false, dashboard_workspace_windows: false } })
}));

beforeEach(async () => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  bridge.mockClear();
  document.documentElement.style.fontSize = '16px';
  testPage.set({ url: new URL('https://rail.invalid/chat/first'), params: {} });
  testPreferences.update((state) => ({ ...state, display: { ...state.display, pinned_agents_collapsed: false } }));
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true });
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true });
  window.matchMedia = vi.fn().mockReturnValue({ matches: true, addEventListener() {}, removeEventListener() {} });
  HTMLElement.prototype.setPointerCapture = vi.fn();
  HTMLElement.prototype.getAnimations = vi.fn().mockReturnValue([]);
  window.PointerEvent = MouseEvent as typeof PointerEvent;
  conversationWorkspace.setOwner(`rail-${crypto.randomUUID()}`, null);
  pinnedAgents.set([
    { agent_id: 'agent-first', name: 'First', avatar_url: '/avatars/original-512.png' },
    { agent_id: 'agent-second', name: 'Second', avatar_url: '/avatars/second-512.png' }
  ] as Agent[]);
  const first = { conversation_id: 'first', agent_id: 'agent-first', title: 'First', status: 'active', has_active_turn: true } as Conversation;
  const second = { conversation_id: 'second', agent_id: 'agent-second', title: 'Second', status: 'idle', has_unread: true } as Conversation;
  await conversationWorkspace.pin(first); await conversationWorkspace.pin(second);
  await conversationWorkspace.execute({ type: 'select', agentId: 'agent-first', key: 'conversation:first' });
  pinnedConversations.set({ first, second });
});
afterEach(() => { cleanup(); conversationWorkspace.setCommand(null); conversationWorkspace.setOwner(null, null); vi.unstubAllGlobals(); });

describe('canonical dock in native rail placement', () => {
  it('uses original image sources and shared activity markup without selecting activity', async () => {
    render(PinnedConversationDock, { nativeRail: true });
    expect(screen.getAllByTestId('activity-avatar')).toHaveLength(2);
    expect(document.querySelector('img')?.getAttribute('src')).toBe('/avatars/original-512.png');
    expect(document.querySelectorAll('[data-selected="true"]')).toHaveLength(0);
    await fireEvent.click(document.querySelector('[data-pinned-agent="agent-first"]')!);
    expect(bridge).toHaveBeenCalledWith('rail', { action: 'open', agentId: 'agent-first' });
    // Clicking is not proof of an open native popup.
    expect(document.querySelectorAll('[data-selected="true"]')).toHaveLength(0);
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: { selectedAgentId: 'agent-second', side: 'right' } }));
    await waitFor(() => expect(document.querySelectorAll('[data-selected="true"]')).toHaveLength(1));
    expect(document.querySelector('[data-selected="true"]')).toHaveAttribute('data-pinned-agent', 'agent-second');
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: { selectedAgentId: null, side: 'left' } }));
    await waitFor(() => expect(document.querySelectorAll('[data-selected="true"]')).toHaveLength(0));
    expect(document.querySelector('.native-left')).not.toBeNull();
    expect(document.querySelector('[data-testid="pinned-chat-veil"]')).toBeNull();
  });

  it('uses the same half-hide preference and reveals on hover', async () => {
    await conversationWorkspace.setTucked(true);
    render(PinnedConversationDock, { nativeRail: true });
    const strip = document.querySelector('[data-visually-tucked]')!;
    expect(strip).toHaveAttribute('data-visually-tucked', 'true');
    await fireEvent.pointerEnter(strip, { pointerType: 'mouse' });
    expect(strip).toHaveAttribute('data-visually-tucked', 'false');
    await fireEvent.pointerLeave(strip);
    expect(strip).toHaveAttribute('data-visually-tucked', 'true');
  });

  it('submits native half-hide through the authoritative command channel', async () => {
    const command = vi.fn((intent) => conversationWorkspace.reduce(intent));
    conversationWorkspace.setCommand(command);
    render(PinnedConversationDock, { nativeRail: true });
    expect(conversationWorkspace.snapshot().tucked).toBe(true);
    window.dispatchEvent(new Event('cognis-rail-tuck'));
    await waitFor(() => expect(document.querySelector('[data-visually-tucked]')).toHaveAttribute('data-visually-tucked', 'false'));
    expect(command).toHaveBeenCalledWith({ type: 'tucked', value: false });
  });

  it('sends hide-before-move lifecycle and never a horizontal movement or automatic reopen', async () => {
    render(PinnedConversationDock, { nativeRail: true });
    const avatar = document.querySelector('[data-pinned-agent="agent-first"]')!;
    bridge.mockClear();
    await fireEvent.pointerDown(avatar, { button: 0, clientY: 20, screenY: 100 });
    await fireEvent.pointerMove(avatar, { clientY: 40, screenY: 120 });
    await fireEvent.pointerUp(avatar, { clientY: 40, screenY: 120 });
    await fireEvent.click(avatar, { detail: 1 });
    expect(bridge.mock.calls.filter(([type]) => type === 'rail')).toEqual([
      ['rail', { action: 'drag-start' }], ['rail', { action: 'drag-move' }],
      ['rail', { action: 'drag-end' }]
    ]);
  });

  it('keeps browser placement on the same component without native open commands', async () => {
    render(PinnedConversationDock);
    expect(screen.getAllByTestId('activity-avatar')).toHaveLength(2);
    expect(document.querySelector('img')?.getAttribute('src')).toBe('/avatars/original-512.png');
    expect(bridge).not.toHaveBeenCalledWith('rail', expect.anything());
  });
  it.each([false, true])('preserves browser selection across unrelated updates (collapsed=%s)', async (collapsed) => {
    testPreferences.update((state) => ({ ...state, display: { ...state.display, pinned_agents_collapsed: collapsed } }));
    render(PinnedConversationDock);
    const avatar = document.querySelector('[data-pinned-agent="agent-first"]')!;
    if (collapsed) await fireEvent.click(avatar);
    await fireEvent.click(avatar);
    await waitFor(() => expect(screen.getByTestId('pinned-chat-panel')).toBeInTheDocument());
    await conversationWorkspace.setPosition(0.2);
    pinnedConversations.update((state) => ({ ...state, second: { ...state.second, has_unread: false } }));
    flushSync();
    expect(screen.getByRole('tab', { name: /First$/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('pinned-chat-dismiss-backdrop')).toBeInTheDocument();
  });
  it.each(['route', 'owner', 'hidden', 'Escape', 'backdrop', 'minimize', 'unpin'])('dismisses browser selection on %s', async (reason) => {
    testPreferences.update((state) => ({ ...state, display: { ...state.display, pinned_agents_collapsed: false } }));
    render(PinnedConversationDock);
    await fireEvent.click(document.querySelector('[data-pinned-agent="agent-first"]')!);
    await waitFor(() => expect(screen.getByTestId('pinned-chat-panel')).toBeInTheDocument());
    if (reason === 'route') testPage.update((state) => ({ ...state, url: new URL('https://rail.invalid/notifications') }));
    if (reason === 'owner') conversationWorkspace.setOwner('another-owner', null);
    if (reason === 'hidden') {
      Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
      await fireEvent(window, new Event('resize'));
    }
    if (reason === 'Escape') await fireEvent.keyDown(screen.getByTestId('pinned-chat-minimize'), { key: 'Escape' });
    if (reason === 'backdrop') await fireEvent.click(screen.getByTestId('pinned-chat-dismiss-backdrop'));
    if (reason === 'minimize') await fireEvent.click(screen.getByTestId('pinned-chat-minimize'));
    if (reason === 'unpin') await fireEvent.click(screen.getByRole('button', { name: 'Unpin agent' }));
    await waitFor(() => expect(screen.queryByTestId('pinned-chat-panel')).not.toBeInTheDocument());
  });
  it('clears stale hover on native close outside, but preserves genuine hover and keyboard focus', async () => {
    await conversationWorkspace.setTucked(true);
    render(PinnedConversationDock, { nativeRail: true });
    const strip = document.querySelector('[data-visually-tucked]')!;
    await fireEvent.pointerEnter(strip, { pointerType: 'mouse' });
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 1, selectedAgentId: 'agent-first', side: 'left', pointerInside: true, focused: false
    } }));
    await waitFor(() => expect(strip).toHaveAttribute('data-visually-tucked', 'false'));
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 2, selectedAgentId: null, side: 'left', pointerInside: false, focused: false
    } }));
    await waitFor(() => expect(strip).toHaveAttribute('data-visually-tucked', 'true'));
    // A delayed pre-close native presentation must not restore selection.
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 1, selectedAgentId: 'agent-first', side: 'left', pointerInside: true, focused: false
    } }));
    expect(document.querySelectorAll('[data-selected=true]')).toHaveLength(0);
  });
  it('activates exactly once on the first native pointer up and ignores canceled capture', async () => {
    render(PinnedConversationDock, { nativeRail: true });
    const avatar = document.querySelector('[data-pinned-agent="agent-first"]')!;
    bridge.mockClear();
    await fireEvent.pointerDown(avatar, { button: 0, clientY: 20 });
    await fireEvent.pointerUp(avatar, { clientY: 20 });
    await fireEvent.click(avatar, { detail: 1 });
    expect(bridge.mock.calls.filter(([, value]) => value.action === 'open')).toHaveLength(1);
    bridge.mockClear();
    await fireEvent.pointerDown(avatar, { button: 0, clientY: 20 });
    await fireEvent.pointerCancel(avatar);
    await fireEvent.click(avatar, { detail: 1 });
    expect(bridge.mock.calls.filter(([, value]) => value.action === 'open')).toHaveLength(0);
  });
  it.each(['left', 'right'])('reconciles real hover and keyboard focus without forced hiding on the %s edge', async (side) => {
    await conversationWorkspace.setTucked(true);
    render(PinnedConversationDock, { nativeRail: true });
    const strip = document.querySelector('[data-visually-tucked]')!;
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 1, selectedAgentId: null, side, pointerInside: true, focused: false
    } }));
    await waitFor(() => expect(strip).toHaveAttribute('data-visually-tucked', 'false'));
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 2, selectedAgentId: null, side, pointerInside: false, focused: true
    } }));
    await fireEvent.keyDown(strip, { key: 'Tab' });
    expect(strip).toHaveAttribute('data-visually-tucked', 'false');
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 3, selectedAgentId: null, side, pointerInside: false, focused: false
    } }));
    await waitFor(() => expect(strip).toHaveAttribute('data-visually-tucked', 'true'));
  });
  it('reveals and indicates the first keyboard focus entering from outside without an in-dock keydown', async () => {
    await conversationWorkspace.setTucked(true);
    render(PinnedConversationDock, { nativeRail: true });
    const strip = document.querySelector('[data-visually-tucked]')!;
    const avatar = document.querySelector<HTMLButtonElement>('[data-pinned-agent="agent-first"]')!;
    expect(strip).toHaveAttribute('data-visually-tucked', 'true');
    avatar.focus();
    await waitFor(() => expect(avatar).toHaveAttribute('data-keyboard-focus', 'true'));
    expect(strip).toHaveAttribute('data-visually-tucked', 'false');
    window.dispatchEvent(new CustomEvent('cognis-rail', { detail: {
      revision: 1, selectedAgentId: null, side: 'right', pointerInside: false, focused: true
    } }));
    expect(document.activeElement).toBe(avatar);
    expect(strip).toHaveAttribute('data-visually-tucked', 'false');
    avatar.blur();
    await waitFor(() => expect(strip).toHaveAttribute('data-visually-tucked', 'true'));
  });
});
