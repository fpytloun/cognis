import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';
import { wsClient, wsState } from '$lib/ws/client';
import type { Agent } from '$lib/types/api';

export const nativeAgents = writable<Agent[]>([]);
export const nativeAgentsReady = writable(false);

/** Authenticated resident discovery, independent of workspace membership. */
export function installAgentCatalogue(): () => void {
  let alive = true, generation = 0, account: string | null = null, request = 0;
  const refresh = async () => {
    const epoch = generation, owner = account, ticket = ++request;
    if (!owner || get(auth).offline) return;
    try {
      const agents = await api.agents.listAll({ include_hidden: true });
      if (alive && epoch === generation && ticket === request && owner === account) {
        nativeAgents.set(agents.filter((agent) => !agent.is_system && !agent.disabled));
        nativeAgentsReady.set(true);
      }
    } catch { /* Retry at the next authenticated invalidation. */ }
  };
  const stops = [
    auth.subscribe((state) => {
      const next = state.status === 'authenticated' ? state.user?.email ?? null : null;
      if (next !== account) {
        account = next; generation++; nativeAgents.set([]); nativeAgentsReady.set(false);
        if (next) void refresh();
      }
    }),
    wsClient.subscribe((event) => {
      if (event.type === 'scope_invalidated' && event.reason === 'sidebar_changed') void refresh();
    }),
    wsState.subscribe((state) => { if (state.status === 'connected') void refresh(); }),
  ];
  window.addEventListener('focus', refresh);
  return () => {
    alive = false; generation++; stops.forEach((stop) => stop());
    window.removeEventListener('focus', refresh); nativeAgents.set([]); nativeAgentsReady.set(false);
  };
}
