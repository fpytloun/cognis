import { describe, expect, it } from 'vitest';

import {
  defaultNewChatAgentId,
  findAgentDirectChat,
  isDashboardConversationActive,
  dashboardConversationWaitingReason,
  dashboardTaskRequiresAction,
  dashboardTaskWaitingReason,
  isDashboardTopicConversation,
  partitionConversationLanes,
  partitionTaskLanes,
  primaryAgentsForStrip,
  summarizeDashboardIssues,
  upcomingSchedules
} from './dashboard';
import type { Agent, AgentDirectChat, Conversation, DashboardIssuesResponse, Schedule, TaskBoard, TaskBoardItem } from '$lib/types/api';

function taskItem(overrides: Partial<TaskBoardItem>): TaskBoardItem {
  return {
    task_id: 'task-1',
    title: 'Task',
    status: 'running',
    priority: 0,
    agent_id: 'laforge',
    workflow_id: null,
    project_id: null,
    source_type: 'user',
    source_ref: null,
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:00Z',
    completed_at: null,
    updated_at: '2026-01-01T00:00:00Z',
    result_summary: null,
    ...overrides
  };
}

function board(running: TaskBoardItem[], done: TaskBoardItem[]): TaskBoard {
  return {
    columns: {
      draft: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
      queued: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
      running: { items: running, groups: [], cursor: null, has_more: false, total_count: running.length },
      paused: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
      done: { items: done, groups: [], cursor: null, has_more: false, total_count: done.length }
    }
  };
}

function conversation(overrides: Partial<Conversation>): Conversation {
  return {
    conversation_id: 'conv-1',
    root_controller_conversation_id: null,
    user_email: 'user@example.com',
    agent_id: 'laforge',
    agent_profile_id: null,
    project_id: null,
    title: 'Conversation',
    title_source: 'auto',
    context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
    active_session_id: null,
    active_executor_id: null,
    active_executor_assigned_at: null,
    active_executor_expires_at: null,
    active_executor_source: null,
    active_session_status: null,
    active_session_completion_reason: null,
    active_turn_chat_mode: null,
    active_turn_chat_mode_source: null,
    pending_notification_types: [],
    starred_at: null,
    status: 'active',
    last_message_at: '2026-01-01T00:00:00Z',
    last_read_at: null,
    has_unread: false,
    has_active_turn: false,
    ...overrides
  } as Conversation;
}

function schedule(overrides: Partial<Schedule>): Schedule {
  return {
    schedule_id: 'sch-1',
    name: 'Schedule',
    description: null,
    schedule_type: 'cron',
    cron_expr: '0 * * * *',
    interval_seconds: null,
    one_shot_at: null,
    timezone: 'UTC',
    agent_id: 'laforge',
    agent_profile_id: null,
    workflow_id: null,
    project_id: null,
    skill_id: null,
    task_template: {},
    enabled: true,
    max_concurrent_runs: 1,
    delete_after_run: false,
    retry_failed_tasks: false,
    fail_paused_task_on_next_fire: false,
    completion_mode_family: 'default',
    allow_silent_completion: false,
    interaction_mode_override: null,
    session_policy: null,
    last_fired_at: null,
    next_fire_at: null,
    last_run_status: null,
    consecutive_errors: 0,
    disabled_reason: null,
    created_by: 'user@example.com',
    created_at: null,
    updated_at: null,
    human_schedule: null,
    is_expired: false,
    expiration_grace_until: null,
    ...overrides
  } as Schedule;
}

