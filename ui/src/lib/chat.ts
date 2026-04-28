import { createMarkdownStreamer, renderMarkdown, type MarkdownStreamer } from '$lib/markdown';
import type { ActiveStreamSnapshot, AttachmentRef, CognisWebSocketEvent, MessageEvent } from '$lib/types/api';

/**
 * Per-message markdown streamers. Streaming assistant replies accumulate
 * tokens chunk-by-chunk; rather than re-parse the whole content on every
 * chunk we give each message its own streamer so only the tail block is
 * re-parsed while earlier blocks stay memoized. See createMarkdownStreamer().
 */
const streamers = new Map<string, MarkdownStreamer>();
/** Per-thinking-block markdown streamers (keyed by block_id). */
const thinkingStreamers = new Map<string, MarkdownStreamer>();

interface PendingStreamChunk {
  delta: string;
  chunkIndex: number | null;
  contentOffset: number;
}

const pendingStreamChunks = new Map<string, PendingStreamChunk[]>();

function getStreamer(messageId: string): MarkdownStreamer {
  let streamer = streamers.get(messageId);
  if (!streamer) {
    streamer = createMarkdownStreamer();
    streamers.set(messageId, streamer);
  }
  return streamer;
}

function getThinkingStreamer(blockId: string): MarkdownStreamer {
  let streamer = thinkingStreamers.get(blockId);
  if (!streamer) {
    streamer = createMarkdownStreamer();
    thinkingStreamers.set(blockId, streamer);
  }
  return streamer;
}

function streamKey(messageId: string, turnId: string | null): string {
  return `${turnId ?? messageId}:${messageId}`;
}

function bufferPendingChunk(messageId: string, turnId: string | null, chunk: PendingStreamChunk): void {
  const key = streamKey(messageId, turnId);
  const chunks = pendingStreamChunks.get(key) ?? [];
  if (
    chunks.some(
      (existing) => existing.chunkIndex === chunk.chunkIndex && existing.contentOffset === chunk.contentOffset,
    )
  ) {
    return;
  }
  chunks.push(chunk);
  chunks.sort((left, right) => {
    if (left.contentOffset !== right.contentOffset) return left.contentOffset - right.contentOffset;
    return (left.chunkIndex ?? 0) - (right.chunkIndex ?? 0);
  });
  pendingStreamChunks.set(key, chunks);
}

function clearPendingChunks(messageId: string | undefined, turnId: string | null): void {
  if (!messageId) return;
  pendingStreamChunks.delete(streamKey(messageId, turnId));
}

export function releaseStreamer(messageId: string): void {
  streamers.delete(messageId);
}

export function releaseThinkingStreamers(blockIds: string[]): void {
  for (const blockId of blockIds) {
    thinkingStreamers.delete(blockId);
  }
}

export type TimelineItem =
  | MessageTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | WorkflowComposedTimelineItem
  | NoticeTimelineItem
  | SystemMessageTimelineItem
  | CompactionTimelineItem
  | ThinkingTimelineItem;

export interface ThinkingBlock {
  block_id: string;
  title: string;
  content: string;
  html: string;
  source: string;
  complete: boolean;
}

export interface ThinkingTimelineItem {
  id: string;
  kind: 'thinking';
  /** Transport message id for the thinking turn */
  messageId: string;
  turnId?: string | null;
  /** Ordered list of contiguous thinking blocks in this segment */
  blocks: ThinkingBlock[];
  /** True while at least one block is still streaming */
  streaming: boolean;
  /** Derived: title of the most recently active (incomplete) block */
  activeTitle: string | null;
  timestamp: string | null;
}

export interface MessageTimelineItem {
  id: string;
  kind: 'message';
  role: 'user' | 'assistant' | 'system';
  content: string;
  html: string;
  seq: number | null;
  timestamp: string | null;
  turnId?: string | null;
  messageId?: string;
  streaming?: boolean;
  attachments?: AttachmentRef[];
  optimistic?: boolean;
  streamChunkCount?: number;
  streamContentOffset?: number;
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
  turnId?: string | null;
  arguments?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  durationMs?: number;
  evaluation?: ToolCallEvaluation;
   attachments?: AttachmentRef[];
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

export interface TodoSnapshotItem {
  content: string;
  status: string;
  priority: string;
}

function normalizeToolName(name: string): string {
  return name.toLowerCase().replace(/_/g, '');
}

function parseTodoSnapshot(value: unknown): TodoSnapshotItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const content = typeof record.content === 'string' ? record.content.trim() : '';
      if (!content) return null;
      return {
        content,
        status: typeof record.status === 'string' ? record.status : 'pending',
        priority: typeof record.priority === 'string' ? record.priority : 'medium'
      } satisfies TodoSnapshotItem;
    })
    .filter((item): item is TodoSnapshotItem => item !== null);
}

