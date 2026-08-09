import type { TimelineScope } from '$lib/chat-v2/types';
import type { WebSocketWorkInvalidatedEvent } from '$lib/types/api';
import { clearWorkFileTreeStates } from './workFileTreeState';
import { invalidateActivityOverview } from '$lib/activityOverviewCache';

export type WorkViewTab = 'files' | 'commands' | 'mutations' | 'artifacts' | 'results';

export interface WorkViewUiState {
  activeTab: WorkViewTab;
  workstreamFilter: string;
  agentFilter: string;
  statusFilter: string;
  workstreamSearch: string;
  timeRange?: WorkTimeRange;
}

export interface WorkTimeRange {
  from: string | null;
  to: string | null;
  label: string;
}

const states = new Map<string, WorkViewUiState>();
const STORAGE_KEY = 'cognis:work-view-state:v1';
const MAX_STATES = 24;
const MAX_RESPONSE_SCOPES = 12;
const MAX_RESPONSES_PER_SCOPE = 3;
const RESPONSE_TTL_MS = 30_000;
type CachedResponse = { value: unknown; expiresAt: number };
const responseCache = new Map<string, Map<WorkViewTab, CachedResponse>>();

export function workViewIdentity(scope: TimelineScope, sessionId?: string | null): string {
  return `${scope.key}::session=${sessionId ?? 'all'}`;
}

/** In-memory only: projections can be large and must never enter web storage. */
export function getWorkResponseCache<T>(
  scope: TimelineScope,
  tab: WorkViewTab,
  sessionId?: string | null,
): T | null {
  const identity = workViewIdentity(scope, sessionId);
  const scoped = responseCache.get(identity);
  const cached = scoped?.get(tab);
  if (cached && cached.expiresAt <= Date.now()) {
    scoped?.delete(tab);
    if (scoped?.size === 0) responseCache.delete(identity);
    return null;
  }
  if (scoped) {
    responseCache.delete(identity);
    responseCache.set(identity, scoped);
  }
  return (cached?.value as T | undefined) ?? null;
}

export function setWorkResponseCache<T>(
  scope: TimelineScope,
  tab: WorkViewTab,
  value: T,
  sessionId?: string | null,
): void {
  const identity = workViewIdentity(scope, sessionId);
  const scoped = responseCache.get(identity) ?? new Map<WorkViewTab, CachedResponse>();
  scoped.delete(tab);
  scoped.set(tab, { value, expiresAt: Date.now() + RESPONSE_TTL_MS });
  while (scoped.size > MAX_RESPONSES_PER_SCOPE) {
    const oldest = scoped.keys().next().value;
    if (oldest === undefined) break;
    scoped.delete(oldest);
  }
  responseCache.delete(identity);
  responseCache.set(identity, scoped);
  while (responseCache.size > MAX_RESPONSE_SCOPES) {
    const oldest = responseCache.keys().next().value;
    if (typeof oldest !== 'string') break;
    responseCache.delete(oldest);
  }
}

export function clearWorkResponseCache(scopeKey?: string, sessionId?: string | null): void {
  if (!scopeKey) {
    responseCache.clear();
    return;
  }
  if (sessionId !== undefined) {
    responseCache.delete(`${scopeKey}::session=${sessionId ?? 'all'}`);
    return;
  }
  for (const identity of responseCache.keys()) {
    if (identity.startsWith(`${scopeKey}::session=`)) responseCache.delete(identity);
  }
}
const VALID_TABS = new Set<WorkViewTab>([
  'files',
  'commands',
  'mutations',
  'artifacts',
  'results',
]);

function isWorkViewUiState(value: unknown): value is WorkViewUiState {
  if (!value || typeof value !== 'object') return false;
  const state = value as Partial<WorkViewUiState>;
  return (
    typeof state.activeTab === 'string'
    && VALID_TABS.has(state.activeTab as WorkViewTab)
    && typeof state.workstreamFilter === 'string'
    && typeof state.agentFilter === 'string'
    && typeof state.statusFilter === 'string'
    && typeof state.workstreamSearch === 'string'
    && (
      state.timeRange === undefined
      || (
        typeof state.timeRange === 'object'
        && state.timeRange !== null
        && (state.timeRange.from === null || typeof state.timeRange.from === 'string')
        && (state.timeRange.to === null || typeof state.timeRange.to === 'string')
        && typeof state.timeRange.label === 'string'
      )
    )
  );
}

function restorePersistedStates(): void {
  if (typeof sessionStorage === 'undefined') return;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const entries = JSON.parse(raw) as unknown;
    if (!Array.isArray(entries)) return;
    for (const entry of entries.slice(-MAX_STATES)) {
      if (
        Array.isArray(entry)
        && typeof entry[0] === 'string'
        && isWorkViewUiState(entry[1])
        && !states.has(entry[0])
      ) {
        states.set(entry[0], { ...entry[1] });
      }
    }
  } catch {
    // Invalid UI preferences are disposable.
  }
}

function persistStates(): void {
  if (typeof sessionStorage === 'undefined') return;
  try {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([...states.entries()].slice(-MAX_STATES)),
    );
  } catch {
    // Storage can be unavailable in privacy-restricted browsers.
  }
}

export function restoreWorkViewState(scope: TimelineScope, sessionId?: string | null): WorkViewUiState | null {
  const identity = workViewIdentity(scope, sessionId);
  if (!states.has(identity)) restorePersistedStates();
  const legacyState = sessionId == null ? states.get(scope.key) : undefined;
  const state = states.get(identity) ?? legacyState;
  if (!state) return null;
  if (legacyState) states.delete(scope.key);
  states.delete(identity);
  states.set(identity, state);
  persistStates();
  return { ...state };
}

export function saveWorkViewState(scope: TimelineScope, state: WorkViewUiState, sessionId?: string | null): void {
  const identity = workViewIdentity(scope, sessionId);
  states.delete(identity);
  states.set(identity, { ...state });
  while (states.size > MAX_STATES) {
    const oldest = states.keys().next().value;
    if (typeof oldest !== 'string') break;
    states.delete(oldest);
  }
  persistStates();
}

export interface WorkInvalidationDetail {
  scopeKey: string;
  workRevision?: number;
  graphRevision?: number;
  reconnect?: boolean;
}

export function invalidateWorkScope(
  scopeKey: string,
  revisions: Omit<WorkInvalidationDetail, 'scopeKey'> = {},
): void {
  clearWorkResponseCache(scopeKey);
  clearWorkFileTreeStates(scopeKey);
  invalidateActivityOverview(scopeKey);
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent<WorkInvalidationDetail>('cognis:work-invalidated', {
    detail: { scopeKey, ...revisions },
  }));
}

export function invalidateWorkFromSocket(event: WebSocketWorkInvalidatedEvent): void {
  const revision = Number(event.revision);
  invalidateWorkScope(event.work_scope_key, {
    workRevision: Number.isSafeInteger(revision) ? revision : undefined,
  });
}

export function clearWorkViewStates(): void {
  states.clear();
  clearWorkResponseCache();
  clearWorkFileTreeStates();
  if (typeof sessionStorage !== 'undefined') {
    sessionStorage.removeItem(STORAGE_KEY);
  }
}
