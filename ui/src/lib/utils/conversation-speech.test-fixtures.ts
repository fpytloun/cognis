import type { MessageTimelineItem, RuntimeOverlaySnapshot } from '$lib/chat-v2/types';

export function speechRuntime(
  content: string,
  overrides: Partial<RuntimeOverlaySnapshot> = {},
  message: Partial<MessageTimelineItem> = {},
): RuntimeOverlaySnapshot {
  const hasActiveTurn = overrides.has_active_turn ?? true;
  return {
    runtime_epoch: 'voice-runtime',
    runtime_revision: 1,
    generated_at: '2026-11-02T08:34:57Z',
    has_active_turn: hasActiveTurn,
    active_turn: hasActiveTurn ? {
      turn_id: 'voice-turn', primary_client_message_id: 'voice-input',
      session_id: 'voice-session', status: 'running',
    } : null,
    volatile_items: content ? [{
      id: 'voice-answer', kind: 'message', role: 'assistant',
      message_id: 'voice-answer', turn_id: 'voice-turn', content,
      sort_key: '001', stable: false, source_refs: [], attachments: [], partial: hasActiveTurn,
      status: hasActiveTurn ? 'running' : 'complete', text_streaming: hasActiveTurn,
      ...message,
    }] : [],
    ...overrides,
  };
}
