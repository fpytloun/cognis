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
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: {
    activityOverview: mocks.activityOverview,
  },
}));

vi.mock('$lib/api/client', () => ({
  api: {
    sessions: { intarisDetail: mocks.intarisDetail },
    llmProviders: { codexUsage: vi.fn() },
  },
}));

import SharedInspectorTabs from './SharedInspectorTabs.svelte';

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

  beforeEach(() => {
    clearActivityOverview();
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

  it('loads one lightweight overview without an automatic exact follow-up', async () => {
    render(SharedInspectorTabs, { scope, sessionId: 'session-1' });

    await waitFor(() => expect(mocks.activityOverview).toHaveBeenCalledOnce());
    expect(mocks.activityOverview.mock.calls[0]?.[1]).not.toMatchObject({ detail: 'full' });
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