function parsedToolResult(item: ToolCallTimelineItem): Record<string, unknown> | null {
  if (typeof item.result !== 'string') return null;
  try {
    const parsed = JSON.parse(item.result.replace(/^<tool_result[^>]*>\n?/, '').replace(/\n?<\/tool_result>\s*$/, ''));
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export function latestTodoSnapshot(items: TimelineItem[], resetOnUserMessage = false): TodoSnapshotItem[] {
  let lowerBound = 0;
  if (resetOnUserMessage) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === 'message' && item.role === 'user') {
        lowerBound = index;
        break;
      }
    }
  }

  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (index < lowerBound) break;
    const item = items[index];
    if (item?.kind !== 'tool_call') continue;
    const toolName = normalizeToolName(item.toolName);
    if (toolName === 'steptodowrite') {
      const parsed = parsedToolResult(item);
      if (Array.isArray(parsed?.todos)) {
        return parseTodoSnapshot(parsed.todos);
      }
      if (item.status === 'started' && Array.isArray(item.arguments?.todos)) {
        return parseTodoSnapshot(item.arguments.todos);
      }
      continue;
    }
    if (toolName === 'steptodolist') {
      const parsed = parsedToolResult(item);
      if (Array.isArray(parsed?.todos)) {
        return parseTodoSnapshot(parsed.todos);
      }
      continue;
    }
  }
  return [];
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
  optimistic = false,
  turnId: string | null = null,
  streamChunkCount = streaming && content ? 1 : 0,
): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    role,
    content,
    html: role !== 'system' ? renderMarkdown(content) : '',
    seq,
    timestamp,
    turnId,
    messageId,
    streaming,
    attachments,
    optimistic,
    streamChunkCount,
    streamContentOffset: content.length,
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

function normalizeEventAttachments(value: unknown): AttachmentRef[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is AttachmentRef =>
      typeof item === 'object'
      && item !== null
      && typeof (item as Record<string, unknown>).artifact_id === 'string'
  );
}

function normalizeEventTurnId(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
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
      if (item.id) {
        const streamer = getStreamer(item.id);
        const finalHtml = streamer.finalize(item.content);
        releaseStreamer(item.id);
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
    if (item.kind === 'thinking' && item.streaming) {
      changed = true;
      const blocks = item.blocks.map((block) => {
        if (block.complete) return block;
        const streamer = getThinkingStreamer(block.block_id);
        const finalHtml = streamer.finalize(block.content);
        thinkingStreamers.delete(block.block_id);
        return { ...block, html: finalHtml, complete: true } satisfies ThinkingBlock;
      });
      return {
        ...item,
        blocks,
        streaming: false,
        activeTitle: null,
      } satisfies ThinkingTimelineItem;
    }
    return item;
  });
  return changed ? next : items;
}

function isAssistantPhaseItem(item: TimelineItem, turnId: string | null): boolean {
  if (!turnId) return false;
  return (
    (item.kind === 'thinking' && item.turnId === turnId)
    || (item.kind === 'message' && item.role === 'assistant' && item.turnId === turnId)
  );
}

function openTurnPhaseStartIndex(items: TimelineItem[], turnId: string | null): number {
  if (!turnId) return items.length;
  let index = items.length;
  while (index > 0 && isAssistantPhaseItem(items[index - 1], turnId)) {
    index -= 1;
  }
  return index;
}

function findOpenPhaseAssistantIndex(items: TimelineItem[], turnId: string | null): number {
  if (!turnId) return -1;
  const start = openTurnPhaseStartIndex(items, turnId);
  for (let index = items.length - 1; index >= start; index -= 1) {
    const item = items[index];
    if (item.kind === 'message' && item.role === 'assistant' && item.turnId === turnId) {
      return index;
    }
  }
  return -1;
}

