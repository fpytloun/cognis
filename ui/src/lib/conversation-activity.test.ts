import { describe, expect, it } from 'vitest';

import { conversationActivityState } from '$lib/conversation-activity';
import type { Conversation } from '$lib/types/api';

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'conversation-1',
    has_active_turn: false,
    has_unread: false,
    last_message_at: '2026-01-01T00:00:00Z',
    active_session_status: 'active',
    active_session_completion_reason: null,
    pending_notification_types: [],
    ...overrides,
  } as Conversation;
}

function taskControlConversation(overrides: Partial<Conversation> = {}): Conversation {
  return conversation({
    context: {
      type: 'web',
      ref: 'web:task_control:task-1',
      platform_data: { kind: 'task_control', task_id: 'task-1' },
      memory_labels: {},
    },
    ...overrides,
  });
}

describe('conversationActivityState', () => {
  it('uses control-chat activity instead of task lifecycle state', () => {
    expect(conversationActivityState(conversation()).active).toBe(false);
    expect(conversationActivityState(conversation({ has_active_turn: true })).active).toBe(true);
  });

  it('shows unread activity only while the control chat is closed', () => {
    const unread = conversation({ has_unread: true });
    expect(conversationActivityState(unread, { open: false }).unread).toBe(true);
    expect(conversationActivityState(unread, { open: true }).unread).toBe(false);
  });

  it('suppresses unread only for an empty task-control conversation', () => {
    const neverMessaged = taskControlConversation({ has_unread: true, last_message_at: null });
    expect(conversationActivityState(neverMessaged, { open: false }).unread).toBe(false);

    const attentionOnly = taskControlConversation({
      has_unread: true,
      last_message_at: null,
      pending_notification_types: ['gate'],
    });
    const state = conversationActivityState(attentionOnly, { open: false });
    expect(state.unread).toBe(false);
    expect(state.attention).toBe(true);

    const realMessage = taskControlConversation({ has_unread: true });
    expect(conversationActivityState(realMessage, { open: false }).unread).toBe(true);

    const normalEventOnly = conversation({ has_unread: true, last_message_at: null });
    expect(conversationActivityState(normalEventOnly, { open: false }).unread).toBe(true);
  });

  it('maps control-chat session failure to error', () => {
    expect(conversationActivityState(conversation({ active_session_status: 'failed' })).error).toBe(true);
  });

  it('preserves conversation-sidebar critical and ordinary attention severity', () => {
    const credential = conversationActivityState(conversation({
      pending_notification_types: ['credential_request'],
    }));
    expect(credential).toMatchObject({ error: true, attention: false, tone: 'rose' });

    const gate = conversationActivityState(conversation({
      pending_notification_types: ['gate'],
    }));
    expect(gate).toMatchObject({ error: false, attention: true, tone: 'amber' });
  });

  it('keeps the label synchronized with the final rendered precedence', () => {
    const combined = conversation({
      has_active_turn: true,
      has_unread: true,
      active_session_status: 'failed',
      pending_notification_types: ['gate'],
    });
    expect(conversationActivityState(combined)).toMatchObject({
      active: false,
      background: false,
      error: false,
      attention: true,
      unread: false,
      tone: 'amber',
      label: 'Waiting for approval',
    });
  });

  it('shows pending input instead of a running indicator', () => {
    expect(conversationActivityState(conversation({
      has_active_turn: true,
      pending_notification_types: ['credential_request'],
    }))).toMatchObject({
      active: false,
      error: true,
      label: 'Credential required',
    });
    expect(conversationActivityState(conversation({
      has_active_turn: true,
      pending_notification_types: ['auth_challenge'],
    }))).toMatchObject({
      active: false,
      attention: true,
      label: 'Authentication required',
    });
    expect(conversationActivityState(conversation({
      has_active_turn: true,
      pending_notification_types: ['step_question'],
    }))).toMatchObject({
      active: false,
      attention: true,
      label: 'Waiting for answer',
    });
  });

  it('matches conversation-sidebar background work semantics', () => {
    const state = conversationActivityState(conversation(), {
      backgroundWork: [{
        kind: 'managed_conversation',
        work_id: 'work-1',
        controller_conversation_id: 'conversation-1',
        title: 'Worker',
        agent_id: 'agent-1',
        status: 'running',
        todos: [],
      }],
    });
    expect(state.background).toBe(true);
    expect(state.active).toBe(false);
  });
});
