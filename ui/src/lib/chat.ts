import { createMarkdownStreamer, renderMarkdown, stripMarkdown, type MarkdownStreamer } from '$lib/markdown';
import { normalizeFileDiffs, type FileDiff } from '$lib/diff';
import { parseTodoSnapshot, type TodoSnapshotItem } from '$lib/todos';
import type { ActiveStreamSnapshot, ActiveThinkingSnapshot, ActiveToolOutputSnapshot, AttachmentRef, ChatMode, ChatModeSource, CognisWebSocketEvent, MessageEvent, ToolOutputPresentationMetadata } from '$lib/types/api';

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

type ToolResultEvent = Extract<CognisWebSocketEvent, { type: 'tool_result' }>;
type ToolOutputChunkEvent = Extract<CognisWebSocketEvent, { type: 'tool_result_chunk' | 'tool_output_chunk' }> & {
  call_id?: string;
  tool_name?: string;
  delta?: string;
  content?: string;
  text?: string;
  stream?: string | null;
  is_error?: boolean;
  chunk_index?: number;
  content_offset?: number;
  session_id?: string;
};

interface PendingToolOutputChunk {
  delta: string;
  isError: boolean;
  chunkIndex?: number;
  contentOffset?: number;
  sessionId?: string;
}

const pendingStreamChunks = new Map<string, PendingStreamChunk[]>();
const pendingToolOutputChunks = new Map<string, PendingToolOutputChunk[]>();
const pendingToolResults = new Map<string, ToolResultEvent>();
const MAX_PENDING_TOOL_OUTPUT_KEYS = 200;

function pruneOldestPendingToolEntry<T>(map: Map<string, T>): void {
  while (map.size > MAX_PENDING_TOOL_OUTPUT_KEYS) {
    const first = map.keys().next().value;
    if (typeof first !== 'string') return;
    map.delete(first);
  }
}

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

function utf16CodeUnits(value: string): number {
  let units = 0;
  for (const char of value) {
    units += char.length;
  }
  return units;
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

function toolEventKey(params: {
  conversationId?: string | null;
  sessionId?: string | null;
  turnId?: string | null;
  callId?: string | null;
}): string {
  return [
    params.conversationId ?? 'unknown-conversation',
    params.sessionId ?? 'unknown-session',
    params.turnId ?? 'unknown-turn',
    params.callId ?? 'unknown-call',
  ].join(':');
}

function bufferPendingToolOutputChunk(key: string, chunk: PendingToolOutputChunk): void {
  if (!key) return;
  const chunks = pendingToolOutputChunks.get(key) ?? [];
  if (
    chunks.some(
      (existing) =>
        existing.chunkIndex === chunk.chunkIndex
        && existing.contentOffset === chunk.contentOffset
        && existing.delta === chunk.delta,
    )
  ) {
    return;
  }
  chunks.push(chunk);
  chunks.sort((left, right) => {
    const leftOffset = left.contentOffset ?? Number.MAX_SAFE_INTEGER;
    const rightOffset = right.contentOffset ?? Number.MAX_SAFE_INTEGER;
    if (leftOffset !== rightOffset) return leftOffset - rightOffset;
    return (left.chunkIndex ?? Number.MAX_SAFE_INTEGER) - (right.chunkIndex ?? Number.MAX_SAFE_INTEGER);
  });
  pendingToolOutputChunks.set(key, chunks);
  pruneOldestPendingToolEntry(pendingToolOutputChunks);
}

function appendToolOutputChunk(
  existing: ToolCallTimelineItem,
  chunk: PendingToolOutputChunk,
): ToolCallTimelineItem {
  if (
    typeof chunk.chunkIndex === 'number'
    && typeof existing.streamChunkCount === 'number'
    && chunk.chunkIndex < existing.streamChunkCount
  ) {
    return existing;
  }
  if (
    typeof chunk.contentOffset === 'number'
    && typeof existing.streamContentOffset === 'number'
    && chunk.contentOffset < existing.streamContentOffset
  ) {
    return existing;
  }
  const streamedOutput = `${existing.streamedOutput ?? ''}${chunk.delta}`;
  return {
    ...existing,
    streamedOutput,
    result: streamedOutput,
    isError: chunk.isError || existing.isError,
    streamChunkCount: typeof chunk.chunkIndex === 'number' ? chunk.chunkIndex + 1 : existing.streamChunkCount,
    streamContentOffset: typeof chunk.contentOffset === 'number' ? chunk.contentOffset + utf16CodeUnits(chunk.delta) : existing.streamContentOffset,
    sessionId: chunk.sessionId ?? existing.sessionId,
    liveOutputAvailable: true,
  };
}

function applyBufferedToolOutputChunks(key: string, item: ToolCallTimelineItem): ToolCallTimelineItem {
  const chunks = pendingToolOutputChunks.get(key);
  if (!chunks || chunks.length === 0) return item;
  pendingToolOutputChunks.delete(key);
  return chunks.reduce((current, chunk) => appendToolOutputChunk(current, chunk), item);
}

function bufferPendingToolResult(key: string, event: ToolResultEvent): void {
  if (!key) return;
  pendingToolResults.set(key, event);
  pruneOldestPendingToolEntry(pendingToolResults);
}

function applyToolResultEvent(
  existing: ToolCallTimelineItem,
  event: ToolResultEvent,
): ToolCallTimelineItem {
  const evaluation = event.evaluation ?? undefined;
  const attachments = normalizeEventAttachments(event.attachments);
  const fileDiffs = normalizeFileDiffs(event.file_diffs);
  const turnId = normalizeEventTurnId(event.turn_id);
  const keepStreamed = Boolean(event.transport_truncated) && (existing.streamedOutput?.length ?? 0) > event.result.length;
  return mergeToolPresentation({
    ...existing,
    status: event.is_error ? 'failed' : 'completed',
    timestamp: event.timestamp ?? existing.timestamp,
    result: keepStreamed ? existing.streamedOutput : event.result,
    isError: event.is_error,
    durationMs: event.duration_ms ?? undefined,
    evaluation,
    attachments: attachments.length > 0 ? attachments : existing.attachments,
    fileDiffs: fileDiffs.length > 0 ? fileDiffs : existing.fileDiffs,
    turnId: existing.turnId ?? turnId,
    sessionId: event.session_id ?? existing.sessionId,
  }, event);
}

function applyPendingToolResult(key: string, item: ToolCallTimelineItem): ToolCallTimelineItem {
  const pending = pendingToolResults.get(key);
  if (!pending) return item;
  pendingToolResults.delete(key);
  return applyToolResultEvent(item, pending);
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
  startedAt?: string | null;
  completedAt?: string | null;
  durationMs?: number | null;
  providerBlockIndex?: number | null;
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
  sessionId?: string | null;
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
  clientMessageId?: string | null;
  queueId?: string | null;
  streamChunkCount?: number;
  streamContentOffset?: number;
  chatMode?: ChatMode;
  chatModeSource?: ChatModeSource;
  partial?: boolean;
  finishReason?: string | null;
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
  fileDiffs?: FileDiff[];
  streamedOutput?: string;
  streamChunkCount?: number;
  streamContentOffset?: number;
  outputSize?: number;
  truncated?: boolean;
  agentVisibleTruncated?: boolean;
  transportTruncated?: boolean;
  hasFullOutput?: boolean;
  recoveryCallId?: string | null;
  toolOutputArtifactId?: string | null;
  sessionId?: string | null;
  liveOutputAvailable?: boolean;
  anchorsAvailable?: boolean;
  anchorCount?: number;
  reconstructed?: boolean;
  /**
   * Notification ID backing a pending `step_request_questions` tool call.
   *
   * The backend creates a `step_question` notification whose
   * `notification_id` is the `pause_id` the agent loop is waiting on, but
   * the `tool_call` event itself does not carry that ID. The chat page
   * annotates the matching `step_request_questions` tool item when the
   * corresponding `workflow_step_question` WebSocket event arrives so we
   * can resolve the pause by typing a reply — even if
   * `pendingDirectQuestion` state has been lost to a race, compaction, or
   * conversation reload.
   */
  notificationId?: string;
  progressPhase?: string;
  progressInputChars?: number;
  progressInputLines?: number;
  progressComplete?: boolean;
}

export { parseTodoSnapshot, type TodoSnapshotItem };

function normalizeToolName(name: string): string {
  return name.toLowerCase().replace(/_/g, '');
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
    let latestToolTurnId: string | null = null;
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === 'tool_call' && typeof item.turnId === 'string' && item.turnId.trim().length > 0) {
        latestToolTurnId = item.turnId;
        break;
      }
    }
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === 'message' && item.role === 'user') {
        if (item.queueId && item.turnId && item.turnId === latestToolTurnId) {
          continue;
        }
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
  agentId: string | null;
  usedAgentId: string | null;
  status: 'started' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
  result: string | null;
  timestamp: string | null;
  /** Live progress fields (present while running) */
  toolCallCount?: number;
  maxToolCalls?: number;
  lastTool?: string;
  todos?: TodoSnapshotItem[];
}

