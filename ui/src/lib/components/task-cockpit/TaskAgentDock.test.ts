import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Agent, TaskControlChatResponse, TaskDetail } from '$lib/types/api';
import { resetOverlayState } from '$lib/stores/overlays';
import { taskAgentDock } from '$lib/stores/taskAgentDock.svelte';

const { controlChat, conversationDetail, conversationSidebar, markRead } = vi.hoisted(() => ({
  controlChat: vi.fn(),
  conversationDetail: vi.fn(),
  conversationSidebar: vi.fn(),
  markRead: vi.fn(),
}));
const { websocketListeners } = vi.hoisted(() => ({
  websocketListeners: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('$lib/api/client', () => ({
  api: {
    tasks: { controlChat },
    conversations: {
      detail: conversationDetail,
      sidebar: conversationSidebar,
      markRead,
    },
  },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error))
}));
vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: vi.fn((listener: (event: Record<string, unknown>) => void) => {
      websocketListeners.add(listener);
      return () => websocketListeners.delete(listener);
    }),
    acquireChatV2: vi.fn(),
    updateChatV2Cursor: vi.fn(),
    releaseChatV2: vi.fn(),
  },
}));

import TaskAgentDock from './TaskAgentDock.svelte';

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

beforeEach(() => {
  conversationDetail.mockImplementation(async (conversationId: string) => ({
    conversation_id: conversationId,
    has_active_turn: false,
    has_unread: false,
    active_session_status: 'active',
    active_session_completion_reason: null,
    pending_notification_types: [],
  }));
  conversationSidebar.mockResolvedValue({
    background_work: { items: [], active_count: 0, truncated: false, generated_at: '' },
  });
  markRead.mockResolvedValue({ ok: true });
  websocketListeners.clear();
});

function task(taskId: string, title: string): TaskDetail {
  return {
    task_id: taskId,
    title,
    agent_id: 'agent-1',
    status: 'running',
    pending_pause: null
  } as TaskDetail;
}

const agent = {
  agent_id: 'agent-1',
  name: 'forge',
  display_name: 'Forge'
} as Agent;

function chat(taskId: string): TaskControlChatResponse {
  return {
    task_id: taskId,
    conversation_id: `conversation-${taskId}`,
    session_id: `session-${taskId}`,
    agent_id: 'agent-1',
    agent_profile_id: null,
    task_status: 'running',
    attempt_number: 1
  };
}

afterEach(() => {
  cleanup();
  controlChat.mockReset();
  conversationDetail.mockReset();
  conversationSidebar.mockReset();
  markRead.mockReset();
  taskAgentDock.reset();
  resetOverlayState();
  document.body.classList.remove('task-agent-dock-modal');
  websocketListeners.clear();
});

function emit(event: Record<string, unknown>): void {
  for (const listener of websocketListeners) listener(event);
}

