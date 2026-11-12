import { describe, expect, it } from 'vitest';
import { chatTranscriptSource } from './chat-transcript-source';
import type { MessageTimelineItem, SourceRef, TimelineItemStatus, TimelineScope } from '$lib/chat-v2/types';

const scope: TimelineScope = { key: crypto.randomUUID(), kind: 'conversation', conversation_id: crypto.randomUUID() };
function ref(seq: number, event_type = 'assistant_message'): SourceRef {
  return { store: 'intaris', session_id: crypto.randomUUID(), seq, event_type, event_id: crypto.randomUUID() };
}
function message(refs: SourceRef[]): MessageTimelineItem {
  return { id: crypto.randomUUID(), kind: 'message', role: 'assistant', content: 'First **second** third',
    stable: true, status: 'complete', partial: false, source_refs: refs, sort_key: crypto.randomUUID(),
    message_id: crypto.randomUUID(), created_at: null, attachments: [] };
}

describe('new chat as-of transcript sources', () => {
  it.each([null, undefined])('accepts finalized persisted messages with status %s and no event ID', status => {
    const event = { ...ref(9), event_id: null };
    const item = { ...message([event]), status, stable: true, partial: false, text_streaming: false };
    const source = chatTranscriptSource(item, scope)!;
    expect(source).toEqual({ kind: 'transcript', representation: 'chat-item-as-of-event-v1',
      conversation_id: scope.conversation_id, source_item_id: item.id, field_path: ['content'],
      source_event: { ...event, event_id: undefined } });
    expect(item.source_refs[0].event_id).toBeNull();
  });
  it.each(['pending', 'running', 'waiting', 'failed', 'cancelled', 'denied', 'compacted', 'skipped'] satisfies TimelineItemStatus[])(
    'rejects explicit %s status even on stable, nonstreaming content',
    status => {
      expect(chatTranscriptSource({ ...message([ref(9)]), status, text_streaming: false }, scope)).toBeNull();
    },
  );
  it.each([null, 'complete'] as const)('rejects active text streaming with status %s', status => {
    expect(chatTranscriptSource({ ...message([ref(9)]), status, text_streaming: true }, scope)).toBeNull();
  });
  it.each(['partial', 'unstable', 'user', 'empty', 'unproven'] as const)('rejects null-status %s content', change => {
    const item = { ...message([ref(9)]), status: null, text_streaming: false };
    if (change === 'partial') item.partial = true;
    if (change === 'unstable') item.stable = false;
    if (change === 'user') item.role = 'user';
    if (change === 'empty') item.content = ' \n';
    if (change === 'unproven') item.source_refs = [ref(9, 'tool_result')];
    expect(chatTranscriptSource(item, scope)).toBeNull();
  });
  it.each(['multi-append', 'replacement then delta'])('pins the last contributor of %s without assembling text', () => {
    const refs = [ref(2, 'message'), ref(3), ref(4)];
    const item = message(refs), source = chatTranscriptSource(item, scope)!;
    expect(source).toEqual({ kind: 'transcript', representation: 'chat-item-as-of-event-v1',
      conversation_id: scope.conversation_id, source_item_id: item.id, field_path: ['content'], source_event: refs[2] });
    expect(item.source_refs).toEqual(refs);
    expect(source).not.toHaveProperty('content');
  });
  it('uses canonical array order, never the maximum sequence or an unrelated later event', () => {
    const refs = [ref(200), ref(4, 'message'), ref(500, 'tool_result'), ref(501, 'lifecycle')];
    expect(chatTranscriptSource(message(refs), scope)?.source_event).toEqual(refs[1]);
  });
  it('pins exactly the current item refs rather than another newer item', () => {
    const own = ref(3), later = message([ref(900)]);
    expect(chatTranscriptSource(message([own]), scope)?.source_event).toEqual(own);
    expect(chatTranscriptSource(message([own]), scope)?.source_item_id).not.toBe(later.id);
  });
  it.each(['user', 'streaming', 'unstable', 'partial', 'unproven', 'unscoped'] as const)('does not activate %s sources', change => {
    const item = message([ref(3)]);
    if (change === 'user') item.role = 'user';
    if (change === 'streaming') item.status = 'running';
    if (change === 'unstable') item.stable = false;
    if (change === 'partial') item.partial = true;
    if (change === 'unproven') item.source_refs = [ref(3, 'tool_result'), { ...ref(4), seq: 0 }];
    expect(chatTranscriptSource(item, change === 'unscoped' ? undefined : scope)).toBeNull();
  });
});