function agent(overrides: Partial<Agent>): Agent {
  return {
    agent_id: 'agent',
    owner_email: 'user@example.com',
    name: 'agent',
    display_name: 'Agent',
    description: null,
    system_prompt: null,
    personality: null,
    skills: null,
    tools: null,
    permissions: null,
    llm_config: null,
    execution: null,
    personality_synced: true,
    personality_sync_error: null,
    personality_sync_checked_at: null,
    avatar_url: null,
    avatar_image_id: null,
    agent_type: 'primary',
    is_system: false,
    hidden: false,
    editable_fields: [],
    has_overrides: false,
    disabled: false,
    disableable: true,
    sync_metadata: null,
    is_shared_with_me: false,
    shared_by_email: null,
    granted_permission: null,
    executor_scope: null,
    is_readonly_for_caller: false,
    status: 'active',
    created_at: null,
    updated_at: null,
    ...overrides
  } as Agent;
}

describe('partitionTaskLanes', () => {
  it('orders running tasks by priority then recency and caps to the limit', () => {
    const running = [
      taskItem({ task_id: 'low-old', priority: 0, updated_at: '2026-01-01T00:00:00Z' }),
      taskItem({ task_id: 'high', priority: 5, updated_at: '2026-01-01T00:00:00Z' }),
      taskItem({ task_id: 'low-new', priority: 0, updated_at: '2026-01-02T00:00:00Z' })
    ];
    const lanes = partitionTaskLanes(board(running, []), 2);
    expect(lanes.running.map((item) => item.task_id)).toEqual(['high', 'low-new']);
  });

  it('orders recent (done) tasks by most recently finished first', () => {
    const done = [
      taskItem({ task_id: 'older', status: 'completed', updated_at: '2026-01-01T00:00:00Z' }),
      taskItem({ task_id: 'newer', status: 'completed', updated_at: '2026-01-03T00:00:00Z' }),
      taskItem({ task_id: 'middle', status: 'failed', updated_at: '2026-01-02T00:00:00Z' })
    ];
    const lanes = partitionTaskLanes(board([], done), 5);
    expect(lanes.recent.map((item) => item.task_id)).toEqual(['newer', 'middle', 'older']);
  });

  it('handles a missing board gracefully', () => {
    expect(partitionTaskLanes(null)).toEqual({ running: [], waiting: [], recent: [] });
  });

  it('classifies every paused task as waiting', () => {
    const waiting = [
      taskItem({ task_id: 'gate', status: 'paused', attention_type: 'gate' }),
      taskItem({ task_id: 'input', status: 'paused', attention_type: 'step_input' }),
      taskItem({ task_id: 'auth', status: 'paused', attention_type: 'auth_challenge' }),
      taskItem({ task_id: 'escalation', status: 'paused', attention_type: 'escalation' })
    ];
    const boardValue = board(
      [taskItem({ task_id: 'running' })],
      [taskItem({ task_id: 'done', status: 'completed' })]
    );
    boardValue.columns.paused.items = [
      ...waiting,
      taskItem({ task_id: 'plain-paused', status: 'paused', attention_type: null })
    ];
    const lanes = partitionTaskLanes(boardValue);
    expect(lanes.running.map((item) => item.task_id)).toEqual(['running']);
    expect(lanes.waiting.map((item) => item.task_id)).toEqual(['gate', 'input', 'auth', 'escalation', 'plain-paused']);
    expect(lanes.recent.map((item) => item.task_id)).toEqual(['done']);
    expect(dashboardTaskWaitingReason(waiting[0])).toBe('Review required');
  });

  it('does not cap the paused lane while preserving activity ordering and exact-once deduplication', () => {
    const paused = Array.from({ length: 7 }, (_, index) => taskItem({
      task_id: `paused-${index + 1}`,
      status: 'paused',
      updated_at: `2026-08-30T20:00:0${index + 1}Z`,
    }));
    const boardValue = board([], [paused[0]]);
    boardValue.columns.paused.items = [...paused, paused[3]];

    const lanes = partitionTaskLanes(boardValue, 5);

    expect(lanes.waiting.map((task) => task.task_id)).toEqual([
      'paused-7', 'paused-6', 'paused-5', 'paused-4', 'paused-3', 'paused-2', 'paused-1',
    ]);
  });

  it('moves a failed task paused after validation out of recent exactly once', () => {
    const failedThenPaused = taskItem({
      task_id: 'task_sched_e6ffc4cc29a8f37f55ab3bbf5f15adc7',
      status: 'paused',
      attention_type: null,
      completed_at: '2026-08-30T19:00:00Z',
      result_summary: 'Validation failed',
      progress_summary: {
        todo_total: 0,
        todo_completed: 0,
        todo_in_progress: 0,
        current_step_name: 'Validate result',
        current_step_status: 'failed',
        changed_files: 0,
        additions: 0,
        deletions: 0
      }
    });
    const boardValue = board([], [failedThenPaused]);
    boardValue.columns.paused.items = [failedThenPaused];

    const lanes = partitionTaskLanes(boardValue);

    expect(lanes.waiting.map((item) => item.task_id)).toEqual([failedThenPaused.task_id]);
    expect(lanes.recent).toEqual([]);
    expect(dashboardTaskRequiresAction(failedThenPaused)).toBe(false);
    expect(dashboardTaskWaitingReason(failedThenPaused)).toBe('Validation failed');
  });

  it('uses the current step then Paused for non-actionable pause reasons', () => {
    const currentStep = taskItem({
      status: 'paused',
      attention_type: null,
      progress_summary: {
        todo_total: 0,
        todo_completed: 0,
        todo_in_progress: 0,
        current_step_name: 'Review output',
        current_step_status: 'paused',
        changed_files: 0,
        additions: 0,
        deletions: 0
      }
    });
    expect(dashboardTaskWaitingReason(currentStep)).toBe('Review output');
    expect(dashboardTaskWaitingReason(taskItem({ status: 'paused', attention_type: null }))).toBe('Paused');
  });
});

