import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';

import { documentCoordination } from '$lib/document-coordination';
import type { Conversation, TaskDetail } from '$lib/types/api';
import DashboardEntityModal from './DashboardEntityModal.svelte';
import {
  conversationWorkspaceSubscriptionCount,
  resetConversationWorkspaceFixture,
} from './conversation-workspace-fixture-state';

const { taskDetail, controlChat, stepRunDetail, conversationDetail, gateResponse, getDeliverable } = vi.hoisted(() => ({
  taskDetail: vi.fn(),
  controlChat: vi.fn(),
  stepRunDetail: vi.fn(),
  conversationDetail: vi.fn(),
  gateResponse: vi.fn(),
  getDeliverable: vi.fn(),
}));

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/chat-v2/api', async (original) => {
  const module = await original<typeof import('$lib/chat-v2/api')>();
  return { ...module, chatV2Api: { ...module.chatV2Api,
    activityOverview: vi.fn().mockResolvedValue({ workstreams: [] }) } };
});
vi.mock('$lib/stores/auth', async () => {
  const { readable, get } = await import('svelte/store');
  const store = readable({
    status: 'authenticated', initialized: true, expiresAt: null,
    user: { email: 'user@example.com', name: null, role: 'user' }, error: null
  });
  return {
    auth: { ...store, getSnapshot: () => get(store) }
  };
});
vi.mock('./ConversationModalWorkspace.svelte', async () => (
  import('./ConversationModalWorkspace.test-fixture.svelte')
));
vi.mock('$lib/components/chat-v2/ScopedChatV2Timeline.svelte', async () => (
  import('./DelegateTimeline.test-fixture.svelte')
));
vi.mock('$lib/api/client', () => ({
  api: {
    tasks: {
      detail: taskDetail,
      controlChat,
      stepRunDetail,
      gateResponse,
      stepResponse: vi.fn()
    },
    conversations: { detail: conversationDetail, sessions: vi.fn().mockResolvedValue([]) },
    deliverables: { getForStepRun: vi.fn(), get: getDeliverable },
    notifications: { list: vi.fn().mockResolvedValue([]), resolve: vi.fn() }
  },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error))
}));

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  });
});

afterEach(cleanup);
beforeEach(async () => {
  await documentCoordination().beginLogin();
  await documentCoordination().verified('user@example.com');
  taskDetail.mockReset();
  controlChat.mockReset();
  stepRunDetail.mockReset();
  conversationDetail.mockReset();
  gateResponse.mockReset();
  gateResponse.mockResolvedValue({ ok: true, task_id: 'task-realtime', status: 'ready' });
  resetConversationWorkspaceFixture();
});

function completedTask(): TaskDetail {
  return {
    task_id: 'task-completed',
    title: 'Completed task',
    description: 'Task description.',
    expected_output: 'Expected result.',
    status: 'completed',
    agent_id: 'riker',
    workflow_id: null,
    project_id: null,
    result_data: null,
    step_runs: [],
    pending_pause: null,
    workflow_projection: null,
    progress: null,
    dependencies: [],
    workflow_run: null,
    attempt_number: 1,
    source_type: 'manual',
    source_ref: null,
    created_by: 'user@example.com',
    queue_name: 'default'
  } as unknown as TaskDetail;
}

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'conversation-1',
    agent_id: 'riker',
    title: 'Original title',
    status: 'active',
    active_session_id: 'session-1',
    ...overrides,
  } as Conversation;
}

