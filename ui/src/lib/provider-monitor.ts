import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';
import type { ProviderMonitor } from '$lib/types/api';

export const providerMonitoring = writable<Record<string, ProviderMonitor>>({});

/** Monitor error published when provider OAuth was rejected and needs the user. */
export const REAUTHORIZATION_REQUIRED_ERROR = 'Reauthorization required';

export function needsReauthorization(value: ProviderMonitor | null | undefined): boolean {
  return value?.error === REAUTHORIZATION_REQUIRED_ERROR;
}
const pending = new Map<string, Promise<ProviderMonitor>>();
let owner: string | undefined;
const generations = new Map<string, number>();

export function invalidateProviderMonitor(id: string): void {
  generations.set(id, (generations.get(id) ?? 0) + 1);
  pending.delete(id);
  providerMonitoring.update((values) => {
    const next = { ...values };
    delete next[id];
    return next;
  });
}

function scope(): void {
  const email = auth.getSnapshot().user?.email;
  if (owner !== email) {
    owner = email;
    pending.clear();
    generations.clear();
    providerMonitoring.set({});
  }
}

auth.subscribe(() => scope());

export function publishProviderMonitors(items: ProviderMonitor[]): void {
  scope();
  providerMonitoring.update((values) => {
    const next = { ...values };
    for (const item of items) next[item.provider_id] = item;
    return next;
  });
}

export async function loadProviderMonitor(id: string, force = false): Promise<ProviderMonitor> {
  scope();
  const cached = get(providerMonitoring)[id];
  if (!force && cached && (!cached.monitoring.enabled || (
    cached.next_refresh_at && Date.parse(cached.next_refresh_at) > Date.now()
  ))) return cached;
  const inflight = pending.get(id);
  if (inflight) return inflight;
  const requestOwner = owner;
  const generation = generations.get(id);
  const promise = (force ? api.llmProviders.refreshMonitor(id) : api.llmProviders.monitor(id))
    .then((value) => {
      scope();
      if (requestOwner === owner && generation === generations.get(id)) publishProviderMonitors([value]);
      return value;
    }).finally(() => {
      if (pending.get(id) === promise) pending.delete(id);
    });
  pending.set(id, promise);
  return promise;
}

export function freshnessTone(stamp: string | null | undefined, now = Date.now()): string {
  if (!stamp || !Number.isFinite(Date.parse(stamp))) return 'text-quiet';
  return now - Date.parse(stamp) > 3_600_000 ? 'text-warning' : 'text-muted';
}

export function quotaSummary(value: ProviderMonitor): string {
  if (!value.usage?.ok) return 'Usage unknown';
  const windows = [
    value.usage.primary, value.usage.secondary,
    ...value.usage.additional_rate_limits.flatMap((limit) => [limit.primary, limit.secondary])
  ].filter((window) => window !== null);
  const parts = windows.map((window) => {
    const remaining = Math.max(0, 100 - window.used_percent);
    const minutes = window.window_duration_mins;
    const label = minutes === 300 ? '5h' : minutes === 10080 ? '7d' : minutes === 1 ? '1m' : minutes ? `${minutes}m` : '';
    return `${label ? `${label} ` : ''}${Math.round(remaining)}% left`;
  });
  return parts.length ? parts.join(' · ') : value.usage.limit_reached ? 'Limit reached' : 'Usage unknown';
}
