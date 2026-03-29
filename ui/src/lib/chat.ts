import { renderMarkdown } from '$lib/markdown';
import type { CognisWebSocketEvent, MessageEvent } from '$lib/types/api';

export type TimelineItem =
  | MessageTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | ReasoningTimelineItem
  | NoticeTimelineItem;

export interface MessageTimelineItem {
  id: string;
  kind: 'message';
  role: 'user' | 'assistant' | 'system';
  content: string;
  html: string;
  seq: number | null;
  timestamp: string | null;
  messageId?: string;
  streaming?: boolean;
}

export interface ToolCallTimelineItem {
  id: string;
  kind: 'tool_call';
  callId: string;
  toolName: string;
  status: string;
  timestamp: string | null;
  arguments?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  durationMs?: number;
}

export interface DelegationTimelineItem {
  id: string;
  kind: 'delegation';
  taskId: string;
  taskLabel: string;
  status: 'started' | 'running' | 'completed' | 'failed' | 'cancelled';
  result: string | null;
  timestamp: string | null;
}

export interface ReasoningTimelineItem {
  id: string;
  kind: 'reasoning';
  messageId: string;
  content: string;
  streaming: boolean;
  timestamp: string | null;
}

export interface NoticeTimelineItem {
  id: string;
  kind: 'notice';
  title: string;
  description: string;
  tone: 'info' | 'warning' | 'error';
  timestamp: string | null;
}

function createMessageItem(
  id: string,
  role: 'user' | 'assistant' | 'system',
  content: string,
  timestamp: string | null,
  seq: number | null,
  messageId?: string,
  streaming = false
): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    role,
    content,
    html: role === 'assistant' ? renderMarkdown(content) : '',
    seq,
    timestamp,
    messageId,
    streaming
  };
}

function createNotice(title: string, description: string, tone: NoticeTimelineItem['tone'] = 'info'): NoticeTimelineItem {
  return {
    id: `notice:${title}:${description}`,
    kind: 'notice',
    title,
    description,
    tone,
    timestamp: new Date().toISOString()
  };
}

export function normalizeHistory(events: MessageEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolCallIndexByCallId = new Map<string, number>();

  for (const event of events) {
    const content = typeof event.data.content === 'string' ? event.data.content : '';
    if (event.type === 'user_message') {
      items.push(createMessageItem(`event:${event.seq}:user`, 'user', content, event.timestamp, event.seq));
      continue;
    }

    if (event.type === 'assistant_message') {
      items.push(
        createMessageItem(`event:${event.seq}:assistant`, 'assistant', content, event.timestamp, event.seq)
      );
      continue;
    }

    if (event.type === 'tool_call') {
      const callId = String(event.data.call_id ?? `tc-${event.seq}`);
      const toolName = String(event.data.name ?? event.data.tool_name ?? 'unknown');
      const args = typeof event.data.arguments === 'object' && event.data.arguments !== null
        ? (event.data.arguments as Record<string, unknown>)
        : undefined;
      const item: ToolCallTimelineItem = {
        id: `tool:${callId}`,
        kind: 'tool_call',
        callId,
        toolName,
        status: typeof event.data.status === 'string' ? event.data.status : 'started',
        timestamp: event.timestamp,
        arguments: args
      };
      toolCallIndexByCallId.set(callId, items.length);
      items.push(item);
      continue;
    }

    if (event.type === 'tool_result') {
      const callId = String(event.data.call_id ?? '');
      const index = toolCallIndexByCallId.get(callId);
      if (index !== undefined && items[index]?.kind === 'tool_call') {
        const existing = items[index] as ToolCallTimelineItem;
        items[index] = {
          ...existing,
          status: event.data.is_error ? 'failed' : 'completed',
          result: typeof event.data.result === 'string' ? event.data.result : undefined,
          isError: typeof event.data.is_error === 'boolean' ? event.data.is_error : undefined,
          durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : undefined
        };
      }
      continue;
    }

    if (event.type === 'reasoning') {
      const messageId = String(event.data.message_id ?? `reasoning-${event.seq}`);
      items.push({
        id: `reasoning:${messageId}:${event.seq}`,
        kind: 'reasoning',
        messageId,
        content,
        streaming: false,
        timestamp: event.timestamp
      });
      continue;
    }

    if (event.type === 'task_result') {
      items.push({
        id: `delegation:${String(event.data.task_id ?? event.seq)}`,
        kind: 'delegation',
        taskId: String(event.data.task_id ?? 'unknown-task'),
        taskLabel: String(event.data.task_title ?? event.data.task_id ?? 'Background task'),
        status: 'completed',
        result: typeof event.data.result_summary === 'string' ? event.data.result_summary : null,
        timestamp: event.timestamp
      });
      continue;
    }

    if (event.type === 'task_failed' || event.type === 'task_cancelled') {
      items.push({
        id: `delegation:${String(event.data.task_id ?? event.seq)}`,
        kind: 'delegation',
        taskId: String(event.data.task_id ?? 'unknown-task'),
        taskLabel: String(event.data.task_title ?? event.data.task_id ?? 'Background task'),
        status: event.type === 'task_failed' ? 'failed' : 'cancelled',
        result: typeof event.data.result_summary === 'string' ? event.data.result_summary : null,
        timestamp: event.timestamp
      });
    }
  }

  return items;
}

