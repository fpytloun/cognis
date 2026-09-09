import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  notifications: vi.fn(),
  activityOverview: vi.fn(),
}));

vi.mock('$lib/components/chat-v2/ScopedChatV2Timeline.svelte', async () => ({
  default: (await import('$lib/components/LoadingState.svelte')).default,
}));

vi.mock('$lib/api/client', () => ({
  api: {
    notifications: {
      list: mocks.notifications,
      resolve: vi.fn(),
    },
    sessions: { intarisDetail: vi.fn() },
    llmProviders: { codexUsage: vi.fn() },
  },
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: {
    activityOverview: mocks.activityOverview,
  },
}));

import SessionLogsDrawer from './SessionLogsDrawer.svelte';

describe('SessionLogsDrawer', () => {
  beforeEach(() => {
    mocks.notifications.mockReset().mockResolvedValue([]);
    mocks.activityOverview.mockReset().mockResolvedValue({
      schema_version: 2,
      projection_version: 'test',
      scope: { key: 'session:session-1', kind: 'session', conversation_id: 'conversation-1', session_id: 'session-1' },
      summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0 },
      materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
      workstreams: [],
      recent: {},
      graph_fingerprint: 'graph',
      graph_truncated: false,
    });
  });

  it('toggles fullscreen and opens the shared Inspector tabs without a nested dialog', async () => {
    render(SessionLogsDrawer, {
      conversationId: 'conversation-1',
      sessionId: 'session-1',
      onclose: vi.fn(),
    });

    const dialog = screen.getByRole('dialog', { name: 'Session logs: session-1' });
    expect(screen.getByTestId('session-logs-overlay').parentElement).toBe(document.body);
    expect(screen.getByTestId('session-logs-panel')).toBe(dialog);
    await fireEvent.click(screen.getByRole('button', { name: 'Open fullscreen logs' }));
    expect(dialog).toHaveAttribute('data-fullscreen', 'true');
    expect(screen.getByRole('button', { name: 'Exit fullscreen logs' })).toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('session-logs-header-info'));
    await waitFor(() => expect(screen.getByTestId('shared-inspector-tabs')).toBeInTheDocument());
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Work' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Session' })).toBeInTheDocument();
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });

  it('stops a handled top-overlay Escape before parent window handlers', async () => {
    const onclose = vi.fn();
    const escapedToParent = vi.fn();
    render(SessionLogsDrawer, {
      conversationId: 'conversation-1',
      sessionId: 'session-1',
      onclose,
    });

    await screen.findByRole('dialog', { name: 'Session logs: session-1' });
    window.addEventListener('keydown', escapedToParent);
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(onclose).toHaveBeenCalledOnce();
    expect(escapedToParent).not.toHaveBeenCalled();
    window.removeEventListener('keydown', escapedToParent);
  });
});
