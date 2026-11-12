import { get } from 'svelte/store';
import { auth } from '$lib/stores/auth';
import { wsClient, wsState } from '$lib/ws/client';
import { conversationWorkspace, retainedConversationId } from '$lib/dashboard/conversation-workspace';
import { pinnedAgents, pinnedConversations, pinnedBackgroundWork } from '$lib/dashboard/conversation-workspace-runtime';
import { conversationActivityState } from '$lib/conversation-activity';
import { isNativeHost, isNativeRoute, nativeConfiguration, nativeComposerFocus, postNative, receiveNativeVisibility } from './host';
import { installResidentNotifications } from './notifications';
import { receiveNotificationStatus } from './notification-status';
import { installAgentCatalogue, nativeAgents, nativeAgentsReady } from './catalogue';


/** One shell/runtime and one web-owned pin collection per native profile. */
export function installNativeRuntime(navigate: (path: string) => Promise<unknown>): () => void {
  if (!isNativeHost()) return () => {};
  const residentOwner = !nativeConfiguration()?.surface || nativeConfiguration()?.surface === 'main';
  const stopCatalogue = residentOwner ? installAgentCatalogue() : () => {};
  const notifications = residentOwner ? installResidentNotifications(navigate) : { review() {}, act() {}, stop() {} };
  let alive = true;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let owner: string | null | undefined;
  let epoch = 0;
  let lastAppearance = '';
  const appearance = () => {
    const style = getComputedStyle(document.documentElement);
    const background = style.getPropertyValue('--app-chrome-bg').trim();
    if (!/^#[0-9a-f]{6}$/i.test(background)) return;
    const theme = style.colorScheme.includes('dark') ? 'dark' : 'light';
    const key = document.documentElement.dataset.themeFamily + theme + background;
    if (lastAppearance !== key) {
      lastAppearance = key;
      postNative('appearance', { theme, background });
    }
  };
  const publish = () => {
    timer = null;
    if (!alive) return;
    const state = get(auth);
    const email = state.status === 'authenticated' ? state.user?.email ?? null : null;
    if (owner !== email) { owner = email; epoch++; }
    const workspace = conversationWorkspace.snapshot();
    const candidates = [
      ...workspace.entries.map((entry) => ({
        id: retainedConversationId(entry.window), agentId: entry.window.agentId, title: entry.window.title
      })),
      ...workspace.agents.map((agent) => ({
        id: agent.homeConversationId, agentId: agent.agentId, title: ''
      }))
    ];
    const seen = new Set<string>();
    const pins = email && workspace.owner === email ? candidates.flatMap((entry) => {
      const id = entry.id;
      // Unresolved agent shortcuts must not silently create a conversation.
      if (!id || seen.has(id)) return [];
      const conversation = get(pinnedConversations)[id];
      const agentId = conversation?.agent_id ?? entry.agentId;
      if (!agentId) return [];
      seen.add(id);
      const agent = get(pinnedAgents).find((item) => item.agent_id === agentId);
      const activity = conversationActivityState(conversation ?? null, {
        backgroundWork: get(pinnedBackgroundWork)
      });
      return [{
        conversationId: id, agentId,
        title: (entry.title || conversation?.title || agent?.name || 'Conversation').slice(0, 160),
        agentName: (agent?.name || 'Agent').slice(0, 80),
        attention: activity.error ? 'error' : activity.attention ? 'attention'
          : activity.active || activity.background ? 'active' : activity.unread ? 'unread' : 'idle'
      }];
    }) : [];
    postNative('snapshot', {
      epoch, authenticated: Boolean(email), connection: get(wsState).status, pins,
      agents: email ? get(nativeAgents).map((agent) => ({
        agentId: agent.agent_id, name: (agent.display_name || agent.name).replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 80)
      })) : [],
      panelTargets: {
        ready: Boolean(email) && get(nativeAgentsReady) && workspace.owner === email,
        pinnedAgents: email ? workspace.agents.map((agent) => agent.agentId) : [],
      },
    });
    appearance();
  };
  const schedule = () => {
    if (timer === null) timer = setTimeout(publish, 0);
  };
  const stops = [
    auth.subscribe((state) => {
      const email = state.status === 'authenticated' ? state.user?.email ?? null : null;
      if (owner !== email) {
        owner = email; epoch++;
        // Do not coalesce away a logout/account transition before clearing native panels.
        postNative('snapshot', { epoch, authenticated: false, connection: 'idle', pins: [], agents: [] });
      }
      schedule();
    }), wsState.subscribe(schedule), conversationWorkspace.subscribe(schedule),
    pinnedAgents.subscribe(schedule), pinnedConversations.subscribe(schedule),
     pinnedBackgroundWork.subscribe(schedule), nativeAgents.subscribe(schedule), nativeAgentsReady.subscribe(schedule)
  ];
  window.__cognisNativeReceive = (value: unknown) => {
    if (!value || typeof value !== 'object') return;
    const command = value as Record<string, unknown>;
    if (command.version !== 1 || command.generation !== nativeConfiguration()?.generation) return;
    if (command.type === 'visibility' && typeof command.visible === 'boolean'
      && typeof command.focused === 'boolean') {
      receiveNativeVisibility(command.visible, command.focused);
    } else if (command.type === 'focus-composer' && nativeConfiguration()?.surface === 'pinned'
      && typeof command.agentId === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(command.agentId)
      && typeof command.request === 'string' && /^[0-9a-f-]{36}$/i.test(command.request)) {
      nativeComposerFocus.set({ agentId: command.agentId, request: command.request });
    } else if (command.type === 'notification-review') {
      notifications.review(command);
    } else if (command.type === 'notification-action') {
      void notifications.act(command);
    } else if (command.type === 'notification-status') {
      receiveNotificationStatus(command);
    } else if (command.type === 'resume') {
      if (get(auth).status === 'authenticated' && !get(auth).offline) wsClient.reconnectNow();
    } else if (command.type === 'workspace' || command.type === 'workspace-reset') {
      window.dispatchEvent(new CustomEvent('cognis-workspace', {
        detail: command.type === 'workspace-reset' ? null : command.frame
      }));
    } else if (command.type === 'rail-state') {
      window.dispatchEvent(new CustomEvent('cognis-rail', { detail: command }));
    } else if (command.type === 'rail-tuck') {
      window.dispatchEvent(new Event('cognis-rail-tuck'));
    } else if (command.type === 'navigate' && isNativeRoute(command.path)) {
      void navigate(command.path);
    }
  };
  const escape = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !event.defaultPrevented
      && !document.querySelector('dialog[open], [role="dialog"][aria-modal="true"]')
      && (window.location.pathname === '/native-pinned' || window.location.search.includes('window=1'))) {
      postNative('dismiss', {});
    }
  };
  window.addEventListener('keydown', escape);
  const themeObserver = new MutationObserver(appearance);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-resolved-theme', 'data-theme-family'] });
  publish();
  return () => {
    alive = false;
    nativeComposerFocus.set(null);
    notifications.stop();
    stopCatalogue();
    stops.forEach((stop) => stop());
    if (timer !== null) clearTimeout(timer);
    delete window.__cognisNativeReceive;
    window.removeEventListener('keydown', escape);
    themeObserver.disconnect();
  };
}