describe('upcomingSchedules', () => {
  it('omits disabled schedules and schedules without a next run', () => {
    const schedules = [
      schedule({ schedule_id: 'disabled', enabled: false, next_fire_at: '2026-01-01T00:00:00Z' }),
      schedule({ schedule_id: 'no-next-run', enabled: true, next_fire_at: null }),
      schedule({ schedule_id: 'valid', enabled: true, next_fire_at: '2026-01-01T00:00:00Z' })
    ];
    expect(upcomingSchedules(schedules).map((item) => item.schedule_id)).toEqual(['valid']);
  });

  it('sorts by soonest next_fire_at first and caps to the limit', () => {
    const schedules = [
      schedule({ schedule_id: 'later', next_fire_at: '2026-02-01T00:00:00Z' }),
      schedule({ schedule_id: 'soonest', next_fire_at: '2026-01-01T00:00:00Z' }),
      schedule({ schedule_id: 'middle', next_fire_at: '2026-01-15T00:00:00Z' })
    ];
    expect(upcomingSchedules(schedules, 2).map((item) => item.schedule_id)).toEqual(['soonest', 'middle']);
  });
});

describe('isDashboardConversationActive', () => {
  it('is active when has_active_turn is true', () => {
    expect(isDashboardConversationActive({ has_active_turn: true, managed_agent: null })).toBe(true);
  });

  it('is active for in-flight managed turn states', () => {
    expect(isDashboardConversationActive({ has_active_turn: false, managed_agent: { turn_state: 'running' } })).toBe(true);
    expect(isDashboardConversationActive({ has_active_turn: false, managed_agent: { turn_state: 'queued' } })).toBe(true);
  });

  it('is idle for terminal or missing managed turn states', () => {
    expect(isDashboardConversationActive({ has_active_turn: false, managed_agent: { turn_state: 'completed' } })).toBe(false);
    expect(isDashboardConversationActive({ has_active_turn: false, managed_agent: null })).toBe(false);
  });
});

