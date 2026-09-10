import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { chatV2Api } from '$lib/chat-v2/api';
import type {
  TimelineScope,
  WorkActivityItem,
  WorkActivityListResponse,
} from '$lib/chat-v2/types';
import ActivityList from './ActivityList.svelte';

type WorkActivitiesLoader = (
  options: { limit: number; cursor: string | null; signal: AbortSignal },
) => Promise<WorkActivityListResponse>;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const canonicalScope: TimelineScope = {
  key: 'task_step:step-authorized',
  kind: 'task_step',
  conversation_id: 'conversation-authorized',
  session_id: 'session-authorized',
  task_id: 'task-authorized',
  step_run_id: 'step-authorized',
  parent_session_id: 'parent-authorized',
  label: 'Server canonical scope',
  status: 'running',
};

function activity(overrides: Partial<WorkActivityItem> = {}): WorkActivityItem {
  return {
    activity_scope_id: 'opaque-activity-scope',
    scope: canonicalScope,
    root: {
      kind: 'task',
      task_id: 'task-authorized',
      step_run_id: 'step-authorized',
      title: 'Build the cache UI',
    },
    agent: {
      agent_id: 'agent-server-owned',
      display_name: 'LaForge',
      avatar_url: null,
    },
    project: { project_id: 'project-server-owned', name: 'Cognis' },
    status: 'running',
    last_activity_at: new Date(Date.now() - 60_000).toISOString(),
    summary: {
      mutations: 2,
      commands: 3,
      changed_files: 4,
      artifacts: 0,
      deliverables: 1,
    },
    materialization: 'ready',
    ...overrides,
  };
}

function page(
  items: WorkActivityItem[],
  nextCursor: string | null = null,
): WorkActivityListResponse {
  return { items, next_cursor: nextCursor, has_more: nextCursor !== null };
}

