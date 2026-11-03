import { fromStore } from 'svelte/store';
import { api, asApiError } from '$lib/api/client';
import { conversationActivityState, type ActivityAvatarState } from '$lib/conversation-activity';
import { conversationWorkspace, retainedConversationId, type RetainedConversation, type PinnedAgent } from '$lib/dashboard/conversation-workspace';
import { pinnedAgents, pinnedConversations, pinnedBackgroundWork, pinnedActivityAt } from '$lib/dashboard/conversation-workspace-runtime';
import { aggregatePinnedActivity, pinnedMessageActivityAt } from '$lib/dashboard/pinned-dock';
import { DashboardConversationReadTracker } from '$lib/dashboard/conversation-read';
import { MAX_RETAINED_WORKSPACE_WINDOWS } from '$lib/dashboard/workspace';
import { blockingOverlayActive } from '$lib/stores/overlays';
import { addToast } from '$lib/stores/toasts';
import type { Conversation } from '$lib/types/api';
import type { CommandV2Response } from '$lib/chat-v2/types';
import { hostAllowsObservation } from '$lib/native/host';

function homeEntry(pin: PinnedAgent): RetainedConversation {
  return {
    window: { key: `home:${pin.agentId}`, kind: 'agent', entityId: pin.agentId, agentId: pin.agentId,
      resolvedConversationId: pin.homeConversationId, title: 'Home', canonicalHref: '/',
      rect: { x: 0, y: 0, width: 480, height: 640 }, minimized: false, zOrder: 0, switcherOrder: 0,
      restoreRect: null, refreshToken: 0, focusRevision: 0 },
    projectId: null, layouts: {},
  };
}

