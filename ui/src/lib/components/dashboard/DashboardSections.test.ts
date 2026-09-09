import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AttentionActionSummary, Conversation, Schedule, TaskBoard } from '$lib/types/api';
import ConversationsSection from './ConversationsSection.svelte';
import TasksSection from './TasksSection.svelte';

const { goto } = vi.hoisted(() => ({ goto: vi.fn() }));

vi.mock('$app/navigation', () => ({ goto }));

const taskBoard = {
  columns: {
    running: {
      total_count: 1,
      items: [{
        task_id: 'task-1',
        title: 'Running task',
        agent_id: 'riker',
        status: 'running',
        priority: 1,
        created_at: '2026-08-22T00:00:00Z',
        updated_at: '2026-08-22T01:00:00Z',
        started_at: '2026-08-22T00:30:00Z',
        completed_at: null,
        progress_summary: {
          todo_total: 4,
          todo_completed: 2,
          todo_in_progress: 1,
          current_step_name: 'implement',
          current_step_status: 'running',
          changed_files: 3,
          additions: 42,
          deletions: 7
        }
      }]
    },
    done: { total_count: 0, items: [] }
  }
} as unknown as TaskBoard;

const conversation = {
  conversation_id: 'conversation-1',
  agent_id: 'riker',
  title: 'Active conversation',
  context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
  managed_agent: null,
  root_controller_conversation_id: null,
  has_active_turn: true,
  last_message_at: '2026-08-22T01:00:00Z',
  pending_notification_types: []
} as unknown as Conversation;
const schedule = {
  schedule_id: 'schedule-1',
  name: 'Daily review',
  agent_id: 'riker',
  enabled: true,
  next_fire_at: '2026-08-24T08:00:00Z'
} as unknown as Schedule;