const terminalDelegationStatuses = new Set<DelegationTimelineItem['status']>([
  'completed',
  'failed',
  'cancelled'
]);

function normalizeDelegationStatus(value: unknown): DelegationTimelineItem['status'] {
  return value === 'running' || value === 'paused' || value === 'completed' || value === 'failed' || value === 'cancelled'
    ? value
    : 'started';
}

function isGenericDelegationLabel(value: string | null | undefined): boolean {
  if (!value) return true;
  return ['Background task', 'Sub-session', 'Delegation'].includes(value);
}

function delegationTaskLabel(value: unknown, fallback = 'Background task'): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function delegationEventLabel(data: Record<string, unknown>, fallback = 'Background task'): string {
  return delegationTaskLabel(data.title ?? data.task_title ?? data.task, fallback);
}

function delegationPreview(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const stripped = stripMarkdown(value);
  return stripped ? stripped : null;
}

function mergeDelegationItem(
  existing: DelegationTimelineItem | null,
  incoming: DelegationTimelineItem
): DelegationTimelineItem {
  if (!existing) return incoming;
  const existingTerminal = terminalDelegationStatuses.has(existing.status);
  const incomingTerminal = terminalDelegationStatuses.has(incoming.status);
  const keepExistingTerminal = existingTerminal && !incomingTerminal;
  const taskLabel = !isGenericDelegationLabel(incoming.taskLabel)
    ? incoming.taskLabel
    : !isGenericDelegationLabel(existing.taskLabel)
      ? existing.taskLabel
      : incoming.taskLabel;

  return {
    ...existing,
    ...incoming,
    taskLabel,
    status: keepExistingTerminal ? existing.status : incoming.status,
    result: keepExistingTerminal ? existing.result : incoming.result ?? existing.result,
    timestamp: keepExistingTerminal ? existing.timestamp : incoming.timestamp ?? existing.timestamp,
    agentId: incoming.agentId ?? existing.agentId,
    usedAgentId: incoming.usedAgentId ?? existing.usedAgentId,
    // Preserve live progress fields from whichever side has them
    toolCallCount: incoming.toolCallCount ?? existing.toolCallCount,
    maxToolCalls: incoming.maxToolCalls ?? existing.maxToolCalls,
    lastTool: incoming.lastTool ?? existing.lastTool,
    todos: incoming.todos && incoming.todos.length > 0 ? incoming.todos : existing.todos,
  };
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
  noticeId?: string | null;
  noticeKind?: string | null;
  noticeScope?: string | null;
  timestamp: string | null;
}

export interface CompactionTimelineItem {
  id: string;
  kind: 'compaction';
  status: 'running' | 'compacted' | 'failed' | 'skipped';
  sessionId?: string;
  previousSessionId?: string;
  summaryPreview: string;
  summary?: string;
  method: string;
  turnsCompacted: number;
  trigger?: string;
  reason?: string;
  previousUsagePercentage?: number | null;
  effectiveUsagePercentage?: number | null;
  hardPressureExceeded?: boolean;
  usedTimeoutFallback?: boolean;
  timestamp: string | null;
}

function createSystemMessageItem(
  id: string,
  text: string,
  timestamp: string | null,
  noticeId: string | null = null,
  noticeKind: string | null = null,
  noticeScope: string | null = null,
): SystemMessageTimelineItem {
  return {
    id,
    kind: 'system_message',
    text,
    noticeId,
    noticeKind,
    noticeScope,
    timestamp
  };
}

function isVisiblePersistedSystemMessage(data: Record<string, unknown>): boolean {
  const noticeId = data.notice_id;
  if (typeof noticeId === 'string' && noticeId.length > 0) return true;

  const kind = data.kind;
  if (kind === 'turn_initiated') return true;

  const event = data.event;
  if (event === 'turn_initiated') return true;

  return false;
}

function createMessageItem(
  id: string,
  sessionId: string | null,
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
  clientMessageId: string | null = null,
  queueId: string | null = null,
): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    sessionId,
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
    clientMessageId,
    queueId,
    streamChunkCount,
    streamContentOffset: content.length,
  };
}

function eventSessionId(event: MessageEvent): string | null {
  const directSessionId = normalizeIdentifier(event.session_id);
  if (directSessionId) return directSessionId;
  return normalizeIdentifier(event.data.session_id);
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

  // Primary match: exact content equality (covers the normal case and the
  // fixed server path where attachment-only messages keep content="").
  if (item.content === content && sameAttachmentIds(item.attachments, attachments)) {
    return index;
  }

  // Defensive match for histories recorded before the server fix: if the
  // optimistic bubble is attachment-only (empty content) and the incoming
  // event carries a server-side placeholder like "User attached a file."
  // but has the same attachment ids, treat them as the same message.
  const isOptimisticAttachmentOnly = item.content === '' && (item.attachments?.length ?? 0) > 0;
  const isServerPlaceholder = content.startsWith('User attached ');
  if (isOptimisticAttachmentOnly && isServerPlaceholder && sameAttachmentIds(item.attachments, attachments)) {
    return index;
  }

  return -1;
}

