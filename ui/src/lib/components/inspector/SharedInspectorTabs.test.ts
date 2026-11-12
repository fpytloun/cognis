// @ts-nocheck -- focused live lifecycle tests own lifecycle fixture validation.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActivityOverviewResponse, TimelineScope } from '$lib/chat-v2/types';
import { conversationTimelineScope } from '$lib/chat-v2/types';
import {
  clearActivityOverview,
  getActivityOverview,
  requestActivityOverview,
} from '$lib/activityOverviewCache';

const mocks = vi.hoisted(() => ({
  activityOverview: vi.fn(),
  intarisDetail: vi.fn(),
  summaries: vi.fn(),
}));
vi.mock('$lib/usage/conversation-api', () => ({ fetchUsageSummaries: mocks.summaries }));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: {
    activityOverview: mocks.activityOverview,
  },
}));

vi.mock('$lib/api/client', () => ({
  api: {
    sessions: { intarisDetail: mocks.intarisDetail },
    llmProviders: { providerUsage: vi.fn() },
  },
}));

import SharedInspectorTabs from './SharedInspectorTabs.svelte';
// Controlled loader promises below own readiness, not fixture compilation time.
import WorkView from '$lib/components/work/WorkView.svelte';
import SessionDetailsBody from '$lib/components/session/SessionDetailsBody.svelte';
import * as workLoader from '$lib/components/work/work-view-loader';
import * as sessionLoader from '$lib/components/session/session-details-loader';
vi.mock('$lib/stores/auth', () => ({
  auth: {
    getSnapshot: () => ({ initialized: true, status: 'authenticated', user: { email: 'usage-owner@cognis.test' } }),
    subscribe: (run) => {
      run({ initialized: true, status: 'authenticated', user: { email: 'usage-owner@cognis.test' } });
      return () => {};
    },
  },
}));

const scope: TimelineScope = {
  key: 'task_step:run-1',
  kind: 'task_step',
  conversation_id: 'conversation-1',
  session_id: 'session-1',
  step_run_id: 'run-1',
};

function overview(targetScope: TimelineScope = scope): ActivityOverviewResponse {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope: targetScope,
    summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0 },
    materialization: { state: 'live' },
    workstreams: [],
    recent: {},
    graph_fingerprint: 'graph',
    graph_truncated: false,
  };
}

function overviewWithFocusedSession(): ActivityOverviewResponse {
  return {
    ...overview(),
    workstreams: [{
      key: 'root',
      kind: 'conversation',
      parent_key: null,
      root_key: 'root',
      edge_kind: 'root',
      ordinal: 0,
      conversation_id: 'conversation-1',
      session_id: 'session-1',
      event_store_session_id: 'session-1',
      title: 'Focused root session',
      agent_id: 'riker',
      status: 'running',
      current: true,
      superseded: false,
      activity_state: 'ongoing',
      execution_state: 'running',
    }],
  };
}

