import { createMarkdownStreamer, renderMarkdown, type MarkdownStreamer } from '$lib/markdown';
import type { AttachmentRef, CognisWebSocketEvent, MessageEvent } from '$lib/types/api';

/**
 * Per-message markdown streamers. Streaming assistant replies accumulate
 * tokens chunk-by-chunk; rather than re-parse the whole content on every
 * chunk we give each message its own streamer so only the tail block is
 * re-parsed while earlier blocks stay memoized. See createMarkdownStreamer().
 */
const streamers = new Map<string, MarkdownStreamer>();

function getStreamer(messageId: string): MarkdownStreamer {
  let streamer = streamers.get(messageId);
  if (!streamer) {
    streamer = createMarkdownStreamer();
    streamers.set(messageId, streamer);
  }
  return streamer;
}

export function releaseStreamer(messageId: string): void {
  streamers.delete(messageId);
}

export type TimelineItem =
  | MessageTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | WorkflowComposedTimelineItem
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
  optimistic?: boolean;
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
  /**
   * Notification ID backing a pending `step_request_input` tool call.
   *
   * The backend creates a `step_question` notification whose
   * `notification_id` is the `pause_id` the agent loop is waiting on, but
   * the `tool_call` event itself does not carry that ID. The chat page
   * annotates the matching `step_request_input` tool item when the
   * corresponding `workflow_step_question` WebSocket event arrives so we
   * can resolve the pause by typing a reply — even if
   * `pendingDirectQuestion` state has been lost to a race, compaction, or
   * conversation reload.
   */
  notificationId?: string;
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

export interface WorkflowComposedTimelineItem {
  id: string;
  kind: 'workflow_composed';
  workflowId: string;
  workflowName: string;
  lifecycle: string;
  taskId: string | null;
  scheduleId: string | null;
  steps: string[];
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

function createSystemMessageItem(
  id: string,
  text: string,
  timestamp: string | null,
): SystemMessageTimelineItem {
  return {
    id,
    kind: 'system_message',
    text,
    timestamp
  };
}

function createMessageItem(
  id: string,
  role: 'user' | 'assistant' | 'system',
  content: string,
  timestamp: string | null,
  seq: number | null,
  messageId?: string,
  streaming = false,
  attachments: AttachmentRef[] = [],
  optimistic = false
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
    attachments,
    optimistic
  };
}

function attachmentIds(attachments: AttachmentRef[] = []): string[] {
  return attachments.map((attachment) => attachment.artifact_id);
}

function sameAttachmentIds(left: AttachmentRef[] = [], right: AttachmentRef[] = []): boolean {
  const leftIds = attachmentIds(left).sort();
  const rightIds = attachmentIds(right).sort();
  return (
    leftIds.length === rightIds.length && leftIds.every((artifactId, index) => artifactId === rightIds[index])
  );
}

function isRecentOptimisticUserMessage(item: MessageTimelineItem): boolean {
  if (!item.optimistic || item.role !== 'user' || item.seq !== null || !item.timestamp) return false;
  const ts = Date.parse(item.timestamp);
  if (Number.isNaN(ts)) return false;
  return Date.now() - ts <= 15_000;
}

function findOptimisticUserMessageIndex(
  items: TimelineItem[],
  content: string,
  attachments: AttachmentRef[]
): number {
  const index = items.length - 1;
  const item = items[index];
  if (item?.kind !== 'message') return -1;
  if (!isRecentOptimisticUserMessage(item)) return -1;
  if (item.content !== content) return -1;
  if (!sameAttachmentIds(item.attachments, attachments)) return -1;
  return index;
}

let _noticeCounter = 0;

function createNotice(
  title: string,
  description: string,
  tone: NoticeTimelineItem['tone'] = 'info',
  id?: string
): NoticeTimelineItem {
  return {
    id: id ?? `notice:${++_noticeCounter}:${title}`,
    kind: 'notice',
    title,
    description,
    tone,
    timestamp: new Date().toISOString()
  };
}

function removeTaskPauseNotices(items: TimelineItem[], taskId: string): TimelineItem[] {
  return items.filter((item) => {
    if (item.kind !== 'notice') return true;
    return !item.description.includes(`Task ${taskId} paused at `);
  });
}

function removeWorkflowPromptNotices(items: TimelineItem[]): TimelineItem[] {
  return items.filter(
    (item) => item.kind !== 'notice' || !item.id.startsWith('notice:workflow_')
  );
}