export function appendOptimisticUserMessage(items: TimelineItem[], content: string): TimelineItem[] {
  return [
    ...items,
    createMessageItem(`local-user:${Date.now()}`, 'user', content, new Date().toISOString(), null)
  ];
}

export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  const next = [...items];

  if (event.type === 'chunk') {
    const itemId = `message:${event.message_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'message');
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      const content = `${message.content}${event.content}`;
      next[index] = {
        ...message,
        content,
        html: renderMarkdown(content),
        streaming: true,
        timestamp: message.timestamp ?? new Date().toISOString()
      };
      return next;
    }

    next.push(
      createMessageItem(itemId, 'assistant', event.content, new Date().toISOString(), null, event.message_id, true)
    );
    return next;
  }

  if (event.type === 'message_complete') {
    const itemId = `message:${event.message_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'message');
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      next[index] = {
        ...message,
        seq: event.seq,
        streaming: false
      };
      return next;
    }

    next.push(createMessageItem(itemId, 'assistant', '', new Date().toISOString(), event.seq, event.message_id));
    return next;
  }

  if (event.type === 'tool_call') {
    const itemId = `tool:${event.call_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    const toolItem: ToolCallTimelineItem = {
      id: itemId,
      kind: 'tool_call',
      callId: event.call_id,
      toolName: event.tool_name,
      status: event.status,
      timestamp: new Date().toISOString(),
      arguments: event.arguments
    };
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        ...toolItem,
        // Preserve result fields if already set
        result: existing.result,
        isError: existing.isError,
        durationMs: existing.durationMs
      };
      return next;
    }
    next.push(toolItem);
    return next;
  }

  if (event.type === 'tool_result') {
    const itemId = `tool:${event.call_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        status: event.is_error ? 'failed' : 'completed',
        result: event.result,
        isError: event.is_error,
        durationMs: event.duration_ms ?? undefined
      };
      return next;
    }
    // tool_result arrived before tool_call — create a placeholder
    next.push({
      id: itemId,
      kind: 'tool_call',
      callId: event.call_id,
      toolName: event.tool_name,
      status: event.is_error ? 'failed' : 'completed',
      timestamp: new Date().toISOString(),
      result: event.result,
      isError: event.is_error,
      durationMs: event.duration_ms ?? undefined
    });
    return next;
  }

  if (event.type === 'reasoning') {
    const itemId = `reasoning:${event.message_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'reasoning');
    if (index >= 0) {
      const existing = next[index] as ReasoningTimelineItem;
      next[index] = {
        ...existing,
        content: `${existing.content}${event.content}`,
        streaming: true
      };
      return next;
    }
    next.push({
      id: itemId,
      kind: 'reasoning',
      messageId: event.message_id,
      content: event.content,
      streaming: true,
      timestamp: new Date().toISOString()
    });
    return next;
  }

  if (event.type === 'conversation_updated') {
    // Title updates are handled by the page handler directly, not the timeline
    return next;
  }

  if (event.type === 'delegation_started' || event.type === 'delegation_progress') {
    const taskId = event.child_session_id;
    const itemId = `delegation:${taskId}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId,
      taskLabel: 'task' in event && typeof event.task === 'string' ? event.task : taskId,
      status: event.type === 'delegation_started' ? 'started' : 'running',
      result: null,
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = { ...(next[index] as DelegationTimelineItem), ...delegation };
      return next;
    }
    next.push(delegation);
    return next;
  }

  if (
    event.type === 'workflow_completed' ||
    event.type === 'workflow_failed' ||
    event.type === 'workflow_cancelled'
  ) {
    const itemId = `delegation:${event.task_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    const status =
      event.type === 'workflow_completed'
        ? 'completed'
        : event.type === 'workflow_failed'
          ? 'failed'
          : 'cancelled';
    const result =
      event.type === 'workflow_completed'
        ? event.result
        : event.type === 'workflow_failed'
          ? event.reason
          : event.reason;
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId: event.task_id,
      taskLabel: event.task_id,
      status,
      result: typeof result === 'string' ? result : null,
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = { ...(next[index] as DelegationTimelineItem), ...delegation };
      return next;
    }
    next.push(delegation);
    return next;
  }

  if (event.type === 'chunk_gap') {
    next.push(
      createNotice(
        'Streaming gap detected',
        `${event.dropped_count} chunks were dropped because the outbound buffer overflowed.`,
        'warning'
      )
    );
    return next;
  }

  if (event.type === 'session_recovered') {
    next.push(createNotice('Session recovered', 'The controller recovered this conversation after a restart.'));
    return next;
  }

  if (event.type === 'workflow_gate' || event.type === 'workflow_step_question') {
    next.push(
      createNotice(
        event.type === 'workflow_gate' ? 'Task waiting for approval' : 'Task requested more input',
        `Task ${event.task_id} paused at ${event.step_name ?? 'a workflow step'}.`,
        'info'
      )
    );
  }

  return next;
}

/** Mark all in-flight reasoning items as done streaming (e.g. on message_complete). */
export function finalizeReasoningItems(items: TimelineItem[]): TimelineItem[] {
  let changed = false;
  const next = items.map((item) => {
    if (item.kind === 'reasoning' && item.streaming) {
      changed = true;
      return { ...item, streaming: false };
    }
    return item;
  });
  return changed ? next : items;
}
