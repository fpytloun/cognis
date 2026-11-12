import { expect, test } from 'vitest';
import { commentReplyPresentation } from './agent-replies';
import type { MessageTimelineItem, ToolCallTimelineItem, TimelineItem } from '$lib/chat-v2/types';

const message = (id: string, turn = 'turn-comment'): MessageTimelineItem => ({
  id, kind: 'message', source_refs: [{ store: 'intaris', session_id: 'session-comments', seq: 1, event_type: 'message' }], turn_id: turn,
  role: 'assistant', content: 'Answered the comments.', status: 'complete',
} as MessageTimelineItem);
const call = (id: string, extra: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem => ({
  id, call_id: id, kind: 'tool_call', source_refs: [{ store: 'intaris', session_id: 'session-comments', seq: 2, event_type: 'tool_call' }], turn_id: 'turn-comment',
  tool_name: 'comment_reply', status: 'complete',
  arguments: { comment_id: 'fbt_comment', reply: 'The full answer.', resolve: true },
  ...extra,
} as ToolCallTimelineItem);

test('answers enrich only the final assistant message of their own execution', () => {
  const items: TimelineItem[] = [
    message('progress'), call('reply-one'), message('final'), message('unrelated', 'turn-next'),
    { ...message('other-session'), source_refs: [{ store: 'intaris', session_id: 'other-session', seq: 1, event_type: 'message' }] },
  ];
  const before = JSON.stringify(items);
  const presented = commentReplyPresentation(items);
  expect([...presented.byMessage.keys()]).toEqual(['final']);
  expect(presented.byMessage.get('final')).toEqual([{
    callId: 'reply-one', commentId: 'fbt_comment', body: 'The full answer.', resolved: true,
  }]);
  expect(JSON.stringify(items)).toBe(before);
});

test('does not expose failed, proposed or resolve-only replies as answers', () => {
  const items = [
    call('failed', { is_error: true }), call('running', { status: 'running' }),
    call('resolve-only', { arguments: { comment_id: 'fbt_comment', resolve: true } }),
    call('missing-execution', { turn_id: null }), message('final'),
  ];
  const presented = commentReplyPresentation(items);
  expect(presented.byMessage.size).toBe(0);
  expect(JSON.parse(presented.completionKey)).toEqual(['resolve-only', 'missing-execution']);
});

test('supports wrapped controller calls and replays without duplicate answers', () => {
  const wrapped = call('wrapped', {
    tool_name: 'call_tool',
    arguments: { tool: 'builtin:comment_reply', arguments: { comment_id: 'fbt_comment', reply: 'Wrapped answer' } },
  });
  const presented = commentReplyPresentation([wrapped, message('final')]);
  expect(presented.byMessage.get('final')?.map(reply => reply.body)).toEqual(['Wrapped answer']);
  expect(commentReplyPresentation([wrapped, message('final')])).toEqual(presented);
});
