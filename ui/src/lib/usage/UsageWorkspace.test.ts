import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthState } from '$lib/stores/auth';
import { createUsageDataSource, type UsageTransport } from './adapter';
import { attempt, catalog, meta, requestPage, response, sessionPage } from './wire.test-data';
import type { Query } from './wire';
import UsageWorkspace from './UsageWorkspace.svelte';
import { fixtureQuery } from './fixtures';
import { usageHistoryState, writeUsageQuery } from './query-state';
import { sessionRef } from './wire.test-data';
const state = vi.hoisted(() => ({ listeners: new Set<(event: { type: string; metadata_only?: boolean }) => void>() }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ status: 'authenticated', initialized: true, user: { email: 'usage-fixture@test.invalid', name: 'Fixture user', role: 'user' }, expiresAt: null, error: null }) };
});
vi.mock('$lib/ws/client', async () => {
  const { writable } = await import('svelte/store');
  return { wsState: writable({ connected: true }), wsClient: { subscribe: (listener: (event: { type: string }) => void) => { state.listeners.add(listener); return () => state.listeners.delete(listener); } } };
});
vi.mock('$lib/system', async () => {
  const { writable } = await import('svelte/store');
  return { usageAnalyticsEnabled: writable(true) };
});
import { auth } from '$lib/stores/auth';
import { usageAnalyticsEnabled } from '$lib/system';
const analyticsEnabled = usageAnalyticsEnabled as ReturnType<typeof writable<boolean>>;
const authStore = auth as unknown as ReturnType<typeof writable<AuthState>>;
const url = new URL('http://localhost/usage?usage_start=2026-10-01&usage_end=2026-10-29');
function backend() {
  const calls = vi.fn();
  const send: UsageTransport = async <T>(path: string, options: Parameters<UsageTransport>[1]) => {
    calls(path, options);
    const body: Query = JSON.parse(String(options?.body));
    if (path === '/api/v1/usage/catalog') return catalog(body.scope) as T;
    if (path === '/api/v1/usage/sessions') return sessionPage('cognis', body.scope) as T;
    if (path === '/api/v1/usage/requests') return requestPage(body.scope) as T;
    if (path.startsWith('/api/v1/usage/attempts')) return { meta: { ...meta(), scope: body.scope }, items: [attempt()], next_cursor: 'next-attempt-page' } as T;
    if (path === '/api/v1/usage/facets') return { meta: meta(), dimension: 'provider', items: [{ key: 'authorized-provider', label: 'Authorized provider' }], truncated: false } as T;
    const value = response();
    value.meta.scope = body.scope;
    value.groups = [{ key: body.group_by === 'session' ? 'visible-session' : 'visible-resource', label: body.group_by === 'session' ? 'Visible session' : 'Visible resource', metrics: value.total }];
    return value as T;
  };
  return { source: createUsageDataSource(send), calls };
}
beforeEach(() => {
  analyticsEnabled.set(true);
  authStore.set({ status: 'authenticated', initialized: true, user: { email: 'usage-fixture@test.invalid', name: 'Fixture user', role: 'user' }, expiresAt: null, error: null });
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
});
afterEach(() => { cleanup(); state.listeners.clear(); });
describe('mounted shipping usage workspace through the real adapter', () => {
  it('does not dispatch or expose charts when analytics is disabled', async () => {
    analyticsEnabled.set(false);
    const { source, calls } = backend();
    render(UsageWorkspace, { source, url });
    await tick();
    expect(screen.getByRole('region', { name: 'Usage analytics disabled' })).toBeTruthy();
    expect(screen.queryByRole('tablist', { name: 'Usage views' })).toBeNull();
    expect(calls).not.toHaveBeenCalled();
    analyticsEnabled.set(true);
    await waitFor(() => expect(calls).toHaveBeenCalled());
    expect(screen.getByRole('tablist', { name: 'Usage views' })).toBeTruthy();
  });
  it('waits for fresh online authority before activating restored private context without an empty request', async () => {
    const { source, calls } = backend();
    const protectedQuery = { ...fixtureQuery, mode: 'personal' as const, surface: 'explorer' as const,
      explorerMode: 'requests' as const, selectionRef: sessionRef, requiresSelection: true,
      detailFilters: { provider_ids: ['authorized-provider'] } };
    const historyState = usageHistoryState(protectedQuery, 'usage-fixture@test.invalid:user');
    authStore.set({ status: 'loading', initialized: false, user: null, expiresAt: null, error: null });
    render(UsageWorkspace, { source, url: writeUsageQuery(url, protectedQuery), historyState });
    await tick();
    expect(calls).not.toHaveBeenCalled();
    const authenticated: AuthState = { status: 'authenticated', initialized: true,
      user: { email: 'usage-fixture@test.invalid', name: 'Fixture user', role: 'user' }, expiresAt: null, error: null };
    authStore.set({ ...authenticated, offline: true });
    await tick();
    expect(calls).not.toHaveBeenCalled();
    authStore.set(authenticated);
    await screen.findByRole('region', { name: 'Sessions and requests explorer' });
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(2));
    expect(calls.mock.calls.map(([path]) => path)).toEqual(['/api/v1/usage/catalog', '/api/v1/usage/requests']);
    expect(JSON.parse(calls.mock.calls[1][1].body)).toMatchObject({
      selection_ref: sessionRef, filters: { provider_ids: ['authorized-provider'] },
    });
    expect(historyState.usage?.context.selectionRef).toBe(sessionRef);
  });
  it('loads visible sections only, lazily discovers facets and drills without transcript access', async () => {
    const { source, calls } = backend();
    render(UsageWorkspace, { source, url });
    await screen.findByRole('heading', { name: 'Estimated cost per day' });
    // Overview: catalog, breakdowns, trend, then the Top spend ranking.
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(4));
    expect(calls.mock.calls.map(([path]) => path)).toEqual(['/api/v1/usage/catalog', '/api/v1/usage/query', '/api/v1/usage/query', '/api/v1/usage/query']);
    await fireEvent.focus(screen.getByRole('combobox', { name: 'Filter provider' }));
    await screen.findByRole('option', { name: 'Authorized provider' });
    expect(calls).toHaveBeenCalledTimes(5);
    await fireEvent.click(screen.getByRole('tab', { name: 'Requests' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'usage-fixture-cognis-session' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Physical trail' }));
    await screen.findByRole('button', { name: /usage-fixture-attempt-1/ });
    expect(screen.getByText('usage-fixture-logical-1')).toBeTruthy();
    expect(calls.mock.calls.at(-1)![0]).toBe('/api/v1/usage/attempts/search');
    await fireEvent.click(screen.getByRole('button', { name: /usage-fixture-attempt-1/ }));
    expect(screen.getByRole('region', { name: 'Attempt details' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: /transcript/i })).toBeNull();
    expect(calls).toHaveBeenCalledTimes(11);
    await fireEvent.click(screen.getByRole('button', { name: 'Next attempt page' }));
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(13));
    expect(JSON.parse(calls.mock.calls.at(-1)![1].body)).toMatchObject({ cursor: 'next-attempt-page' });
  });
  it('keeps My and Project reads fixed to the mounted project and unmounts cleanly', async () => {
    const { source, calls } = backend();
    const mounted = render(UsageWorkspace, { source, url, project: { id: 'fixed-project', label: 'Fixed project' } });
    await screen.findByRole('heading', { name: 'Estimated cost per day' });
    const body = () => JSON.parse(String(calls.mock.calls.at(-1)![1].body)) as Query;
    expect(body()).toMatchObject({ scope: { mode: 'project', project_id: 'fixed-project' }, filters: { project_ids: ['fixed-project'] } });
    await fireEvent.change(screen.getByRole('combobox', { name: 'Usage scope' }), { target: { value: 'personal' } });
    await waitFor(() => expect(body().scope.mode).toBe('personal'));
    expect(body()).toMatchObject({ scope: { project_id: 'fixed-project' }, filters: { project_ids: ['fixed-project'] } });
    expect(screen.queryByRole('combobox', { name: 'Filter project' })).toBeNull();
    mounted.unmount();
    expect(state.listeners.size).toBe(0);
  });
  it('explicit surface navigation starts the first page and clears the opaque cursor requirement together', async () => {
    const { source, calls } = backend();
    render(UsageWorkspace, { url: new URL('http://localhost/usage?usage_scope=personal&usage_surface=explorer&usage_explorer=attempts'), source });
    await screen.findByRole('button', { name: 'Next attempt page' });
    await fireEvent.click(screen.getByRole('button', { name: 'Next attempt page' }));
    await waitFor(() => expect(JSON.parse(calls.mock.calls.at(-1)![1].body)).toHaveProperty('cursor'));
    await fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    await screen.findByRole('heading', { name: 'Estimated cost per day' });
    await fireEvent.click(screen.getByRole('tab', { name: 'Requests' }));
    await screen.findByRole('button', { name: 'Next attempt page' });
    const [path, options] = calls.mock.calls.at(-1)!;
    expect(path).toBe('/api/v1/usage/attempts/search');
    expect(JSON.parse(options.body).cursor).toBeUndefined();
    expect(screen.queryByText(/cursor page is no longer available/)).toBeNull();
  });
  it('ignores streaming events, coalesces invalidations and performs no hidden-view reads', async () => {
    const { source, calls } = backend();
    render(UsageWorkspace, { source, url });
    await screen.findByRole('heading', { name: 'Estimated cost per day' });
    for (const type of ['chat_v2_frame', 'llm_stream', 'thinking']) for (const listener of state.listeners) listener({ type });
    await tick();
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(4));
    for (let i = 0; i < 10; i++) for (const listener of state.listeners) listener({ type: 'turn_settled' });
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(7));
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    for (const listener of state.listeners) listener({ type: 'turn_settled' });
    await tick();
    expect(calls).toHaveBeenCalledTimes(7);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(calls).toHaveBeenCalledTimes(10));
    authStore.set({ status: 'anonymous', initialized: true, user: null, expiresAt: null, error: null });
    await tick();
    expect(screen.queryByRole('heading', { name: 'Estimated cost per day' })).toBeNull();
    expect(screen.queryByText('Visible session')).toBeNull();
  });
});
