import { describe, expect, it } from 'vitest';

import { conversationActivityState } from '$lib/conversation-activity';
import type { Conversation } from '$lib/types/api';

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'conversation-1',
    has_active_turn: false,
    has_unread: false,
    active_session_status: 'active',
    active_session_completion_reason: null,
    pending_notification_types: [],
    ...overrides,
  } as Conversation;
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
      active: true,
      background: false,
      error: false,
      attention: false,
      unread: false,
      tone: 'rose',
      label: 'Control chat is working',
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