describe('ActivityList', () => {
  it('lists activities without calling Work and passes the canonical scope unchanged', async () => {
    const work = vi.spyOn(chatV2Api, 'work');
    const onSelect = vi.fn();
    const item = activity();
    render(ActivityList, {
      onSelect,
      loadActivities: vi.fn<WorkActivitiesLoader>().mockResolvedValue(page([item])),
    });

    const button = await screen.findByRole('button', { name: 'Build the cache UI' });
    expect(work).not.toHaveBeenCalled();
    expect(screen.getByText('4 files · 3 commands')).toBeTruthy();
    expect(button).toHaveAccessibleDescription(
      expect.stringMatching(/LaForge · Cognis.*running.*4 files · 3 commands.*ago/),
    );
    await fireEvent.click(button);
    expect(onSelect).toHaveBeenCalledWith(item);
    expect(onSelect.mock.calls[0][0].scope).toStrictEqual(canonicalScope);
    expect(work).not.toHaveBeenCalled();
  });

  it('renders an unobtrusive absent indicator without deriving scope from opaque activity data', async () => {
    const item = activity({
      activity_scope_id: 'tenant-secret-looking-value',
      scope: { key: 'session:safe-session', kind: 'session', session_id: 'safe-session' },
      summary: null,
      materialization: 'absent',
    });
    render(ActivityList, {
      onSelect: vi.fn(),
      loadActivities: vi.fn<WorkActivitiesLoader>().mockResolvedValue(page([item])),
    });
    expect(await screen.findByText('Materializes when opened')).toBeTruthy();
    expect(screen.queryByText('tenant-secret-looking-value')).toBeNull();
  });

  it('appends stable opaque-cursor pages and removes duplicate boundary items', async () => {
    const first = activity();
    const second = activity({
      activity_scope_id: 'opaque-second',
      scope: { key: 'conversation:second', kind: 'conversation', conversation_id: 'second' },
      root: { kind: 'conversation', conversation_id: 'second', title: 'Second activity' },
    });
    const loader = vi.fn<WorkActivitiesLoader>()
      .mockResolvedValueOnce(page([first], 'opaque+/=cursor'))
      .mockResolvedValueOnce(page([first, second]));
    render(ActivityList, { onSelect: vi.fn(), loadActivities: loader });

    const more = await screen.findByTestId('work-activities-more');
    await fireEvent.click(more);
    await screen.findByText('Second activity');
    expect(loader.mock.calls[1][0].cursor).toBe('opaque+/=cursor');
    expect(screen.getAllByText('Build the cache UI')).toHaveLength(1);
  });

  it('aborts a stale pagination request when refresh replaces the list and ignores its race', async () => {
    let pageSignal: AbortSignal | undefined;
    let resolvePage!: (value: WorkActivityListResponse) => void;
    const refreshed = activity({
      activity_scope_id: 'refreshed',
      scope: { key: 'conversation:refreshed', kind: 'conversation', conversation_id: 'refreshed' },
      root: { kind: 'conversation', conversation_id: 'refreshed', title: 'Refreshed activity' },
    });
    const loader = vi.fn<WorkActivitiesLoader>()
      .mockResolvedValueOnce(page([activity()], 'next'))
      .mockImplementationOnce(({ signal }) => {
        pageSignal = signal;
        return new Promise((resolve) => { resolvePage = resolve; });
      })
      .mockResolvedValueOnce(page([refreshed]));
    render(ActivityList, { onSelect: vi.fn(), loadActivities: loader });
    await fireEvent.click(await screen.findByTestId('work-activities-more'));
    await waitFor(() => expect(pageSignal).toBeDefined());
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh Work activities' }));
    expect(pageSignal?.aborted).toBe(true);
    await screen.findByText('Refreshed activity');
    resolvePage(page([activity({ root: { kind: 'conversation', title: 'Stale result' } })]));
    await Promise.resolve();
    expect(screen.queryByText('Stale result')).toBeNull();
  });

  it('shows loading, empty, and retryable error states', async () => {
    let reject!: (error: unknown) => void;
    const loader = vi.fn<WorkActivitiesLoader>()
      .mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }))
      .mockResolvedValueOnce(page([]));
    render(ActivityList, { onSelect: vi.fn(), loadActivities: loader });
    expect(screen.getByTestId('work-activities-loading')).toBeTruthy();
    reject(new Error('Activity list failed'));
    expect(await screen.findByText('Activity list failed')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('work-activities-empty')).toBeTruthy();
  });

  it('retries a failed refresh as replacement instead of paginating stale data', async () => {
    const refreshed = activity({
      activity_scope_id: 'refreshed-after-error',
      scope: { key: 'conversation:refreshed-after-error', kind: 'conversation', conversation_id: 'refreshed-after-error' },
      root: { kind: 'conversation', title: 'Fresh replacement' },
    });
    const loader = vi.fn<WorkActivitiesLoader>()
      .mockResolvedValueOnce(page([activity()], 'stale-cursor'))
      .mockRejectedValueOnce(new Error('Refresh failed'))
      .mockResolvedValueOnce(page([refreshed]));
    render(ActivityList, { onSelect: vi.fn(), loadActivities: loader });
    await screen.findByText('Build the cache UI');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh Work activities' }));
    await screen.findByText('Refresh failed');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry refresh' }));
    await screen.findByText('Fresh replacement');
    expect(loader.mock.calls[2][0].cursor).toBeNull();
    expect(screen.queryByText('Build the cache UI')).toBeNull();
  });

  it('does not let Load more interrupt a pending refresh', async () => {
    let resolveRefresh!: (value: WorkActivityListResponse) => void;
    const loader = vi.fn<WorkActivitiesLoader>()
      .mockResolvedValueOnce(page([activity()], 'stale-cursor'))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRefresh = resolve; }));
    render(ActivityList, { onSelect: vi.fn(), loadActivities: loader });
    const more = await screen.findByTestId('work-activities-more');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh Work activities' }));
    expect(more).toBeDisabled();
    await fireEvent.click(more);
    expect(loader).toHaveBeenCalledTimes(2);
    resolveRefresh(page([]));
  });
});
