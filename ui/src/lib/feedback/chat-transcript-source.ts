import type { SourceRef, TimelineItem, TimelineScope } from '$lib/chat-v2/types';
import type { FeedbackSourceRef } from './types';

/** Additive producer contract; the shared DTO addition is owned by the backend slice. */
type ChatTranscriptSource = Extract<FeedbackSourceRef, { kind: 'transcript' }> & {
  representation: 'chat-item-as-of-event-v1';
};

function contributor(ref: SourceRef): boolean {
  return (ref.event_type === 'assistant_message' || ref.event_type === 'message')
    && Boolean(ref.store && ref.session_id) && Number.isSafeInteger(ref.seq) && ref.seq > 0;
}

export function chatTranscriptSource(item: TimelineItem, scope?: TimelineScope): ChatTranscriptSource | null {
  // Persisted messages need not carry a presentation status; stable, complete
  // content must still never opt in while text is actively streaming.
  if (item.kind !== 'message' || item.role !== 'assistant' || !item.stable
    || (item.status != null && item.status !== 'complete') || item.text_streaming === true
    || item.partial || !item.content.trim() || !scope?.conversation_id) return null;
  // The canonical projector appends/deduplicates refs in contributor order.
  // `message` refs inherit their assistant role from that canonical item;
  // unrelated event kinds are not assistant-content contributors.
  const event = item.source_refs.findLast(contributor);
  if (!event) return null;
  return {
    kind: 'transcript', representation: 'chat-item-as-of-event-v1',
    conversation_id: scope.conversation_id, source_item_id: item.id, field_path: ['content'],
    source_event: { ...event, event_id: event.event_id ?? undefined },
  };
}