function finalizeInFlightAssistantItems(items: TimelineItem[]): TimelineItem[] {
  let changed = false;
  const next = items.map((item) => {
    if (item.kind === 'message' && item.role === 'assistant' && item.streaming) {
      changed = true;
      if (item.messageId) {
        const streamer = getStreamer(item.messageId);
        const finalHtml = streamer.finalize(item.content);
        releaseStreamer(item.messageId);
        return {
          ...item,
          html: finalHtml,
          streaming: false,
        } satisfies MessageTimelineItem;
      }
      return {
        ...item,
        streaming: false,
      } satisfies MessageTimelineItem;
    }
    if (item.kind === 'reasoning' && item.streaming) {
      changed = true;
      return { ...item, streaming: false } satisfies ReasoningTimelineItem;
    }
    return item;
  });
  return changed ? next : items;
}

function insertBeforeTrailingStreamingAssistant(items: TimelineItem[], item: TimelineItem): void {
  let insertionIndex = items.length;
  for (let index = items.length - 1; index >= 0; index--) {
    const candidate = items[index];
    if (candidate.kind === 'message' && candidate.role === 'assistant' && candidate.streaming && candidate.seq === null) {
      insertionIndex = index;
      continue;
    }
    if (candidate.kind === 'reasoning' && candidate.streaming) {
      insertionIndex = index;
      continue;
    }
    break;
  }
  items.splice(insertionIndex, 0, item);
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
      if (content.trim() || attachments.length > 0) {
        items.push(
          createMessageItem(`event:${eid}:assistant`, 'assistant', content, event.timestamp, event.seq, undefined, false, attachments)
        );
      }
      continue;
    }

    if (event.type === 'workflow_composed') {
      items.push({
        id: `workflow-composed:${eid}`,
        kind: 'workflow_composed',
        workflowId: String(event.data.workflow_id ?? ''),
        workflowName: String(event.data.workflow_name ?? event.data.workflow_id ?? 'Workflow'),
        lifecycle: String(event.data.lifecycle ?? 'ephemeral'),
        taskId: typeof event.data.task_id === 'string' ? event.data.task_id : null,
        scheduleId: typeof event.data.schedule_id === 'string' ? event.data.schedule_id : null,
        steps: Array.isArray(event.data.steps)
          ? event.data.steps.filter((item): item is string => typeof item === 'string')
          : [],
        timestamp: event.timestamp
      });
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
      const sessionId = typeof event.data.session_id === 'string' ? event.data.session_id : eid;
      items.push(
        createSystemMessageItem(
          `session-recovered:${sessionId}`,
          'The controller recovered this conversation after a restart.',
          event.timestamp
        )
      );
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
    createMessageItem(
      `local-user:${Date.now()}`,
      'user',
      content,
      new Date().toISOString(),
      null,
      undefined,
      false,
      attachments,
      true
    )
  ];
}

/** Tool status values we treat as "still running, waiting for resolution". */
const PENDING_TOOL_STATUSES = new Set(['started', 'running', 'paused']);

/**
 * Canonical match for the `step_request_input` controller tool. The backend
 * uses snake_case; this normalizer keeps the comparison resilient against
 * casing/underscore noise from older history rows.
 */
function isStepRequestInputToolName(name: string): boolean {
  return name.toLowerCase().replace(/_/g, '') === 'steprequestinput';
}

/**
 * Return the most recent `step_request_input` tool call that has not yet
 * resolved (status still pending, no tool_result recorded). Used by the chat
 * page to route the user's next text reply into `respondStepQuestion` so the
 * message becomes the resolution of the pause instead of a stray bubble.
 */
export function findPendingStepRequestInputCall(items: TimelineItem[]): ToolCallTimelineItem | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind !== 'tool_call') continue;
    const toolItem = item as ToolCallTimelineItem;
    if (!isStepRequestInputToolName(toolItem.toolName)) continue;
    if (!PENDING_TOOL_STATUSES.has(toolItem.status)) continue;
    if (toolItem.result !== undefined) continue;
    return toolItem;
  }
  return null;
}

/**
 * Attach a notification ID to the latest unresolved `step_request_input`
 * tool call in the timeline. Called when a `workflow_step_question`
 * WebSocket event arrives, so the tool call knows the `pause_id` it can be
 * resolved against later even if `pendingDirectQuestion` state is lost.
 */
export function annotateStepRequestInputWithNotification(
  items: TimelineItem[],
  notificationId: string,
): TimelineItem[] {
  const pending = findPendingStepRequestInputCall(items);
  if (!pending) return items;
  if (pending.notificationId === notificationId) return items;
  return items.map((item) =>
    item.id === pending.id
      ? ({ ...(item as ToolCallTimelineItem), notificationId } satisfies ToolCallTimelineItem)
      : item,
  );
}

/**
 * Optimistically mark a `step_request_input` tool call as resolved with the
 * user's response. The tool call block immediately shows the answer in its
 * Resolution area while the backend's real `tool_result` is in flight.
 */
