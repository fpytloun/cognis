import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  notifications: vi.fn(),
  activityOverview: vi.fn(),
}));

vi.mock('$lib/components/chat-v2/ScopedChatV2Timeline.svelte', async () => ({
  default: (await import('$lib/components/LoadingState.svelte')).default,
}));
vi.mock('$lib/components/chat-v2/CompactConversationChat.svelte', async () => (
  import('$lib/components/dashboard/ConversationModalWorkspace.chat-test-fixture.svelte')
));

vi.mock('$lib/api/client', () => ({
  api: {
    notifications: {
      list: mocks.notifications,
      resolve: vi.fn(),
    },
    sessions: { intarisDetail: vi.fn() },
    conversations: { sessions: vi.fn(async () => []) },
    tasks: { stepRunDetail: vi.fn() },
    llmProviders: { providerUsage: vi.fn() },
  },
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: {
    activityOverview: mocks.activityOverview,
  },
}));

import SessionLogsDrawer from './SessionLogsDrawer.svelte';
import type { Notification as AppNotification, StepRun, Agent } from '$lib/types/api';

describe('SessionLogsDrawer', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
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

  it('binds pending approvals to the displayed child instead of keeping root approvals', async () => {
    const { api } = await import('$lib/api/client');
    vi.mocked(api.notifications.list).mockImplementation(async (_conversationId, options) => [{
      notification_id: `approval-${options?.sessionId}`, notification_type: 'escalation', status: 'pending',
      conversation_id: 'conversation-one', task_id: null, step_name: null, step_run_id: null,
      session_id: options?.sessionId ?? null, created_at: new Date().toISOString(), resolution: null, resolved_at: null,
      payload: { tool_name: options?.sessionId === 'session-one' ? 'root-action' : 'child-action', timeout_seconds: 300 },
    }] satisfies AppNotification[]);
    render(SessionLogsDrawer, {
      conversationId: 'conversation-one', sessionId: 'session-one', onclose: vi.fn(),
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approval required · root-action' })).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: 'View child session' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approval required · child-action' })).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Approval required · root-action' })).toBeNull();
    expect(api.notifications.list).toHaveBeenLastCalledWith('conversation-one', { sessionId: 'session-child' });
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

  it('keeps separate retry attempts reachable and changes the canonical scope', async () => {
    const original = {
      step_run_id: 'step-first', task_id: 'task-weekly', step_name: 'execute',
      session_id: 'session-first', conversation_id: 'conversation-first', attempt: 1, status: 'failed', agent_id: 'agent-first',
    } as StepRun;
    const current = {
      ...original, step_run_id: 'step-second', session_id: 'session-second',
      conversation_id: 'conversation-second', attempt: 2, status: 'running', agent_id: 'agent-second',
    } as StepRun;
    const detail = vi.mocked((await import('$lib/api/client')).api.tasks.stepRunDetail);
    detail.mockResolvedValue(original);
    render(SessionLogsDrawer, {
      conversationId: current.conversation_id!, sessionId: current.session_id!,
      stepRunId: current.step_run_id, taskId: current.task_id, stepRun: current,
      attempts: [original, current], onclose: vi.fn(),
      agents: [{ agent_id: 'agent-first', name: 'First agent' }, { agent_id: 'agent-second', name: 'Second agent' }] as Agent[],
    });
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('task_step:step-second');
    await fireEvent.change(screen.getByRole('combobox', { name: 'Step attempt' }), { target: { value: original.step_run_id } });
    await waitFor(() => expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('task_step:step-first'));
    expect(detail).toHaveBeenCalledWith(original.step_run_id);
    expect(screen.getByRole('combobox', { name: 'Step attempt' })).toHaveDisplayValue('Attempt 1 of 2 · failed');
    expect(screen.getByTestId('compact-chat-agent')).toHaveTextContent('agent-first');
  });

  it('numbers task restarts oldest to newest even when step attempt resets to one', () => {
    const attempts = [5, 4, 3, 2, 1].map(number => ({
      step_run_id: `step-restart-${number}`, task_id: 'task-restarts', step_name: 'execute',
      session_id: `session-restart-${number}`, conversation_id: `conversation-restart-${number}`,
      attempt: 1, attempt_number: number, status: 'failed',
    } as StepRun));
    const latest = attempts[0];
    render(SessionLogsDrawer, {
      conversationId: latest.conversation_id!, sessionId: latest.session_id!,
      stepRunId: latest.step_run_id, taskId: latest.task_id, stepName: 'execute',
      stepRun: latest, attempts, onclose: vi.fn(),
    });
    const picker = screen.getByRole('combobox', { name: 'Step attempt' });
    expect(picker).toHaveDisplayValue('Attempt 5 of 5 · failed');
    expect(picker.closest('header')).toBe(screen.getByTestId('session-logs-header'));
    expect(screen.getAllByRole('option').map(option => option.textContent))
      .toEqual([1, 2, 3, 4, 5].map(number => `Attempt ${number} of 5 · failed`));
    expect(screen.queryByText('Step history')).not.toBeInTheDocument();
    expect(screen.queryByText('Earlier attempts remain available here.')).not.toBeInTheDocument();
  });

  it('shows one step in a single viewer with Result and Transcript tabs', async () => {
    const run = {
      step_run_id: 'step-viewer', task_id: 'task-viewer', step_name: 'execute', session_id: 'session-viewer',
      conversation_id: 'conversation-viewer', attempt: 1, status: 'failed', agent_id: 'iris',
      output: { summary: 'The partner feed rejected the credentials.' },
    } as unknown as StepRun;
    render(SessionLogsDrawer, {
      conversationId: 'conversation-viewer', sessionId: 'session-viewer', stepRunId: run.step_run_id,
      taskId: run.task_id, stepName: 'execute', stepRun: run, initialTab: 'result', onclose: vi.fn(),
    });

    expect(screen.getByRole('dialog', { name: 'Step: execute' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Result' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('The partner feed rejected the credentials.')).toBeVisible();

    await fireEvent.click(screen.getByRole('tab', { name: 'Transcript' }));
    expect(screen.getByRole('tab', { name: 'Transcript' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText('The partner feed rejected the credentials.')).not.toBeInTheDocument();
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('task_step:step-viewer');
  });

  it('offers only the result for a step without a recorded session', () => {
    const run = {
      step_run_id: 'step-tool', task_id: 'task-viewer', step_name: 'fetch', session_id: null,
      conversation_id: null, attempt: 1, status: 'completed', agent_id: 'iris',
      output: { summary: 'Fetched 12 rows.' },
    } as unknown as StepRun;
    render(SessionLogsDrawer, {
      conversationId: '', sessionId: '', stepRunId: run.step_run_id, taskId: run.task_id,
      stepName: 'fetch', stepRun: run, initialTab: 'transcript', onclose: vi.fn(),
    });

    expect(screen.getByText('Fetched 12 rows.')).toBeVisible();
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent?.trim())).toEqual(['Result']);
    expect(mocks.notifications).not.toHaveBeenCalled();
  });
});
