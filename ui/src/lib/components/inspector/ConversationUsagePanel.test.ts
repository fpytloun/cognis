import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { conversationTimelineScope, sessionTimelineScope } from '$lib/chat-v2/types';
import ConversationUsagePanel from './ConversationUsagePanel.svelte';
import type { UsageResult, UsageSource } from './conversation-usage';
import { auth, type AuthState } from '$lib/stores/auth';
import type { Writable } from 'svelte/store';
import { meta, metrics } from '$lib/usage/wire.test-data';
import type { UsageSummaryResponse } from '$lib/types/api';

const transport = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('$lib/usage/conversation-api', () => ({ fetchUsageSummaries: transport.fetch }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({}) };
});
const authStore = auth as unknown as Writable<AuthState>;
const signedIn: AuthState = {
  initialized: true, status: 'authenticated', user: { email: 'usage-owner@cognis.test', name: 'Owner', role: 'user' },
  expiresAt: null, error: null,
};

const hooks = vi.hoisted(() => ({
  reconnect: null as (() => void) | null,
  event: null as ((event: { type: string; conversation_id?: string; metadata_only?: boolean }) => void) | null,
}));
vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: (callback: NonNullable<typeof hooks.event>) => {
      hooks.event = callback;
      return () => { hooks.event = null; };
    },
  },
}));
vi.mock('$lib/connectivity', () => ({
  onConnectivityRestored: (callback: () => void) => {
    hooks.reconnect = callback;
    return () => { hooks.reconnect = null; };
  },
}));
const result: UsageResult = {
  status: 'ready',
  summary: {
    coverage: 'partial', unpricedAttempts: null,
    costs: [
      { amount: '0.0000123400', currency: 'USD', basis: 'historical', complete: false },
      { amount: '0', currency: 'EUR', basis: 'current estimate', complete: true },
    ],
    tokens: { input: 120, output: 40, cacheRead: null, cacheWrite: 0, reasoning: null },
    attempts: { total: 3, succeeded: 2, failed: null },
    notes: ['Some contributions are not priced.'],
  },
};
describe('ConversationUsagePanel', () => {
  beforeEach(() => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    authStore.set(signedIn);
    transport.fetch.mockReset();
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('keeps both radio label hit areas at 44 CSS pixels with a 14px root font', async () => {
    const previous = document.documentElement.style.fontSize;
    document.documentElement.style.fontSize = '14px';
    try {
      render(ConversationUsagePanel, {
        scope: conversationTimelineScope('retained-conversation'), source: async () => result,
      });
      await screen.findByText('€0.00');
      for (const radio of screen.getAllByRole('radio')) {
        expect(getComputedStyle(radio.closest('label')!).minHeight).toBe('44px');
      }
      await fireEvent.click(screen.getByRole('radio', { name: 'Including children' }));
      expect(screen.getByRole('radio', { name: 'Including children' })).toBeChecked();
    } finally {
      document.documentElement.style.fontSize = previous;
    }
  });

  it('shows rounded per-currency costs, real zero and no combined total', async () => {
    const source = vi.fn<UsageSource>(async () => result);
    render(ConversationUsagePanel, { scope: conversationTimelineScope('retained-conversation'), source });
    const usd = await screen.findByText('<$0.01');
    expect(usd).toHaveAttribute('title', '<$0.01 USD · estimated');
    expect(screen.getByText('€0.00')).toHaveAttribute('title', '€0.00 EUR · estimated');
    expect(screen.getByText('EUR')).toBeInTheDocument();
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Fresh input 75%, cached input 0%, output 25%' })).toBeInTheDocument();
    // Facts only: no collapsed accounting dump.
    expect(screen.queryByText('Accounting details')).toBeNull();
    await fireEvent.click(screen.getByRole('radio', { name: 'Including children' }));
    await waitFor(() => expect(source).toHaveBeenLastCalledWith(
      { kind: 'conversation', id: 'retained-conversation' }, 'inclusive', expect.any(AbortSignal),
    ));
  });
  it('labels provider-reported media charges separately from estimates', async () => {
    render(ConversationUsagePanel, {
      scope: conversationTimelineScope('retained-conversation'),
      source: async () => ({
        ...result,
        summary: { ...result.summary, costs: [
          { amount: '0.25', currency: 'USD', basis: 'provider_reported', complete: true }
        ] }
      })
    });
    expect(await screen.findByText('$0.25')).toBeInTheDocument();
    expect(screen.getByText('Cost', { exact: true })).toBeInTheDocument();
    expect(screen.getByText('USD · Provider-reported cost')).toBeInTheDocument();
    expect(screen.queryByText('Estimated cost', { exact: true })).toBeNull();
  });

  it('gates reads on panel and document visibility and scopes invalidation', async () => {
    const source = vi.fn<UsageSource>(async () => result);
    const scope = sessionTimelineScope('delegate-session', 'parent-conversation');
    const view = render(ConversationUsagePanel, { scope, source, visible: false });
    hooks.reconnect?.();
    expect(source).not.toHaveBeenCalled();
    await view.rerender({ visible: true });
    await waitFor(() => expect(source).toHaveBeenCalledOnce());
    expect(screen.getByRole('radio', { name: 'Session only' })).toBeChecked();
    window.dispatchEvent(new CustomEvent('cognis:work-invalidated', { detail: { scopeKey: 'conversation:parent-conversation' } }));
    expect(source).toHaveBeenCalledOnce();
    window.dispatchEvent(new CustomEvent('cognis:work-invalidated', { detail: { scopeKey: scope.key } }));
    await waitFor(() => expect(source).toHaveBeenCalledTimes(2));
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(screen.queryByText('€0.00')).not.toBeInTheDocument());
    hooks.reconnect?.();
    expect(source).toHaveBeenCalledTimes(2);
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(source).toHaveBeenCalledTimes(3));
    hooks.reconnect?.();
    await waitFor(() => expect(source).toHaveBeenCalledTimes(4));
    view.unmount();
    expect(hooks.reconnect).toBeNull();
  });
  it('does not fabricate production totals when no source is connected', async () => {
    render(ConversationUsagePanel, { scope: conversationTimelineScope('retained-conversation'), source: null });
    expect(await screen.findByText('Accounting data is not connected yet.')).toBeInTheDocument();
    expect(screen.queryByText('Cost')).not.toBeInTheDocument();
  });
  it('refreshes on applicable authenticated events without reading hidden or unrelated targets', async () => {
    const source = vi.fn<UsageSource>(async () => result);
    const view = render(ConversationUsagePanel, { scope: conversationTimelineScope('retained-conversation'), source });
    await waitFor(() => expect(source).toHaveBeenCalledOnce());
    hooks.event?.({ type: 'turn_settled', conversation_id: 'unrelated-conversation' });
    expect(source).toHaveBeenCalledOnce();
    hooks.event?.({ type: 'turn_settled', conversation_id: 'retained-conversation' });
    await waitFor(() => expect(source).toHaveBeenCalledTimes(2));
    await view.rerender({ visible: false });
    hooks.event?.({ type: 'scope_invalidated', metadata_only: false });
    expect(source).toHaveBeenCalledTimes(2);
    view.unmount();
    expect(hooks.event).toBeNull();
  });
  it('uses the default source with the exact raw contract and a fixed execution anchor', async () => {
    transport.fetch.mockImplementation(async (body): Promise<UsageSummaryResponse> => ({
      meta: meta(), items: [{
        selector: body.items[0], inclusion: body.inclusion, available: true,
        metrics: metrics(), physical_sessions: 3,
      }],
    }));
    const view = render(ConversationUsagePanel, {
      scope: sessionTimelineScope('pinned-physical-anchor', 'parent-conversation'),
    });
    await screen.findByTitle('$0.12 USD · estimated');
    expect(transport.fetch).toHaveBeenLastCalledWith({
      items: [{ kind: 'execution', id: 'pinned-physical-anchor' }],
      inclusion: 'direct', pricing_basis: 'historical',
    }, { signal: expect.any(AbortSignal) });
    await fireEvent.click(screen.getByRole('radio', { name: 'Including children' }));
    await waitFor(() => expect(transport.fetch).toHaveBeenCalledTimes(2));
    expect(transport.fetch.mock.calls[1][0]).toEqual({
      items: [{ kind: 'execution', id: 'pinned-physical-anchor' }],
      inclusion: 'inclusive', pricing_basis: 'historical',
    });
    await view.rerender({ scope: conversationTimelineScope('managed-child-conversation') });
    await waitFor(() => expect(transport.fetch).toHaveBeenCalledTimes(3));
    expect(transport.fetch.mock.calls[2][0].items).toEqual([{ kind: 'conversation', id: 'managed-child-conversation' }]);
  });

  it('waits for fresh auth and cancels/clears on actor changes and logout', async () => {
    authStore.set({ ...signedIn, offline: true });
    const signals: AbortSignal[] = [];
    const pending: ((value: UsageResult) => void)[] = [];
    const source = vi.fn<UsageSource>((_target, _mode, signal) => {
      signals.push(signal);
      return new Promise((resolve) => pending.push(resolve));
    });
    render(ConversationUsagePanel, { scope: conversationTimelineScope('retained-conversation'), source });
    expect(await screen.findByText('Usage requires a verified online sign-in.')).toBeInTheDocument();
    expect(source).not.toHaveBeenCalled();
    authStore.set(signedIn);
    await waitFor(() => expect(source).toHaveBeenCalledOnce());
    authStore.set({ ...signedIn, user: { ...signedIn.user!, email: 'other-owner@cognis.test' } });
    await waitFor(() => expect(source).toHaveBeenCalledTimes(2));
    expect(signals[0].aborted).toBe(true);
    pending[0](result);
    await Promise.resolve();
    expect(screen.queryByText('€0.00')).not.toBeInTheDocument();
    authStore.set({ ...signedIn, status: 'anonymous', user: null });
    await screen.findByText('Usage requires a verified online sign-in.');
    expect(signals[1].aborted).toBe(true);
    pending[1](result);
    await Promise.resolve();
    expect(screen.queryByText('€0.00')).not.toBeInTheDocument();
  });
});