function overviewWithRotatedFocusedSession(): ActivityOverviewResponse {
  const value = overviewWithFocusedSession();
  return {
    ...value,
    workstreams: value.workstreams.map((node) => ({
      ...node,
      session_id: 'session-current',
      backing_session_ids: ['session-1'],
      title: 'Rotated focused session',
    })),
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

describe('SharedInspectorTabs', () => {
  afterEach(cleanup);
  afterEach(() => vi.restoreAllMocks());

  beforeEach(() => {
    clearActivityOverview();
    mocks.summaries.mockReset().mockImplementation(async (body) => ({
      meta: {}, items: body.items.map((selector) => ({
        selector, inclusion: body.inclusion, available: false, metrics: null,
      })),
    }));
    mocks.activityOverview.mockReset().mockResolvedValue(overview());
    mocks.intarisDetail.mockReset().mockResolvedValue({
      session_id: 'session-1',
      intaris_session_id: 'intaris-1',
      intention: 'Implement parity',
      summary: 'Session summary',
      status: 'completed',
      total_calls: 2,
      approved_count: 2,
      denied_count: 0,
      escalated_count: 0,
    });
  });

  it('shares the Overview, Work, and Session tab contract with task drawers', async () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });

    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Work' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('tab', { name: 'Session' }));

    await waitFor(() => expect(mocks.intarisDetail).toHaveBeenCalledWith('session-1'));
    expect((await screen.findAllByText('Session summary')).length).toBeGreaterThan(0);
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'shared-inspector-tab-session');
    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('loads the session renderer only on Session and ignores completion after leaving', async () => {
    const pending = deferred();
    const load = vi.spyOn(sessionLoader, 'loadSessionDetails').mockReturnValue(pending.promise);
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    expect(load).not.toHaveBeenCalled();
    const overviewTab = screen.getByRole('tab', { name: 'Overview' });
    overviewTab.focus();
    await fireEvent.keyDown(overviewTab, { key: 'End' });
    const usageTab = screen.getByRole('tab', { name: 'Usage' });
    await waitFor(() => expect(usageTab).toHaveFocus());
    await fireEvent.keyDown(usageTab, { key: 'ArrowLeft' });
    const sessionTab = screen.getByRole('tab', { name: 'Session' });
    await waitFor(() => expect(sessionTab).toHaveFocus());
    expect(await screen.findByRole('status')).toHaveTextContent('Loading session details');
    expect(load).toHaveBeenCalledOnce();
    await fireEvent.keyDown(sessionTab, { key: 'Home' });
    await waitFor(() => expect(overviewTab).toHaveFocus());
    pending.resolve(SessionDetailsBody);
    await pending.promise;
    await Promise.resolve();
    expect(screen.queryByTestId('session-details-content')).not.toBeInTheDocument();
    expect(overviewTab).toHaveAttribute('aria-selected', 'true');
    await fireEvent.click(sessionTab);
    await screen.findByTestId('session-details-content');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('opens optional Work by keyboard, keeps tab focus, and ignores a late module after leaving Work', async () => {
    const pending = deferred();
    const moduleLoad = vi.spyOn(workLoader, 'loadWorkView').mockReturnValue(pending.promise);
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });
    const overviewTab = screen.getByRole('tab', { name: 'Overview' });
    overviewTab.focus();
    await fireEvent.keyDown(overviewTab, { key: 'ArrowRight' });
    const workTab = screen.getByRole('tab', { name: 'Work' });
    await waitFor(() => expect(workTab).toHaveFocus());
    expect(workTab).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Loading Work inspector…')).toBeInTheDocument();
    expect(moduleLoad).toHaveBeenCalledTimes(1);
    await fireEvent.keyDown(workTab, { key: 'ArrowRight' });
    const sessionTab = screen.getByRole('tab', { name: 'Session' });
    await waitFor(() => expect(sessionTab).toHaveFocus());
    pending.resolve(WorkView);
    await screen.findAllByText('Session summary');
    expect(screen.queryByText('Loading Work inspector…')).toBeNull();
    expect(sessionTab).toHaveAttribute('aria-selected', 'true');
  });

  it('reuses the same controlled tab header in the chat Inspector without duplicate loading', () => {
    render(SharedInspectorTabs, {
      scope,
      sessionId: 'session-1',
      activeTab: 'session',
      headerOnly: true,
      idPrefix: 'conversation-info',
      testIdPrefix: 'conversation-info',
    });

    expect(screen.getByRole('tab', { name: 'Session' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tabpanel')).not.toBeInTheDocument();
    expect(mocks.activityOverview).not.toHaveBeenCalled();
    expect(mocks.intarisDetail).not.toHaveBeenCalled();
  });

  it('provides the fourth Usage tab with keyboard navigation and matching panel ARIA', async () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });
    const overviewTab = screen.getByRole('tab', { name: 'Overview' });
    overviewTab.focus();
    await fireEvent.keyDown(overviewTab, { key: 'End' });
    const usageTab = screen.getByRole('tab', { name: 'Usage' });
    await waitFor(() => expect(usageTab).toHaveFocus());
    expect(usageTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'shared-inspector-tab-usage');
    expect(await screen.findByText('Usage is unavailable for this selected scope.')).toBeInTheDocument();
    expect(mocks.intarisDetail).not.toHaveBeenCalled();
    await fireEvent.keyDown(usageTab, { key: 'ArrowRight' });
    await waitFor(() => expect(overviewTab).toHaveFocus());
  });

  it('renders a Usage header only without mounting any data panel', () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1', activeTab: 'usage', headerOnly: true });
    expect(screen.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('region', { name: 'Usage accounting' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tabpanel')).not.toBeInTheDocument();
    expect(mocks.activityOverview).not.toHaveBeenCalled();
    expect(mocks.intarisDetail).not.toHaveBeenCalled();
    expect(mocks.summaries).not.toHaveBeenCalled();
  });

  it('loads the default API source only on the active Usage body, never the header', async () => {
    render(SharedInspectorTabs, { scope: conversationTimelineScope('retained-conversation'), sessionId: 'active-physical-session' });
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    expect(mocks.summaries).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('tab', { name: 'Usage' }));
    await waitFor(() => expect(mocks.summaries).toHaveBeenCalledOnce());
    expect(mocks.summaries).toHaveBeenLastCalledWith({
      items: [{ kind: 'conversation', id: 'retained-conversation' }],
      inclusion: 'direct', pricing_basis: 'historical',
    }, { signal: expect.any(AbortSignal) });
    expect(await screen.findByText('Accounting is unavailable for this selection or its lineage.')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(mocks.summaries).toHaveBeenCalledOnce();
  });

  it('keeps Usage selected when inspecting a different scope or a rotated session', async () => {
    const view = render(SharedInspectorTabs, { scope, sessionId: 'session-1', initialTab: 'usage' });
    await screen.findByText('Usage is unavailable for this selected scope.');
    await view.rerender({
      scope: conversationTimelineScope('selected-managed-conversation'),
      sessionId: 'rotated-session',
    });
    expect(screen.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByRole('region', { name: 'Usage accounting' })).toBeInTheDocument();
    expect(mocks.intarisDetail).not.toHaveBeenCalled();
    expect(mocks.activityOverview).not.toHaveBeenCalled();
  });

  it('loads one lightweight overview without an automatic exact follow-up', async () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });

    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    expect(mocks.activityOverview.mock.calls[0]?.[1]).not.toMatchObject({ detail: 'full' });
  });

  it('resolves the focused session card from the same activity overview', async () => {
    mocks.activityOverview.mockResolvedValue(overviewWithFocusedSession());
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });

    expect(await screen.findByTestId('focused-session-card')).toHaveTextContent('Focused root session');
    expect(screen.queryByTestId('focused-session-loading')).not.toBeInTheDocument();
  });

  it('resolves a rotated focused session through its backing session IDs', async () => {
    mocks.activityOverview.mockResolvedValue(overviewWithRotatedFocusedSession());
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });

    expect(await screen.findByTestId('focused-session-card')).toHaveTextContent('Rotated focused session');
    expect(screen.queryByTestId('focused-session-loading')).not.toBeInTheDocument();
  });

  it('keeps Session content visible during a background Overview refresh', async () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    await fireEvent.click(screen.getByRole('tab', { name: 'Session' }));
    expect((await screen.findAllByText('Session summary')).length).toBeGreaterThan(0);

    window.dispatchEvent(new CustomEvent('cognis:work-invalidated', {
      detail: { scopeKey: scope.key },
    }));

    expect(screen.getAllByText('Session summary').length).toBeGreaterThan(0);
    expect(screen.queryByText('Loading inspector…')).not.toBeInTheDocument();
  });

  it('does not supersede a pending Session load with a background Overview refresh', async () => {
    const sessionDetail = deferred<Awaited<ReturnType<typeof mocks.intarisDetail>>>();
    mocks.intarisDetail.mockReturnValue(sessionDetail.promise);
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());

    await fireEvent.click(screen.getByRole('tab', { name: 'Session' }));
    window.dispatchEvent(new CustomEvent('cognis:work-invalidated', {
      detail: { scopeKey: scope.key },
    }));
    sessionDetail.resolve({
      session_id: 'session-1',
      intaris_session_id: 'intaris-1',
      intention: 'Implement parity',
      summary: 'Loaded after refresh',
      status: 'completed',
      total_calls: 2,
      approved_count: 2,
      denied_count: 0,
      escalated_count: 0,
    });

    expect((await screen.findAllByText('Loaded after refresh')).length).toBeGreaterThan(0);
    expect(screen.queryByText('Loading inspector…')).not.toBeInTheDocument();
  });

  it('keeps loaded Session content visible when background Overview fails without retained data', async () => {
    mocks.activityOverview.mockRejectedValue(new Error('Overview unavailable'));
    render(SharedInspectorTabs, { scope, sessionId: 'session-1', initialTab: 'session' });
    expect((await screen.findAllByText('Session summary')).length).toBeGreaterThan(0);
    expect(mocks.activityOverview).not.toHaveBeenCalled();

    window.dispatchEvent(new CustomEvent('cognis:work-invalidated', {
      detail: { scopeKey: scope.key },
    }));
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getAllByText('Session summary').length).toBeGreaterThan(0);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Overview unavailable');
  });

  it('aborts its non-settling overview on teardown and releases the queued current request', async () => {
    const blockerReleases: Array<() => void> = [];
    const blockers = Array.from({ length: 3 }, (_, index) => {
      const blockerScope = conversationTimelineScope(`inspector-blocker-${index}`);
      return requestActivityOverview(blockerScope, () => new Promise((resolve) => {
        blockerReleases.push(() => resolve(overview(blockerScope)));
      }));
    });
    const stale = deferred<ActivityOverviewResponse>();
    mocks.activityOverview.mockReturnValue(stale.promise);
    const rendered = render(SharedInspectorTabs, {
      scope,
      sessionId: 'session-1',
    });
    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());

    const queuedScope = conversationTimelineScope('current-after-inspector-close');
    const queuedLoader = vi.fn(async () => overview(queuedScope));
    const queued = requestActivityOverview(queuedScope, queuedLoader);
    expect(queuedLoader).not.toHaveBeenCalled();

    rendered.unmount();
    await waitFor(() => expect(queuedLoader).toHaveBeenCalledOnce());
    await expect(queued).resolves.toMatchObject({ scope: queuedScope });

    stale.resolve(overview(scope));
    await Promise.resolve();
    expect(getActivityOverview(scope)).toBeNull();
    blockerReleases.forEach((release) => release());
    await Promise.all(blockers);
  });
});
