import type { Conversation } from '$lib/types/api';

export function conversationFixture(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'test-conversation', user_email: 'owner@cognis.test', agent_id: 'test-agent',
    project_id: null, title: 'Conversation', title_source: 'user',
    context: { type: 'web', ref: null, memory_labels: {}, platform_data: {} },
    active_session_id: null, active_executor_id: null, active_executor_assigned_at: null,
    active_executor_expires_at: null, active_executor_source: null, active_session_status: null,
    active_session_completion_reason: null, active_turn_chat_mode: null, active_turn_chat_mode_source: null,
    pending_notification_types: [], starred_at: null, status: 'active',
    last_message_at: null, last_read_at: null, has_unread: false, has_active_turn: false,
    created_at: '2026-10-28T18:00:00Z', updated_at: '2026-10-28T18:00:00Z',
    ...overrides,
  };
}
