import { describe, expect, it } from 'vitest';
import type { TimelineItem, TurnCycleState } from '$lib/chat-v2/types';
import type { BackgroundWorkItem } from '$lib/types/api';
import {
  backgroundWorkItemIsRunning,
  currentCycleDelegations,
  mergeCurrentCycleDelegations,
  overlayManagedConversationStatus,
  sortBackgroundWorkByActivity,
} from './ongoing-work';

function delegation(
  sessionId: string,
  cycle: number,
  status: 'running' | 'complete',
): TimelineItem {
  return {
    id: `delegation:${sessionId}`,
    kind: 'delegation',
    child_session_id: sessionId,
    turn_id: 'turn_1',
    turn_cycle_index: cycle,
    agent_id: 'system:implement',
    title: `Task ${sessionId}`,
    status,
    todos: [{ content: 'Implement slice', status: status === 'complete' ? 'completed' : 'in_progress' }],
    sort_key: sessionId,
    source_refs: [],
    stable: status === 'complete',
  };
}

function foldedDelegation(
  sessionId: string,
  cycle: number,
  status: 'running' | 'completed',
): TimelineItem {
  return {
    id: `tool:${sessionId}`,
    kind: 'tool_call',
    call_id: `call_${sessionId}`,
    tool_name: 'delegate',
    turn_id: 'turn_1',
    turn_cycle_index: cycle,
    status: status === 'completed' ? 'complete' : 'running',
    arguments: {},
    result_preview: '',
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: false,
    has_full_output: true,
    sort_key: sessionId,
    source_refs: [],
    stable: status === 'completed',
    delegation: {
      child_session_id: sessionId,
      title: `Folded ${sessionId}`,
      used_agent_id: 'system:review',
      used_agent_profile_id: 'default',
      status,
      todos: [{
        content: 'Review slice',
        status: status === 'completed' ? 'completed' : 'in_progress',
      }],
    },
  };
}

describe('currentCycleDelegations', () => {
  it('keeps completed siblings visible until the next cycle opens', () => {
    const items = [
      delegation('sess_old', 2, 'complete'),
      delegation('sess_running', 3, 'running'),
      delegation('sess_done', 3, 'complete'),
    ];
    const cycles: TurnCycleState[] = [
      { turn_id: 'turn_1', turn_cycle_index: 2, lifecycle_status: 'complete', has_tool_activity: true },
      { turn_id: 'turn_1', turn_cycle_index: 3, lifecycle_status: 'open', has_tool_activity: true },
    ];

    expect(currentCycleDelegations(items, cycles, 'turn_1', 'conv_1').map((item) => item.work_id))
      .toEqual(['sess_running', 'sess_done']);
  });

  it('drops the previous cycle when a new cycle opens', () => {
    const cycles: TurnCycleState[] = [
      { turn_id: 'turn_1', turn_cycle_index: 3, lifecycle_status: 'complete', has_tool_activity: true },
      { turn_id: 'turn_1', turn_cycle_index: 4, lifecycle_status: 'open', has_tool_activity: false },
    ];

    expect(currentCycleDelegations([delegation('sess_done', 3, 'complete')], cycles, 'turn_1', 'conv_1'))
      .toEqual([]);
  });

  it('retains a completed latest cycle until a successor cycle exists', () => {
    const cycles: TurnCycleState[] = [
      { turn_id: 'turn_1', turn_cycle_index: 3, lifecycle_status: 'complete', has_tool_activity: true },
    ];

    expect(currentCycleDelegations([delegation('sess_done', 3, 'complete')], cycles, 'turn_1', 'conv_1'))
      .toHaveLength(1);
  });

  it('includes folded delegate tool calls from the current cycle', () => {
    const work = currentCycleDelegations(
      [
        foldedDelegation('sess_running', 3, 'running'),
        foldedDelegation('sess_done', 3, 'completed'),
      ],
      [{ turn_id: 'turn_1', turn_cycle_index: 3, lifecycle_status: 'open', has_tool_activity: true }],
      'turn_1',
      'conv_1',
    );

    expect(work).toMatchObject([
      {
        work_id: 'sess_running',
        title: 'Folded sess_running',
        agent_id: 'system:review',
        agent_profile_id: 'default',
        status: 'running',
      },
      {
        work_id: 'sess_done',
        status: 'completed',
        todos: [{ content: 'Review slice', status: 'completed' }],
      },
    ]);
  });

  it('deduplicates standalone and folded forms in favor of folded progress', () => {
    const work = currentCycleDelegations(
      [
        delegation('sess_1', 3, 'running'),
        foldedDelegation('sess_1', 3, 'completed'),
      ],
      [{ turn_id: 'turn_1', turn_cycle_index: 3, lifecycle_status: 'complete', has_tool_activity: true }],
      'turn_1',
      'conv_1',
    );

    expect(work).toHaveLength(1);
    expect(work[0]).toMatchObject({
      work_id: 'sess_1',
      title: 'Folded sess_1',
      status: 'completed',
    });
  });
});

