import { renderMarkdown } from '$lib/markdown';
import type { AttachmentRef, CognisWebSocketEvent, MessageEvent } from '$lib/types/api';

export type TimelineItem =
  | MessageTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | ReasoningTimelineItem
  | NoticeTimelineItem
  | SystemMessageTimelineItem
  | CompactionTimelineItem;

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
  attachments?: AttachmentRef[];
}

export interface ToolCallEvaluation {
  decision: string;
  reasoning?: string;
  risk?: string;
  path?: string;
  latency_ms?: number;
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
  evaluation?: ToolCallEvaluation;
  reconstructed?: boolean;
}

export interface DelegationTimelineItem {
  id: string;
  kind: 'delegation';
  taskId: string;
  taskLabel: string;
  status: 'started' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
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

export interface SystemMessageTimelineItem {
  id: string;
  kind: 'system_message';
  text: string;
  timestamp: string | null;
}

export interface CompactionTimelineItem {
  id: string;
  kind: 'compaction';
  previousSessionId: string;
  summaryPreview: string;
  method: string;
  turnsCompacted: number;
  timestamp: string | null;
}

function createMessageItem(
  id: string,
  role: 'user' | 'assistant' | 'system',
  content: string,
  timestamp: string | null,
  seq: number | null,
  messageId?: string,
  streaming = false,
  attachments: AttachmentRef[] = []
): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    role,
    content,
    html: role !== 'system' ? renderMarkdown(content) : '',
    seq,
    timestamp,
    messageId,
    streaming,
    attachments
  };
}

let _noticeCounter = 0;

