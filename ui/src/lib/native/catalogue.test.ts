import { afterEach, describe, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';
import type { Agent, CognisWebSocketEvent } from '$lib/types/api';
import type { AuthState } from '$lib/stores/auth';

const mocks = vi.hoisted(() => ({ list: vi.fn(), event: null as null | ((event: CognisWebSocketEvent) => void) }));
const state = writable<AuthState>({ status: 'anonymous', initialized: true, user: null, expiresAt: null, error: null });
const socket = writable({ status: 'idle' });
vi.mock('$lib/stores/auth', () => ({ auth: { subscribe: (...args: Parameters<typeof state.subscribe>) => state.subscribe(...args) } }));
vi.mock('$lib/api/client', () => ({ api: { agents: { listAll: mocks.list } } }));
vi.mock('$lib/ws/client', () => ({ wsState: { subscribe: (...args: Parameters<typeof socket.subscribe>) => socket.subscribe(...args) }, wsClient: { subscribe(callback: typeof mocks.event) {
  mocks.event = callback; return () => { mocks.event = null; };
} } }));
import { installAgentCatalogue, nativeAgents, nativeAgentsReady } from './catalogue';

let stop: (() => void) | undefined;
const user = (email: string) => state.set({ status: 'authenticated', initialized: true,
  user: { email } as AuthState['user'], expiresAt: null, error: null });
afterEach(() => {
  stop?.(); stop = undefined; vi.resetAllMocks();
  state.set({ status: 'anonymous', initialized: true, user: null, expiresAt: null, error: null });
});

describe('resident agent catalogue', () => {
  it('loads every available user agent without any pin or open window and refreshes on invalidation', async () => {
    const agents = Array.from({ length: 75 }, (_, index) => ({ agent_id: `agent_${index}`, name: `Agent ${index}` } as Agent));
    mocks.list.mockResolvedValue([...agents, { agent_id: 'system_agent', is_system: true }, { agent_id: 'disabled_agent', disabled: true }]);
    stop = installAgentCatalogue();
    expect(mocks.list).not.toHaveBeenCalled();
    user('catalogue@fixture.invalid');
    expect(get(nativeAgentsReady)).toBe(false);
    await vi.waitFor(() => expect(get(nativeAgents)).toHaveLength(75));
    expect(get(nativeAgentsReady)).toBe(true);
    expect(mocks.list).toHaveBeenCalledWith({ include_hidden: true });
    mocks.list.mockResolvedValue(agents.slice(1));
    mocks.event?.({ type: 'scope_invalidated', reason: 'sidebar_changed', revision: 'changed' });
    await vi.waitFor(() => expect(get(nativeAgents)).toHaveLength(74));
  });
  it('clears immediately on logout and fences a late previous-account response', async () => {
    let finish!: (value: Agent[]) => void;
    mocks.list.mockImplementation(() => new Promise((done) => { finish = done; }));
    stop = installAgentCatalogue(); user('first@fixture.invalid');
    state.set({ status: 'anonymous', initialized: true, user: null, expiresAt: null, error: null });
    finish([{ agent_id: 'old_agent' } as Agent]); await Promise.resolve();
    expect(get(nativeAgents)).toEqual([]);
    expect(get(nativeAgentsReady)).toBe(false);
    mocks.list.mockResolvedValue([{ agent_id: 'new_agent' } as Agent]);
    user('next@fixture.invalid');
    await vi.waitFor(() => expect(get(nativeAgents)[0]?.agent_id).toBe('new_agent'));
    expect(get(nativeAgentsReady)).toBe(true);
  });
});