function findThinkingItemIndexByBlockId(
  items: TimelineItem[],
  blockId: string,
  turnId: string | null,
): number {
  return items.findIndex(
    (item) => item.kind === 'thinking'
      && item.turnId === turnId
      && item.blocks.some((block) => block.block_id === blockId),
  );
}

function findOpenPhaseThinkingIndex(items: TimelineItem[], turnId: string | null): number {
  const assistantIndex = findOpenPhaseAssistantIndex(items, turnId);
  const candidateIndex = (assistantIndex >= 0 ? assistantIndex : items.length) - 1;
  if (candidateIndex < 0) return -1;
  const candidate = items[candidateIndex];
  if (candidate?.kind !== 'thinking') return -1;
  return candidate.turnId === turnId ? candidateIndex : -1;
}

function appendThinkingBlockToSegment(
  items: TimelineItem[],
  segmentIndex: number,
  block: ThinkingBlock,
  turnId: string | null,
  timestamp: string | null,
  streaming: boolean,
  activeTitle: string | null,
): void {
  const existing = items[segmentIndex] as ThinkingTimelineItem;
  items[segmentIndex] = {
    ...existing,
    turnId,
    timestamp: existing.timestamp ?? timestamp,
    blocks: [...existing.blocks, block],
    streaming,
    activeTitle,
  } satisfies ThinkingTimelineItem;
}

function insertBeforeOpenPhaseAssistant(
  items: TimelineItem[],
  item: TimelineItem,
  turnId: string | null,
): number {
  const assistantIndex = findOpenPhaseAssistantIndex(items, turnId);
  if (assistantIndex < 0) {
    items.push(item);
    return items.length - 1;
  }
  items.splice(assistantIndex, 0, item);
  return assistantIndex;
}

function upsertAssistantTurnMessage(
  items: TimelineItem[],
  {
    id,
    content,
    timestamp,
    seq,
    messageId,
    attachments,
    turnId,
    streaming = false,
  }: {
    id: string;
    content: string;
    timestamp: string | null;
    seq: number | null;
    messageId: string | undefined;
    attachments: AttachmentRef[];
    turnId: string | null;
    streaming?: boolean;
  },
): void {
  const existingIndex = findOpenPhaseAssistantIndex(items, turnId);
  if (existingIndex >= 0 && items[existingIndex]?.kind === 'message') {
    const existing = items[existingIndex] as MessageTimelineItem;
    const nextContent = existing.content && content ? `${existing.content}\n\n${content}` : existing.content || content;
    const nextAttachments = attachments.length > 0 ? [...(existing.attachments ?? []), ...attachments] : existing.attachments ?? [];
    items[existingIndex] = {
      ...existing,
      content: nextContent,
      html: renderMarkdown(nextContent),
      seq,
      timestamp: existing.timestamp ?? timestamp,
      attachments: nextAttachments,
      streaming,
      messageId: messageId ?? existing.messageId,
      turnId,
    } satisfies MessageTimelineItem;
    return;
  }

  items.push(
    createMessageItem(id, 'assistant', content, timestamp, seq, messageId, streaming, attachments, false, turnId),
  );
}

function applyChunkToMessage(
  message: MessageTimelineItem,
  delta: string,
  chunkIndex: number | null,
  contentOffset: number | null,
): MessageTimelineItem | null {
  if (chunkIndex !== null && message.streamChunkCount !== undefined && chunkIndex < message.streamChunkCount) {
    return null;
  }

  if (contentOffset !== null) {
    if (contentOffset < message.content.length) {
      const existing = message.content.slice(contentOffset, contentOffset + delta.length);
      if (existing === delta) {
        return null;
      }
      return null;
    }
    if (contentOffset > message.content.length) {
      return null;
    }
  }

  const content = `${message.content}${delta}`;
  const streamer = getStreamer(message.id);
  return {
    ...message,
    content,
    html: streamer.render(content),
    streaming: true,
    streamChunkCount: chunkIndex !== null ? chunkIndex + 1 : (message.streamChunkCount ?? 0) + 1,
    streamContentOffset: content.length,
  } satisfies MessageTimelineItem;
}