describe('mergeCurrentCycleDelegations', () => {
  it('uses richer cycle state for projected active delegates', () => {
    const projected: BackgroundWorkItem[] = [{
      kind: 'delegated_session',
      work_id: 'sess_1',
      controller_conversation_id: 'conv_1',
      session_id: 'sess_1',
      title: 'Projected',
      agent_id: 'system:implement',
      status: 'active',
      todos: [],
    }];
    const current = currentCycleDelegations(
      [delegation('sess_1', 1, 'running')],
      [{ turn_id: 'turn_1', turn_cycle_index: 1, lifecycle_status: 'open', has_tool_activity: true }],
      'turn_1',
      'conv_1',
    );

    expect(mergeCurrentCycleDelegations(projected, current)[0]).toMatchObject({
      title: 'Task sess_1',
      status: 'running',
      todos: [{ content: 'Implement slice', status: 'in_progress' }],
    });
  });
});

describe('backgroundWorkItemIsRunning', () => {
  const managed = (status: string): BackgroundWorkItem => ({
    kind: 'managed_conversation',
    work_id: 'mconv_1',
    controller_conversation_id: 'conv_1',
    target_conversation_id: 'conv_target',
    title: 'Managed work',
    agent_id: 'laforge',
    status,
    todos: [],
  });

  it('only treats queued or running managed turns as running', () => {
    expect(backgroundWorkItemIsRunning(managed('running'))).toBe(true);
    expect(backgroundWorkItemIsRunning(managed('queued'))).toBe(true);
    expect(backgroundWorkItemIsRunning(managed('active'))).toBe(false);
    expect(backgroundWorkItemIsRunning(managed('error'))).toBe(false);
  });

  it('treats an active delegated session as running', () => {
    expect(backgroundWorkItemIsRunning({
      ...managed('active'),
      kind: 'delegated_session',
      session_id: 'sess_1',
    })).toBe(true);
  });
});

describe('sortBackgroundWorkByActivity', () => {
  const work = (
    workId: string,
    status: string,
    updatedAt?: string,
    startedAt?: string,
  ): BackgroundWorkItem => ({
    kind: 'delegated_session',
    work_id: workId,
    controller_conversation_id: 'conv_1',
    session_id: workId,
    title: workId,
    agent_id: 'system:explore',
    status,
    updated_at: updatedAt,
    started_at: startedAt,
    todos: [],
  });

  it('sorts globally by latest activity regardless of status or work kind', () => {
    const recentFailure = work('failed', 'failed', '2026-07-29T12:03:00Z');
    const olderRunning = work('running', 'running', '2026-07-29T12:02:00Z');
    const oldestManaged: BackgroundWorkItem = {
      ...work('managed', 'active', '2026-07-29T12:01:00Z'),
      kind: 'managed_conversation',
      target_conversation_id: 'conv_target',
    };

    expect(sortBackgroundWorkByActivity([
      oldestManaged,
      olderRunning,
      recentFailure,
    ]).map((item) => item.work_id)).toEqual([
      'failed',
      'running',
      'managed',
    ]);
  });

  it('falls back to started time and preserves source order for ties or missing timestamps', () => {
    expect(sortBackgroundWorkByActivity([
      work('missing-a', 'interrupted'),
      work('started', 'complete', undefined, '2026-07-29T12:01:00Z'),
      work('missing-b', 'running'),
      work('same-a', 'failed', '2026-07-29T12:02:00Z'),
      work('same-b', 'running', '2026-07-29T12:02:00Z'),
    ]).map((item) => item.work_id)).toEqual([
      'same-a',
      'same-b',
      'started',
      'missing-a',
      'missing-b',
    ]);
  });
});

describe('overlayManagedConversationStatus', () => {
  it('reconciles a stale running tool card from the background projection', () => {
    const tool: TimelineItem = {
      id: 'tool_1',
      kind: 'tool_call',
      call_id: 'call_1',
      tool_name: 'agent_conversation_create',
      status: 'complete',
      arguments: {},
      result_preview: '',
      is_error: false,
      attachments: [],
      file_diffs: [],
      truncated: false,
      has_full_output: true,
      sort_key: '1',
      source_refs: [],
      stable: true,
      managed_conversation: {
        status: 'running',
        conversation: {
          conversation_id: 'conv_target',
          turn_state: 'running',
          active_turn_id: 'turn_stale',
        },
      },
    };
    const work: BackgroundWorkItem[] = [{
      kind: 'managed_conversation',
      work_id: 'mconv_1',
      controller_conversation_id: 'conv_1',
      target_conversation_id: 'conv_target',
      title: 'Managed work',
      agent_id: 'laforge',
      status: 'active',
      todos: [],
    }];

    const [updated] = overlayManagedConversationStatus([tool], work);
    expect(updated).toMatchObject({
      managed_conversation: {
        status: 'active',
        conversation: {
          turn_state: 'active',
          active_turn_id: null,
        },
      },
    });
  });
});