describe('partitionConversationLanes', () => {
  it('gives canonical waiting notifications precedence over active and recent', () => {
    const canonical = ['step_question', 'gate', 'auth_challenge', 'credential_request', 'escalation'];
    const conversations = canonical.map((type, index) =>
      conversation({
        conversation_id: `waiting-${index}`,
        has_active_turn: index === 0,
        pending_notification_types: [type],
        last_message_at: `2026-01-0${index + 1}T00:00:00Z`
      })
    );
    conversations.push(conversation({ conversation_id: 'active', has_active_turn: true }));
    conversations.push(conversation({ conversation_id: 'recent' }));
    const lanes = partitionConversationLanes(conversations);
    expect(lanes.waiting).toHaveLength(5);
    expect(lanes.active.map((item) => item.conversation_id)).toEqual(['active']);
    expect(lanes.recent.map((item) => item.conversation_id)).toEqual(['recent']);
    expect(dashboardConversationWaitingReason(conversations[0])).toBe('Question waiting');
  });
  it('includes only ordinary web topics before partitioning', () => {
    const topic = conversation({ conversation_id: 'topic' });
    const rows = [
      topic,
      conversation({ conversation_id: 'slack', context: { type: 'slack', ref: 'C1', platform_data: {}, memory_labels: {} } }),
      conversation({ conversation_id: 'signal', context: { type: 'signal', ref: 'group', platform_data: {}, memory_labels: {} } }),
      conversation({ conversation_id: 'task', context: { type: 'web', ref: 'web:task_control:t1', platform_data: { kind: 'task_control' }, memory_labels: {} } }),
      conversation({ conversation_id: 'task-state', conversation_state: { task: { task_id: 't2' } } as Conversation['conversation_state'] }),
      conversation({ conversation_id: 'direct-kind', context: { type: 'web', ref: null, platform_data: { kind: 'agent_direct' }, memory_labels: {} } }),
      conversation({ conversation_id: 'direct-ref', context: { type: 'web', ref: 'web:agent_direct:user@example.com:laforge', platform_data: {}, memory_labels: {} } }),
      conversation({ conversation_id: 'main', context: { type: 'web', ref: 'web:user:user@example.com:default', platform_data: {}, memory_labels: {} } }),
      conversation({
        conversation_id: 'managed',
        context: { type: 'web', ref: null, platform_data: { kind: 'managed_agent_conversation' }, memory_labels: {} },
        managed_agent: { channel: 'managed_agent_conversation' }
      }),
      conversation({ conversation_id: 'managed-root', root_controller_conversation_id: 'controller-1' })
    ];

    expect(rows.map((row) => [row.conversation_id, isDashboardTopicConversation(row)])).toEqual([
      ['topic', true],
      ['slack', false],
      ['signal', false],
      ['task', false],
      ['task-state', false],
      ['direct-kind', false],
      ['direct-ref', false],
      ['main', false],
      ['managed', false],
      ['managed-root', false]
    ]);
    const lanes = partitionConversationLanes(rows, { active: 5, recent: 8 });
    expect(lanes.active).toEqual([]);
    expect(lanes.waiting).toEqual([]);
    expect(lanes.recent.map((row) => row.conversation_id)).toEqual(['topic']);
  });

  it('keeps active and recent disjoint even when active exceeds its display limit', () => {
    const conversations = [
      conversation({ conversation_id: 'a1', has_active_turn: true, last_message_at: '2026-01-05T00:00:00Z' }),
      conversation({ conversation_id: 'a2', has_active_turn: true, last_message_at: '2026-01-04T00:00:00Z' }),
      conversation({ conversation_id: 'a3', has_active_turn: true, last_message_at: '2026-01-03T00:00:00Z' }),
      conversation({ conversation_id: 'idle1', has_active_turn: false, last_message_at: '2026-01-02T00:00:00Z' })
    ];
    const lanes = partitionConversationLanes(conversations, { active: 2, recent: 8 });
    expect(lanes.active.map((c) => c.conversation_id)).toEqual(['a1', 'a2']);
    // a3 is active (beyond the display cap) and must never leak into recent.
    expect(lanes.recent.map((c) => c.conversation_id)).toEqual(['idle1']);
  });

  it('orders recent conversations by most recent last_message_at first and caps to the limit', () => {
    const conversations = [
      conversation({ conversation_id: 'old', last_message_at: '2026-01-01T00:00:00Z' }),
      conversation({ conversation_id: 'new', last_message_at: '2026-01-03T00:00:00Z' }),
      conversation({ conversation_id: 'mid', last_message_at: '2026-01-02T00:00:00Z' })
    ];
    const lanes = partitionConversationLanes(conversations, { active: 5, recent: 2 });
    expect(lanes.recent.map((c) => c.conversation_id)).toEqual(['new', 'mid']);
  });
});