describe('TaskAgentDock', () => {
  it('generation-scopes chat loading across a delayed task rebind and renders only the new task', async () => {
    let resolveA: (value: TaskControlChatResponse) => void = () => {};
    const delayedA = new Promise<TaskControlChatResponse>((resolve) => { resolveA = resolve; });
    controlChat.mockImplementation((taskId: string) => taskId === 'task-a' ? delayedA : Promise.resolve(chat(taskId)));
    const props = { task: task('task-a', 'Task A'), agent, onGate: vi.fn(), onQuestion: vi.fn() };
    const { rerender } = render(TaskAgentDock, props);

    taskAgentDock.open();
    await waitFor(() => expect(controlChat).toHaveBeenCalledWith('task-a'));
    await rerender({ ...props, task: task('task-b', 'Task B') });
    taskAgentDock.open();

    await waitFor(() => expect(screen.getByTestId('task-control-native-chat')).toBeVisible());
    expect(screen.getByTestId('task-agent-dock')).toHaveTextContent('Task B');
    expect(screen.getByTestId('task-agent-dock')).not.toHaveTextContent('Task A');

    resolveA(chat('task-a'));
    await Promise.resolve();
    expect(screen.getByTestId('task-control-native-chat')).toBeVisible();
    expect(document.querySelector('iframe')).not.toBeInTheDocument();
  });

  it('moves focus into the dock for an external store transition and restores it on minimize', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    const external = document.createElement('button');
    external.textContent = 'External Ask';
    document.body.append(external);
    external.focus();
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn()
    });

    taskAgentDock.open();
    await waitFor(() => expect(screen.getByTestId('task-agent-dock')).toHaveFocus());
    await fireEvent.click(screen.getByRole('button', { name: 'Minimize agent dock' }));
    await waitFor(() => expect(external).toHaveFocus());
    external.remove();
  });

  it('implements linked ARIA tabs with roving keyboard focus', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn()
    });
    taskAgentDock.open();

    const chatTab = await screen.findByRole('tab', { name: 'Chat' });
    const workTab = screen.getByRole('tab', { name: 'Work' });
    expect(chatTab).toHaveAttribute('aria-controls', 'task-agent-panel-chat');
    expect(chatTab).toHaveAttribute('tabindex', '0');
    expect(workTab).toHaveAttribute('tabindex', '-1');

    chatTab.focus();
    await fireEvent.keyDown(chatTab, { key: 'ArrowRight' });
    expect(workTab).toHaveFocus();
    expect(workTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'task-agent-panel-work');

    await fireEvent.keyDown(workTab, { key: 'Home' });
    expect(chatTab).toHaveFocus();
    await fireEvent.keyDown(chatTab, { key: 'End' });
    expect(workTab).toHaveFocus();
  });

  it('opens visible Work with the exact requested task-step scope and category', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn()
    });
    const scope = {
      key: 'task_step:run-1',
      kind: 'task_step' as const,
      step_run_id: 'run-1',
      conversation_id: 'conversation-task-a',
      session_id: 'session-descendant-b'
    };
    taskAgentDock.openWork(scope, 'mutations', 'session-a');

    await waitFor(() => expect(screen.getByTestId('task-agent-work')).toBeTruthy());
    expect(screen.getByRole('tab', { name: 'Work' })).toHaveAttribute('aria-selected', 'true');
    expect(taskAgentDock.state).toBe('open');
    expect(taskAgentDock.workScope).toEqual(scope);
    expect(taskAgentDock.workCategory).toBe('mutations');
    expect(taskAgentDock.workSessionId).toBe('session-a');
  });

  it('makes the page background inert while the dock is modal', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    const background = document.createElement('main');
    background.textContent = 'Task background';
    document.body.append(background);
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn()
    });

    taskAgentDock.expand();
    await waitFor(() => expect(background.inert).toBe(true));
    taskAgentDock.minimize();
    await waitFor(() => expect(background.inert).toBe(false));
    background.remove();
  });

  it('does not show an orbit when the task runs but its control chat is idle', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    const props = {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    };
    render(TaskAgentDock, props);
    await waitFor(() => expect(conversationDetail).toHaveBeenCalled());
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();
  });

  it('shows an orbit when the control chat runs while the task is idle', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationDetail.mockResolvedValue({
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
      has_unread: false,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
    });
    render(TaskAgentDock, {
      task: { ...task('task-a', 'Task A'), status: 'completed' } as TaskDetail,
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    await waitFor(() => expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument());
    expect(screen.getByLabelText('Control chat is working')).toBeInTheDocument();
  });

  it('clears unread activity with conversation read semantics when opened', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationDetail.mockResolvedValue({
      conversation_id: 'conversation-task-a',
      has_active_turn: false,
      has_unread: true,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
    });
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    await waitFor(() => expect(screen.getByTestId('activity-avatar-unread')).toBeInTheDocument());
    await fireEvent.click(screen.getByTestId('task-agent-dock-launcher'));
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('conversation-task-a'));
    expect(screen.queryByTestId('activity-avatar-unread')).toBeNull();
  });

  it('clears unread when conversation detail arrives after the dock opens', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    let resolveDetail: (value: Record<string, unknown>) => void = () => {};
    conversationDetail.mockReturnValue(new Promise((resolve) => { resolveDetail = resolve; }));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    taskAgentDock.open();
    resolveDetail({
      conversation_id: 'conversation-task-a',
      has_active_turn: false,
      has_unread: true,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
    });

    await waitFor(() => expect(markRead).toHaveBeenCalledWith('conversation-task-a'));
    taskAgentDock.minimize();
    expect(screen.queryByTestId('activity-avatar-unread')).toBeNull();
  });

  it('clears unread updates received while open and keeps them clear after minimize', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });
    taskAgentDock.open();
    await waitFor(() => expect(conversationDetail).toHaveBeenCalled());

    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-task-a',
      has_unread: true,
    });

    await waitFor(() => expect(markRead).toHaveBeenCalledWith('conversation-task-a'));
    taskAgentDock.minimize();
    expect(screen.queryByTestId('activity-avatar-unread')).toBeNull();
  });

  it('serializes another read when unread arrives during an in-flight markRead', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    let resolveFirstRead: (value: { ok: boolean }) => void = () => {};
    markRead
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirstRead = resolve; }))
      .mockResolvedValueOnce({ ok: true });
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });
    taskAgentDock.open();
    await waitFor(() => expect(conversationDetail).toHaveBeenCalled());

    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-task-a',
      has_unread: true,
    });
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-task-a',
      has_unread: true,
    });
    resolveFirstRead({ ok: true });

    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    taskAgentDock.minimize();
    expect(screen.queryByTestId('activity-avatar-unread')).toBeNull();
  });

  it('keeps markRead coalescing scoped to the current task generation', async () => {
    controlChat.mockImplementation(async (taskId: string) => chat(taskId));
    conversationDetail.mockImplementation(async (conversationId: string) => ({
      conversation_id: conversationId,
      has_active_turn: false,
      has_unread: true,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
    }));
    let resolveA: (value: { ok: boolean }) => void = () => {};
    let resolveB: (value: { ok: boolean }) => void = () => {};
    markRead
      .mockReturnValueOnce(new Promise((resolve) => { resolveA = resolve; }))
      .mockReturnValueOnce(new Promise((resolve) => { resolveB = resolve; }))
      .mockResolvedValueOnce({ ok: true });
    const props = {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    };
    const { rerender } = render(TaskAgentDock, props);
    taskAgentDock.open();
    await waitFor(() => expect(markRead).toHaveBeenNthCalledWith(1, 'conversation-task-a'));

    await rerender({ ...props, task: task('task-b', 'Task B') });
    taskAgentDock.open();
    await waitFor(() => expect(markRead).toHaveBeenNthCalledWith(2, 'conversation-task-b'));
    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-task-b',
      has_unread: true,
    });

    resolveA({ ok: true });
    await Promise.resolve();
    expect(markRead).toHaveBeenCalledTimes(2);
    resolveB({ ok: true });
    await waitFor(() => expect(markRead).toHaveBeenNthCalledWith(3, 'conversation-task-b'));
  });

  it.each(['delegation_completed', 'delegation_failed'])(
    'refreshes managed background work on delegation_started and %s',
    async (settledType) => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationSidebar
      .mockResolvedValueOnce({
        background_work: { items: [], active_count: 0, truncated: false, generated_at: '' },
      })
      .mockResolvedValueOnce({
        background_work: {
          items: [{
            kind: 'managed_conversation',
            work_id: 'work-1',
            controller_conversation_id: 'conversation-task-a',
            title: 'Worker',
            agent_id: 'agent-1',
            status: 'running',
            todos: [],
          }],
          active_count: 1,
          truncated: false,
          generated_at: '',
        },
      })
      .mockResolvedValueOnce({
        background_work: { items: [], active_count: 0, truncated: false, generated_at: '' },
      });
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });
    await waitFor(() => expect(conversationDetail).toHaveBeenCalled());

    emit({
      type: 'delegation_started',
      conversation_id: 'conversation-task-a',
      child_session_id: 'child-1',
      mode: 'run',
    });
    await waitFor(() => expect(screen.getByLabelText('Background work active')).toBeInTheDocument());

    emit({
      type: settledType,
      conversation_id: 'conversation-task-a',
      child_session_id: 'child-1',
    });
    await waitFor(() => expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull());
    },
  );

  it('keeps conversation detail live when the initial sidebar projection fails', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationSidebar.mockRejectedValueOnce(new Error('sidebar unavailable'));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });
    taskAgentDock.open();

    await screen.findByTestId('task-control-native-chat');
    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-task-a',
      has_unread: true,
    });
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('conversation-task-a'));

    conversationSidebar.mockResolvedValueOnce({
      background_work: {
        items: [{
          kind: 'delegated_session',
          work_id: 'child-1',
          controller_conversation_id: 'conversation-task-a',
          title: 'Worker',
          agent_id: 'agent-1',
          status: 'running',
          todos: [],
        }],
        active_count: 1,
        truncated: false,
        generated_at: '',
      },
    });
    emit({
      type: 'delegation_started',
      conversation_id: 'conversation-task-a',
      child_session_id: 'child-1',
      mode: 'run',
    });
    await waitFor(() => expect(screen.getByLabelText('Background work active')).toBeInTheDocument());
  });

  it('starts the new task sidebar refresh while the previous task refresh is in flight', async () => {
    controlChat.mockImplementation(async (taskId: string) => chat(taskId));
    let resolveA: (value: Record<string, unknown>) => void = () => {};
    let resolveB: (value: Record<string, unknown>) => void = () => {};
    conversationSidebar
      .mockReturnValueOnce(new Promise((resolve) => { resolveA = resolve; }))
      .mockReturnValueOnce(new Promise((resolve) => { resolveB = resolve; }));
    const props = {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    };
    const { rerender } = render(TaskAgentDock, props);
    await waitFor(() => expect(conversationSidebar).toHaveBeenCalledTimes(1));

    await rerender({ ...props, task: task('task-b', 'Task B') });
    await waitFor(() => expect(conversationSidebar).toHaveBeenCalledTimes(2));
    resolveB({
      background_work: {
        items: [{
          kind: 'managed_conversation',
          work_id: 'work-b',
          controller_conversation_id: 'conversation-task-b',
          title: 'Worker B',
          agent_id: 'agent-1',
          status: 'running',
          todos: [],
        }],
        active_count: 1,
        truncated: false,
        generated_at: '',
      },
    });
    await waitFor(() => expect(screen.getByLabelText('Background work active')).toBeInTheDocument());

    resolveA({
      background_work: {
        items: [],
        active_count: 0,
        truncated: false,
        generated_at: '',
      },
    });
    await Promise.resolve();
    expect(screen.getByLabelText('Background work active')).toBeInTheDocument();
  });

  it('clears scoped runtime activity when chat unmounts on tab switch or minimize', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationDetail.mockResolvedValue({
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
      has_unread: false,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
    });
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });
    taskAgentDock.open();
    await screen.findByTestId('task-control-native-chat');
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
    });
    await waitFor(() => expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument());

    await fireEvent.click(screen.getByRole('tab', { name: 'Work' }));
    await waitFor(() => expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull());

    await fireEvent.click(screen.getByRole('tab', { name: 'Chat' }));
    await screen.findByTestId('task-control-native-chat');
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
    });
    await waitFor(() => expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: 'Minimize agent dock' }));
    await waitFor(() => expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull());
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
    });
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();
  });

  it('ignores late runtime snapshots across tab, minimize, and task generations', async () => {
    controlChat.mockImplementation(async (taskId: string) => chat(taskId));
    const props = {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    };
    const { rerender } = render(TaskAgentDock, props);
    taskAgentDock.open();
    await screen.findByTestId('task-control-native-chat');
    await fireEvent.click(screen.getByRole('tab', { name: 'Work' }));
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
    });
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();

    await rerender({ ...props, task: task('task-b', 'Task B') });
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-a',
      has_active_turn: true,
    });
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-b',
      has_active_turn: true,
    });
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();

    taskAgentDock.open();
    await screen.findByTestId('task-control-native-chat');
    emit({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-task-b',
      has_active_turn: true,
    });
    await waitFor(() => expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument());
  });

  it('shows control-chat session failure as an avatar error', async () => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationDetail.mockResolvedValue({
      conversation_id: 'conversation-task-a',
      has_active_turn: false,
      has_unread: false,
      active_session_status: 'failed',
      active_session_completion_reason: 'error',
      pending_notification_types: [],
    });
    render(TaskAgentDock, {
      task: { ...task('task-a', 'Task A'), status: 'completed' } as TaskDetail,
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    await waitFor(() => expect(screen.getByTestId('activity-avatar-error')).toBeInTheDocument());
  });

  it.each([
    {
      name: 'unread',
      detail: { has_unread: true },
      label: 'Unread control chat activity',
    },
    {
      name: 'error',
      detail: { active_session_status: 'failed' },
      label: 'requires attention: session failed or ended unexpectedly',
    },
    {
      name: 'running',
      detail: { has_active_turn: true },
      label: 'Control chat is working',
    },
  ])('includes $name activity in the launcher accessible name', async ({ detail, label }) => {
    controlChat.mockResolvedValue(chat('task-a'));
    conversationDetail.mockResolvedValue({
      conversation_id: 'conversation-task-a',
      has_active_turn: false,
      has_unread: false,
      active_session_status: 'active',
      active_session_completion_reason: null,
      pending_notification_types: [],
      ...detail,
    });
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    await waitFor(() => expect(
      screen.getByRole('button', {
        name: `Open Forge for task Task A. ${label}`,
      }),
    ).toBeInTheDocument());
  });

  it('uses the rendered chat error state in the launcher accessible name', async () => {
    controlChat.mockRejectedValue(new Error('control chat unavailable'));
    render(TaskAgentDock, {
      task: task('task-a', 'Task A'),
      agent,
      onGate: vi.fn(),
      onQuestion: vi.fn(),
    });

    await waitFor(() => expect(
      screen.getByRole('button', {
        name: 'Open Forge for task Task A. Task agent connection failed',
      }),
    ).toBeInTheDocument());
    expect(screen.getByTestId('activity-avatar-error')).toBeInTheDocument();
  });
});
