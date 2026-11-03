import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { waitFor } from '@testing-library/svelte';
import { conversationWorkspace } from '$lib/dashboard/conversation-workspace';
import { pinnedConversations } from '$lib/dashboard/conversation-workspace-runtime';
import { api } from '$lib/api/client';
import type { Conversation } from '$lib/types/api';
import { pinnedPanelTestState } from './pinnedPanelTestState.svelte';

const cleanup: Array<() => void> = [];
afterEach(() => {
  cleanup.splice(0).forEach((stop) => stop());
  conversationWorkspace.setOwner(null, null);
  pinnedConversations.set({});
  vi.restoreAllMocks();
});
const conversation = (id: string) => ({
  conversation_id: id, agent_id: 'pinned-agent', title: id, status: 'completed',
} as Conversation);

describe('shared pinned panel state ownership', () => {
  it('retains readiness through hiding and same-tab selection, but waits for a fresh picker transcript', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const markRead = vi.spyOn(api.conversations, 'markRead').mockResolvedValue({ ok: true });
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    const item = {
      ...conversation('observed-pin'),
      has_unread: true,
      last_message_at: '2026-08-24T12:00:00Z',
    };
    await conversationWorkspace.pin(item);
    pinnedConversations.set({ [item.conversation_id]: item });
    let visible = true;
    const { panel, destroy } = pinnedPanelTestState(vi.fn(), () => visible);
    cleanup.push(destroy);
    flushSync();
    const entry = panel.groups[0].entries.find(entry => entry.window.entityId === item.conversation_id)!;
    await panel.select(entry);
    flushSync();
    expect(markRead).not.toHaveBeenCalled();
    panel.loaded();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));

    visible = false;
    panel.observe();
    pinnedConversations.update(state => ({
      ...state,
      [item.conversation_id]: { ...item, last_message_at: '2026-08-24T12:01:00Z' },
    }));
    flushSync();
    expect(markRead).toHaveBeenCalledTimes(1);
    visible = true;
    panel.observe();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    await panel.select(entry);
    flushSync();
    panel.observe();
    expect(markRead).toHaveBeenCalledTimes(2);

    panel.picker = true;
    flushSync();
    pinnedConversations.update(state => ({
      ...state,
      [item.conversation_id]: { ...item, last_message_at: '2026-08-24T12:02:00Z' },
    }));
    panel.loaded();
    panel.picker = false;
    flushSync();
    panel.observe();
    expect(markRead).toHaveBeenCalledTimes(2);
    panel.loaded();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(3));
    panel.loaded();
    panel.observe();
    expect(markRead).toHaveBeenCalledTimes(3);
  });

  it('retains selection across updates; silently resets on owner change and handles the next owner', async () => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    const first = conversation('retained-one'), second = conversation('retained-two');
    await conversationWorkspace.pin(first); await conversationWorkspace.pin(second);
    pinnedConversations.set({ [first.conversation_id]: first, [second.conversation_id]: second });
    const closed = vi.fn(), { panel, destroy } = pinnedPanelTestState(closed);
    cleanup.push(destroy);
    flushSync();
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === first.conversation_id)!);
    flushSync();
    expect(panel.selectedId).toBe(first.conversation_id);
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === second.conversation_id)!);
    conversationWorkspace.setPosition(0.2);
    pinnedConversations.update((state) => ({ ...state, [second.conversation_id]: { ...second, has_unread: true } }));
    flushSync();
    expect(panel.selectedId).toBe(second.conversation_id);
    expect(closed).not.toHaveBeenCalled();
    conversationWorkspace.setOwner('new-panel-owner@cognis.test', null);
    flushSync();
    expect(panel.selectedId).toBeNull();
    expect(closed).not.toHaveBeenCalled();
    await conversationWorkspace.pin(first);
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === first.conversation_id)!);
    flushSync();
    expect(panel.selectedId).toBe(first.conversation_id);
    panel.close(); panel.close(); flushSync();
    expect(panel.selectedId).toBeNull();
    expect(panel.resolving).toBe(false);
    expect(panel.creating).toBe(false);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(conversationWorkspace.snapshot().entries).toHaveLength(1);
    destroy();
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it('notifies once when the active pin becomes unavailable, but never on disposal', async () => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    const first = conversation('active-pin');
    await conversationWorkspace.pin(first);
    const closed = vi.fn(), { panel, destroy } = pinnedPanelTestState(closed);
    flushSync();
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === first.conversation_id)!);
    flushSync();
    await conversationWorkspace.unpinAgent('pinned-agent'); flushSync();
    expect(panel.selectedId).toBeNull();
    expect(closed).toHaveBeenCalledTimes(1);
    destroy();
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it.each(['close', 'account change'])('does not apply an unresolved Home completion after %s', async (action) => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    await conversationWorkspace.pinAgent('pinned-agent');
    let resolve!: (value: Conversation) => void;
    vi.spyOn(api.conversations, 'resolve').mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { panel, destroy } = pinnedPanelTestState(vi.fn());
    cleanup.push(destroy);
    flushSync();
    const pending = panel.openAgent('pinned-agent');
    await waitFor(() => expect(api.conversations.resolve).toHaveBeenCalled());
    if (action === 'close') panel.close();
    else conversationWorkspace.setOwner('new-panel-owner@cognis.test', null);
    flushSync();
    resolve(conversation('late-home'));
    await pending;
    flushSync();
    expect(panel.selectedId).toBeNull();
    expect(conversationWorkspace.snapshot().agents.every((agent) => !agent.homeConversationId)).toBe(true);
  });

  it('releases creation state after successful selection and permits consecutive creations', async () => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    await conversationWorkspace.pin(conversation('initial'));
    const { panel, destroy } = pinnedPanelTestState(vi.fn()); cleanup.push(destroy);
    flushSync();
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === 'initial')!);
    const create = vi.spyOn(api.conversations, 'create')
      .mockResolvedValueOnce(conversation('created-first')).mockResolvedValueOnce(conversation('created-second'));
    await panel.newConversation(); flushSync();
    expect(panel.selectedId).toBe('created-first');
    expect(panel.creating).toBe(false);
    await panel.newConversation(); flushSync();
    expect(create).toHaveBeenCalledTimes(2);
    expect(panel.selectedId).toBe('created-second');
    expect(panel.creating).toBe(false);
  });

  it('finishes pending creation without stealing a same-owner tab selection', async () => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    await conversationWorkspace.pin(conversation('initial'));
    await conversationWorkspace.pin(conversation('other-tab'));
    const { panel, destroy } = pinnedPanelTestState(vi.fn()); cleanup.push(destroy);
    flushSync();
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === 'initial')!);
    let finish!: (value: Conversation) => void;
    vi.spyOn(api.conversations, 'create').mockImplementation(() => new Promise((done) => { finish = done; }));
    const pending = panel.newConversation();
    expect(panel.creating).toBe(true);
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === 'other-tab')!);
    finish(conversation('created-background')); await pending; flushSync();
    expect(panel.selectedId).toBe('other-tab');
    expect(panel.creating).toBe(false);
    expect(conversationWorkspace.snapshot().entries.some((entry) => entry.window.entityId === 'created-background')).toBe(true);
  });

  it('cannot let an obsolete creation reopen or clear the next verified owner creation', async () => {
    conversationWorkspace.setOwner('panel-owner@cognis.test', null);
    await conversationWorkspace.pin(conversation('initial'));
    const { panel, destroy } = pinnedPanelTestState(vi.fn()); cleanup.push(destroy); flushSync();
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === 'initial')!);
    const finish: Array<(value: Conversation) => void> = [];
    vi.spyOn(api.conversations, 'create').mockImplementation(() => new Promise((done) => { finish.push(done); }));
    const old = panel.newConversation();
    conversationWorkspace.setOwner('next-panel-owner@cognis.test', null); flushSync();
    expect(panel.creating).toBe(false);
    await conversationWorkspace.pin(conversation('next-initial'));
    await panel.select(panel.groups[0].entries.find((entry) => entry.window.entityId === 'next-initial')!);
    const next = panel.newConversation();
    finish[0](conversation('obsolete-create')); await old; flushSync();
    expect(panel.creating).toBe(true);
    expect(panel.selectedId).toBe('next-initial');
    finish[1](conversation('next-create')); await next; flushSync();
    expect(panel.creating).toBe(false);
    expect(panel.selectedId).toBe('next-create');
    expect(conversationWorkspace.snapshot().entries.some((entry) => entry.window.entityId === 'obsolete-create')).toBe(false);
  });
});
