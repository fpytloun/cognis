import { describe, expect, it, vi } from 'vitest';
import {
  dashboardBatchRefreshesModal,
  dashboardConversationPatch,
  dashboardModalDetailInvalidation,
  dashboardModalDetailInvalidations,
  dashboardRealtimeLoads,
  subscribeDashboardRealtime,
} from './realtime';

describe('subscribeDashboardRealtime', () => {
  it('coalesces bounded affected resources and unsubscribes without polling', async () => {
    let listener: ((event: { type: string }) => void) | null = null;
    const unsubscribe = vi.fn();
    const client = { subscribe: vi.fn((next) => { listener = next; return unsubscribe; }) };
    const flush = vi.fn();
    const cleanup = subscribeDashboardRealtime(client, flush);
    const emit = listener as unknown as (event: { type: string }) => void;
    emit({ type: 'message_complete' });
    emit({ type: 'conversation_updated' });
    emit({ type: 'task_paused' });
    emit({ type: 'schedule_action_changed' });
    emit({ type: 'conversation_state_delta' });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(flush).toHaveBeenCalledTimes(1);
    expect([...flush.mock.calls[0][0].loads]).toEqual(['conversations', 'tasks', 'schedules', 'issues']);
    expect(flush.mock.calls[0][0].modalDetails).toEqual([]);
    expect(flush.mock.calls[0][0].conversationPatches).toEqual([]);
    cleanup();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(String(subscribeDashboardRealtime)).not.toContain('setInterval');
  });

  it('separates lane invalidation from matching metadata detail invalidation', async () => {
    let listener: ((event: { type: string; [key: string]: unknown }) => void) | null = null;
    const flush = vi.fn();
    subscribeDashboardRealtime(
      { subscribe: (next) => { listener = next as typeof listener; return () => undefined; } },
      flush
    );
    const emit = listener as unknown as (event: { type: string; [key: string]: unknown }) => void;
    emit({ type: 'message_complete', conversation_id: 'conversation-1' });
    emit({ type: 'turn_started', conversation_id: 'conversation-1' });
    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-1',
      has_active_turn: true
    });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect([...flush.mock.calls[0][0].loads]).toEqual(['conversations']);
    expect(flush.mock.calls[0][0].modalDetails).toEqual([]);
    expect(flush.mock.calls[0][0].conversationPatches).toEqual([
      { conversation_id: 'conversation-1', has_active_turn: true }
    ]);

    emit({
      type: 'conversation_updated',
      conversation_id: 'conversation-1',
      title: 'Updated title'
    });
    emit({ type: 'workflow_completed', task_id: 'task-2' });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(flush.mock.calls[1][0].modalDetails).toEqual([
      { kind: 'conversation', id: 'conversation-1' },
      { kind: 'task', id: 'task-2' }
    ]);
  });

  it('does not treat unrelated or lifecycle-only events as modal metadata changes', () => {
    expect(dashboardModalDetailInvalidation({
      type: 'conversation_updated',
      conversation_id: 'conversation-1',
      has_unread: true,
      has_active_turn: false
    })).toBeNull();
    expect(dashboardModalDetailInvalidation({
      type: 'conversation_runtime_snapshot',
      conversation_id: 'conversation-1'
    })).toBeNull();
    expect(dashboardModalDetailInvalidation({
      type: 'workflow_completed',
      task_id: 'task-elsewhere'
    })).toEqual({ kind: 'task', id: 'task-elsewhere' });
    const batch = {
      loads: new Set(['tasks'] as const),
      modalDetails: [{ kind: 'task', id: 'task-elsewhere' }] as const,
      conversationPatches: []
    };
    expect(dashboardBatchRefreshesModal(batch, { kind: 'task', id: 'task-open' })).toBe(false);
    expect(dashboardBatchRefreshesModal(batch, { kind: 'conversation', id: 'conversation-1' })).toBe(false);
    expect(dashboardBatchRefreshesModal(batch, { kind: 'schedule', id: 'schedule-1' })).toBe(false);
    expect(dashboardBatchRefreshesModal(batch, { kind: 'task', id: 'task-elsewhere' })).toBe(true);
  });

  it('invalidates exact schedule detail from schedule actions', () => {
    const detail = dashboardModalDetailInvalidation({
      type: 'schedule_action_changed',
      schedule_id: 'schedule-1',
    });
    expect(detail).toEqual({ kind: 'schedule', id: 'schedule-1' });
    expect(dashboardBatchRefreshesModal(
      { loads: new Set(['schedules']), modalDetails: [detail!], conversationPatches: [] },
      { kind: 'schedule', id: 'schedule-1' },
    )).toBe(true);
  });

  it('classifies canonical cluster invalidations and exact details', () => {
    expect(dashboardRealtimeLoads('scope_invalidated', 'sidebar_changed')).toEqual(['conversations']);
    expect(dashboardRealtimeLoads('scope_invalidated', 'chat_scope_changed')).toEqual(['conversations']);
    expect(dashboardRealtimeLoads('scope_invalidated', 'notification_state_changed')).toEqual([
      'conversations', 'tasks', 'schedules', 'issues'
    ]);
    expect(dashboardRealtimeLoads('scope_invalidated', 'task_progress_changed')).toEqual([
      'tasks', 'schedules', 'issues'
    ]);
    expect(dashboardRealtimeLoads('scope_invalidated', 'schedule_action_changed')).toEqual([
      'schedules', 'issues'
    ]);
    expect(dashboardModalDetailInvalidation({
      type: 'scope_invalidated',
      reason: 'chat_scope_changed',
      conversation_id: 'conversation-1'
    })).toEqual({ kind: 'conversation', id: 'conversation-1' });
    expect(dashboardModalDetailInvalidation({
      type: 'scope_invalidated',
      reason: 'task_progress_changed',
      task_id: 'task-1'
    })).toEqual({ kind: 'task', id: 'task-1' });
    expect(dashboardModalDetailInvalidations({
      type: 'scope_invalidated',
      reason: 'notification_state_changed',
      conversation_id: 'conversation-1',
      task_id: 'task-1',
      schedule_id: 'schedule-1'
    })).toEqual([
      { kind: 'conversation', id: 'conversation-1' },
      { kind: 'task', id: 'task-1' },
      { kind: 'schedule', id: 'schedule-1' }
    ]);
    expect(dashboardModalDetailInvalidations({
      type: 'scope_invalidated',
      reason: 'schedule_action_changed',
      conversation_id: 'conversation-1',
      task_id: 'task-1',
      schedule_id: 'schedule-1'
    })).toEqual([
      { kind: 'conversation', id: 'conversation-1' },
      { kind: 'task', id: 'task-1' },
      { kind: 'schedule', id: 'schedule-1' }
    ]);
  });

  it('extracts only safe immediate conversation fields', () => {
    expect(dashboardConversationPatch({
      type: 'conversation_updated',
      conversation_id: 'conversation-1',
      title: 'Canonical title',
      has_active_turn: true,
      has_unread: false,
      pending_notification_types: ['question'],
      payload: { ignored: true }
    })).toEqual({
      conversation_id: 'conversation-1',
      title: 'Canonical title',
      has_active_turn: true,
      has_unread: false,
      pending_notification_types: ['question']
    });
    expect(dashboardConversationPatch({
      type: 'sidebar_conversation_upsert',
      conversation_id: 'conversation-2',
      conversation: {
        conversation_id: 'conversation-2',
        title: 'Nested canonical title',
        has_active_turn: false,
        pending_notification_types: ['gate']
      }
    })).toEqual({
      conversation_id: 'conversation-2',
      title: 'Nested canonical title',
      has_active_turn: false,
      pending_notification_types: ['gate']
    });
  });

  it('drops a queued flush after cleanup', async () => {
    let listener: ((event: { type: string }) => void) | null = null;
    const flush = vi.fn();
    const cleanup = subscribeDashboardRealtime(
      { subscribe: (next) => { listener = next; return () => undefined; } },
      flush
    );
    const emit = listener as unknown as (event: { type: string }) => void;
    emit({ type: 'workflow_completed' });
    cleanup();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(flush).not.toHaveBeenCalled();
  });
});
