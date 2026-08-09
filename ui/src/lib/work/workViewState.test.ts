import { describe, expect, it, vi } from 'vitest';

import { conversationTimelineScope } from '$lib/chat-v2/types';
import {
  clearWorkViewStates,
  clearWorkResponseCache,
  getWorkResponseCache,
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