describe('primaryAgentsForStrip', () => {
  it('filters to primary, non-hidden, non-disabled agents and sorts by display name', () => {
    const agents = [
      agent({ agent_id: 'b', display_name: 'Bravo', agent_type: 'primary' }),
      agent({ agent_id: 'secondary', display_name: 'Secondary', agent_type: 'secondary' }),
      agent({ agent_id: 'hidden', display_name: 'Hidden', agent_type: 'primary', hidden: true }),
      agent({ agent_id: 'disabled', display_name: 'Disabled', agent_type: 'primary', disabled: true }),
      agent({ agent_id: 'a', display_name: 'Alpha', agent_type: 'primary' })
    ];
    expect(primaryAgentsForStrip(agents).map((item) => item.agent_id)).toEqual(['a', 'b']);
  });
});

describe('findAgentDirectChat', () => {
  it('resolves the existing agent-direct conversation for an agent', () => {
    const chats: AgentDirectChat[] = [
      { agent: agent({ agent_id: 'laforge' }), conversation: conversation({ conversation_id: 'direct-1' }) }
    ];
    expect(findAgentDirectChat(chats, 'laforge')?.conversation.conversation_id).toBe('direct-1');
    expect(findAgentDirectChat(chats, 'missing')).toBeNull();
  });
});

describe('defaultNewChatAgentId', () => {
  it('prefers an active primary agent over an inactive one', () => {
    const agents = [
      agent({ agent_id: 'inactive', agent_type: 'primary', status: 'suspended' }),
      agent({ agent_id: 'active', agent_type: 'primary', status: 'active' })
    ];
    expect(defaultNewChatAgentId(agents)).toBe('active');
  });

  it('falls back to the first primary agent when none are active', () => {
    const agents = [
      agent({ agent_id: 'first', agent_type: 'primary', status: 'suspended', display_name: 'Alpha' }),
      agent({ agent_id: 'second', agent_type: 'primary', status: 'suspended', display_name: 'Bravo' })
    ];
    expect(defaultNewChatAgentId(agents)).toBe('first');
  });

  it('returns null when there are no eligible primary agents', () => {
    expect(defaultNewChatAgentId([])).toBeNull();
  });
});

describe('summarizeDashboardIssues', () => {
  function issuesResponse(overrides: Partial<DashboardIssuesResponse['summary']>): DashboardIssuesResponse {
    return {
      generated_at: '2026-01-01T00:00:00Z',
      issues: [],
      summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false, ...overrides }
    };
  }

  it('reports healthy when there are no issues', () => {
    expect(summarizeDashboardIssues(issuesResponse({ total: 0 }))).toEqual({
      tone: 'healthy',
      headline: 'All systems healthy'
    });
  });

  it('prioritizes critical over warning and info', () => {
    expect(summarizeDashboardIssues(issuesResponse({ total: 3, critical: 1, warning: 1, info: 1 })).tone).toBe('critical');
  });

  it('reports warning when no critical issues exist', () => {
    expect(summarizeDashboardIssues(issuesResponse({ total: 2, warning: 2 })).tone).toBe('warning');
  });

  it('treats a missing response as healthy', () => {
    expect(summarizeDashboardIssues(null).tone).toBe('healthy');
  });
});
