import { render, screen } from '@testing-library/svelte';
import { get, writable } from 'svelte/store';
import { describe, expect, it, vi } from 'vitest';
import type { ProviderMonitor } from '$lib/types/api';
import ProviderHealthLine from './ProviderHealthLine.svelte';

const { monitor, refreshMonitor } = vi.hoisted(() => ({ monitor: vi.fn(), refreshMonitor: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { llmProviders: { monitor, refreshMonitor } } }));
vi.mock('$lib/stores/auth', () => {
  const store = writable({ user: { email: 'owner@cognis.test', role: 'user' } });
  return { auth: { subscribe: store.subscribe, getSnapshot: () => get(store) } };
});

const rejected: ProviderMonitor = {
  provider_id: 'claude max', display_name: 'Claude Max',
  monitoring: { enabled: true, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
  health_status: 'error',
  health_detail: 'Claude Max sign-in expired or was revoked. Reauthorize the provider in Settings > Providers.',
  health_checked_at: '2026-11-08T20:05:36Z', checked_at: '2026-11-09T00:05:34Z',
  next_refresh_at: '2099-01-01T00:00:00Z', usage: null, error: 'Reauthorization required',
  alert_severity: 'critical', stale: true,
};

describe('provider health line', () => {
  it('turns rejected credentials into a reauthorization link instead of a generic failure', async () => {
    monitor.mockResolvedValue(rejected);
    render(ProviderHealthLine, { providerId: 'claude max' });
    const link = await screen.findByRole('alert');
    expect(link).toHaveTextContent('Claude Max sign-in expired or was revoked.');
    expect(link).toHaveAttribute('href', '/settings?tab=providers&provider=claude%20max');
    expect(screen.getByRole('img', { name: 'Provider health: critical' })).toBeInTheDocument();
    expect(screen.queryByText('Reauthorization required')).not.toBeInTheDocument();
  });
});