function createNotice(title: string, description: string, tone: NoticeTimelineItem['tone'] = 'info'): NoticeTimelineItem {
  return {
    id: `notice:${++_noticeCounter}:${title}`,
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
    const attachments = Array.isArray(event.data.attachments)
      ? event.data.attachments.filter((item): item is AttachmentRef => typeof item === 'object' && item !== null && typeof (item as Record<string, unknown>).artifact_id === 'string')
      : [];
    // Use session_id from event data to build lineage-safe IDs (seq is session-local).
    const sid = typeof event.data.session_id === 'string' ? event.data.session_id : '';
    const eid = sid ? `${sid}:${event.seq}` : `${event.seq}`;
    if (event.type === 'user_message') {
      items.push(createMessageItem(`event:${eid}:user`, 'user', content, event.timestamp, event.seq, undefined, false, attachments));
      continue;
    }

    if (event.type === 'assistant_message') {
      if (content.trim()) {
        items.push(
          createMessageItem(`event:${eid}:assistant`, 'assistant', content, event.timestamp, event.seq, undefined, false, attachments)
        );
      }
      continue;
    }

    if (event.type === 'tool_call') {
      const toolName = String(event.data.name ?? event.data.tool_name ?? 'unknown');
      // Orchestration tools are displayed as delegation cards, not tool blocks
      if (['delegate', 'fork'].includes(toolName)) continue;
      const callId = String(event.data.call_id ?? `tc-${eid}`);
      let args: Record<string, unknown> | undefined;
      if (typeof event.data.arguments === 'object' && event.data.arguments !== null) {
        args = event.data.arguments as Record<string, unknown>;
      } else if (typeof event.data.arguments === 'string') {
        try { args = JSON.parse(event.data.arguments as string); } catch { args = undefined; }
      }
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

    if (event.type === 'delegation') {
      const childSessionId = String(event.data.child_session_id ?? event.data.call_id ?? `del-${eid}`);
      const itemId = `delegation:${childSessionId}`;
      const delegationStatus = typeof event.data.status === 'string' ? event.data.status : 'started';
      const rawTask = event.data.task;
      const taskDesc = typeof rawTask === 'string' && rawTask.trim() ? rawTask.trim() : 'Background task';

      if (delegationStatus === 'completed' || delegationStatus === 'failed') {
        // Completion/failure event — update existing card or create new one
        const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
        const result = delegationStatus === 'completed'
          ? (typeof event.data.result_summary === 'string' ? event.data.result_summary : null)
          : (typeof event.data.error === 'string' ? event.data.error : 'Failed');
        if (existingIdx >= 0) {
          const existing = items[existingIdx] as DelegationTimelineItem;
          items[existingIdx] = { ...existing, status: delegationStatus, result };
        } else {
          items.push({
            id: itemId,
            kind: 'delegation',
            taskId: childSessionId,
            taskLabel: taskDesc,
            status: delegationStatus,
            result,
            timestamp: event.timestamp
          });
        }
      } else {
        // Initial delegation event (started/running) — update existing or create new
        const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
        if (existingIdx >= 0) {
          const existing = items[existingIdx] as DelegationTimelineItem;
          items[existingIdx] = { ...existing, status: delegationStatus as DelegationTimelineItem['status'] };
        } else {
          items.push({
            id: itemId,
            kind: 'delegation',
            taskId: childSessionId,
            taskLabel: taskDesc,
            status: 'started',
            result: null,
            timestamp: event.timestamp
          });
        }
      }
      continue;
    }

    if (event.type === 'tool_result') {
      const callId = String(event.data.call_id ?? '');
      const evaluation =
        typeof event.data.evaluation === 'object' && event.data.evaluation !== null
          ? (event.data.evaluation as ToolCallEvaluation)
          : undefined;
      const index = toolCallIndexByCallId.get(callId);
      if (index !== undefined && items[index]?.kind === 'tool_call') {
        const existing = items[index] as ToolCallTimelineItem;
        items[index] = {
          ...existing,
          status: event.data.is_error ? 'failed' : 'completed',
          result: typeof event.data.result === 'string' ? event.data.result : undefined,
          isError: typeof event.data.is_error === 'boolean' ? event.data.is_error : undefined,
          durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : undefined,
          evaluation
        };
      } else {
        items.push({
          id: `tool:${callId || `tc-${eid}`}`,
          kind: 'tool_call',
          callId: callId || `tc-${eid}`,
          toolName: String(event.data.name ?? event.data.tool_name ?? 'unknown'),
          status: event.data.is_error ? 'failed' : 'completed',
          timestamp: event.timestamp,
          result: typeof event.data.result === 'string' ? event.data.result : undefined,
          isError: typeof event.data.is_error === 'boolean' ? event.data.is_error : undefined,
          durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : undefined,
          evaluation,
          reconstructed: true
        });
      }
      continue;
    }

    if (event.type === 'history_gap') {
      const reason = String(event.data?.reason ?? 'unknown');
      const descriptionMap: Record<string, string> = {
        stream_missing: 'A session event stream was missing in Intaris, so part of this history could not be loaded.',
        read_failed: 'A session event stream could not be read from Intaris, so part of this history may be incomplete.',
        lineage_truncated: 'Older conversation lineage was truncated during history bootstrap. Load the session directly for more detail.',
        bootstrap_cap_reached: 'History bootstrap reached the configured safety cap. Refresh or inspect the session directly to load more.'
      };
      items.push({
        id: `history-gap:${eid}:${reason}`,
        kind: 'notice',
        title: 'History incomplete',
        description: descriptionMap[reason] ?? 'Some persisted history could not be loaded completely.',
        tone: 'warning',
        timestamp: event.timestamp
      });
      continue;
    }

    if (event.type === 'reasoning') {
      const messageId = String(event.data.message_id ?? `reasoning-${eid}`);
      items.push({
        id: `reasoning:${messageId}:${eid}`,
        kind: 'reasoning',
        messageId,
        content,
        streaming: false,
        timestamp: event.timestamp
      });
      continue;
    }

    // Delegation events from Intaris use type="delegation" with data.status
    // (started, completed, failed). These are the actual recorded events.
    if (event.type === 'delegation') {
      const childSessionId = String(event.data?.child_session_id ?? eid);
      const itemId = `delegation:${childSessionId}`;
      const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
      const dataStatus = String(event.data?.status ?? 'started');
      const statusMap: Record<string, DelegationTimelineItem['status']> = {
        started: 'started',
        completed: 'completed',
        failed: 'failed',
      };
      const delegation: DelegationTimelineItem = {
        id: itemId,
        kind: 'delegation',
        taskId: childSessionId,
        taskLabel: String(event.data?.task ?? event.data?.description ?? 'Sub-session'),
        status: statusMap[dataStatus] ?? 'started',
        result: typeof event.data?.result_summary === 'string' ? event.data.result_summary : (typeof event.data?.result_content === 'string' ? event.data.result_content : null),
        timestamp: event.timestamp
      };
      if (existingIdx >= 0) {
        const existing = items[existingIdx] as DelegationTimelineItem;
        items[existingIdx] = { ...existing, ...delegation, taskLabel: existing.taskLabel || delegation.taskLabel };
      } else {
        items.push(delegation);
      }
      continue;
    }

    if (event.type === 'task_result') {
      const taskId = String(event.data.task_id ?? eid);
      const itemId = `delegation:${taskId}`;
      const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
      const delegation: DelegationTimelineItem = {
        id: itemId,
        kind: 'delegation',
        taskId: String(event.data.task_id ?? 'unknown-task'),
        taskLabel: String(event.data.task_title ?? event.data.task_id ?? 'Background task'),
        status: 'completed',
        result: typeof event.data.result_summary === 'string' ? event.data.result_summary : null,
        timestamp: event.timestamp
      };
      if (existingIdx >= 0) {
        const existing = items[existingIdx] as DelegationTimelineItem;
        items[existingIdx] = { ...existing, ...delegation, taskLabel: existing.taskLabel || delegation.taskLabel };
      } else {
        items.push(delegation);
      }
      continue;
    }

    if (event.type === 'session_recovered') {
      items.push(createNotice('Session recovered', 'The controller recovered this conversation after a restart.'));
      continue;
    }

    if (event.type === 'compaction_summary') {
      const summary = typeof event.data.summary === 'string' ? event.data.summary : '';
      const method = typeof event.data.method === 'string' ? event.data.method : 'unknown';
      const turnsCompacted = typeof event.data.turns_compacted === 'number' ? event.data.turns_compacted : 0;
      items.push({
        id: `compaction:${eid}`,
        kind: 'compaction',
        previousSessionId: '',  // Not available from Intaris event data
        summaryPreview: summary.slice(0, 500),
        method,
        turnsCompacted,
        timestamp: event.timestamp
      });
      continue;
    }

    if (event.type === 'task_failed' || event.type === 'task_cancelled') {
      const taskId = String(event.data.task_id ?? eid);
      const itemId = `delegation:${taskId}`;
      const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
      const delegation: DelegationTimelineItem = {
        id: itemId,
        kind: 'delegation',
        taskId: String(event.data.task_id ?? 'unknown-task'),
        taskLabel: String(event.data.task_title ?? event.data.task_id ?? 'Background task'),
        status: event.type === 'task_failed' ? 'failed' : 'cancelled',
        result: typeof event.data.result_summary === 'string' ? event.data.result_summary : null,
        timestamp: event.timestamp
      };
      if (existingIdx >= 0) {
        const existing = items[existingIdx] as DelegationTimelineItem;
        items[existingIdx] = { ...existing, ...delegation, taskLabel: existing.taskLabel || delegation.taskLabel };
      } else {
        items.push(delegation);
      }
      continue;
    }

    // Intaris lifecycle events wrap task result/failed/cancelled events
    // with type="lifecycle" and data.event="task_result" etc.
    if (event.type === 'lifecycle') {
      const lifecycleEvent = String(event.data?.event ?? '');
      if (['task_result', 'task_failed', 'task_cancelled'].includes(lifecycleEvent)) {
        const taskId = String(event.data.task_id ?? eid);
        const itemId = `delegation:${taskId}`;
        const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
        const statusMap: Record<string, DelegationTimelineItem['status']> = {
          task_result: 'completed',
          task_failed: 'failed',
          task_cancelled: 'cancelled'
        };
        const delegation: DelegationTimelineItem = {
          id: itemId,
          kind: 'delegation',
          taskId,
          taskLabel: String(event.data.title ?? event.data.task_id ?? 'Background task'),
          status: statusMap[lifecycleEvent] ?? 'completed',
          result: typeof event.data.result_summary === 'string' ? event.data.result_summary : null,
          timestamp: event.timestamp
        };
        if (existingIdx >= 0) {
          const existing = items[existingIdx] as DelegationTimelineItem;
          items[existingIdx] = { ...existing, ...delegation, taskLabel: existing.taskLabel || delegation.taskLabel };
        } else {
          items.push(delegation);
        }
      } else if (lifecycleEvent === 'system_notice') {
        const message = String(event.data?.message ?? '');
        if (message) {
          items.push({
            id: `system:${eid}`,
            kind: 'system_message',
            text: message,
            timestamp: event.timestamp
          });
        }
      }
      continue;
    }

    // Evaluation feedback events from the step evaluator
    if (event.type === 'evaluation') {
      const evalEvent = String(event.data?.event ?? '');
      if (evalEvent === 'evaluation_feedback') {
        const decision = String(event.data?.decision ?? 'unknown');
        const feedback = String(event.data?.feedback ?? '');
        const attempt = event.data?.attempt ?? '?';
        const tone: 'info' | 'warning' | 'error' =
          decision === 'approved' || decision === 'approve'
            ? 'info'
            : decision === 'failed' || decision === 'reject'
              ? 'error'
              : 'warning';
        items.push({
          id: `eval:${eid}`,
          kind: 'notice',
          title: `Step Evaluation (attempt ${attempt})`,
          description: `${decision} — ${feedback}`,
          tone,
          timestamp: event.timestamp
        });
      }
      continue;
    }
  }

  return items;
}

export function appendOptimisticUserMessage(items: TimelineItem[], content: string, attachments: AttachmentRef[] = []): TimelineItem[] {
  return [
    ...items,
    createMessageItem(`local-user:${Date.now()}`, 'user', content, new Date().toISOString(), null, undefined, false, attachments)
  ];
}

export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  const next = [...items];

  if (event.type === 'user_message') {
    const itemId = `user-msg:${Date.now()}:${next.length}`;
    const attachments = Array.isArray(event.attachments) ? event.attachments : [];
    next.push(
      createMessageItem(itemId, 'user', event.content, new Date().toISOString(), null, undefined, false, attachments)
    );
    return next;
  }

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
      const attachments = Array.isArray(event.attachments) ? event.attachments : message.attachments;
      next[index] = {
        ...message,
        seq: event.seq,
        streaming: false,
        attachments
      };
      return next;
    }

    // No chunks were received for this message — skip creating empty bubble
    return next;
  }

  if (event.type === 'tool_call') {
    // Orchestration tools are displayed as delegation cards, not tool blocks
    if (['delegate', 'fork'].includes(event.tool_name)) return next;
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
        // Preserve fields from the original event when not provided by the update
        arguments: toolItem.arguments ?? existing.arguments,
        result: existing.result,
        isError: existing.isError,
        durationMs: existing.durationMs,
        evaluation: existing.evaluation
      };
      return next;
    }
    next.push(toolItem);
    return next;
  }

  if (event.type === 'tool_result') {
    const itemId = `tool:${event.call_id}`;
    const evaluation = event.evaluation ?? undefined;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        status: event.is_error ? 'failed' : 'completed',
        result: event.result,
        isError: event.is_error,
        durationMs: event.duration_ms ?? undefined,
        evaluation
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
      durationMs: event.duration_ms ?? undefined,
      evaluation
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
        content: typeof event.seq === 'number' ? event.content : `${existing.content}${event.content}`,
        streaming: typeof event.seq !== 'number'
      };
      return next;
    }
    next.push({
      id: itemId,
      kind: 'reasoning',
      messageId: event.message_id,
      content: event.content,
      streaming: typeof event.seq !== 'number',
      timestamp: new Date().toISOString()
    });
    return next;
  }

  if (event.type === 'conversation_updated') {
    // Title updates are handled by the page handler directly, not the timeline
    return next;
  }

  if (event.type === 'history_notice') {
    const itemId =
      typeof event.seq === 'number'
        ? `history-notice:${event.seq}`
        : `history-notice:${Date.now()}:${next.length}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'notice');
    const notice = {
      id: itemId,
      kind: 'notice' as const,
      title: event.title,
      description: event.description,
      tone: event.tone ?? 'info',
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = notice;
      return next;
    }
    next.push(notice);
    return next;
  }

  if (event.type === 'delegation_started' || event.type === 'delegation_progress') {
    const taskId = event.child_session_id;
    const itemId = `delegation:${taskId}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    const progressText =
      event.type === 'delegation_progress' && 'progress' in event && typeof event.progress === 'string'
        ? event.progress
        : null;
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId,
      taskLabel: 'task' in event && typeof event.task === 'string' ? event.task : 'Background task',
      status: event.type === 'delegation_started' ? 'started' : 'running',
      result: progressText,
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = { ...(next[index] as DelegationTimelineItem), ...delegation };
      return next;
    }
    next.push(delegation);
    return next;
  }

  if (event.type === 'delegation_completed' || event.type === 'delegation_failed') {
    const taskId = event.child_session_id;
    const itemId = `delegation:${taskId}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    const status = event.type === 'delegation_completed' ? 'completed' : 'failed';
    const result =
      event.type === 'delegation_completed' ? event.result : event.reason;
    const existing = index >= 0 ? (next[index] as DelegationTimelineItem) : null;
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId,
      taskLabel: existing?.taskLabel ?? 'Background task',
      status,
      result: typeof result === 'string' ? result : null,
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = { ...existing!, ...delegation, taskLabel: existing!.taskLabel };
      return next;
    }
    next.push(delegation);
    return next;
  }

  if (event.type === 'task_paused') {
    const taskId = event.task_id;
    const itemId = `delegation:${taskId}`;
    let index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    // Fallback: search by taskId field (card may be keyed by child_session_id)
    if (index < 0) {
      index = next.findIndex(
        (item) => item.kind === 'delegation' && (item as DelegationTimelineItem).taskId === taskId
      );
    }
    if (index >= 0) {
      const existing = next[index] as DelegationTimelineItem;
      next[index] = { ...existing, status: 'paused', result: 'Waiting for input' };
    }
    return next;
  }

  if (
    event.type === 'workflow_completed' ||
    event.type === 'workflow_failed' ||
    event.type === 'workflow_cancelled'
  ) {
    const taskId = event.task_id;
    const itemId = `delegation:${taskId}`;
    let index = next.findIndex((item) => item.id === itemId && item.kind === 'delegation');
    // Fallback: search by taskId field (card may be keyed by child_session_id)
    if (index < 0) {
      index = next.findIndex(
        (item) => item.kind === 'delegation' && (item as DelegationTimelineItem).taskId === taskId
      );
    }
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
    const existing = index >= 0 ? (next[index] as DelegationTimelineItem) : null;
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId,
      taskLabel: existing?.taskLabel ?? 'Background task',
      status,
      result: typeof result === 'string' ? result : null,
      timestamp: new Date().toISOString()
    };
    if (index >= 0) {
      next[index] = { ...existing!, ...delegation, taskLabel: existing!.taskLabel };
      return next;
    }
    next.push(delegation);
    return next;
  }

  if (event.type === 'workflow_step_started' || event.type === 'workflow_step_completed') {
    const taskId = event.task_id;
    const stepName = 'step_name' in event && typeof event.step_name === 'string' ? event.step_name : '';
    // Delegation cards may be keyed by child_session_id or task_id — search both
    let index = next.findIndex((item) => item.id === `delegation:${taskId}` && item.kind === 'delegation');
    if (index < 0) {
      index = next.findIndex((item) => item.kind === 'delegation' && (item as DelegationTimelineItem).taskId === taskId);
    }
    if (index >= 0) {
      const existing = next[index] as DelegationTimelineItem;
      next[index] = {
        ...existing,
        status: 'running',
        result:
          event.type === 'workflow_step_started'
            ? `Running step: ${stepName}`
            : `Completed step: ${stepName}`,
      };
      return next;
    }
    return next;
  }

  if (event.type === 'session_compacted') {
    next.push({
      id: `compaction:${Date.now()}`,
      kind: 'compaction',
      previousSessionId: event.previous_session_id,
      summaryPreview: event.summary_preview?.slice(0, 500) ?? '',
      method: event.method ?? 'unknown',
      turnsCompacted: event.turns_compacted ?? 0,
      timestamp: new Date().toISOString()
    });
    return next;
  }

  if (event.type === 'system_message') {
    const itemId =
      typeof event.seq === 'number'
        ? `sysmsg:${event.seq}`
        : `sysmsg:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'system_message');
    const systemMessage = {
      id: itemId,
      kind: 'system_message' as const,
      text: event.text,
      timestamp: new Date().toISOString(),
    };
    if (index >= 0) {
      next[index] = systemMessage;
      return next;
    }
    next.push(systemMessage);
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
    const isDirectQuestion = event.type === 'workflow_step_question' && !event.task_id;
    const title = event.type === 'workflow_gate'
      ? 'Task waiting for approval'
      : isDirectQuestion
        ? 'Assistant requested more input'
        : 'Task requested more input';
    const description = event.type === 'workflow_gate'
      ? `Task ${event.task_id} paused at ${event.step_name ?? 'a workflow step'}.`
      : isDirectQuestion
        ? event.question?.trim() || 'Conversation paused until you answer the clarification request.'
        : `Task ${event.task_id} paused at ${event.step_name ?? 'a workflow step'}.`;
    next.push(
      createNotice(
        title,
        description,
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