function findUserMessageByCorrelationIndex(
  items: TimelineItem[],
  clientMessageId?: string | null,
  queueId?: string | null,
): number {
  if (!clientMessageId && !queueId) return -1;
  return items.findIndex((item) => (
    item.kind === 'message'
    && item.role === 'user'
    && ((clientMessageId && item.clientMessageId === clientMessageId) || (queueId && item.queueId === queueId))
  ));
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

function normalizeIdentifier(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function stableStringHash(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
}

function userMessageItemId(event: {
  session_id?: string | null;
  seq?: number | null;
  message_id?: string | null;
  event_id?: string | null;
  turn_id?: string | null;
  client_message_id?: string | null;
  queue_id?: string | null;
  content: string;
}): string {
  const messageId = normalizeIdentifier(event.message_id) ?? normalizeIdentifier(event.event_id);
  if (messageId) return `user-msg:${messageId}`;
  const sessionId = normalizeIdentifier(event.session_id);
  if (sessionId && typeof event.seq === 'number') return `user-msg:${sessionId}:${event.seq}`;
  const clientMessageId = normalizeIdentifier(event.client_message_id);
  if (clientMessageId) return `user-msg:client:${clientMessageId}`;
  const queueId = normalizeIdentifier(event.queue_id);
  if (queueId) return `user-msg:queue:${queueId}`;
  const turnId = normalizeIdentifier(event.turn_id);
  if (sessionId && turnId) return `user-msg:${sessionId}:${turnId}:user`;
  return `user-msg:fallback:${utf16CodeUnits(event.content)}:${stableStringHash(event.content)}`;
}

function normalizeChatMode(value: unknown): ChatMode | undefined {
  return value === 'default' || value === 'plan' || value === 'build' ? value : undefined;
}

function normalizeChatModeSource(value: unknown): ChatModeSource | undefined {
  return value === 'one_shot'
    || value === 'conversation_override'
    || value === 'agent_default'
    || value === 'system_default'
    ? value
    : undefined;
}

function chatModeSourceRank(source: ChatModeSource | undefined): number {
  if (source === 'one_shot' || source === 'conversation_override') return 4;
  if (source === 'agent_default') return 3;
  if (source === 'system_default') return 1;
  return 0;
}

function modeCandidateRank(chatMode: ChatMode | undefined, source: ChatModeSource | undefined): number {
  const sourceRank = chatModeSourceRank(source);
  if (chatMode && chatMode !== 'default') return Math.max(sourceRank, 2);
  return sourceRank;
}

function chooseTurnMode(
  current: { chatMode?: ChatMode; chatModeSource?: ChatModeSource },
  candidate: { chatMode?: ChatMode; chatModeSource?: ChatModeSource },
): { chatMode?: ChatMode; chatModeSource?: ChatModeSource } {
  const currentRank = modeCandidateRank(current.chatMode, current.chatModeSource);
  const candidateRank = modeCandidateRank(candidate.chatMode, candidate.chatModeSource);
  return candidateRank >= currentRank ? candidate : current;
}

function chatModeForTurn(items: TimelineItem[], turnId: string | null): { chatMode?: ChatMode; chatModeSource?: ChatModeSource } {
  if (!turnId) return {};
  let mode: { chatMode?: ChatMode; chatModeSource?: ChatModeSource } = {};
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind !== 'message' || item.turnId !== turnId) continue;
    if (item.chatMode || item.chatModeSource) {
      mode = chooseTurnMode(mode, { chatMode: item.chatMode, chatModeSource: item.chatModeSource });
    }
  }
  return mode;
}

function applyChatModeToTurnMessages(
  items: TimelineItem[],
  turnId: string | null,
  chatMode?: ChatMode,
  chatModeSource?: ChatModeSource,
): TimelineItem[] {
  if (!turnId || (!chatMode && !chatModeSource)) return items;
  let changed = false;
  const next = items.map((item) => {
    if (item.kind !== 'message' || item.turnId !== turnId) return item;
    const selected = chooseTurnMode(
      { chatMode: item.chatMode, chatModeSource: item.chatModeSource },
      { chatMode, chatModeSource },
    );
    const updatedChatMode = selected.chatMode;
    const updatedChatModeSource = selected.chatModeSource;
    if (updatedChatMode === item.chatMode && updatedChatModeSource === item.chatModeSource) return item;
    changed = true;
    return {
      ...item,
      chatMode: updatedChatMode,
      chatModeSource: updatedChatModeSource,
    } satisfies MessageTimelineItem;
  });
  return changed ? next : items;
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

function findMergeableAssistantIndex(
  items: TimelineItem[],
  turnId: string | null,
  messageId: string | undefined,
): number {
  const existingIndex = findOpenPhaseAssistantIndex(items, turnId);
  if (existingIndex < 0 || items[existingIndex]?.kind !== 'message') return -1;
  const existing = items[existingIndex] as MessageTimelineItem;
  if (existing.streaming) return existingIndex;
  return messageId && existing.messageId === messageId ? existingIndex : -1;
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

function hasDuplicateThinkingBlock(
  item: ThinkingTimelineItem,
  block: ThinkingBlock,
): boolean {
  return item.blocks.some((existing) =>
    existing.block_id === block.block_id
    || (
      existing.complete
      && block.complete
      && existing.content === block.content
      && existing.title === block.title
      && existing.source === block.source
      && (
        (
          existing.providerBlockIndex != null
          && block.providerBlockIndex != null
          && existing.providerBlockIndex === block.providerBlockIndex
        )
        || (
          existing.startedAt != null
          && block.startedAt != null
          && existing.startedAt === block.startedAt
        )
        || (
          existing.completedAt != null
          && block.completedAt != null
          && existing.completedAt === block.completedAt
        )
      )
    )
  );
}

function mergeThinkingBlock(existing: ThinkingBlock, incoming: ThinkingBlock): ThinkingBlock {
  const content = incoming.content || existing.content;
  const html = incoming.html || existing.html;
  return {
    ...existing,
    ...incoming,
    content,
    html,
    title: incoming.title || existing.title,
    source: incoming.source || existing.source,
    complete: incoming.complete || existing.complete,
    startedAt: incoming.startedAt ?? existing.startedAt,
    completedAt: incoming.completedAt ?? existing.completedAt,
    durationMs: incoming.durationMs ?? existing.durationMs,
    providerBlockIndex: incoming.providerBlockIndex ?? existing.providerBlockIndex,
  };
}

function upsertThinkingBlockInSegment(
  items: TimelineItem[],
  segmentIndex: number,
  block: ThinkingBlock,
  turnId: string | null,
  timestamp: string | null,
  streaming: boolean,
  activeTitle: string | null,
): void {
  const existing = items[segmentIndex] as ThinkingTimelineItem;
  const blockIdx = existing.blocks.findIndex((existingBlock) => existingBlock.block_id === block.block_id);
  let blocks: ThinkingBlock[];
  if (blockIdx >= 0) {
    blocks = existing.blocks.map((existingBlock, index) =>
      index === blockIdx ? mergeThinkingBlock(existingBlock, block) : existingBlock
    );
  } else if (hasDuplicateThinkingBlock(existing, block)) {
    blocks = existing.blocks;
  } else {
    blocks = [...existing.blocks, block];
  }
  items[segmentIndex] = {
    ...existing,
    turnId,
    timestamp: existing.timestamp ?? timestamp,
    blocks,
    streaming: streaming || blocks.some((existingBlock) => !existingBlock.complete),
    activeTitle,
  } satisfies ThinkingTimelineItem;
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

function mergeAssistantContent(existing: string, incoming: string): string {
  if (!existing) return incoming;
  if (!incoming) return existing;
  if (existing === incoming) return existing;
  if (incoming.startsWith(existing)) return incoming;

  const existingSegments = existing.split(/\n{2,}/);
  if (existingSegments.some((segment) => segment === incoming)) return existing;

  return `${existing}\n\n${incoming}`;
}

function reconcileCompletedAssistantContent(existing: string, completed: string | null): string {
  if (!completed) return existing;
  if (!existing) return completed;
  if (existing === completed) return existing;
  if (completed.startsWith(existing)) return completed;
  const existingSegments = existing.split(/\n{2,}/);
  if (existingSegments.at(-1) === completed) return existing;
  return completed;
}

function upsertAssistantTurnMessage(
  items: TimelineItem[],
  {
    id,
    sessionId,
    content,
    timestamp,
    seq,
    messageId,
    attachments,
    turnId,
    chatMode,
    chatModeSource,
    partial = false,
    finishReason = null,
    streaming = false,
  }: {
    id: string;
    sessionId?: string | null;
    content: string;
    timestamp: string | null;
    seq: number | null;
    messageId: string | undefined;
    attachments: AttachmentRef[];
    turnId: string | null;
    chatMode?: ChatMode;
    chatModeSource?: ChatModeSource;
    partial?: boolean;
    finishReason?: string | null;
    streaming?: boolean;
  },
): void {
  const existingIndex = findMergeableAssistantIndex(items, turnId, messageId);
  if (existingIndex >= 0) {
    const existing = items[existingIndex] as MessageTimelineItem;
    const nextContent = mergeAssistantContent(existing.content, content);
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
      sessionId: sessionId ?? existing.sessionId,
      turnId,
      chatMode: chatMode ?? existing.chatMode,
      chatModeSource: chatModeSource ?? existing.chatModeSource,
      partial: partial || existing.partial,
      finishReason: finishReason ?? existing.finishReason,
    } satisfies MessageTimelineItem;
    return;
  }

  const item = createMessageItem(id, sessionId ?? null, 'assistant', content, timestamp, seq, messageId, streaming, attachments, false, turnId);
  item.chatMode = chatMode;
  item.chatModeSource = chatModeSource;
  item.partial = partial;
  item.finishReason = finishReason;
  items.push(item);
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
  const turnMode = chatModeForTurn(items, turnId);
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
      sessionId: snapshot.session_id ?? existing.sessionId,
      turnId,
      streamChunkCount: snapshot.chunk_count,
      streamContentOffset: snapshot.content_offset,
      chatMode: existing.chatMode ?? turnMode.chatMode,
      chatModeSource: existing.chatModeSource ?? turnMode.chatModeSource,
      timestamp: existing.timestamp ?? snapshot.updated_at ?? new Date().toISOString(),
    } satisfies MessageTimelineItem, turnId);
    return next;
  }

  const item = createMessageItem(
    `message:${snapshot.message_id}:${items.length}`,
    snapshot.session_id ?? null,
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
  item.chatMode = turnMode.chatMode;
  item.chatModeSource = turnMode.chatModeSource;
  return [
    ...items,
    applyBufferedChunksToMessage(item, turnId),
  ];
}