function applyBufferedChunksToMessage(message: MessageTimelineItem, turnId: string | null): MessageTimelineItem {
  if (!message.messageId) return message;
  const key = streamKey(message.messageId, turnId);
  let chunks = pendingStreamChunks.get(key);
  if (!chunks?.length) return message;

  let current = message;
  let changed = true;
  while (changed) {
    changed = false;
    chunks = pendingStreamChunks.get(key);
    if (!chunks?.length) break;
    const nextIndex = chunks.findIndex((chunk) => chunk.contentOffset <= current.content.length);
    if (nextIndex < 0) break;
    const [chunk] = chunks.splice(nextIndex, 1);
    if (chunks.length > 0) {
      pendingStreamChunks.set(key, chunks);
    } else {
      pendingStreamChunks.delete(key);
    }
    const updated = applyChunkToMessage(current, chunk.delta, chunk.chunkIndex, chunk.contentOffset);
    if (updated) {
      current = updated;
    }
    changed = true;
  }
  return current;
}

function finalizeOpenPhaseAssistant(items: TimelineItem[], turnId: string | null): TimelineItem[] {
  const index = findOpenPhaseAssistantIndex(items, turnId);
  if (index < 0 || items[index]?.kind !== 'message') return items;
  const message = items[index] as MessageTimelineItem;
  if (!message.streaming) return items;
  const streamer = getStreamer(message.id);
  const next = [...items];
  next[index] = {
    ...message,
    html: streamer.finalize(message.content),
    streaming: false,
    streamChunkCount: undefined,
    streamContentOffset: undefined,
  } satisfies MessageTimelineItem;
  releaseStreamer(message.id);
  clearPendingChunks(message.messageId, turnId);
  return next;
}

function applyActiveStreamSnapshot(items: TimelineItem[], snapshot: ActiveStreamSnapshot): TimelineItem[] {
  const content = typeof snapshot.content === 'string' ? snapshot.content : '';
  if (!content) return items;

  const turnId = normalizeEventTurnId(snapshot.turn_id) ?? snapshot.message_id;
  const existingIndex = findOpenPhaseAssistantIndex(items, turnId);
  if (existingIndex >= 0 && items[existingIndex]?.kind === 'message') {
    const existing = items[existingIndex] as MessageTimelineItem;
    if (
      existing.streaming
      && snapshot.chunk_count <= (existing.streamChunkCount ?? 0)
      && content.length <= existing.content.length
    ) {
      return items;
    }
    if (!existing.streaming && existing.content === content) {
      return items;
    }
    const streamer = getStreamer(existing.id);
    const next = [...items];
    next[existingIndex] = applyBufferedChunksToMessage({
      ...existing,
      content,
      html: streamer.render(content),
      streaming: true,
      messageId: snapshot.message_id,
      turnId,
      streamChunkCount: snapshot.chunk_count,
      streamContentOffset: snapshot.content_offset,
      timestamp: existing.timestamp ?? snapshot.updated_at ?? new Date().toISOString(),
    } satisfies MessageTimelineItem, turnId);
    return next;
  }

  const item = createMessageItem(
    `message:${snapshot.message_id}:${items.length}`,
    'assistant',
    content,
    snapshot.updated_at ?? new Date().toISOString(),
    null,
    snapshot.message_id,
    true,
    [],
    false,
    turnId,
    snapshot.chunk_count,
  );
  return [
    ...items,
    applyBufferedChunksToMessage(item, turnId),
  ];
}

export function applyActiveStreamSnapshots(
  items: TimelineItem[],
  snapshots: ActiveStreamSnapshot[] | undefined | null,
): TimelineItem[] {
  if (!snapshots?.length) return items;
  return snapshots.reduce((next, snapshot) => applyActiveStreamSnapshot(next, snapshot), items);
}