/** Product state for the pinned panel, independent of its browser/native placement. */
export function createPinnedPanel(options: {
  visible: () => boolean;
  projectId: () => string | null;
  onClose?: () => void;
}) {
  const workspace = fromStore(conversationWorkspace);
  const conversations = fromStore(pinnedConversations);
  const agents = fromStore(pinnedAgents);
  const background = fromStore(pinnedBackgroundWork);
  const activityTimes = fromStore(pinnedActivityAt);
  const overlay = fromStore(blockingOverlayActive);
  const owner = $derived(workspace.current.owner);
  let selectedKey = $state<string | null>(null);
  let loadedId = $state<string | null>(null);
  let initialLoadPending = false;
  let picker = $state(false);
  let resolving = $state(false);
  let error = $state('');
  let creationRequest = $state<symbol | null>(null);
  const creating = $derived(creationRequest !== null);
  let generation = 0;
  const groups = $derived(workspace.current.agents.map((pin) => {
    const entries = [homeEntry(pin), ...workspace.current.entries.filter((entry) => {
      const id = retainedConversationId(entry.window);
      const agentId = conversations.current[id ?? '']?.agent_id ?? entry.window.agentId ?? entry.window.entityId;
      return agentId === pin.agentId && entry.window.kind !== 'agent' && id !== pin.homeConversationId;
    })];
    const activityAt = Math.max(0, ...entries.flatMap((entry) => {
      const id = retainedConversationId(entry.window) ?? '';
      return [pinnedMessageActivityAt(conversations.current[id]), activityTimes.current[id] ?? 0];
    }));
    return { id: pin.agentId, entries, activityAt, lastSelectedKey: pin.lastSelectedKey,
      agent: agents.current.find((agent) => agent.agent_id === pin.agentId),
      state: aggregatePinnedActivity(entries.map(activity)) };
  }));
  const selected: RetainedConversation | undefined = $derived(selectedKey?.startsWith('home:')
    ? workspace.current.agents.filter((pin) => `home:${pin.agentId}` === selectedKey).map(homeEntry)[0]
    : workspace.current.entries.find((entry) => entry.window.key === selectedKey));
  const selectedId: string | null = $derived(selected ? retainedConversationId(selected.window) : null);
  const group = $derived(groups.find((item) => item.entries.some((entry) => entry.window.key === selectedKey)));
  const conversation = $derived(selectedId ? conversations.current[selectedId] : null);
  let observed: string | null = null;
  const tracker = new DashboardConversationReadTracker({
    markRead: (id) => api.conversations.markRead(id),
    optimisticRead: (id, readThrough) => pinnedConversations.update((state) => ({
      ...state, [id]: { ...state[id], has_unread: false, ...(readThrough ? { last_read_at: readThrough } : {}) },
    })),
    reconcile: async () => { if (selectedId) await refresh(selectedId); },
    isRetryable: (failure) => {
      const status = asApiError(failure).status ?? 0;
      return status === 0 || status === 408 || status === 429 || status >= 500;
    },
  });
  function activity(entry: RetainedConversation): ActivityAvatarState {
    const id = retainedConversationId(entry.window);
    return conversationActivityState(conversations.current[id ?? ''] ?? null, {
      open: options.visible() && hostAllowsObservation() && !picker && selectedId === id && loadedId === id && !overlay.current,
      backgroundWork: background.current,
    });
  }
  async function refresh(id: string) {
    const owner = workspace.current.owner, request = generation;
    try {
      const next = await api.conversations.detail(id, { includeState: false });
      if (request === generation && owner === workspace.current.owner) pinnedConversations.update((state) => ({ ...state, [id]: next }));
    } catch { /* Shared runtime retries summary reconciliation. */ }
  }
  function observe() {
    const id = options.visible() && hostAllowsObservation() && !picker && selectedId === loadedId
      && !overlay.current && document.visibilityState === 'visible' ? selectedId : null;
    if (observed !== id) {
      if (observed) tracker.suspend(observed);
      observed = id;
      if (id) tracker.open(id);
    }
    if (id && conversation) {
      if (initialLoadPending) {
        initialLoadPending = false;
        void tracker.initialLoaded(conversation);
      }
      void tracker.observe([conversation]);
    }
  }
  /** Owner replacement/disposal is silent; only a logical close notifies placement. */
  function reset() {
    generation++; selectedKey = null; loadedId = null; picker = false; resolving = false; creationRequest = null; error = '';
    initialLoadPending = false;
    tracker.close();
    observed = null;
  }
  function close() {
    const wasOpen = selectedKey !== null || resolving;
    reset();
    if (wasOpen) options.onClose?.();
  }
  async function select(entry: RetainedConversation) {
    {
      const request = ++generation, owner = workspace.current.owner;
      const agentId = entry.window.agentId ?? entry.window.entityId;
      const result = await conversationWorkspace.execute({ type: 'select', agentId, key: entry.window.key });
      if (!result.ok || request !== generation || owner !== workspace.current.owner) return;
      if (!entry.window.key.startsWith('home:') && !workspace.current.entries.some((item) => item.window.key === entry.window.key)) return;
    }
    if (retainedConversationId(entry.window) !== selectedId || picker || error || resolving) {
      tracker.close();
      observed = null;
      loadedId = null;
      initialLoadPending = false;
    }
    selectedKey = entry.window.key; picker = false; error = ''; resolving = false;
    const agentId = entry.window.agentId ?? entry.window.entityId;
    const request = ++generation, owner = workspace.current.owner;
    if (retainedConversationId(entry.window)) return;
    resolving = true;
    try {
      const next = await api.conversations.resolve({ agent_id: agentId, scope: 'agent_direct' });
      if (request !== generation || owner !== workspace.current.owner) return;
      pinnedConversations.update((state) => ({ ...state, [next.conversation_id]: next }));
      await conversationWorkspace.home(next);
    } catch (failure) {
      if (request === generation) error = asApiError(failure).message || 'Could not open conversation.';
    } finally { if (request === generation) resolving = false; }
  }
  function openAgent(id: string) {
    const target = groups.find((item) => item.id === id);
    if (target) return select(target.entries.find((entry) => entry.window.key === target.lastSelectedKey) ?? target.entries[0]);
  }
  async function openConversation(next: Conversation) {
    if (!group || next.agent_id !== group.id) return;
    if (next.conversation_id === retainedConversationId(group.entries[0].window)) return select(group.entries[0]);
    const owner = workspace.current.owner, request = generation;
    const result = await conversationWorkspace.pinAndSelect(next);
    if (!result.ok) { addToast('Pin state could not be confirmed. Refresh before trying again.', 'info'); return; }
    if (request !== generation || owner !== workspace.current.owner) return;
    pinnedConversations.update((state) => ({ ...state, [next.conversation_id]: next }));
    const entry = workspace.current.entries.find((item) => retainedConversationId(item.window) === next.conversation_id);
    if (entry) await select(entry);
  }
  async function unpin(entry: RetainedConversation) {
    const siblings = group?.entries.filter((item) => item !== entry) ?? [];
    const request = generation, owner = workspace.current.owner;
    const result = await conversationWorkspace.execute({ type: 'unpin', key: entry.window.key });
    if (!result.ok || request !== generation || owner !== workspace.current.owner) return;
    if (entry.window.key === selectedKey) {
      if (siblings.length) void select(siblings[0]); else close();
    }
  }
  async function newConversation() {
    if (!group || creating || conversation?.is_readonly_for_caller) return;
    if (workspace.current.entries.length >= MAX_RETAINED_WORKSPACE_WINDOWS) {
      addToast('Close a pinned conversation first (limit 10).', 'info'); return;
    }
    const owner = workspace.current.owner, agentId = group.id, key = selectedKey;
    // Creation outlives same-owner tab selection, but not a reset. Its identity
    // cannot share the selection generation that its own success advances.
    const request = Symbol();
    creationRequest = request;
    try {
      const next = await api.conversations.create({ agent_id: agentId, context: { type: 'web' },
        project_id: options.projectId() ?? conversation?.project_id ?? null });
      if (creationRequest === request && owner === workspace.current.owner && group?.id === agentId && workspace.current.agents.some((pin) => pin.agentId === agentId)) {
        if (selectedKey === key) await openConversation(next); else await conversationWorkspace.pinAndSelect(next, false);
      }
    } catch (failure) { if (creationRequest === request && owner === workspace.current.owner) addToast(asApiError(failure).message || 'Could not create conversation.', 'error'); }
    finally { if (creationRequest === request) creationRequest = null; }
  }
  async function command(response: CommandV2Response) {
    const id = response.data?.conversation_id, previous = selected, owner = workspace.current.owner, request = generation;
    if (typeof id !== 'string' || !id || id === selectedId || !previous) return;
    const next = await api.conversations.detail(id, { includeState: false });
    if (request !== generation || owner !== workspace.current.owner || selectedKey !== previous.window.key) return;
    const result = await conversationWorkspace.pinAndSelect(next, true,
      previous.window.key.startsWith('home:') ? undefined : previous.window.key);
    if (!result.ok || request !== generation || owner !== workspace.current.owner) return;
    pinnedConversations.update((state) => ({ ...state, [id]: next }));
    const entry = workspace.current.entries.find((item) => retainedConversationId(item.window) === id);
    if (entry) void select(entry);
  }
  $effect(() => { const currentOwner = owner; return () => { void currentOwner; reset(); }; });
  $effect(() => { if (selectedKey && !selected) close(); });
  $effect(() => { observe(); });
  return {
    get groups() { return groups; }, get selectedKey() { return selectedKey; }, get selected() { return selected; },
    get selectedId() { return selectedId; }, get group() { return group; }, get conversation() { return conversation; },
    get picker() { return picker; }, set picker(value) {
      if (value && !picker) {
        tracker.close();
        observed = null;
        loadedId = null;
        initialLoadPending = false;
      }
      picker = value;
    },
    get resolving() { return resolving; }, get error() { return error; }, get creating() { return creating; },
    activity, refresh, observe, close, select, openAgent, openConversation, unpin, newConversation, command,
    loaded() {
      if (!selectedId || picker || loadedId === selectedId) return;
      loadedId = selectedId;
      initialLoadPending = true;
      observe();
    },
    async unpinAgent() {
      const request = generation, owner = workspace.current.owner;
      if (group && (await conversationWorkspace.execute({ type: 'agent', agentId: group.id, pinned: false })).ok
        && request === generation && owner === workspace.current.owner) close();
    },
  };
}
export type PinnedPanel = ReturnType<typeof createPinnedPanel>;
