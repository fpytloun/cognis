import { request } from '$lib/api/client';
import type { CodexUsageWindow, ProviderMonitor } from '$lib/types/api';
import type { QuotaResponse } from './wire';

export interface LimitPoint { at: number; percent: number }
export interface LimitWindow {
  /** Stored sample window key (`primary`, `secondary`, `additional:<sha256>:<role>`). */
  key: string;
  label: string;
  current: number | null;
  /** Native units when the provider reports them (for example characters). */
  used: number | null;
  limit: number | null;
  resetsAt: string | null;
  points: LimitPoint[];
}
export interface ProviderLimits {
  providerId: string;
  name: string;
  plan: string | null;
  windows: LimitWindow[];
  /** Highest current utilization across windows; drives ordering and status. */
  peak: number | null;
}

const MAX_PAGES = 5;
const MAX_HISTORY_MS = 90 * 86_400_000;

/** The history API accepts at most 90 days; keep the newest part of longer windows. */
export function historyWindow(start: string, end: string): { start: string; end: string } {
  const endMs = Date.parse(end);
  const startMs = Math.max(Date.parse(start), endMs - MAX_HISTORY_MS);
  return { start: new Date(startMs).toISOString(), end: new Date(endMs).toISOString() };
}

function duration(window: CodexUsageWindow | null | undefined): string | null {
  const minutes = window?.window_duration_mins ?? null;
  if (!minutes) return null;
  if (minutes % 10080 === 0 && minutes / 10080 <= 1) return '7 days';
  if (minutes % 1440 === 0) return minutes === 1440 ? '24 hours' : `${minutes / 1440} days`;
  if (minutes % 60 === 0) return `${minutes / 60} ${minutes === 60 ? 'hour' : 'hours'}`;
  return `${minutes} min`;
}

/** "5 hours", "7 days"; named extra limits keep their provider name. */
export function windowLabel(role: 'primary' | 'secondary', window: CodexUsageWindow | null | undefined, name?: string | null): string {
  const span = duration(window);
  if (name) return span ? `${name} · ${span}` : name;
  // Providers without a rolling window (character or credit quotas) bill per period.
  return span ?? (role === 'primary' ? 'Quota' : 'Secondary quota');
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

interface LiveWindow { key: string; label: string; window: CodexUsageWindow }

/** Current windows from the monitor, keyed like stored samples (see core/usage/quota.py). */
export async function liveWindows(monitor: ProviderMonitor): Promise<LiveWindow[]> {
  const usage = monitor.usage;
  if (!usage) return [];
  const windows: LiveWindow[] = [];
  for (const role of ['primary', 'secondary'] as const) {
    const window = usage[role];
    if (window) windows.push({ key: role, label: windowLabel(role, window), window });
  }
  for (const extra of (usage.additional_rate_limits ?? []).slice(0, 3)) {
    if (!extra.limit_id) continue;
    const identity = await sha256(extra.limit_id);
    for (const role of ['primary', 'secondary'] as const) {
      const window = extra[role];
      if (window) windows.push({ key: `additional:${identity}:${role}`, label: windowLabel(role, window, extra.limit_name), window });
    }
  }
  return windows;
}

/**
 * Stored observations per window, oldest first. Each stored sample covers the
 * span from its first to its last observation with one value, so both ends are
 * plotted. Unknown utilization is skipped, not zero.
 */
export function pointsByWindow(samples: QuotaResponse['samples']): Map<string, LimitPoint[]> {
  const groups = new Map<string, LimitPoint[]>();
  for (const sample of samples) {
    if (!sample.window) continue;
    const raw = sample.utilization ?? sample.maximum_utilization;
    const percent = raw == null ? NaN : Number(raw);
    if (!Number.isFinite(percent)) continue;
    const value = Math.max(0, Math.min(100, percent));
    const points = groups.get(sample.window) ?? [];
    for (const at of new Set([Date.parse(sample.first_observed_at ?? sample.observed_at), Date.parse(sample.observed_at)])) {
      if (Number.isFinite(at)) points.push({ at, percent: value });
    }
    groups.set(sample.window, points);
  }
  for (const points of groups.values()) points.sort((a, b) => a.at - b.at);
  return groups;
}

export async function providerLimits(monitor: ProviderMonitor, samples: QuotaResponse['samples']): Promise<ProviderLimits | null> {
  const live = await liveWindows(monitor);
  if (!live.length) return null;
  const history = pointsByWindow(samples);
  const windows = live.map(({ key, label, window }): LimitWindow => {
    const limit = window.limit ?? null;
    const used = limit !== null && window.remaining != null ? Math.max(0, limit - window.remaining) : null;
    return { key, label, current: Number.isFinite(window.used_percent) ? window.used_percent : null, used, limit,
      resetsAt: window.resets_at, points: history.get(key) ?? [] };
  });
  const known = windows.map((item) => item.current).filter((value): value is number => value !== null);
  return { providerId: monitor.provider_id, name: monitor.display_name || monitor.provider_id,
    plan: monitor.usage?.plan_type ?? null, windows, peak: known.length ? Math.max(...known) : null };
}

/**
 * Stored samples. Account history needs provider ownership or the admin scope:
 * administrators read every monitored provider, others only providers they own.
 */
export async function loadSubscriptionLimits(start: string, end: string, signal: AbortSignal, mode: 'personal' | 'admin' = 'personal'): Promise<ProviderLimits[]> {
  const monitors = await request<ProviderMonitor[]>('/api/v1/provider-monitor', { signal });
  ({ start, end } = historyWindow(start, end));
  const loaded = await Promise.all(monitors.map(async (monitor) => {
    if (!monitor.usage?.primary && !monitor.usage?.secondary && !monitor.usage?.additional_rate_limits?.length) return null;
    const samples: QuotaResponse['samples'] = [];
    let cursor: string | null = null;
    try {
      for (let page = 0; page < MAX_PAGES; page++) {
        const params = new URLSearchParams({ limit: '500', scope: JSON.stringify({ mode }), start, end });
        if (cursor) params.set('cursor', cursor);
        const response: QuotaResponse = await request<QuotaResponse>(
          `/api/v1/usage/providers/${encodeURIComponent(monitor.provider_id)}/history?${params}`, { signal });
        samples.push(...response.samples);
        cursor = response.next_cursor;
        if (!cursor) break;
      }
    } catch (error) {
      if (signal.aborted) throw error;
    }
    return providerLimits(monitor, samples);
  }));
  return loaded.filter((item): item is ProviderLimits => item !== null)
    .sort((a, b) => (b.peak ?? -1) - (a.peak ?? -1));
}