export function normalizeHistory(events: MessageEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolCallIndexByCallId = new Map<string, number>();

  for (const event of events) {
    const content = typeof event.data.content === 'string' ? event.data.content : '';
    const attachments = normalizeEventAttachments(event.data.attachments);
    const turnId = normalizeEventTurnId(event.data.turn_id);
    // Use session_id from event data to build lineage-safe IDs (seq is session-local).
    const sid = typeof event.data.session_id === 'string' ? event.data.session_id : '';
    const eid = sid ? `${sid}:${event.seq}` : `${event.seq}`;
    if (event.type === 'user_message') {
      items.push(createMessageItem(`event:${eid}:user`, 'user', content, event.timestamp, event.seq, undefined, false, attachments, false, turnId));
      continue;
    }

    if (event.type === 'assistant_message') {
      if (content.trim() || attachments.length > 0) {
        upsertAssistantTurnMessage(items, {
          id: `event:${eid}:assistant`,
          content,
          timestamp: event.timestamp,
          seq: event.seq,
          messageId: turnId ?? undefined,
          attachments,
          turnId,
        });
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

    if (event.type === 'assistant_thinking') {
      const messageId = typeof event.data.message_id === 'string' ? event.data.message_id
        : typeof event.data.turn_id === 'string' ? event.data.turn_id
        : eid;
      const blockId = typeof event.data.block_id === 'string' ? event.data.block_id : `thk_${eid}`;
      const title = typeof event.data.title === 'string' && event.data.title ? event.data.title : 'Thinking';
      const blockContent = typeof event.data.content === 'string' ? event.data.content : '';
      const block: ThinkingBlock = {
        block_id: blockId,
        title,
        content: blockContent,
        html: renderMarkdown(blockContent),
        source: typeof event.data.reasoning_source === 'string' ? event.data.reasoning_source : 'summary',
        complete: true,
      };
      const contiguousIndex = findOpenPhaseThinkingIndex(items, turnId);
      if (contiguousIndex >= 0) {
        appendThinkingBlockToSegment(items, contiguousIndex, block, turnId, event.timestamp, false, null);
      } else {
        insertBeforeOpenPhaseAssistant(items, {
          id: `thinking:${eid}:${blockId}`,
          kind: 'thinking',
          messageId,
          turnId,
          blocks: [block],
          streaming: false,
          activeTitle: null,
          timestamp: event.timestamp,
        } satisfies ThinkingTimelineItem, turnId);
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
        turnId,
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
      const resultAttachments = normalizeEventAttachments(event.data.attachments);
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
          evaluation,
          attachments: resultAttachments.length > 0 ? resultAttachments : existing.attachments,
          turnId: existing.turnId ?? turnId,
        };
      } else {
        toolCallIndexByCallId.set(callId || `tc-${eid}`, items.length);
        items.push({
          id: `tool:${callId || `tc-${eid}`}`,
          kind: 'tool_call',
          callId: callId || `tc-${eid}`,
          turnId,
          toolName: String(event.data.name ?? event.data.tool_name ?? 'unknown'),
          status: event.data.is_error ? 'failed' : 'completed',
          timestamp: event.timestamp,
          result: typeof event.data.result === 'string' ? event.data.result : undefined,
          isError: typeof event.data.is_error === 'boolean' ? event.data.is_error : undefined,
          durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : undefined,
          evaluation,
          attachments: resultAttachments.length > 0 ? resultAttachments : undefined,
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
function normalizedToolName(name: string): string {
  return name.toLowerCase().replace(/_/g, '');
}

function hasDeferredAuthChallenge(value: unknown): boolean {
  if (typeof value === 'string') return value.startsWith('$auth_challenge:');
  if (Array.isArray(value)) return value.some(hasDeferredAuthChallenge);
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.value_ref === 'string' && record.value_ref.startsWith('$auth_challenge:')) return true;
    if (record.auth_challenge && typeof record.auth_challenge === 'object') return true;
    return Object.values(record).some(hasDeferredAuthChallenge);
  }
  return false;
}

function isPendingInputToolCall(tool: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(tool.toolName);
  if (name === 'steprequestinput' || name === 'requestauthchallenge') return true;
  if ((name === 'browserfill' || name === 'browsereval') && hasDeferredAuthChallenge(tool.arguments)) return true;
  return false;
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
    if (!isPendingInputToolCall(toolItem)) continue;
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
    if (!isPendingInputToolCall(tool)) return item;
    return {
      ...tool,
      status: 'completed',
      isError: false,
      result: JSON.stringify({ response: normalizedToolName(tool.toolName) === 'steprequestinput' ? response : '<redacted>' }),
    } satisfies ToolCallTimelineItem;
  });
}

export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  let next = [...items];

  if (event.type === 'user_message') {
    const attachments = normalizeEventAttachments(event.attachments);
    const turnId = normalizeEventTurnId(event.turn_id);
    const optimisticIndex = findOptimisticUserMessageIndex(next, event.content, attachments);
    if (optimisticIndex >= 0 && next[optimisticIndex]?.kind === 'message') {
      const existing = next[optimisticIndex] as MessageTimelineItem;
      next[optimisticIndex] = {
        ...existing,
        attachments,
        turnId,
        optimistic: false
      };
      return next;
    }
    const itemId = `user-msg:${Date.now()}:${next.length}`;
    next.push(
      createMessageItem(itemId, 'user', event.content, new Date().toISOString(), null, undefined, false, attachments, false, turnId)
    );
    return next;
  }

  if (event.type === 'chunk') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const chunkIndex = typeof event.index === 'number' ? event.index : null;
    const contentOffset = typeof event.content_offset === 'number' ? event.content_offset : null;
    const index = findOpenPhaseAssistantIndex(next, turnId);
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      if (contentOffset !== null && contentOffset > message.content.length) {
        bufferPendingChunk(event.message_id, turnId, {
          delta: event.content,
          chunkIndex,
          contentOffset,
        });
        return next;
      }
      const updated = applyChunkToMessage(message, event.content, chunkIndex, contentOffset);
      if (!updated) return next;
      const withBuffered = applyBufferedChunksToMessage(
        { ...updated, turnId, timestamp: message.timestamp ?? new Date().toISOString() },
        turnId,
      );
      next[index] = {
        ...withBuffered,
        turnId,
      };
      return next;
    }

    if (contentOffset !== null && contentOffset > 0) {
      bufferPendingChunk(event.message_id, turnId, {
        delta: event.content,
        chunkIndex,
        contentOffset,
      });
      return next;
    }

    const itemId = `message:${event.message_id}:${next.length}`;
    const item = createMessageItem(
        itemId,
        'assistant',
        event.content,
        new Date().toISOString(),
        null,
        event.message_id,
        true,
        [],
        false,
        turnId,
        chunkIndex !== null ? chunkIndex + 1 : 1,
      );
    next.push(applyBufferedChunksToMessage(item, turnId));
    return next;
  }

  if (event.type === 'assistant_stream_snapshot') {
    return applyActiveStreamSnapshot(next, event);
  }

  if (event.type === 'message_complete') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const itemId = `message:${event.message_id}:${next.length}`;
    const index = findOpenPhaseAssistantIndex(next, turnId);
    const attachments = normalizeEventAttachments(event.attachments);
    const finalContent = typeof event.content === 'string' ? event.content : null;
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      const completeContent = finalContent ?? message.content;
      // Finalize and release the streamer for this message.
      const streamer = getStreamer(message.id);
      const finalHtml = streamer.finalize(completeContent);
      releaseStreamer(message.id);
      clearPendingChunks(event.message_id, turnId);
      next[index] = {
        ...message,
        content: completeContent,
        html: finalHtml,
        seq: event.seq,
        streaming: false,
        turnId,
        attachments: attachments.length > 0 ? attachments : message.attachments,
        streamChunkCount: undefined,
        streamContentOffset: undefined,
      };
      return next;
    }

    if (finalContent || attachments.length > 0) {
      clearPendingChunks(event.message_id, turnId);
      upsertAssistantTurnMessage(next, {
        id: itemId,
        content: finalContent ?? '',
        timestamp: new Date().toISOString(),
        seq: event.seq,
        messageId: event.message_id,
        attachments,
        turnId,
      });
      return next;
    }

    // No chunks were received for this message — skip creating empty bubble
    return next;
  }

  if (event.type === 'assistant_thinking_chunk') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const blockId = event.block_id;
    const delta = event.delta ?? '';
    const index = findThinkingItemIndexByBlockId(next, blockId, turnId);
    if (index >= 0) {
      const existing = next[index] as ThinkingTimelineItem;
      // Block ids can repeat across separate reasoning cycles within the same
      // turn. Only reuse an in-flight block; if the prior one is already
      // complete, start a new block instead of appending into old content.
      const blockIdx = existing.blocks.findIndex((b) => b.block_id === blockId && !b.complete);
      if (blockIdx >= 0) {
        const blocks = existing.blocks.slice();
        const block = blocks[blockIdx];
        const newContent = block.content + delta;
        const streamer = getThinkingStreamer(blockId);
        blocks[blockIdx] = {
          ...block,
          content: newContent,
          html: streamer.render(newContent),
          title: event.title ?? block.title,
        };
        next[index] = {
          ...existing,
          blocks,
          streaming: true,
          turnId,
          activeTitle: event.title ?? existing.activeTitle,
        };
      } else {
        // New block within existing thinking item
        const streamer = getThinkingStreamer(blockId);
        const blocks = [
          ...existing.blocks,
          {
            block_id: blockId,
            title: event.title ?? 'Thinking',
            content: delta,
            html: streamer.render(delta),
            source: 'summary',
            complete: false,
          } satisfies ThinkingBlock,
        ];
        next[index] = {
          ...existing,
          blocks,
          streaming: true,
          turnId,
          activeTitle: event.title ?? existing.activeTitle,
        };
      }
    } else {
      const streamer = getThinkingStreamer(blockId);
      const block = {
        block_id: blockId,
        title: event.title ?? 'Thinking',
        content: delta,
        html: streamer.render(delta),
        source: 'summary',
        complete: false,
      } satisfies ThinkingBlock;
      const contiguousIndex = findOpenPhaseThinkingIndex(next, turnId);
      if (contiguousIndex >= 0) {
        appendThinkingBlockToSegment(
          next,
          contiguousIndex,
          block,
          turnId,
          new Date().toISOString(),
          true,
          event.title ?? null,
        );
      } else {
        insertBeforeOpenPhaseAssistant(next, {
          id: `thinking:${turnId}:${blockId}`,
          kind: 'thinking',
          messageId: event.message_id,
          turnId,
          blocks: [block],
          streaming: true,
          activeTitle: event.title ?? null,
          timestamp: new Date().toISOString(),
        } satisfies ThinkingTimelineItem, turnId);
      }
    }
    return next;
  }

  if (event.type === 'assistant_thinking_block') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const messageId = event.message_id;
    const blockId = event.block_id;
    const index = findThinkingItemIndexByBlockId(next, blockId, turnId);

    if (event.content) {
      // Replay frame with full content (no prior chunks)
      const html = renderMarkdown(event.content);
      const block: ThinkingBlock = {
        block_id: blockId,
        title: event.title ?? 'Thinking',
        content: event.content,
        html,
        source: 'summary',
        complete: true,
      };
      if (index >= 0) {
        const existing = next[index] as ThinkingTimelineItem;
        const blockIdx = existing.blocks.findIndex((b) => b.block_id === blockId && !b.complete);
        const blocks =
          blockIdx >= 0
            ? existing.blocks.map((b, i) => (i === blockIdx ? block : b))
            : [...existing.blocks, block];
        next[index] = {
          ...existing,
          blocks,
          streaming: blocks.some((b) => !b.complete),
          turnId,
          activeTitle: null,
        };
      } else {
        const contiguousIndex = findOpenPhaseThinkingIndex(next, turnId);
        if (contiguousIndex >= 0) {
          appendThinkingBlockToSegment(next, contiguousIndex, block, turnId, new Date().toISOString(), false, null);
        } else {
          insertBeforeOpenPhaseAssistant(next, {
            id: `thinking:${turnId}:${blockId}`,
            kind: 'thinking',
            messageId,
            turnId,
            blocks: [block],
            streaming: false,
            activeTitle: null,
            timestamp: new Date().toISOString(),
          } satisfies ThinkingTimelineItem, turnId);
        }
      }
    } else if (index >= 0) {
      // Complete signal for streaming block
      const existing = next[index] as ThinkingTimelineItem;
      const blockIdx = existing.blocks.findIndex((b) => b.block_id === blockId && !b.complete);
      const blocks = existing.blocks.slice();
      if (blockIdx >= 0) {
        const block = blocks[blockIdx];
        const streamer = getThinkingStreamer(blockId);
        const finalHtml = streamer.finalize(block.content);
        thinkingStreamers.delete(blockId);
        blocks[blockIdx] = {
          ...block,
          html: finalHtml,
          title: event.title ?? block.title,
          complete: true,
        };
      }
      next[index] = {
        ...existing,
        blocks,
        streaming: blocks.some((b) => !b.complete),
        turnId,
        activeTitle: null,
      };
    } else {
      const fallbackBlock: ThinkingBlock = {
        block_id: blockId,
        title: event.title ?? 'Thinking',
        content: '',
        html: '',
        source: 'summary',
        complete: true,
      };
      const contiguousIndex = findOpenPhaseThinkingIndex(next, turnId);
      if (contiguousIndex >= 0) {
        appendThinkingBlockToSegment(next, contiguousIndex, fallbackBlock, turnId, new Date().toISOString(), false, null);
      } else {
        insertBeforeOpenPhaseAssistant(next, {
          id: `thinking:${turnId}:${blockId}`,
          kind: 'thinking',
          messageId,
          turnId,
          blocks: [fallbackBlock],
          streaming: false,
          activeTitle: null,
          timestamp: new Date().toISOString(),
        } satisfies ThinkingTimelineItem, turnId);
      }
    }
    return next;
  }

  if (event.type === 'tool_call') {
    const turnId = normalizeEventTurnId(event.turn_id);
    next = finalizeOpenPhaseAssistant(next, turnId);
    // Orchestration tools are displayed as delegation cards, not tool blocks
    if (['delegate', 'fork'].includes(event.tool_name)) return next;
    const itemId = `tool:${event.call_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    const toolItem: ToolCallTimelineItem = {
      id: itemId,
      kind: 'tool_call',
      callId: event.call_id,
      toolName: event.tool_name,
      turnId,
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
        evaluation: existing.evaluation,
        attachments: existing.attachments,
        turnId: toolItem.turnId ?? existing.turnId,
      };
      return next;
    }
    next.push(toolItem);
    return next;
  }

  if (event.type === 'tool_result') {
    const turnId = normalizeEventTurnId(event.turn_id);
    const itemId = `tool:${event.call_id}`;
    const evaluation = event.evaluation ?? undefined;
    const attachments = normalizeEventAttachments(event.attachments);
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
        evaluation,
        attachments: attachments.length > 0 ? attachments : existing.attachments,
        turnId: existing.turnId ?? turnId,
      };
      return next;
    }
    // tool_result arrived before tool_call — create a placeholder
    next.push({
      id: itemId,
      kind: 'tool_call',
      callId: event.call_id,
      toolName: event.tool_name,
      turnId,
      status: event.is_error ? 'failed' : 'completed',
      timestamp: event.timestamp ?? new Date().toISOString(),
      result: event.result,
      isError: event.is_error,
      durationMs: event.duration_ms ?? undefined,
      evaluation,
      attachments: attachments.length > 0 ? attachments : undefined,
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

  if (event.type === 'workflow_gate' || event.type === 'workflow_step_question' || event.type === 'auth_challenge' || event.type === 'credential_request') {
    const isDirectQuestion = event.type === 'workflow_step_question' && !event.task_id;
    const isDirectAuth = event.type === 'auth_challenge' && !event.task_id;
    if (isDirectQuestion || isDirectAuth) {
      return next;
    }
    const noticeId = event.notification_id
      ? `notice:${event.type}:${event.notification_id}`
      : undefined;
    const title = event.type === 'workflow_gate'
      ? 'Task waiting for approval'
      : event.type === 'auth_challenge'
        ? 'Task waiting for authentication'
        : event.type === 'credential_request'
          ? 'Task waiting for credentials'
          : isDirectQuestion
            ? 'Assistant requested more input'
            : 'Task requested more input';
    const description = event.type === 'workflow_gate'
      ? `Task ${event.task_id} paused at ${event.step_name ?? 'a workflow step'}.`
      : event.type === 'auth_challenge'
        ? event.message?.trim() || `Task ${event.task_id} paused for authentication.`
        : event.type === 'credential_request'
          ? event.message?.trim() || `Task ${event.task_id} paused for credentials.`
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

  if (event.type === 'workflow_gate_resolved' || event.type === 'workflow_step_question_resolved' || event.type === 'auth_challenge_resolved' || event.type === 'credential_request_resolved') {
    if (!event.notification_id) return next;
    const sourceType = event.type === 'workflow_gate_resolved'
      ? 'workflow_gate'
      : event.type === 'auth_challenge_resolved'
        ? 'auth_challenge'
        : event.type === 'credential_request_resolved'
          ? 'credential_request'
          : 'workflow_step_question';
    return next.filter((item) => item.id !== `notice:${sourceType}:${event.notification_id}`);
  }

  return next;
}