export function optimisticallyResolveStepRequestInput(
  items: TimelineItem[],
  toolId: string,
  response: string,
): TimelineItem[] {
  return items.map((item) => {
    if (item.id !== toolId || item.kind !== 'tool_call') return item;
    const tool = item as ToolCallTimelineItem;
    if (!isStepRequestInputToolName(tool.toolName)) return item;
    return {
      ...tool,
      status: 'completed',
      isError: false,
      result: JSON.stringify({ response }),
    } satisfies ToolCallTimelineItem;
  });
}

export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  let next = [...items];

  if (event.type === 'user_message') {
    const attachments = Array.isArray(event.attachments) ? event.attachments : [];
    const optimisticIndex = findOptimisticUserMessageIndex(next, event.content, attachments);
    if (optimisticIndex >= 0 && next[optimisticIndex]?.kind === 'message') {
      const existing = next[optimisticIndex] as MessageTimelineItem;
      next[optimisticIndex] = {
        ...existing,
        attachments,
        optimistic: false
      };
      return next;
    }
    const itemId = `user-msg:${Date.now()}:${next.length}`;
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
      // Use the per-message streamer: finalized blocks are memoized, only the
      // in-progress tail is re-parsed. See docstring on createMarkdownStreamer.
      const streamer = getStreamer(event.message_id);
      next[index] = {
        ...message,
        content,
        html: streamer.render(content),
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
    const attachments = Array.isArray(event.attachments) ? event.attachments : [];
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      // Finalize and release the streamer for this message.
      const streamer = getStreamer(event.message_id);
      const finalHtml = streamer.finalize(message.content);
      releaseStreamer(event.message_id);
      next[index] = {
        ...message,
        html: finalHtml,
        seq: event.seq,
        streaming: false,
        attachments: attachments.length > 0 ? attachments : message.attachments
      };
      return next;
    }

    if (attachments.length > 0) {
      next.push(createMessageItem(itemId, 'assistant', '', new Date().toISOString(), event.seq, event.message_id, false, attachments));
      return next;
    }

    // No chunks were received for this message — skip creating empty bubble
    return next;
  }

  if (event.type === 'tool_call') {
    next = finalizeInFlightAssistantItems(next);
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
      timestamp: event.timestamp ?? new Date().toISOString(),
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
    insertBeforeTrailingStreamingAssistant(next, toolItem);
    return next;
  }

  if (event.type === 'tool_result') {
    next = finalizeInFlightAssistantItems(next);
    const itemId = `tool:${event.call_id}`;
    const evaluation = event.evaluation ?? undefined;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        status: event.is_error ? 'failed' : 'completed',
        timestamp: event.timestamp ?? existing.timestamp,
        result: event.result,
        isError: event.is_error,
        durationMs: event.duration_ms ?? undefined,
        evaluation
      };
      return next;
    }
    // tool_result arrived before tool_call — create a placeholder
    insertBeforeTrailingStreamingAssistant(next, {
      id: itemId,
      kind: 'tool_call',
      callId: event.call_id,
      toolName: event.tool_name,
      status: event.is_error ? 'failed' : 'completed',
      timestamp: event.timestamp ?? new Date().toISOString(),
      result: event.result,
      isError: event.is_error,
      durationMs: event.duration_ms ?? undefined,
      evaluation
    });
    return next;
  }

  if (event.type === 'workflow_composed') {
    next.push({
      id: `workflow-composed:${event.workflow_id}:${event.task_id ?? event.schedule_id ?? next.length}`,
      kind: 'workflow_composed',
      workflowId: event.workflow_id,
      workflowName: event.workflow_name,
      lifecycle: event.lifecycle,
      taskId: event.task_id ?? null,
      scheduleId: event.schedule_id ?? null,
      steps: Array.isArray(event.steps) ? event.steps : [],
      timestamp: new Date().toISOString()
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
    next = removeTaskPauseNotices(next, event.task_id);
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
    const itemId = `session-recovered:${event.session_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'system_message');
    const systemMessage = createSystemMessageItem(
      itemId,
      'The controller recovered this conversation after a restart.',
      new Date().toISOString()
    );
    if (index >= 0) {
      next[index] = systemMessage;
      return next;
    }
    next.push(systemMessage);
    return next;
  }

  if (event.type === 'reconnected') {
    return removeWorkflowPromptNotices(next);
  }

  if (event.type === 'workflow_gate' || event.type === 'workflow_step_question') {
    const isDirectQuestion = event.type === 'workflow_step_question' && !event.task_id;
    if (isDirectQuestion) {
      return next;
    }
    const noticeId = event.notification_id
      ? `notice:${event.type}:${event.notification_id}`
      : undefined;
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
        'info',
        noticeId
      )
    );
    return next;
  }

  if (event.type === 'workflow_gate_resolved' || event.type === 'workflow_step_question_resolved') {
    if (!event.notification_id) return next;
    const sourceType = event.type === 'workflow_gate_resolved' ? 'workflow_gate' : 'workflow_step_question';
    return next.filter((item) => item.id !== `notice:${sourceType}:${event.notification_id}`);
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