describe('Control Center sections', () => {
  beforeEach(() => goto.mockReset());

  it('keeps task data visible and retries when the schedules request fails', async () => {
    const onSchedulesRetry = vi.fn();
    render(TasksSection, {
      board: taskBoard,
      schedules: null,
      agents: [],
      schedulesError: 'schedule service unavailable',
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn(),
      onSchedulesRetry,
    });

    expect(screen.getByTestId('dashboard-task-row-task-1')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-schedules-error')).toHaveTextContent('schedule service unavailable');
    expect(screen.getByTestId('workstream-todo-progress')).toHaveAttribute('data-progress', '0.625');
    expect(screen.getByTestId('workstream-execution-status')).toHaveTextContent('running');
    expect(screen.getByTestId('diff-stat')).toHaveTextContent('3 files');
    expect(screen.getByText('2/4 todos')).toBeInTheDocument();
    expect(screen.getByText('implement')).toBeInTheDocument();
    expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-task-row-task-1')).toHaveAccessibleName(/riker/i);
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onSchedulesRetry).toHaveBeenCalledOnce();
  });

  it('opens one focused action without opening its task card', async () => {
    const onOpenTask = vi.fn();
    const onOpenAttention = vi.fn();
    const board = structuredClone(taskBoard);
    board.columns.running.items[0].attention_actions = [{
      action_id: 'approval-1',
      kind: 'escalation',
      status: 'pending',
      availability: 'actionable',
      title: 'Tool approval required',
      source: {
        notification_id: 'approval-1',
        conversation_id: 'conversation-1',
        managed_origin_conversation_id: null,
        task_id: 'task-1',
        step_name: null,
        step_run_id: null,
        session_id: null,
      },
      can_resolve: true,
      has_action_form: true,
      expires_at: null,
      revision: 1,
      convergence_id: 'approval-1:1',
    }];
    render(TasksSection, {
      board,
      schedules: [],
      agents: [],
      onOpenTask,
      onOpenSchedule: vi.fn(),
      onOpenAttention,
      onNewTask: vi.fn(),
    });

    await fireEvent.click(screen.getByRole('button', {
      name: 'Tool approval required for Running task',
    }));

    expect(onOpenAttention).toHaveBeenCalledOnce();
    expect(onOpenTask).not.toHaveBeenCalled();
  });

  it('shows quick actions only for server-confirmed actionable forms', () => {
    const board = structuredClone(taskBoard);
    const base = {
      action_id: '',
      kind: 'escalation',
      status: 'pending',
      availability: 'actionable',
      title: 'Tool approval required',
      source: {
        notification_id: '',
        conversation_id: 'conversation-1',
        managed_origin_conversation_id: null,
        task_id: 'task-1',
        step_name: null,
        step_run_id: null,
        session_id: null,
      },
      can_resolve: true,
      has_action_form: true,
      expires_at: null,
      revision: 1,
      convergence_id: '',
    } satisfies AttentionActionSummary;
    board.columns.running.items[0].attention_type = 'gate';
    board.columns.running.items[0].attention_actions = [
      { ...base, action_id: 'valid', convergence_id: 'valid:1' },
      { ...base, action_id: 'unsupported', kind: 'unsupported', availability: 'unsupported', can_resolve: false, has_action_form: false, convergence_id: 'unsupported:1' },
      { ...base, action_id: 'malformed', availability: 'recovery_required', can_resolve: false, has_action_form: false, convergence_id: 'malformed:1' },
      { ...base, action_id: 'read-only', availability: 'read_only', can_resolve: false, convergence_id: 'read-only:1' },
      { ...base, action_id: 'expired', status: 'expired', availability: 'expired', can_resolve: false, convergence_id: 'expired:1' },
      { ...base, action_id: 'resolving', status: 'resolving', availability: 'resolving', convergence_id: 'resolving:1' },
      { ...base, action_id: 'no-form', has_action_form: false, convergence_id: 'no-form:1' },
    ];

    render(TasksSection, {
      board,
      schedules: [],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onOpenAttention: vi.fn(),
      onNewTask: vi.fn(),
    });

    expect(screen.getByTestId('dashboard-attention-valid')).toBeInTheDocument();
    expect(screen.queryAllByTestId(/^dashboard-attention-/)).toHaveLength(1);
  });

  it('does not infer a quick action from legacy task attention metadata', () => {
    const board = structuredClone(taskBoard);
    board.columns.running.items[0].attention_type = 'gate';
    board.columns.running.items[0].attention_actions = [];

    render(TasksSection, {
      board,
      schedules: [],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onOpenAttention: vi.fn(),
      onNewTask: vi.fn(),
    });

    expect(screen.queryAllByTestId(/^dashboard-attention-/)).toHaveLength(0);
    expect(screen.getByTestId('dashboard-task-row-task-1')).toBeInTheDocument();
  });

  it('offers local retries for conversation and agent load errors', async () => {
    const onRetry = vi.fn();
    const onAgentsRetry = vi.fn();
    render(ConversationsSection, {
      conversations: null,
      agents: [],
      error: 'conversation service unavailable',
      agentsError: 'agent service unavailable',
      onOpenConversation: vi.fn(),
      onNewChat: vi.fn(),
      onRetry,
      onAgentsRetry,
    });

    const retries = screen.getAllByRole('button', { name: 'Try again' });
    await fireEvent.click(retries[0]);
    await fireEvent.click(retries[1]);
    expect(onAgentsRetry).toHaveBeenCalledOnce();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('opens a task row in the modal but the external link only navigates', async () => {
    const onOpenTask = vi.fn();
    render(TasksSection, {
      board: taskBoard,
      schedules: [],
      agents: [],
      onOpenTask,
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn()
    });

    await fireEvent.click(screen.getByTestId('dashboard-task-row-task-1'));
    expect(onOpenTask).toHaveBeenCalledWith('task-1');

    onOpenTask.mockClear();
    await fireEvent.click(screen.getByTestId('dashboard-task-row-link-task-1'));
    expect(goto).toHaveBeenCalledWith('/tasks/task-1');
    expect(onOpenTask).not.toHaveBeenCalled();

    goto.mockClear();
    await fireEvent.keyDown(screen.getByTestId('dashboard-task-row-link-task-1'), { key: 'Enter' });
    expect(onOpenTask).not.toHaveBeenCalled();

    const todoProgress = screen.getByTestId('workstream-todo-progress');
    await fireEvent.click(todoProgress);
    expect(onOpenTask).not.toHaveBeenCalled();
    await fireEvent.keyDown(todoProgress, { key: 'Enter' });
    expect(onOpenTask).not.toHaveBeenCalled();
    await fireEvent.focusIn(todoProgress);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
    await fireEvent.keyDown(todoProgress, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    expect(onOpenTask).not.toHaveBeenCalled();
  });

  it('opens a conversation row in the modal but the external link only navigates', async () => {
    const onOpenConversation = vi.fn();
    render(ConversationsSection, {
      conversations: [conversation],
      agents: [],
      onOpenConversation,
      onNewChat: vi.fn()
    });

    await fireEvent.click(screen.getByTestId('dashboard-conversation-row-conversation-1'));
    expect(onOpenConversation).toHaveBeenCalledWith('conversation-1');

    onOpenConversation.mockClear();
    await fireEvent.click(screen.getByTestId('dashboard-conversation-row-link-conversation-1'));
    expect(goto).toHaveBeenCalledWith('/chat/conversation-1');
    expect(onOpenConversation).not.toHaveBeenCalled();

    goto.mockClear();
    await fireEvent.keyDown(screen.getByTestId('dashboard-conversation-row-link-conversation-1'), { key: 'Enter' });
    expect(onOpenConversation).not.toHaveBeenCalled();
  });

  it('shows the schedule agent and separates row, keyboard, and external-link behavior', async () => {
    const onOpenSchedule = vi.fn();
    render(TasksSection, {
      board: taskBoard,
      schedules: [schedule],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule,
      onNewTask: vi.fn()
    });

    const row = screen.getByTestId('dashboard-schedule-row-schedule-1');
    expect(screen.getByTestId('dashboard-schedule-agent-schedule-1')).toBeInTheDocument();
    await fireEvent.click(row);
    expect(onOpenSchedule).toHaveBeenCalledWith(schedule);
    onOpenSchedule.mockClear();
    await fireEvent.keyDown(row, { key: 'Enter' });
    expect(onOpenSchedule).toHaveBeenCalledWith(schedule);

    onOpenSchedule.mockClear();
    await fireEvent.click(screen.getByTestId('dashboard-schedule-row-link-schedule-1'));
    expect(goto).toHaveBeenCalledWith('/schedules');
    expect(onOpenSchedule).not.toHaveBeenCalled();
  });

  it('shows a waiting conversation reason and opens its row', async () => {
    const onOpenConversation = vi.fn();
    render(ConversationsSection, {
      conversations: [{
        ...conversation,
        has_active_turn: false,
        has_unread: true,
        pending_notification_types: ['credential_request']
      } as Conversation],
      agents: [],
      onOpenConversation,
      onNewChat: vi.fn()
    });

    expect(screen.getByTestId('dashboard-conversations-waiting')).toBeInTheDocument();
    expect(screen.queryByTestId('activity-avatar-orbit')).not.toBeInTheDocument();
    expect(screen.getByTestId('workstream-execution-status')).toHaveTextContent('Pending');
    expect(screen.getByTestId('dashboard-conversation-waiting-reason-conversation-1')).toHaveTextContent('Credentials required');
    await fireEvent.click(screen.getByTestId('dashboard-conversation-row-conversation-1'));
    expect(onOpenConversation).toHaveBeenCalledWith('conversation-1');
  });

  it('shows an activity ring only for Active-now conversations', () => {
    render(ConversationsSection, {
      conversations: [
        conversation,
        { ...conversation, conversation_id: 'recent', has_active_turn: false } as Conversation,
        {
          ...conversation,
          conversation_id: 'waiting',
          has_active_turn: false,
          pending_notification_types: ['gate']
        } as Conversation
      ],
      agents: [],
      onOpenConversation: vi.fn(),
      onNewChat: vi.fn()
    });

    expect(screen.getAllByTestId('activity-avatar-orbit')).toHaveLength(1);
    expect(screen.getByTestId('dashboard-conversation-row-conversation-1')).toContainElement(
      screen.getByTestId('activity-avatar-orbit')
    );
    expect(screen.getByTestId('dashboard-conversation-row-conversation-1')).toHaveAccessibleName(/working/i);
  });

  it('uses only canonical avatar activity markers and suppresses unread while open', () => {
    render(ConversationsSection, {
      conversations: [
        { ...conversation, has_unread: true } as Conversation,
        {
          ...conversation,
          conversation_id: 'recent-unread',
          has_active_turn: false,
          has_unread: true,
        } as Conversation,
        {
          ...conversation,
          conversation_id: 'waiting-attention',
          has_active_turn: false,
          has_unread: true,
          pending_notification_types: ['gate'],
        } as Conversation,
      ],
      agents: [],
      openConversationId: 'recent-unread',
      onOpenConversation: vi.fn(),
      onNewChat: vi.fn(),
    });

    const activeRow = screen.getByTestId('dashboard-conversation-row-conversation-1');
    expect(activeRow.querySelector('[data-testid="activity-avatar-orbit"]')).not.toBeNull();
    expect(activeRow.querySelector('[data-testid="activity-avatar-unread"]')).toBeNull();
    const openRecent = screen.getByTestId('dashboard-conversation-row-recent-unread');
    expect(openRecent.querySelector('[data-testid="activity-avatar-unread"]')).toBeNull();
    const waiting = screen.getByTestId('dashboard-conversation-row-waiting-attention');
    expect(waiting.querySelector('[data-testid="activity-avatar-attention"]')).not.toBeNull();
    expect(screen.queryByText('Unread', { selector: '[data-testid="workstream-execution-status"]' })).toBeNull();
    expect(document.querySelector('[aria-label="Unread"]')).toBeNull();
  });

  it('shows an actionable paused task between running and upcoming', async () => {
    const onOpenTask = vi.fn();
    const waitingBoard = structuredClone(taskBoard);
    waitingBoard.columns.paused = {
      total_count: 1,
      items: [{
        ...taskBoard.columns.running.items[0],
        task_id: 'waiting-task',
        title: 'Review gate',
        status: 'paused',
        attention_type: 'gate'
      }]
    } as TaskBoard['columns']['paused'];
    render(TasksSection, {
      board: waitingBoard,
      schedules: [],
      agents: [],
      onOpenTask,
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn()
    });

    expect(screen.getByTestId('dashboard-tasks-waiting')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-task-waiting-reason-waiting-task')).toHaveTextContent('Review required');
    const sections = screen.getByTestId('dashboard-tasks-section').querySelectorAll('section');
    expect([...sections].map((section) => section.dataset.testid)).toEqual([
      'dashboard-tasks-running',
      'dashboard-tasks-waiting',
      'dashboard-tasks-upcoming',
      'dashboard-tasks-recent'
    ]);
    await fireEvent.click(screen.getByTestId('dashboard-task-row-waiting-task'));
    expect(onOpenTask).toHaveBeenCalledWith('waiting-task');
  });

  it('shows a non-actionable failed-to-paused task as paused', () => {
    const pausedBoard = structuredClone(taskBoard);
    pausedBoard.columns.paused = {
      total_count: 1,
      items: [{
        ...taskBoard.columns.running.items[0],
        task_id: 'failed-paused-task',
        title: 'Failed then paused',
        status: 'paused',
        attention_type: null,
        completed_at: '2026-08-30T19:00:00Z',
        result_summary: 'Validation failed'
      }]
    } as TaskBoard['columns']['paused'];
    render(TasksSection, {
      board: pausedBoard,
      schedules: [],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn()
    });

    expect(screen.getByText('Paused / waiting')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-task-row-failed-paused-task')).toHaveTextContent('Paused');
    expect(screen.getByTestId('dashboard-task-waiting-reason-failed-paused-task')).toHaveTextContent('Validation failed');
    expect(screen.getByTestId('dashboard-task-row-failed-paused-task')).not.toHaveTextContent('Action required');
  });

  it('keeps more than five paused tasks in the independently scrollable waiting lane', () => {
    const pausedBoard = structuredClone(taskBoard);
    pausedBoard.columns.paused = {
      total_count: 7,
      items: Array.from({ length: 7 }, (_, index) => ({
        ...taskBoard.columns.running.items[0],
        task_id: `paused-task-${index + 1}`,
        title: `Paused task ${index + 1}`,
        status: 'paused',
        attention_type: null,
        updated_at: `2026-08-30T20:00:0${index + 1}Z`
      }))
    } as TaskBoard['columns']['paused'];
    render(TasksSection, {
      board: pausedBoard,
      schedules: [],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn()
    });

    const waitingList = screen.getByTestId('dashboard-tasks-waiting-list');
    expect(waitingList).toHaveClass('md:overflow-y-auto', 'md:max-h-56');
    expect(waitingList.querySelectorAll('li')).toHaveLength(7);
  });

  it('scopes overflow scrolling to the Recent list body, not the whole tasks card', () => {
    const boardWithRecent = structuredClone(taskBoard);
    boardWithRecent.columns.done = {
      total_count: 1,
      items: [{
        ...taskBoard.columns.running.items[0],
        task_id: 'recent-task',
        status: 'completed',
        completed_at: '2026-08-22T02:00:00Z'
      }]
    } as TaskBoard['columns']['done'];
    render(TasksSection, {
      board: boardWithRecent,
      schedules: [schedule],
      agents: [],
      onOpenTask: vi.fn(),
      onOpenSchedule: vi.fn(),
      onNewTask: vi.fn()
    });

    const card = screen.getByTestId('dashboard-tasks-section');
    // Section headings stay in normal flow (no independent scroll region) —
    // only the Recent list body is a designated scrollable region.
    expect(screen.getByTestId('dashboard-tasks-running-list')).toHaveAccessibleName('Running tasks');
    expect(screen.getByTestId('dashboard-tasks-upcoming-list')).toHaveAccessibleName('Upcoming schedules');
    const recentList = screen.getByTestId('dashboard-tasks-recent-list');
    expect(recentList).toHaveAccessibleName('Recently finished tasks');
    expect(recentList.tagName).toBe('UL');
    expect(card).toContainElement(recentList);
  });

  it('scopes overflow scrolling to the Recent conversation list body', () => {
    render(ConversationsSection, {
      conversations: [
        conversation,
        { ...conversation, conversation_id: 'recent-1', has_active_turn: false } as Conversation
      ],
      agents: [],
      onOpenConversation: vi.fn(),
      onNewChat: vi.fn()
    });

    expect(screen.getByTestId('dashboard-conversations-active-list')).toHaveAccessibleName('Active conversations');
    const recentList = screen.getByTestId('dashboard-conversations-recent-list');
    expect(recentList).toHaveAccessibleName('Recent conversations');
    expect(recentList.tagName).toBe('UL');
  });
});
