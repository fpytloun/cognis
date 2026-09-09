import { describe, expect, it, vi } from 'vitest';

import { conversationTimelineScope } from '$lib/chat-v2/types';
import {
  clearWorkViewStates,
  clearWorkResponseCache,
  getWorkResponseCache,
  invalidateAllWorkScopes,
  invalidateWorkFromSocket,
  invalidateWorkScope,
  restoreWorkViewState,
  saveWorkViewState,
  setWorkResponseCache,
} from './workViewState';

describe('workViewState', () => {
  it('keeps independent UI state for each scope', () => {
    const first = conversationTimelineScope('conversation-a');
    const second = conversationTimelineScope('conversation-b');
    saveWorkViewState(first, {
      activeTab: 'commands',
      workstreamFilter: 'root',
      agentFilter: 'laforge',
      statusFilter: 'running',
      workstreamSearch: 'frontend',
    });
    saveWorkViewState(second, {
      activeTab: 'results',
      workstreamFilter: 'all',
      agentFilter: 'all',
      statusFilter: 'all',
      workstreamSearch: '',
    });

    expect(restoreWorkViewState(first)?.activeTab).toBe('commands');
    expect(restoreWorkViewState(second)?.activeTab).toBe('results');
  });

  it('dispatches a scope-keyed realtime invalidation', () => {
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);
    invalidateWorkScope('conversation:conversation-a');
    expect(listener).toHaveBeenCalledOnce();
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      scopeKey: 'conversation:conversation-a',
      overviewAdvanced: true,
    });
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('includes typed Work revisions in realtime invalidations', () => {
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);
    invalidateWorkScope('conversation:conversation-a', {
      workRevision: 2,
      graphRevision: 3,
    });
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      scopeKey: 'conversation:conversation-a',
      workRevision: 2,
      graphRevision: 3,
      overviewAdvanced: true,
    });
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('maps the backend Work invalidation envelope to the matching scope', () => {
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);
    invalidateWorkFromSocket({
      type: 'work_invalidated',
      reason: 'work_invalidated',
      revision: '42',
      work_scope_key: 'conversation:conversation-a',
    });
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      scopeKey: 'conversation:conversation-a',
      workRevision: 42,
      overviewAdvanced: true,
    });
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('maps owner-wide Work invalidation to every visible scope', () => {
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);
    invalidateWorkFromSocket({
      type: 'work_invalidated',
      reason: 'work_invalidated',
      revision: '43',
      work_scope_key: '*',
    });
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      scopeKey: '',
      workRevision: 43,
      reconnect: false,
    });
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('rejects duplicate and out-of-order owner-wide revisions', () => {
    clearWorkViewStates();
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);
    for (const revision of ['43', '43', '42', '44']) {
      invalidateWorkFromSocket({
        type: 'work_invalidated',
        reason: 'work_invalidated',
        revision,
        work_scope_key: '*',
      });
    }
    expect(listener).toHaveBeenCalledTimes(2);
    expect((listener.mock.calls[1][0] as CustomEvent).detail.workRevision).toBe(44);
    listener.mockClear();
    for (let index = 0; index < 30; index += 1) {
      invalidateWorkScope(`conversation:eviction-${index}`, { workRevision: 100 + index });
    }
    invalidateWorkFromSocket({
      type: 'work_invalidated',
      reason: 'work_invalidated',
      revision: '43',
      work_scope_key: '*',
    });
    expect(listener).toHaveBeenCalledTimes(30);
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('clears known Work responses and broadcasts a reconnect invalidation', () => {
    const scope = conversationTimelineScope('conversation-reconnect');
    setWorkResponseCache(scope, 'files', 'stale');
    const listener = vi.fn();
    window.addEventListener('cognis:work-invalidated', listener);

    invalidateAllWorkScopes();

    expect(getWorkResponseCache(scope, 'files')).toBeNull();
    expect((listener.mock.calls.at(-1)?.[0] as CustomEvent).detail).toEqual({
      scopeKey: '',
      reconnect: true,
    });
    window.removeEventListener('cognis:work-invalidated', listener);
  });

  it('restores compact per-scope UI state from session storage after reload', () => {
    const scope = conversationTimelineScope('conversation-reload');
    saveWorkViewState(scope, {
      activeTab: 'artifacts',
      workstreamFilter: 'root',
      agentFilter: 'laforge',
      statusFilter: 'running',
      workstreamSearch: 'migration',
    });
    const persisted = sessionStorage.getItem('cognis:work-view-state:v1');
    expect(persisted).toBeTruthy();

    clearWorkViewStates();
    sessionStorage.setItem('cognis:work-view-state:v1', persisted!);

    expect(restoreWorkViewState(scope)).toEqual({
      activeTab: 'artifacts',
      workstreamFilter: 'root',
      agentFilter: 'laforge',
      statusFilter: 'running',
      workstreamSearch: 'migration',
    });
  });

  it('isolates cached responses by scope and category', () => {
    const first = conversationTimelineScope('cache-a');
    const second = conversationTimelineScope('cache-b');
    setWorkResponseCache(first, 'files', 'files-a');
    setWorkResponseCache(first, 'commands', 'commands-a');
    setWorkResponseCache(second, 'files', 'files-b');
    expect(getWorkResponseCache(first, 'files')).toBe('files-a');
    expect(getWorkResponseCache(first, 'commands')).toBe('commands-a');
    expect(getWorkResponseCache(second, 'files')).toBe('files-b');
    clearWorkResponseCache(first.key);
    expect(getWorkResponseCache(first, 'files')).toBeNull();
    expect(getWorkResponseCache(second, 'files')).toBe('files-b');
  });

  it('isolates cached responses by complete time-range query identity', () => {
    const scope = conversationTimelineScope('cache-ranges');
    const lastHour = { from: '2026-08-16T07:00:00Z', to: '2026-08-16T08:00:00Z', admittedRevision: 4 };
    const allTime = { from: null, to: null, admittedRevision: 4 };
    setWorkResponseCache(scope, 'files', 'hour', null, lastHour);
    setWorkResponseCache(scope, 'files', 'all', null, allTime);
    expect(getWorkResponseCache(scope, 'files', null, lastHour)).toBe('hour');
    expect(getWorkResponseCache(scope, 'files', null, allTime)).toBe('all');
  });

  it('does not restore a response admitted before a newer invalidation revision', () => {
    const scope = conversationTimelineScope('cache-revision-race');
    const query = { from: null, to: null, admittedRevision: 4 };
    setWorkResponseCache(scope, 'files', 'revision-4', null, query);
    expect(getWorkResponseCache(scope, 'files', null, query)).toBe('revision-4');
    invalidateWorkScope(scope.key, { workRevision: 5 });
    expect(getWorkResponseCache(scope, 'files', null, query)).toBeNull();
    setWorkResponseCache(scope, 'files', 'late-revision-4', null, query);
    expect(getWorkResponseCache(scope, 'files', null, query)).toBeNull();
    const current = { ...query, admittedRevision: 5 };
    setWorkResponseCache(scope, 'files', 'revision-5', null, current);
    expect(getWorkResponseCache(scope, 'files', null, current)).toBe('revision-5');
  });

  it('isolates response and UI state by exact session and clears all variants by scope', () => {
    const scope = conversationTimelineScope('cache-session-variants');
    setWorkResponseCache(scope, 'files', 'session-a-value', 'session-a');
    setWorkResponseCache(scope, 'files', 'session-b-value', 'session-b');
    setWorkResponseCache(scope, 'files', 'all-value');
    expect(getWorkResponseCache(scope, 'files', 'session-a')).toBe('session-a-value');
    expect(getWorkResponseCache(scope, 'files', 'session-b')).toBe('session-b-value');
    expect(getWorkResponseCache(scope, 'files')).toBe('all-value');

    saveWorkViewState(scope, {
      activeTab: 'commands', workstreamFilter: 'all', agentFilter: 'all',
      statusFilter: 'all', workstreamSearch: '',
    }, 'session-a');
    saveWorkViewState(scope, {
      activeTab: 'mutations', workstreamFilter: 'all', agentFilter: 'all',
      statusFilter: 'all', workstreamSearch: '',
    }, 'session-b');
    expect(restoreWorkViewState(scope, 'session-a')?.activeTab).toBe('commands');
    expect(restoreWorkViewState(scope, 'session-b')?.activeTab).toBe('mutations');

    invalidateWorkScope(scope.key);
    expect(getWorkResponseCache(scope, 'files', 'session-a')).toBeNull();
    expect(getWorkResponseCache(scope, 'files', 'session-b')).toBeNull();
    expect(getWorkResponseCache(scope, 'files')).toBeNull();
  });

  it('evicts the least recently used response scope', () => {
    clearWorkResponseCache();
    for (let index = 0; index < 13; index += 1) {
      setWorkResponseCache(conversationTimelineScope(`lru-${index}`), 'files', index);
    }
    expect(getWorkResponseCache(conversationTimelineScope('lru-0'), 'files')).toBeNull();
    expect(getWorkResponseCache(conversationTimelineScope('lru-12'), 'files')).toBe(12);
  });

  it('keeps at most three cached categories per response scope', () => {
    const scope = conversationTimelineScope('category-bound');
    setWorkResponseCache(scope, 'files', 'files');
    setWorkResponseCache(scope, 'commands', 'commands');
    setWorkResponseCache(scope, 'mutations', 'mutations');
    setWorkResponseCache(scope, 'artifacts', 'artifacts');
    expect(getWorkResponseCache(scope, 'files')).toBeNull();
    expect(getWorkResponseCache(scope, 'artifacts')).toBe('artifacts');
  });
});