function toolPresentationFields(source: ToolOutputPresentationMetadata | Record<string, unknown>): Partial<ToolCallTimelineItem> {
  const record = source as Record<string, unknown>;
  const meta = typeof record.tool_output_presentation === 'object' && record.tool_output_presentation !== null
    ? record.tool_output_presentation as ToolOutputPresentationMetadata
    : source as ToolOutputPresentationMetadata;
  return {
    outputSize: typeof meta.output_size === 'number' ? meta.output_size : undefined,
    truncated: Boolean(meta.truncated),
    agentVisibleTruncated: Boolean(meta.agent_visible_truncated),
    transportTruncated: Boolean(meta.transport_truncated),
    hasFullOutput: Boolean(meta.has_full_output),
    recoveryCallId: typeof meta.recovery_call_id === 'string' ? meta.recovery_call_id : null,
    toolOutputArtifactId: typeof meta.tool_output_artifact_id === 'string' ? meta.tool_output_artifact_id : null,
    liveOutputAvailable: Boolean(record.live_output_available || record.output_page_available),
    anchorsAvailable: Boolean(meta.anchors_available),
    anchorCount: typeof meta.anchor_count === 'number' ? meta.anchor_count : undefined,
  };
}

function mergeToolPresentation(item: ToolCallTimelineItem, source: ToolOutputPresentationMetadata | Record<string, unknown>): ToolCallTimelineItem {
  const fields = toolPresentationFields(source);
  return {
    ...item,
    ...Object.fromEntries(Object.entries(fields).filter(([, value]) => value !== undefined)),
  } as ToolCallTimelineItem;
}

