import { get, writable } from 'svelte/store';

import { api, asApiError } from '$lib/api/client';
import type { HealthResponse } from '$lib/types/api';

const HEALTH_POLL_INTERVAL_MS = 30_000;

interface WorkspaceHealthState {
  loading: boolean;
  health: HealthResponse | null;
  error: string | null;
  lastUpdatedAt: string | null;
}

const state = writable<WorkspaceHealthState>({
  loading: false,
  health: null,
  error: null,
  lastUpdatedAt: null
});

let pollTimer: number | null = null;

async function refresh(): Promise<void> {
  state.update((current) => ({ ...current, loading: true }));
  try {
    const health = await api.system.health();
    state.set({
      loading: false,
      health,
      error: null,
      lastUpdatedAt: new Date().toISOString()
    });
  } catch (error) {
    state.update((current) => ({
      ...current,
      loading: false,
      error: asApiError(error).message,
      lastUpdatedAt: current.lastUpdatedAt
    }));
  }
}

function clearPolling(): void {
  if (typeof window !== 'undefined' && pollTimer !== null) {
    window.clearInterval(pollTimer);
  }
  pollTimer = null;
}

function handleVisibilityChange(): void {
  if (typeof document === 'undefined') {
    return;
  }
  if (document.hidden) {
    clearPolling();
    return;
  }
  void refresh();
  startPolling();
}

function startPolling(): void {
  if (typeof window === 'undefined' || typeof document === 'undefined' || document.hidden) {
    return;
  }
  clearPolling();
  pollTimer = window.setInterval(() => {
    void refresh();
  }, HEALTH_POLL_INTERVAL_MS);
}

let visibilityListenerRegistered = false;

export const workspaceHealth = {
  subscribe: state.subscribe,
  async refresh(): Promise<void> {
    await refresh();
  },
  start(): void {
    if (visibilityListenerRegistered || typeof document === 'undefined') {
      void refresh();
      startPolling();
      return;
    }
    document.addEventListener('visibilitychange', handleVisibilityChange);
    visibilityListenerRegistered = true;
    void refresh();
    startPolling();
  },
  stop(): void {
    clearPolling();
    if (visibilityListenerRegistered && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      visibilityListenerRegistered = false;
    }
  }
};

export function hasBlockingProviderOutage(): boolean {
  const health = get(state).health;
  if (!health) {
    return false;
  }
  return ['guardrails', 'llm'].some((providerName) => {
    const status = String(health.providers?.[providerName]?.status ?? 'unknown');
    return status !== 'healthy' && status !== 'unknown';
  });
}

export function isMemoryDegraded(): boolean {
  const health = get(state).health;
  const status = String(health?.providers?.memory?.status ?? 'unknown');
  return status !== 'healthy' && status !== 'unknown';
}

export function isLlmConfigured(): boolean {
  const llm = get(state).health?.providers?.llm;
  const detail = JSON.stringify(llm ?? {}).toLowerCase();
  return !detail.includes('no llm model configured') && !detail.includes('not configured');
}