describe('DashboardEntityModal', () => {
  beforeAll(async () => {
    await import('../task-cockpit/TaskWorkPanelBody.svelte');
  });
  it('opens the canonical result from task detail without a second document request', async () => {
    getDeliverable.mockClear();
    taskDetail.mockResolvedValue({
      ...completedTask(),
      result_data: { final_deliverable_id: 'dlv-final' },
      final_deliverable: {
        deliverable_id: 'dlv-final', step_run_id: 'run-final',
        version: 1, attempt_number: 1, format: 'markdown', title: 'Immediate dashboard result',
        content: '# Immediate dashboard result\n\nReady on first task response.',
        target: null, outputs: {}, status: 'approved', evaluator_feedback: null,
        created_at: null, updated_at: null,
      },
    });
    render(DashboardEntityModal, { kind: 'task', taskId: 'task-completed', onClose: vi.fn() });
    await screen.findByText('Ready on first task response.');
    expect(getDeliverable).not.toHaveBeenCalled();
    expect(screen.queryByText('Loading deliverable…')).toBeNull();
  });

  it('loads a shared task result by ID instead of an owner-only work projection', async () => {
    taskDetail.mockResolvedValue({ ...completedTask(), created_by: 'other@example.com', result_data: { final_deliverable_id: 'dlv-final' } });
    getDeliverable.mockResolvedValue({
      deliverable_id: 'dlv-final', format: 'markdown', content: '# Shared final result',
      title: 'Shared result', status: 'published', version: 1, outputs: {},
    });
    render(DashboardEntityModal, { kind: 'task', taskId: 'task-completed', onClose: vi.fn() });
    await screen.findByTestId('task-brief');
    await fireEvent.click(screen.getByTestId('dashboard-task-modal-tab-deliverable'));
    await waitFor(() => expect(getDeliverable).toHaveBeenCalledWith('dlv-final'));
    expect(screen.queryByTestId('task-work-panel')).not.toBeInTheDocument();
    expect(controlChat).not.toHaveBeenCalled();
  });
  it('does not expose mutation tabs or create control chat for another author', async () => {
    taskDetail.mockResolvedValue({ ...completedTask(), created_by: 'other@example.com', status: 'running' });
    render(DashboardEntityModal, { kind: 'task', taskId: 'task-completed', onClose: vi.fn() });
    await screen.findByTestId('task-brief');
    expect(screen.queryByTestId('dashboard-task-modal-tab-control-chat')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dashboard-task-modal-tab-activity')).not.toBeInTheDocument();
    expect(controlChat).not.toHaveBeenCalled();
  });
  it('opens terminal tasks on the final deliverable without creating control chat', async () => {
    taskDetail.mockResolvedValue(completedTask());
    render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-completed',
      agents: [],
      onClose: vi.fn()
    });

    await waitFor(() => expect(screen.getByTestId('dashboard-task-modal-tab-deliverable')).toHaveAttribute('aria-selected', 'true'));
    expect(controlChat).not.toHaveBeenCalled();
    expect(await screen.findByTestId('task-final-result-empty')).toBeInTheDocument();
  });

  it('keeps task details usable when lazy control chat is forbidden', async () => {
    taskDetail.mockResolvedValue(completedTask());
    controlChat.mockRejectedValue(new Error('Forbidden'));
    render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-completed',
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-task-modal-tab-control-chat'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Forbidden'));
    await fireEvent.click(screen.getByTestId('dashboard-task-modal-tab-description'));
    expect(screen.getByTestId('task-brief')).toBeInTheDocument();
  });

  it('opens a waiting task on Description with its Attention action visible', async () => {
    taskDetail.mockResolvedValue({
      ...completedTask(),
      task_id: 'task-waiting',
      status: 'paused',
      pending_pause: {
        pause_type: 'gate',
        step_name: 'review',
        question: 'Approve this result?',
        questions: [],
        options: [{ action: 'continue', label: 'Approve' }]
      }
    });
    render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-waiting',
      agents: [],
      onClose: vi.fn()
    });

    await waitFor(() => expect(screen.getByTestId('dashboard-task-modal-tab-description')).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByTestId('task-attention')).toHaveTextContent('Approve this result?');
    expect(controlChat).not.toHaveBeenCalled();
  });

  it('shows protected auth attention without an unusable question form', async () => {
    taskDetail.mockResolvedValue({
      ...completedTask(),
      status: 'paused',
      pending_pause: {
        pause_type: 'auth_challenge',
        step_name: 'authenticate',
        question: 'Approve authentication?',
        questions: [],
        options: []
      }
    });
    render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-auth',
      agents: [],
      onClose: vi.fn()
    });

    await waitFor(() => expect(screen.getByTestId('dashboard-task-modal-tab-description')).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByTestId('task-attention-summary')).toHaveTextContent('Approve authentication?');
    expect(screen.queryByRole('button', { name: 'Send response' })).not.toBeInTheDocument();
  });

  it('refreshes the Control tab after realtime resolution and closes the resolved form', async () => {
    const paused = {
      ...completedTask(),
      status: 'paused',
      pending_pause: {
        pause_id: 'pause-1',
        task_id: 'task-realtime',
        step_run_id: 'run-1',
        session_id: 'session-1',
        pause_type: 'gate',
        step_name: 'review',
        question: 'Approve this result?',
        questions: [],
        options: [{ action: 'continue', label: 'Approve' }],
        context: {}
      }
    } as TaskDetail;
    taskDetail
      .mockResolvedValueOnce(paused)
      .mockResolvedValueOnce({ ...paused, status: 'ready', pending_pause: null });
    const view = render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-realtime',
      modalRefreshToken: 0,
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-task-modal-tab-control'));
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();

    await view.rerender({
      kind: 'task',
      taskId: 'task-realtime',
      modalRefreshToken: 1,
      agents: [],
      onClose: vi.fn()
    });

    await waitFor(() => expect(taskDetail).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument());
    expect(screen.getByTestId('dashboard-task-control')).toHaveTextContent('ready');
  });

  it('keeps the realtime state when it supersedes a mutation refresh', async () => {
    let resolveMutationRefresh!: (value: TaskDetail) => void;
    const mutationRefresh = new Promise<TaskDetail>((resolve) => { resolveMutationRefresh = resolve; });
    const paused = {
      ...completedTask(),
      task_id: 'task-refresh-race',
      status: 'paused',
      pending_pause: {
        pause_id: 'pause-race',
        task_id: 'task-refresh-race',
        step_run_id: 'run-race',
        session_id: 'session-race',
        pause_type: 'gate',
        step_name: 'review',
        question: 'Approve this result?',
        questions: [],
        options: [{ action: 'continue', label: 'Approve' }],
        context: {}
      }
    } as TaskDetail;
    taskDetail
      .mockResolvedValueOnce(paused)
      .mockReturnValueOnce(mutationRefresh)
      .mockResolvedValueOnce({ ...paused, status: 'ready', pending_pause: null });
    const view = render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-refresh-race',
      modalRefreshToken: 0,
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-task-modal-tab-control'));
    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(taskDetail).toHaveBeenCalledTimes(2));

    await view.rerender({
      kind: 'task',
      taskId: 'task-refresh-race',
      modalRefreshToken: 1,
      agents: [],
      onClose: vi.fn()
    });
    await waitFor(() => expect(taskDetail).toHaveBeenCalledTimes(3));
    resolveMutationRefresh({ ...paused, status: 'ready', pending_pause: null });

    await waitFor(() => expect(screen.getByTestId('dashboard-task-control')).toHaveTextContent('ready'));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('submits the exact gate and refreshes stale controls after a conflict', async () => {
    const paused = {
      ...completedTask(),
      task_id: 'task-realtime',
      status: 'paused',
      pending_pause: {
        pause_id: 'pause-conflict', task_id: 'task-realtime', step_name: 'review',
        step_run_id: null, session_id: null,
        pause_type: 'gate', question: 'Retry failed step?', questions: [],
        options: [{ action: 'revise(execute)', label: 'Retry step' }], context: {}
      }
    } as TaskDetail;
    taskDetail.mockResolvedValueOnce(paused).mockResolvedValue({
      ...paused, status: 'cancelled', pending_pause: null
    });
    gateResponse.mockRejectedValue(new Error('Gate is no longer actionable; refresh the task'));
    render(DashboardEntityModal, {
      kind: 'task', taskId: 'task-realtime', agents: [], onClose: vi.fn()
    });
    await fireEvent.click(await screen.findByTestId('dashboard-task-modal-tab-control'));
    await fireEvent.click(screen.getByRole('button', { name: 'Retry step' }));
    await waitFor(() => expect(taskDetail).toHaveBeenCalledTimes(2));
    expect(gateResponse).toHaveBeenCalledWith('task-realtime', expect.objectContaining({
      pause_id: 'pause-conflict', action: 'revise(execute)'
    }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry step' })).not.toBeInTheDocument());
  });

  it('does not reopen step output after Escape cancels slow projection hydration', async () => {
    let resolveDetail!: (value: unknown) => void;
    stepRunDetail.mockReturnValue(new Promise((resolve) => { resolveDetail = resolve; }));
    const projectedRun = {
      step_run_id: 'run-slow',
      step_name: 'prepare',
      step_type: 'run',
      status: 'completed',
      agent_id: 'riker',
      is_projection: true,
      updated_at: '2026-01-01T00:00:00Z',
      session_id: 'session-prepare',
      conversation_id: 'conversation-prepare',
      output: null
    };
    taskDetail.mockResolvedValue({
      ...completedTask(),
      status: 'running',
      step_runs: [projectedRun],
      workflow_projection: {
        workflow_id: 'workflow-1',
        current_step_name: 'prepare',
        phases: [{
          id: 'phase-1',
          title: 'Implementation',
          status: 'completed',
          steps: [{
            name: 'prepare',
            type: 'run',
            status: 'completed',
            action_required: false,
            has_output: true,
            has_logs: true,
            has_deliverable: false,
            attempt_count: 1
          }]
        }]
      }
    });
    render(DashboardEntityModal, {
      kind: 'task',
      taskId: 'task-running',
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-task-modal-tab-steps'));
    await fireEvent.click(screen.getByRole('button', { name: 'Result' }));
    expect(screen.getByTestId('dashboard-step-viewer-loading')).toBeInTheDocument();
    await fireEvent.keyDown(window, { key: 'Escape' });
    resolveDetail({ ...projectedRun, is_projection: false, output: 'Late output' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'prepare' })).not.toBeInTheDocument();
      expect(screen.getByTestId('dashboard-task-modal-panel-steps')).toBeInTheDocument();
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Result' }));
    expect(await screen.findByTestId('step-result-view')).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Result' })).toHaveAttribute('aria-selected', 'true');
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('session-logs-panel')).not.toBeInTheDocument());
    expect(screen.getByTestId('dashboard-task-modal-panel-steps')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    expect(await screen.findByTestId('session-logs-panel')).toBeVisible();
    expect(screen.getByTestId('delegate-timeline')).toHaveAttribute('data-scope', 'task_step:run-slow');
    await fireEvent.click(screen.getByRole('button', { name: 'View delegate session' }));
    await waitFor(() => expect(screen.getByTestId('delegate-timeline')).toHaveAttribute('data-scope', 'session:session-delegate'));
    const parentLink = screen.getByRole('link', { name: 'Back to parent conversation' });
    expect(parentLink).toHaveAttribute('href', '/chat/conversation-prepare');
    await fireEvent.click(parentLink);
    await waitFor(() => expect(screen.getByTestId('delegate-timeline')).toHaveAttribute('data-scope', 'task_step:run-slow'));
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('session-logs-panel')).not.toBeInTheDocument());
    expect(screen.getByTestId('dashboard-task-modal-panel-steps')).toBeInTheDocument();
  });

  it('refreshes conversation metadata without remounting or losing workspace state', async () => {
    let resolveRefresh!: (value: Conversation) => void;
    const pendingRefresh = new Promise<Conversation>((resolve) => { resolveRefresh = resolve; });
    conversationDetail
      .mockResolvedValueOnce(conversation())
      .mockReturnValueOnce(pendingRefresh)
      .mockRejectedValueOnce(new Error('refresh unavailable'));
    const view = render(DashboardEntityModal, {
      kind: 'conversation',
      conversationId: 'conversation-1',
      modalRefreshToken: 0,
      agents: [],
      onClose: vi.fn(),
    });

    const workspace = await screen.findByTestId('conversation-modal-workspace');
    expect(screen.getByTestId('dashboard-entity-modal-inspector')).toBeInTheDocument();
    expect(workspace).toHaveAttribute('data-inspector-control-in-header', 'true');
    expect(screen.queryByTestId('conversation-mobile-inspector-control')).not.toBeInTheDocument();
    const inspectorButton = screen.getByTestId('dashboard-entity-modal-inspector');
    await fireEvent.click(inspectorButton);
    expect(workspace).toHaveAttribute('data-inspector-open', 'false');
    expect(inspectorButton).toHaveAttribute('aria-expanded', 'false');
    await fireEvent.click(inspectorButton);
    expect(workspace).toHaveAttribute('data-inspector-open', 'true');
    expect(inspectorButton).toHaveAttribute('aria-expanded', 'true');
    await fireEvent.input(screen.getByTestId('workspace-draft'), {
      target: { value: 'preserved draft' },
    });
    await fireEvent.input(screen.getByTestId('workspace-scroll'), {
      target: { value: '320' },
    });
    await fireEvent.change(screen.getByTestId('workspace-inspector-tab'), {
      target: { value: 'session' },
    });
    expect(conversationWorkspaceSubscriptionCount()).toBe(1);

    await view.rerender({
      kind: 'conversation',
      conversationId: 'conversation-1',
      modalRefreshToken: 1,
      agents: [],
      onClose: vi.fn(),
    });
    await waitFor(() => expect(conversationDetail).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('Loading…')).toBeNull();
    expect(screen.getByTestId('conversation-modal-workspace')).toBe(workspace);
    expect(screen.getByTestId('workspace-draft')).toHaveValue('preserved draft');
    expect(screen.getByTestId('workspace-scroll')).toHaveValue('320');
    expect(screen.getByTestId('workspace-inspector-tab')).toHaveValue('session');
    expect(screen.getByTestId('workspace-autotail')).toHaveTextContent('paused');
    expect(screen.getByTestId('workspace-queue')).toHaveTextContent('queued:2');
    expect(screen.getByTestId('workspace-outbox')).toHaveTextContent('pending:1');
    expect(conversationWorkspaceSubscriptionCount()).toBe(1);

    resolveRefresh(conversation({
      title: 'Updated title',
      status: 'closed',
      active_session_id: 'session-2',
    }));
    await screen.findByText('Updated title');
    expect(screen.getByTestId('conversation-modal-workspace')).toBe(workspace);
    expect(screen.getByTestId('workspace-session')).toHaveTextContent('session-2');
    expect(screen.getByTestId('workspace-inspector-tab')).toHaveValue('session');
    expect(conversationWorkspaceSubscriptionCount()).toBe(1);

    await view.rerender({
      kind: 'conversation',
      conversationId: 'conversation-1',
      modalRefreshToken: 2,
      agents: [],
      onClose: vi.fn(),
    });
    await screen.findByText(/Could not refresh: refresh unavailable/);
    expect(screen.getByText('Updated title')).toBeInTheDocument();
    expect(screen.getByTestId('conversation-modal-workspace')).toBe(workspace);
    expect(screen.getByTestId('workspace-draft')).toHaveValue('preserved draft');
    expect(screen.getByTestId('workspace-inspector-tab')).toHaveValue('session');
    expect(conversationWorkspaceSubscriptionCount()).toBe(1);
  });
});