export function applyActiveToolOutputSnapshots(
  items: TimelineItem[],
  snapshots: ActiveToolOutputSnapshot[] | undefined | null,
): TimelineItem[] {
  if (!snapshots?.length) return items;
  let next = [...items];
  for (const snapshot of snapshots) {
    if (!snapshot.call_id || !snapshot.result) continue;
    const itemId = `tool:${snapshot.call_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    const fields = toolPresentationFields(snapshot);
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      if (existing.status === 'completed' || existing.status === 'failed') continue;
      next[index] = {
        ...existing,
        ...fields,
        status: snapshot.status || existing.status,
        streamedOutput: snapshot.result,
        result: snapshot.result,
        isError: snapshot.is_error ?? existing.isError,
        streamChunkCount: snapshot.chunk_count,
        streamContentOffset: snapshot.content_offset,
        sessionId: snapshot.session_id,
        liveOutputAvailable: true,
        timestamp: existing.timestamp ?? snapshot.updated_at ?? new Date().toISOString(),
      };
      continue;
    }
    next.push({
      id: itemId,
      kind: 'tool_call',
      callId: snapshot.call_id,
      toolName: snapshot.tool_name || 'unknown',
      turnId: normalizeEventTurnId(snapshot.turn_id),
      status: snapshot.status || 'started',
      timestamp: snapshot.updated_at ?? new Date().toISOString(),
      streamedOutput: snapshot.result,
      result: snapshot.result,
      isError: snapshot.is_error,
      streamChunkCount: snapshot.chunk_count,
      streamContentOffset: snapshot.content_offset,
      sessionId: snapshot.session_id,
      reconstructed: true,
      ...fields,
      liveOutputAvailable: true,
    });
  }
  return next;
}

export function applyActiveStreamSnapshots(
  items: TimelineItem[],
  snapshots: ActiveStreamSnapshot[] | undefined | null,
): TimelineItem[] {
  if (!snapshots?.length) return items;
  return snapshots.reduce((next, snapshot) => applyActiveStreamSnapshot(next, snapshot), items);
}

export function applyActiveThinkingSnapshots(
  items: TimelineItem[],
  snapshots: ActiveThinkingSnapshot[] | undefined | null,
): TimelineItem[] {
  if (!snapshots?.length) return items;
  let next = [...items];
  for (const snapshot of snapshots) {
    const turnId = normalizeEventTurnId(snapshot.turn_id) ?? snapshot.message_id;
    const blocks = (snapshot.blocks ?? [])
      .filter((block) => typeof block.content === 'string' && block.content.length > 0)
      .map((block) => ({
        block_id: block.block_id,
        title: block.title || 'Thinking',
        content: block.content,
        html: renderMarkdown(block.content),
        source: block.source || 'summary',
        complete: block.complete,
        startedAt: block.started_at ?? null,
        completedAt: block.completed_at ?? null,
        durationMs: typeof block.duration_ms === 'number' ? block.duration_ms : null,
        providerBlockIndex: typeof block.provider_block_index === 'number' ? block.provider_block_index : null,
      }) satisfies ThinkingBlock);
    if (blocks.length === 0) continue;

    const existingIndex = next.findIndex(
      (item) => item.kind === 'thinking'
        && item.turnId === turnId
        && (item as ThinkingTimelineItem).messageId === snapshot.message_id,
    );
    const item: ThinkingTimelineItem = {
      id: `thinking:${turnId}:active`,
      kind: 'thinking',
      messageId: snapshot.message_id,
      turnId,
      blocks,
      streaming: blocks.some((block) => !block.complete),
      activeTitle: [...blocks].reverse().find((block) => !block.complete)?.title ?? null,
      timestamp: snapshot.updated_at ?? new Date().toISOString(),
    };
    if (existingIndex >= 0) {
      next[existingIndex] = item;
    } else {
      insertBeforeOpenPhaseAssistant(next, item, turnId);
    }
  }
  return next;
}

export function normalizeHistory(events: MessageEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolCallIndexByCallId = new Map<string, number>();

  for (const event of events) {
    const content = typeof event.data.content === 'string' ? event.data.content : '';
    const attachments = normalizeEventAttachments(event.data.attachments);
    const turnId = normalizeEventTurnId(event.data.turn_id);
    // Use session_id from event data to build lineage-safe IDs (seq is session-local).
    const sid = eventSessionId(event) ?? '';
    const eid = sid ? `${sid}:${event.seq}` : `${event.seq}`;
    if (event.type === 'user_message') {
      const item = createMessageItem(`event:${eid}:user`, sid || null, 'user', content, event.timestamp, event.seq, undefined, false, attachments, false, turnId);
      item.chatMode = normalizeChatMode(event.data.chat_mode);
      item.chatModeSource = normalizeChatModeSource(event.data.chat_mode_source);
      items.push(item);
      continue;
    }

    if (event.type === 'assistant_message') {
      if (content.trim() || attachments.length > 0) {
        upsertAssistantTurnMessage(items, {
          id: `event:${eid}:assistant`,
          sessionId: sid || null,
          content,
          timestamp: event.timestamp,
          seq: event.seq,
          messageId: turnId ?? undefined,
          attachments,
          turnId,
          chatMode: normalizeChatMode(event.data.chat_mode),
          chatModeSource: normalizeChatModeSource(event.data.chat_mode_source),
          partial: event.data.partial === true,
          finishReason: typeof event.data.finish_reason === 'string' ? event.data.finish_reason : null,
        });
      }
      continue;
    }

    if (event.type === 'system_message') {
      if (!isVisiblePersistedSystemMessage(event.data)) {
        continue;
      }
      const message = content || (typeof event.data.text === 'string' ? event.data.text : '');
      if (message) {
        const noticeId = typeof event.data.notice_id === 'string' ? event.data.notice_id : null;
        const itemId = noticeId ? `system:${noticeId}` : `system:${eid}`;
        const systemMessage = createSystemMessageItem(
          itemId,
          message,
          event.timestamp,
          noticeId,
          typeof event.data.kind === 'string' ? event.data.kind : null,
          typeof event.data.scope === 'string' ? event.data.scope : null
        );
        const existingIdx = items.findIndex(
          (item) => item.id === itemId && item.kind === 'system_message'
        );
        if (existingIdx >= 0) {
          items[existingIdx] = systemMessage;
        } else {
          items.push(systemMessage);
        }
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
        startedAt: typeof event.data.started_at === 'string' ? event.data.started_at : null,
        completedAt: typeof event.data.completed_at === 'string' ? event.data.completed_at : null,
        durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : null,
        providerBlockIndex: typeof event.data.provider_block_index === 'number' ? event.data.provider_block_index : null,
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
        try { args = JSON.parse(event.data.arguments as string); } catch { args = { _raw: event.data.arguments }; }
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
      const status = normalizeDelegationStatus(event.data.status);
      const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
      const existing = existingIdx >= 0 ? (items[existingIdx] as DelegationTimelineItem) : null;
      const fallbackLabel = childSessionId.startsWith('sess_') ? 'Sub-session' : 'Background task';
      const result = status === 'completed'
        ? (delegationPreview(event.data.result_summary) ?? delegationPreview(event.data.result_content))
        : status === 'failed'
          ? (delegationPreview(event.data.error) ?? 'Failed')
          : null;
      const todos = parseTodoSnapshot(event.data.todos);
      const incoming: DelegationTimelineItem = {
        id: itemId,
        kind: 'delegation',
        taskId: childSessionId,
        taskLabel: delegationEventLabel(event.data, fallbackLabel),
        agentId: typeof event.data.agent_id === 'string' ? event.data.agent_id : null,
        usedAgentId: typeof event.data.used_agent_id === 'string' ? event.data.used_agent_id : null,
        status,
        result,
        timestamp: event.timestamp,
        todos: todos.length > 0 ? todos : undefined
      };
      if (existingIdx >= 0) {
        items[existingIdx] = mergeDelegationItem(existing, incoming);
      } else {
        items.push(incoming);
      }
      continue;
    }

    if (event.type === 'tool_result') {
      const callId = String(event.data.call_id ?? '');
      const resultAttachments = normalizeEventAttachments(event.data.attachments);
      const fileDiffs = normalizeFileDiffs(event.data.file_diffs);
      const evaluation =
        typeof event.data.evaluation === 'object' && event.data.evaluation !== null
          ? (event.data.evaluation as ToolCallEvaluation)
          : undefined;
      const index = toolCallIndexByCallId.get(callId);
      if (index !== undefined && items[index]?.kind === 'tool_call') {
        const existing = items[index] as ToolCallTimelineItem;
        items[index] = mergeToolPresentation({
          ...existing,
          status: event.data.is_error ? 'failed' : 'completed',
          result: typeof event.data.result === 'string' ? event.data.result : undefined,
          isError: typeof event.data.is_error === 'boolean' ? event.data.is_error : undefined,
          durationMs: typeof event.data.duration_ms === 'number' ? event.data.duration_ms : undefined,
          evaluation,
          attachments: resultAttachments.length > 0 ? resultAttachments : existing.attachments,
          fileDiffs: fileDiffs.length > 0 ? fileDiffs : existing.fileDiffs,
          turnId: existing.turnId ?? turnId,
        }, event.data);
      } else {
        toolCallIndexByCallId.set(callId || `tc-${eid}`, items.length);
        items.push(mergeToolPresentation({
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
          fileDiffs: fileDiffs.length > 0 ? fileDiffs : undefined,
          reconstructed: true
        }, event.data));
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

    if (event.type === 'task_result') {
      const taskId = String(event.data.task_id ?? eid);
      const itemId = `delegation:${taskId}`;
      const existingIdx = items.findIndex((i) => i.id === itemId && i.kind === 'delegation');
      const delegation: DelegationTimelineItem = {
        id: itemId,
        kind: 'delegation',
        taskId: String(event.data.task_id ?? 'unknown-task'),
        taskLabel: String(event.data.task_title ?? event.data.task_id ?? 'Background task'),
        agentId: typeof event.data.agent_id === 'string' ? event.data.agent_id : null,
        usedAgentId: typeof event.data.used_agent_id === 'string' ? event.data.used_agent_id : null,
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
      const markerRole = typeof event.data.marker_role === 'string' ? event.data.marker_role : null;
      if (
        event.data.timeline_visible === false
        || markerRole === 'context_seed'
        || method === 'rotation'
      ) {
        continue;
      }
      const turnsCompacted = typeof event.data.turns_compacted === 'number' ? event.data.turns_compacted : 0;
      const sessionId = typeof event.data.session_id === 'string' ? event.data.session_id : sid || undefined;
      const sourceSessionId =
        typeof event.data.source_session_id === 'string' ? event.data.source_session_id : undefined;
      const previousUsagePercentage =
        typeof event.data.previous_usage_percentage === 'number'
          ? event.data.previous_usage_percentage
          : null;
      const effectiveUsagePercentage =
        typeof event.data.effective_usage_percentage === 'number'
          ? event.data.effective_usage_percentage
          : null;
      items.push({
        id: sessionId && sourceSessionId ? `compaction:${sourceSessionId}:${sessionId}` : `compaction:${eid}`,
        kind: 'compaction',
        status: 'compacted',
        sessionId,
        previousSessionId: sourceSessionId,
        summaryPreview: summary.slice(0, 500),
        summary,
        method,
        turnsCompacted,
        trigger: typeof event.data.trigger === 'string' ? event.data.trigger : undefined,
        reason: typeof event.data.reason === 'string' ? event.data.reason : undefined,
        previousUsagePercentage,
        effectiveUsagePercentage,
        hardPressureExceeded: event.data.hard_pressure_exceeded === true,
        usedTimeoutFallback: event.data.used_timeout_fallback === true,
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
        agentId: typeof event.data.agent_id === 'string' ? event.data.agent_id : null,
        usedAgentId: typeof event.data.used_agent_id === 'string' ? event.data.used_agent_id : null,
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
          agentId: typeof event.data.agent_id === 'string' ? event.data.agent_id : null,
          usedAgentId: typeof event.data.used_agent_id === 'string' ? event.data.used_agent_id : null,
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
          const noticeId = typeof event.data?.notice_id === 'string' ? event.data.notice_id : null;
          const itemId = noticeId ? `system:${noticeId}` : `system:${eid}`;
          const systemMessage = createSystemMessageItem(
            itemId,
            message,
            event.timestamp,
            noticeId,
            typeof event.data?.kind === 'string' ? event.data.kind : null,
            typeof event.data?.scope === 'string' ? event.data.scope : null
          );
          const existingIdx = items.findIndex(
            (item) => item.id === itemId && item.kind === 'system_message'
          );
          if (existingIdx >= 0) {
            items[existingIdx] = systemMessage;
          } else {
            items.push(systemMessage);
          }
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

export function appendOptimisticUserMessage(
  items: TimelineItem[],
  content: string,
  attachments: AttachmentRef[] = [],
  clientMessageId: string | null = null,
): TimelineItem[] {
  const localId = clientMessageId
    ? `local-user:${clientMessageId}`
    : `local-user:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
  return [
    ...items,
    createMessageItem(
      localId,
      null,
      'user',
      content,
      new Date().toISOString(),
      null,
      undefined,
      false,
      attachments,
      true,
      undefined,
      undefined,
      clientMessageId
    )
  ];
}

export function removeQueuedOptimisticUserMessage(
  items: TimelineItem[],
  queueId?: string | null,
  clientMessageId?: string | null,
  content?: string,
  attachments: AttachmentRef[] = [],
): TimelineItem[] {
  const correlatedIndex = findUserMessageByCorrelationIndex(items, clientMessageId, queueId);
  const fallbackIndex = correlatedIndex >= 0
    ? correlatedIndex
    : findOptimisticUserMessageIndex(items, content ?? '', attachments);
  if (fallbackIndex < 0) return items;
  const item = items[fallbackIndex];
  if (item?.kind !== 'message' || item.role !== 'user' || !item.optimistic) return items;
  return items.filter((_, index) => index !== fallbackIndex);
}

/** Tool status values we treat as "still running, waiting for resolution". */
const PENDING_TOOL_STATUSES = new Set(['started', 'running', 'paused']);

/**
 * Canonical match for the `step_request_questions` controller tool. The backend
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
  if (name === 'steprequestquestions' || name === 'requestauthchallenge') return true;
  if ((name === 'browserfill' || name === 'browsereval') && hasDeferredAuthChallenge(tool.arguments)) return true;
  return false;
}

/**
 * Return the most recent `step_request_questions` tool call that has not yet
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
 * Attach a notification ID to the latest unresolved `step_request_questions`
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
 * Optimistically mark a `step_request_questions` tool call as resolved with the
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
      result: JSON.stringify(
        normalizedToolName(tool.toolName) === 'steprequestquestions'
          ? { mode: 'plain_text', answers: [{ question_id: 'q1', selected_option_ids: [], custom_answer: response }] }
          : { response: '<redacted>' },
      ),
    } satisfies ToolCallTimelineItem;
  });
}

export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  let next = [...items];

  if (event.type === 'user_message') {
    const attachments = normalizeEventAttachments(event.attachments);
    const turnId = normalizeEventTurnId(event.turn_id);
    const stableItemId = userMessageItemId(event);
    const correlatedIndex = findUserMessageByCorrelationIndex(next, event.client_message_id, event.queue_id);
    const optimisticIndex = correlatedIndex >= 0 ? correlatedIndex : findOptimisticUserMessageIndex(next, event.content, attachments);
    if (optimisticIndex >= 0 && next[optimisticIndex]?.kind === 'message') {
      const existing = next[optimisticIndex] as MessageTimelineItem;
      const chatMode = normalizeChatMode(event.chat_mode);
      const chatModeSource = normalizeChatModeSource(event.chat_mode_source);
      next[optimisticIndex] = {
        ...existing,
        id: stableItemId,
        content: event.content,
        html: renderMarkdown(event.content),
        seq: typeof event.seq === 'number' ? event.seq : existing.seq,
        timestamp: event.timestamp ?? existing.timestamp,
        attachments,
        turnId,
        messageId: event.message_id ?? event.event_id ?? existing.messageId,
        chatMode,
        chatModeSource,
        clientMessageId: event.client_message_id ?? existing.clientMessageId,
        queueId: event.queue_id ?? existing.queueId,
        optimistic: false
      };
      return applyChatModeToTurnMessages(next, turnId, chatMode, chatModeSource);
    }
    if (next.some((item) => item.kind === 'message' && item.role === 'user' && item.id === stableItemId)) {
      return next;
    }
    const item = createMessageItem(
      stableItemId,
      event.session_id ?? null,
      'user',
      event.content,
      event.timestamp ?? new Date().toISOString(),
      typeof event.seq === 'number' ? event.seq : null,
      event.message_id ?? event.event_id ?? undefined,
      false,
      attachments,
      false,
      turnId,
      undefined,
      event.client_message_id ?? null,
      event.queue_id ?? null
    );
    const chatMode = normalizeChatMode(event.chat_mode);
    const chatModeSource = normalizeChatModeSource(event.chat_mode_source);
    item.chatMode = chatMode;
    item.chatModeSource = chatModeSource;
    next.push(item);
    return applyChatModeToTurnMessages(next, turnId, chatMode, chatModeSource);
  }

  if (event.type === 'chunk') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const turnMode = chatModeForTurn(next, turnId);
    const eventChatMode = normalizeChatMode(event.chat_mode) ?? turnMode.chatMode;
    const eventChatModeSource = normalizeChatModeSource(event.chat_mode_source) ?? turnMode.chatModeSource;
    const chunkIndex = typeof event.index === 'number' ? event.index : null;
    const contentOffset = typeof event.content_offset === 'number' ? event.content_offset : null;
    const index = findMergeableAssistantIndex(next, turnId, event.message_id);
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
        chatMode: withBuffered.chatMode ?? eventChatMode,
        chatModeSource: withBuffered.chatModeSource ?? eventChatModeSource,
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
      event.session_id ?? null,
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
    item.chatMode = eventChatMode;
    item.chatModeSource = eventChatModeSource;
    next.push(applyBufferedChunksToMessage(item, turnId));
    return next;
  }

  if (event.type === 'assistant_stream_snapshot') {
    return applyActiveStreamSnapshot(next, event);
  }

  if (event.type === 'message_complete') {
    const turnId = normalizeEventTurnId(event.turn_id) ?? event.message_id;
    const turnMode = chatModeForTurn(next, turnId);
    const eventChatMode = normalizeChatMode(event.chat_mode) ?? turnMode.chatMode;
    const eventChatModeSource = normalizeChatModeSource(event.chat_mode_source) ?? turnMode.chatModeSource;
    const itemId = `message:${event.message_id}:${next.length}`;
    const index = findMergeableAssistantIndex(next, turnId, event.message_id);
    const attachments = normalizeEventAttachments(event.attachments);
    const finalContent = typeof event.content === 'string' ? event.content : null;
    if (index >= 0) {
      const message = next[index] as MessageTimelineItem;
      const completeContent = reconcileCompletedAssistantContent(message.content, finalContent);
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
        chatMode: eventChatMode ?? message.chatMode,
        chatModeSource: eventChatModeSource ?? message.chatModeSource,
        partial: event.partial === true || message.partial,
        finishReason: event.finish_reason ?? message.finishReason ?? null,
        streamChunkCount: undefined,
        streamContentOffset: undefined,
      };
      return next;
    }

    if (finalContent || attachments.length > 0) {
      clearPendingChunks(event.message_id, turnId);
      upsertAssistantTurnMessage(next, {
        id: itemId,
        sessionId: event.session_id ?? null,
        content: finalContent ?? '',
        timestamp: new Date().toISOString(),
        seq: event.seq,
        messageId: event.message_id,
        attachments,
        turnId,
        chatMode: eventChatMode,
        chatModeSource: eventChatModeSource,
        partial: event.partial === true,
        finishReason: event.finish_reason ?? null,
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
            source: event.source ?? 'summary',
            complete: false,
            startedAt: event.started_at ?? null,
            completedAt: event.completed_at ?? null,
            durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
            providerBlockIndex: typeof event.provider_block_index === 'number' ? event.provider_block_index : null,
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
        source: event.source ?? 'summary',
        complete: false,
        startedAt: event.started_at ?? null,
        completedAt: event.completed_at ?? null,
        durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
        providerBlockIndex: typeof event.provider_block_index === 'number' ? event.provider_block_index : null,
      } satisfies ThinkingBlock;
      const contiguousIndex = findOpenPhaseThinkingIndex(next, turnId);
      if (contiguousIndex >= 0) {
        upsertThinkingBlockInSegment(
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
        source: event.source ?? 'summary',
        complete: true,
        startedAt: event.started_at ?? null,
        completedAt: event.completed_at ?? null,
        durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
        providerBlockIndex: typeof event.provider_block_index === 'number' ? event.provider_block_index : null,
      };
      if (index >= 0) {
        const existing = next[index] as ThinkingTimelineItem;
        const blockIdx = existing.blocks.findIndex((b) => b.block_id === blockId);
        const blocks = blockIdx >= 0
          ? existing.blocks.map((b, i) => (i === blockIdx ? mergeThinkingBlock(b, block) : b))
          : hasDuplicateThinkingBlock(existing, block)
            ? existing.blocks
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
          upsertThinkingBlockInSegment(next, contiguousIndex, block, turnId, new Date().toISOString(), false, null);
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
          startedAt: event.started_at ?? block.startedAt,
          completedAt: event.completed_at ?? block.completedAt,
          durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : block.durationMs,
          providerBlockIndex: typeof event.provider_block_index === 'number' ? event.provider_block_index : block.providerBlockIndex,
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
        source: event.source ?? 'summary',
        complete: true,
        startedAt: event.started_at ?? null,
        completedAt: event.completed_at ?? null,
        durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
        providerBlockIndex: typeof event.provider_block_index === 'number' ? event.provider_block_index : null,
      };
      const contiguousIndex = findOpenPhaseThinkingIndex(next, turnId);
      if (contiguousIndex >= 0) {
        upsertThinkingBlockInSegment(next, contiguousIndex, fallbackBlock, turnId, new Date().toISOString(), false, null);
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
    const key = toolEventKey({
      conversationId: event.conversation_id,
      sessionId: event.session_id,
      turnId,
      callId: event.call_id,
    });
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
      next[index] = applyPendingToolResult(key, applyBufferedToolOutputChunks(key, {
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
      }));
      return next;
    }
    next.push(applyPendingToolResult(key, applyBufferedToolOutputChunks(key, toolItem)));
    return next;
  }

  if (event.type === 'tool_progress') {
    const rawEvent = event as typeof event & {
      progress?: {
        phase?: string;
        input_chars?: number;
        input_lines?: number;
        complete?: boolean;
      };
      timestamp?: string | null;
    };
    const turnId = normalizeEventTurnId(event.turn_id);
    next = finalizeOpenPhaseAssistant(next, turnId);
    const callId = rawEvent.call_id ?? '';
    if (!callId) return next;
    const itemId = `tool:${callId}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    const progress = rawEvent.progress ?? {};
    const patch = {
      progressPhase: typeof progress.phase === 'string' ? progress.phase : 'preparing_input',
      progressInputChars: typeof progress.input_chars === 'number' ? progress.input_chars : undefined,
      progressInputLines: typeof progress.input_lines === 'number' ? progress.input_lines : undefined,
      progressComplete: typeof progress.complete === 'boolean' ? progress.complete : undefined,
    };
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        ...patch,
        status: existing.status === 'completed' || existing.status === 'failed' ? existing.status : 'started',
        turnId: existing.turnId ?? turnId,
      };
      return next;
    }
    next.push({
      id: itemId,
      kind: 'tool_call',
      callId,
      toolName: rawEvent.tool_name ?? 'unknown',
      turnId,
      status: 'started',
      timestamp: rawEvent.timestamp ?? new Date().toISOString(),
      ...patch,
    });
    return next;
  }

  if (event.type === 'tool_result') {
    const turnId = normalizeEventTurnId(event.turn_id);
    const key = toolEventKey({
      conversationId: event.conversation_id,
      sessionId: event.session_id,
      turnId,
      callId: event.call_id,
    });
    const itemId = `tool:${event.call_id}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = applyToolResultEvent(existing, event);
      return next;
    }
    bufferPendingToolResult(key, event);
    return next;
  }

  if (event.type === 'tool_result_chunk' || event.type === 'tool_output_chunk') {
    const rawEvent = event as ToolOutputChunkEvent;
    const callId = rawEvent.call_id ?? '';
    const turnId = normalizeEventTurnId(event.turn_id);
    const key = toolEventKey({
      conversationId: 'conversation_id' in rawEvent ? rawEvent.conversation_id : null,
      sessionId: rawEvent.session_id,
      turnId,
      callId,
    });
    const itemId = `tool:${callId}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'tool_call');
    const delta = rawEvent.delta ?? rawEvent.content ?? rawEvent.text ?? '';
    const isErrorChunk = rawEvent.is_error ?? rawEvent.stream === 'stderr';
    if (index >= 0) {
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = appendToolOutputChunk(existing, {
        delta,
        isError: isErrorChunk,
        chunkIndex: rawEvent.chunk_index,
        contentOffset: rawEvent.content_offset,
        sessionId: rawEvent.session_id,
      });
      return next;
    }
    bufferPendingToolOutputChunk(key, {
      delta,
      isError: isErrorChunk,
      chunkIndex: rawEvent.chunk_index,
      contentOffset: rawEvent.content_offset,
      sessionId: rawEvent.session_id,
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
    const existing = index >= 0 ? (next[index] as DelegationTimelineItem) : null;
    const progressText =
      event.type === 'delegation_progress' && 'progress' in event && typeof event.progress === 'string'
        ? event.progress
        : null;
    const delegation: DelegationTimelineItem = {
      id: itemId,
      kind: 'delegation',
      taskId,
      taskLabel: delegationTaskLabel('task' in event ? event.task : null),
      agentId: 'agent_id' in event && typeof event.agent_id === 'string' ? event.agent_id : null,
      usedAgentId: 'used_agent_id' in event && typeof event.used_agent_id === 'string' ? event.used_agent_id : null,
      status: event.type === 'delegation_started' ? 'started' : 'running',
      result: progressText,
      timestamp: new Date().toISOString(),
      toolCallCount: 'tool_call_count' in event ? (event as typeof event & { tool_call_count?: number }).tool_call_count : undefined,
      maxToolCalls: 'max_tool_calls' in event ? (event as typeof event & { max_tool_calls?: number }).max_tool_calls : undefined,
      lastTool: 'last_tool' in event ? (event as typeof event & { last_tool?: string }).last_tool : undefined,
      todos: parseTodoSnapshot('todos' in event ? event.todos : undefined),
    };
    if (index >= 0) {
      next[index] = mergeDelegationItem(existing, delegation);
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
      taskLabel: delegationTaskLabel('task' in event ? event.task : null, existing?.taskLabel ?? 'Background task'),
      agentId: 'agent_id' in event && typeof event.agent_id === 'string' ? event.agent_id : null,
      usedAgentId: 'used_agent_id' in event && typeof event.used_agent_id === 'string' ? event.used_agent_id : null,
      status,
      result: delegationPreview(result),
      timestamp: new Date().toISOString(),
      todos: parseTodoSnapshot('todos' in event ? event.todos : undefined),
    };
    if (index >= 0) {
      next[index] = mergeDelegationItem(existing, delegation);
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
      agentId: existing?.agentId ?? null,
      usedAgentId: existing?.usedAgentId ?? null,
      status,
      result: delegationPreview(result),
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

  if (event.type === 'session_compaction_started') {
    const itemId = `compaction:running:${event.session_id}`;
    const item: CompactionTimelineItem = {
      id: itemId,
      kind: 'compaction',
      status: 'running',
      sessionId: event.session_id,
      previousSessionId: event.session_id,
      summaryPreview: '',
      method: 'pending',
      turnsCompacted: 0,
      trigger: event.trigger ?? undefined,
      reason: event.reason ?? undefined,
      previousUsagePercentage: event.previous_usage_percentage ?? null,
      effectiveUsagePercentage: event.effective_usage_percentage ?? null,
      hardPressureExceeded: event.hard_pressure_exceeded === true,
      usedTimeoutFallback: event.used_timeout_fallback === true,
      timestamp: new Date().toISOString()
    };
    const index = next.findIndex((existing) => existing.id === itemId && existing.kind === 'compaction');
    if (index >= 0) {
      next[index] = item;
      return next;
    }
    next.push(item);
    return next;
  }

  if (event.type === 'session_compaction_finished') {
    const runningId = `compaction:running:${event.session_id}`;
    return next.filter((existing) => existing.id !== runningId);
  }

  if (event.type === 'session_compacted') {
    const itemId = `compaction:${event.previous_session_id}:${event.session_id}`;
    const runningId = `compaction:running:${event.previous_session_id}`;
    const item: CompactionTimelineItem = {
      id: itemId,
      kind: 'compaction',
      status: 'compacted',
      sessionId: event.session_id,
      previousSessionId: event.previous_session_id,
      summaryPreview: event.summary_preview?.slice(0, 500) ?? '',
      summary: event.summary_preview ?? '',
      method: event.method ?? 'unknown',
      turnsCompacted: event.turns_compacted ?? 0,
      trigger: event.trigger,
      reason: event.reason,
      previousUsagePercentage: event.previous_usage_percentage ?? null,
      effectiveUsagePercentage: event.effective_usage_percentage ?? null,
      hardPressureExceeded: event.hard_pressure_exceeded === true,
      usedTimeoutFallback: event.used_timeout_fallback === true,
      timestamp: new Date().toISOString()
    };
    const runningIndex = next.findIndex((existing) => existing.id === runningId && existing.kind === 'compaction');
    if (runningIndex >= 0) {
      next[runningIndex] = item;
      return next;
    }
    const existingIndex = next.findIndex((existing) => existing.id === itemId && existing.kind === 'compaction');
    if (existingIndex >= 0) {
      next[existingIndex] = item;
      return next;
    }
    next.push(item);
    return next;
  }

  if (event.type === 'system_message') {
    const itemId =
      typeof event.notice_id === 'string' && event.notice_id.length > 0
        ? `sysmsg:${event.notice_id}`
        : typeof event.seq === 'number'
        ? `sysmsg:${event.seq}`
        : `sysmsg:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
    const index = next.findIndex((item) => item.id === itemId && item.kind === 'system_message');
    const systemMessage = {
      id: itemId,
      kind: 'system_message' as const,
      text: event.text,
      noticeId: event.notice_id ?? null,
      noticeKind: event.kind ?? null,
      noticeScope: event.scope ?? null,
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
            ? event.questions?.[0]?.question?.trim() || 'Conversation paused until you answer the clarification request.'
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
