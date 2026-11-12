import type { TimelineItem, ToolCallTimelineItem } from '$lib/chat-v2/types';

export interface AgentCommentReply {
  callId: string;
  commentId: string;
  body: string;
  resolved: boolean;
}

/** Successful controller calls only; failed or merely proposed replies are not answers. */
export function completedCommentCall(item: TimelineItem): ToolCallTimelineItem | null {
  if (item.kind !== 'tool_call' || item.status !== 'complete' || item.is_error) return null;
  const name = item.tool_name.replace(/^(builtin:|functions\.)/, '');
  if (name === 'comment_reply') return item;
  if (name === 'call_tool' && typeof item.arguments?.tool === 'string'
    && item.arguments.tool.replace(/^builtin:/, '') === 'comment_reply') return item;
  return null;
}

/** Pure render enrichment: keep canonical messages, source text and event identities unchanged. */
export function commentReplyPresentation(items: TimelineItem[]) {
  const replies = new Map<string, AgentCommentReply[]>();
  const messages = new Map<string, string>();
  const completed: string[] = [];
  const execution = (item: TimelineItem & { turn_id?: string | null }) => {
    const session = item.source_refs[0]?.session_id;
    return item.turn_id && session ? JSON.stringify([session, item.turn_id]) : null;
  };
  for (const item of items) {
    if (item.kind === 'message' && item.role === 'assistant') {
      const key = execution(item);
      if (key) messages.set(key, item.id);
    }
    const call = completedCommentCall(item);
    if (!call) continue;
    completed.push(call.call_id);
    const key = execution(call);
    const args = call.tool_name.replace(/^(builtin:|functions\.)/, '') === 'call_tool'
      ? call.arguments?.arguments as Record<string, unknown> | undefined : call.arguments;
    if (!key || !args || typeof args.comment_id !== 'string'
      || typeof args.reply !== 'string' || !args.reply.trim()) continue;
    const answer = { callId: call.call_id, commentId: args.comment_id, body: args.reply, resolved: args.resolve === true };
    replies.set(key, [...(replies.get(key) ?? []), answer]);
  }
  return {
    byMessage: new Map([...replies].flatMap(([key, answers]) => {
      const message = messages.get(key);
      return message ? [[message, answers] as const] : [];
    })),
    completionKey: JSON.stringify(completed),
  };
}
