import { createMarkdownStreamer, renderMarkdown, stripMarkdown, type MarkdownStreamer } from '$lib/markdown';
import { normalizeFileDiffs, type FileDiff } from '$lib/diff';
import { parseTodoSnapshot, type TodoSnapshotItem } from '$lib/todos';
import type { OptimisticUserMessageDraft } from '$lib/interactive-drafts';
import type { AttachmentRef, ChatMode, ChatModeSource, CognisWebSocketEvent, MessageEvent, MessageRuntimeMetadata, QueuedMessage, QuestionSetReply, TimelineProjectionItem, ToolOutputPresentationMetadata } from '$lib/types/api';

// ---------------------------------------------------------------------------
// Timeline debug logging — flag-gated, silent by default.
// Enable in browser console: localStorage.setItem('cognis_timeline_debug','1')
// Disable:                   localStorage.removeItem('cognis_timeline_debug')
// ---------------------------------------------------------------------------
function _timelineDebugEnabled(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('cognis_timeline_debug') === '1';
  } catch {
    return false;
  }
}
function _dbg(...args: unknown[]): void {
  if (_timelineDebugEnabled()) console.debug('[timeline]', ...args);
}
function _itemSummary(item: unknown): string {
  if (!item || typeof item !== 'object') return 'null';
  const i = item as Record<string, unknown>;
  const kind = String(i['kind'] ?? '');
  if (kind === 'tool_call') {
    return `{id:${i['id']} kind:tool_call callId:${i['callId'] ?? '-'} tool:${i['toolName'] ?? '-'} status:${i['status'] ?? '-'} hasArgs:${i['arguments'] != null} hasEval:${i['evaluation'] != null} key:${String(i['orderKey'] ?? '')}}`;
  }
  return `{id:${i['id']} kind:${kind} role:${i['role'] ?? '-'} msgId:${i['messageId'] ?? '-'} turnId:${i['turnId'] ?? '-'} phase:${i['assistantPhaseIndex'] ?? '-'} key:${String(i['orderKey'] ?? '')} streaming:${i['streaming'] ?? '-'}}`;
}

/**
 * Per-message markdown streamers. Streaming assistant replies accumulate
 * tokens chunk-by-chunk; rather than re-parse the whole content on every
 * chunk we give each message its own streamer so only the tail block is
 * re-parsed while earlier blocks stay memoized. See createMarkdownStreamer().
 */
const streamers = new Map<string, MarkdownStreamer>();
/** Per-thinking-block markdown streamers (keyed by block_id). */
const thinkingStreamers = new Map<string, MarkdownStreamer>();

/**
 * Pending escalation evaluations keyed by the real tool call id (tc.call_id /
 * tool_call_id). When an escalation event arrives before the on_tool_call
 * timeline_patch, we store the evaluation here and apply it when the real
 * tool_call item lands. This prevents the escalation from synthesizing a ghost
 * tool_call item that duplicates the real one.
 *
 * Entries are consumed (deleted) when the tool_call item is created or updated.
 * The map is bounded by the number of concurrent escalations (typically 1).
 */
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

function utf16CodeUnits(value: string): number {
  return value.length;
}

function mergeToolPresentation(
  item: ToolCallTimelineItem,
  presentation: ToolOutputPresentationMetadata | null | undefined,
): ToolCallTimelineItem {
  if (!presentation) return item;
  return {
    ...item,
    liveOutputAvailable: item.liveOutputAvailable || presentation.live_output_available || false,
    anchorsAvailable: item.anchorsAvailable || presentation.anchors_available || false,
    anchorCount: typeof presentation.anchor_count === 'number' ? presentation.anchor_count : item.anchorCount,
    outputSize: typeof presentation.output_size === 'number' ? presentation.output_size : item.outputSize,
  };
}

function releaseStreamer(messageId: string): void {
  streamers.delete(messageId);
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
  sessionId?: string | null;
  /** Transport message id for the thinking turn */
  messageId: string;
  turnId?: string | null;
  /** Ordered list of contiguous thinking blocks in this segment */
  blocks: ThinkingBlock[];
  /** Stable assistant phase number this thinking segment belongs to. */
  assistantPhaseIndex?: number;
  /** Zero-based model invocation index within the user turn. */
  turnCycleIndex?: number;
  /** True while at least one block is still streaming */
  streaming: boolean;
  /** Derived: title of the most recently active (incomplete) block */
  activeTitle: string | null;
  timestamp: string | null;
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
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
  deliveryStatus?: 'sending' | 'queued' | 'failed';
  clientMessageId?: string | null;
  queueId?: string | null;
  streamChunkCount?: number;
  streamContentOffset?: number;
  chatMode?: ChatMode;
  chatModeSource?: ChatModeSource;
  partial?: boolean;
  finishReason?: string | null;
  runtime?: MessageRuntimeMetadata | null;
  /**
   * Stable assistant phase number within a turn. One assistant turn can emit
   * multiple user-visible assistant messages separated by tool calls; message
   * ids and turn ids alone are not enough to distinguish those phases.
   */
  assistantPhaseIndex?: number;
  /** Zero-based model invocation index within the user turn. */
  turnCycleIndex?: number;
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
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
  displayToolName?: string;
  canonicalToolName?: string;
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
  runtimeChunkOnly?: boolean;
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
  /**
   * Delegation details folded onto a delegate/fork tool call so it renders as a
   * single rich tool call (title/progress/todos/result) instead of a separate
   * delegation card.
   */
  delegation?: DelegationRuntime | null;
  /** Assistant phase closed by this tool boundary. */
  assistantPhaseIndex?: number;
  /** Zero-based model invocation index within the user turn. */
  turnCycleIndex?: number;
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
}

export interface DelegationRuntime {
  childSessionId?: string | null;
  status?: string | null;
  agentId?: string | null;
  usedAgentId?: string | null;
  title?: string | null;
  summary?: string | null;
  startedAt?: string | null;
  durationMs?: number | null;
  resultSummary?: string | null;
  resultContent?: string | null;
  resultSource?: string | null;
  resultTruncated?: boolean | null;
  resultAnchors?: unknown;
  todos?: TodoSnapshotItem[] | null;
  toolCallCount?: number | null;
  maxToolCalls?: number | null;
  lastTool?: string | null;
  error?: string | null;
}

export { parseTodoSnapshot, type TodoSnapshotItem };

function normalizeToolName(name: string): string {
  return name.toLowerCase().replace(/_/g, '');
}

function isUserMessageReasoningDiagnostic(
  source: string | null | undefined,
  title: string | null | undefined,
  content: string | null | undefined,
): boolean {
  return (source ?? '') === 'reasoning'
    && (title ?? 'Reasoning') === 'Reasoning'
    && (content ?? '').trimStart().startsWith('User message');
}

export function isTerminalToolStatus(status: string | null | undefined): boolean {
  return ['completed', 'failed', 'cancelled'].includes(status ?? '');
}

export function isActiveToolStatus(status: string | null | undefined): boolean {
  return !isTerminalToolStatus(status);
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
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
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
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
}

export interface NoticeTimelineItem {
  id: string;
  kind: 'notice';
  title: string;
  description: string;
  tone: 'info' | 'warning' | 'error';
  timestamp: string | null;
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
}

export interface SystemMessageTimelineItem {
  id: string;
  kind: 'system_message';
  text: string;
  noticeId?: string | null;
  noticeKind?: string | null;
  noticeScope?: string | null;
  followUpConversationId?: string | null;
  followUpSessionId?: string | null;
  timestamp: string | null;
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
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
  /** Backend-assigned stable sort key. Lexicographically comparable. */
  orderKey?: string;
}

function createSystemMessageItem(
  id: string,
  text: string,
  timestamp: string | null,
  noticeId: string | null = null,
  noticeKind: string | null = null,
  noticeScope: string | null = null,
  followUpConversationId: string | null = null,
  followUpSessionId: string | null = null,
): SystemMessageTimelineItem {
  return {
    id,
    kind: 'system_message',
    text,
    noticeId,
    noticeKind,
    noticeScope,
    followUpConversationId,
    followUpSessionId,
    timestamp,
    orderKey: mintClientOrderKey('system_message'),
  };
}

function formatTokenCount(value: unknown): string | null {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.round(value).toLocaleString('en-US')
    : null;
}

function createToolCallContextPressureNotice(
  id: string,
  data: Record<string, unknown>,
  timestamp: string | null,
): NoticeTimelineItem {
  const promptTokens = formatTokenCount(data.prompt_tokens);
  const availablePromptTokens = formatTokenCount(data.available_prompt_tokens);
  const thresholdPromptTokens = formatTokenCount(data.loop_pressure_threshold_prompt_tokens);
  const toolCallCount = formatTokenCount(data.tool_call_count);
  const stepName = typeof data.step_name === 'string' && data.step_name ? data.step_name : null;
  const parts = [
    'Context window is critically full; this turn was stopped before more tool calls.',
    promptTokens && availablePromptTokens
      ? `Usage is ${promptTokens}/${availablePromptTokens} prompt-budget tokens.`
      : null,
    thresholdPromptTokens ? `Threshold is ${thresholdPromptTokens}.` : null,
    toolCallCount ? `Tool calls this turn: ${toolCallCount}.` : null,
    stepName ? `Step: ${stepName}.` : null,
  ].filter((part): part is string => Boolean(part));

  return {
    id,
    kind: 'notice',
    title: 'Tool-call context pressure',
    description: parts.join(' '),
    tone: 'warning',
    timestamp,
    orderKey: mintClientOrderKey('notice'),
  };
}

function isVisiblePersistedSystemMessage(data: Record<string, unknown>): boolean {
  if (isTransientCompactionStartSystemMessage(data)) return false;

  const noticeId = data.notice_id;
  if (typeof noticeId === 'string' && noticeId.length > 0) return true;

  const kind = data.kind;
  if (kind === 'turn_initiated') return true;

  const event = data.event;
  if (event === 'turn_initiated') return true;

  return false;
}

function systemNoticeText(data: Record<string, unknown>): string {
  for (const key of ['message', 'text', 'content']) {
    const value = data[key];
    if (typeof value === 'string') return value.trim();
  }
  return '';
}

function isTransientCompactionStartSystemMessage(data: Record<string, unknown>): boolean {
  if (data.kind === 'compaction_start' || data.noticeKind === 'compaction_start') return true;

  const text = systemNoticeText(data).toLowerCase();
  if (text.startsWith('automatic compaction is starting before this turn continues')) return true;

  return text.startsWith('the model provider rejected the request because the context window is full')
    && text.includes('compacting the saved conversation')
    && (data.status === 'started' || data.status === 'running');
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
  assistantPhaseIndex?: number,
): MessageTimelineItem {
  const displayContent = role === 'assistant' ? normalizeRepeatedAssistantContent(content) : content;
  return {
    id,
    kind: 'message',
    sessionId,
    role,
    content,
    html: renderMessageHtml({
      id,
      sessionId,
      role,
      content: displayContent,
      turnId,
      messageId,
      assistantPhaseIndex,
      streaming,
    }),
    seq,
    timestamp,
    turnId,
    messageId,
    streaming,
    attachments,
    optimistic,
    deliveryStatus: optimistic ? 'sending' : undefined,
    clientMessageId,
    queueId,
    streamChunkCount,
    streamContentOffset: content.length,
    assistantPhaseIndex,
  };
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function normalizeMessageRuntime(value: unknown): MessageRuntimeMetadata | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const runtime: MessageRuntimeMetadata = {
    agent_id: stringOrNull(raw.agent_id) ?? undefined,
    agent_name: stringOrNull(raw.agent_name) ?? undefined,
    agent_display_name: stringOrNull(raw.agent_display_name),
    requested_agent_profile_id: stringOrNull(raw.requested_agent_profile_id),
    agent_profile_id: stringOrNull(raw.agent_profile_id),
    agent_profile_source: stringOrNull(raw.agent_profile_source),
    agent_profile_synthetic: typeof raw.agent_profile_synthetic === 'boolean' ? raw.agent_profile_synthetic : null,
    provider_id: stringOrNull(raw.provider_id),
    model: stringOrNull(raw.model),
    reasoning_effort: stringOrNull(raw.reasoning_effort),
  };
  return Object.values(runtime).some((item) => item !== null && item !== undefined) ? runtime : null;
}

function nextAssistantPhaseIndex(items: TimelineItem[], turnId: string | null): number | undefined {
  if (!turnId) return undefined;
  let highestExplicitPhase = -1;
  let matchingAssistantMessages = 0;
  for (const item of items) {
    if (item.kind !== 'message' || item.role !== 'assistant' || item.turnId !== turnId) continue;
    matchingAssistantMessages += 1;
    if (typeof item.assistantPhaseIndex === 'number') {
      highestExplicitPhase = Math.max(highestExplicitPhase, item.assistantPhaseIndex);
    }
  }
  return highestExplicitPhase >= 0 ? highestExplicitPhase + 1 : matchingAssistantMessages;
}

/**
 * The lexicographically largest possible orderKey value.  Used as a sentinel
 * for items that have no backend-assigned key (local optimistic rows, items
 * from paths that do not yet emit orderKey) so they sort after all real items.
 * Also used by mergeOrderKey to treat absent/empty orderKey as "largest".
 */
const _ORDER_KEY_MAX = '\uffff\uffff\uffff\uffff';

// ---------------------------------------------------------------------------
// Client-side orderKey minting — mirrors the backend _encode_order_key format
//
// Format: {lineage:04d}:{seq:015d}:{phase:06d}:{kind_rank:02d}:{local:09d}
//
// Client-minted items use the active-session lineage band (9998).
//
// IMPORTANT: within lineage 9998 there are two sub-bands:
//   - Persisted active-session events: seq = real small integer (e.g. 42)
//   - Server runtime/streaming items:  seq = 999999999999999 (CLIENT_NO_SEQ,
//     the "no real seq yet" sentinel — sorts LAST within the lineage band)
//
// A newly-sent user message must sort AFTER all in-flight runtime items so it
// appears below the current assistant turn's tool calls. Using a fixed counter
// as seq (counter ~1e9) placed it BEFORE runtime items (seq ~1e15), causing
// the message to appear above the assistant's tool calls until a server refresh
// corrected the position.
//
// The fix: mintClientOrderKey() for non-positional items (notices, system
// messages) still uses the counter-as-seq approach (correct for those).
// For user messages (appendOptimisticUserMessage), use mintTailOrderKey()
// which computes a key strictly after the current timeline maximum.
// ---------------------------------------------------------------------------

/** Must match backend _ORDER_KEY_ACTIVE_LINEAGE = 9998 */
const CLIENT_ACTIVE_LINEAGE = 9998;
/** Must match backend _ORDER_KEY_NO_SEQ = 10**15 - 1 = 999999999999999 */
const CLIENT_NO_SEQ = 999999999999999;

/** Must match backend _KIND_RANK */
const CLIENT_KIND_RANK: Record<string, number> = {
  'message:user': 0,
  'thinking': 1,
  'message:assistant': 2,
  'tool_call': 3,
  'delegation': 4,
  'compaction': 5,
  'system_message': 6,
  'notice': 7,
  'workflow_composed': 8,
};

/**
 * Monotonically increasing counter for client-minted items.
 * Used by mintClientOrderKey() for non-positional items (notices, etc.).
 */
let _clientOrderKeyCounter = 1_000_000_000;

/**
 * Mint a stable orderKey for a client-originated non-positional timeline item
 * (notices, system messages, compaction markers, etc.).
 *
 * Uses the active-session lineage band (9998) with the monotonic counter as
 * the seq component. Counter starts at 1e9 so these items sort after persisted
 * active-session events (small seq) but before runtime sentinel items (seq 1e15).
 * This is correct for notices/system messages which are not user turns.
 *
 * For user messages use mintTailOrderKey() instead — see below.
 *
 * @param kindKey - key into CLIENT_KIND_RANK, e.g. 'notice', 'system_message'
 */
function mintClientOrderKey(kindKey: string): string {
  const counter = ++_clientOrderKeyCounter;
  const kindRank = CLIENT_KIND_RANK[kindKey] ?? 9;
  const li = String(CLIENT_ACTIVE_LINEAGE).padStart(4, '0');
  const s = String(counter % (CLIENT_NO_SEQ + 1)).padStart(15, '0');
  const p = '000000';
  const kr = String(kindRank).padStart(2, '0');
  const lo = '000000000';
  return `${li}:${s}:${p}:${kr}:${lo}`;
}

/**
 * Mint an orderKey for a new user message that is guaranteed to sort AFTER
 * all items currently in the timeline, including in-flight runtime/streaming
 * items that carry the sentinel seq (999999999999999).
 *
 * The problem with mintClientOrderKey() for user messages: it uses a counter
 * (~1e9) as the seq component, which sorts BEFORE runtime items at seq ~1e15.
 * This caused a just-sent user message to appear above the assistant's in-flight
 * tool calls until a server refresh corrected the position.
 *
 * This function finds the lexicographically largest orderKey in the current
 * timeline and produces a key strictly greater by incrementing the local
 * component (last field). If the max key is already at the sentinel seq band,
 * we increment local; if the timeline is empty we fall back to mintClientOrderKey.
 *
 * The server's real key (assigned on echo/persist) will be smaller (real seq)
 * and will replace this via mergeOrderKey, landing the message in its canonical
 * position. The tail key is only needed for the brief optimistic window.
 *
 * @param items - current timeline items (used to find the current max key)
 * @param kindKey - key into CLIENT_KIND_RANK, typically 'message:user'
 */
function mintTailOrderKey(items: TimelineItem[], kindKey: string): string {
  const kindRank = CLIENT_KIND_RANK[kindKey] ?? 9;
  // Find the lexicographically largest orderKey in the current timeline.
  // Items without an orderKey are treated as sorting at _ORDER_KEY_MAX
  // (they will be placed last by sortByOrderKey), so we must produce a key
  // that sorts after them too.
  let maxKey = '';
  let hasUnkeyed = false;
  for (const item of items) {
    if (!item.orderKey) {
      hasUnkeyed = true;
    } else if (item.orderKey > maxKey) {
      maxKey = item.orderKey;
    }
  }
  if (!maxKey && !hasUnkeyed) {
    // Empty timeline — fall back to the counter-based approach.
    return mintClientOrderKey(kindKey);
  }
  // If any item has no orderKey it sorts at _ORDER_KEY_MAX ('\uffff...')
  // which is not a valid 5-part key. Use the sentinel seq at active lineage
  // as the effective max so we produce a valid key that sorts after everything.
  if (hasUnkeyed) {
    // Produce a key at the very top of the active lineage band with max local.
    const kindRank = CLIENT_KIND_RANK[kindKey] ?? 9;
    const li = String(CLIENT_ACTIVE_LINEAGE).padStart(4, '0');
    const s = String(CLIENT_NO_SEQ).padStart(15, '0');
    const p = '999999';
    const kr = String(kindRank).padStart(2, '0');
    const lo = String(++_clientOrderKeyCounter % 1_000_000_000).padStart(9, '0');
    return `${li}:${s}:${p}:${kr}:${lo}`;
  }
  // Parse the max key and produce one that sorts strictly after it.
  // Strategy: keep the same lineage/seq/phase, set kind_rank to the requested
  // value, and increment the local component by 1. If the max key's kind_rank
  // is already >= ours (e.g. a tool_call at rank 3 vs user at rank 0), bump
  // the local component high enough to sort after regardless of kind_rank.
  const parts = maxKey.split(':');
  if (parts.length === 5 && parts.every((p) => /^\d+$/.test(p))) {
    const [li, s] = parts;
    const maxPhase = parseInt(parts[2]!, 10);
    const kindRankStr = String(kindRank).padStart(2, '0');
    // Produce a key with the same lineage:seq but phase = maxPhase + 1.
    // Bumping the phase field guarantees we sort after all items at the same
    // lineage:seq regardless of their kind_rank (phase is a higher-priority
    // field than kind_rank in the sort key). This is safe because the phase
    // field in a user-message tail key is purely a sort tiebreaker — it does
    // not affect the item's logical phase (which is stored in assistantPhaseIndex).
    const nextPhase = Math.min(maxPhase + 1, 999999);
    const ph = String(nextPhase).padStart(6, '0');
    return `${li}:${s}:${ph}:${kindRankStr}:000000000`;
  }
  // Unparseable max key — fall back.
  return mintClientOrderKey(kindKey);
}

function orderKeyBeforeToolBoundary(boundaryOrderKey: string | undefined, kindKey: string): string | undefined {
  if (!boundaryOrderKey) return undefined;
  const parts = boundaryOrderKey.split(':');
  if (parts.length !== 5 || !parts.every((part) => /^\d+$/.test(part))) return undefined;
  const kindRank = CLIENT_KIND_RANK[kindKey] ?? 9;
  return [
    parts[0],
    parts[1],
    // Match the boundary phase component so kindRank is the tiebreaker that
    // places thinking/assistant before the tool call closing this phase.
    parts[2],
    String(kindRank).padStart(2, '0'),
    parts[4],
  ].join(':');
}

function orderKeyPhase(orderKey: string | undefined): number | undefined {
  if (!orderKey) return undefined;
  const parts = orderKey.split(':');
  if (parts.length !== 5 || !/^\d+$/.test(parts[2] ?? '')) return undefined;
  return Number(parts[2]);
}

function assistantStablePhase(assistantPhaseIndex: number | undefined, orderKey: string | undefined): number | undefined {
  return assistantPhaseIndex ?? orderKeyPhase(orderKey);
}

function assistantRenderKey(params: {
  id: string;
  sessionId?: string | null;
  turnId?: string | null;
  messageId?: string;
  assistantPhaseIndex?: number;
  orderKey?: string;
}): string {
  const sessionId = params.sessionId ?? 'unknown-session';
  const turnId = params.turnId ?? params.messageId ?? null;
  const phase = assistantStablePhase(params.assistantPhaseIndex, params.orderKey);
  if (turnId && params.messageId && typeof phase === 'number') {
    return `assistant:${sessionId}:${turnId}:${params.messageId}:${phase}`;
  }
  if (turnId && params.messageId) {
    return `assistant:${sessionId}:${turnId}:${params.messageId}:unphased`;
  }
  if (params.messageId) {
    return `assistant:${sessionId}:${params.messageId}`;
  }
  return params.id;
}

function renderAssistantHtml(params: {
  id: string;
  sessionId?: string | null;
  turnId?: string | null;
  messageId?: string;
  assistantPhaseIndex?: number;
  orderKey?: string;
  content: string;
  streaming?: boolean;
  existingHtml?: string;
}): string {
  const displayContent = normalizeRepeatedAssistantContent(params.content);
  const key = assistantRenderKey(params);
  if (params.streaming) {
    const html = getStreamer(key).render(displayContent);
    return html || (params.content ? params.existingHtml ?? renderMarkdown(displayContent) : '');
  }

  const streamer = streamers.get(key);
  if (!streamer) return renderMarkdown(displayContent);
  const html = streamer.finalize(displayContent);
  streamers.delete(key);
  return html;
}

function renderMessageHtml(params: {
  id: string;
  sessionId?: string | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  turnId?: string | null;
  messageId?: string;
  assistantPhaseIndex?: number;
  orderKey?: string;
  streaming?: boolean;
  existingHtml?: string;
}): string {
  if (params.role === 'system') return '';
  if (params.role === 'assistant') return renderAssistantHtml(params);
  return renderMarkdown(params.content);
}

function renderThinkingBlockHtml(
  block: Pick<ThinkingBlock, 'block_id' | 'content' | 'complete'>,
  existingHtml?: string,
): string {
  if (!block.complete) {
    const html = getThinkingStreamer(block.block_id).render(block.content);
    return html || (block.content ? existingHtml ?? renderMarkdown(block.content) : '');
  }

  const streamer = thinkingStreamers.get(block.block_id);
  if (!streamer) return renderMarkdown(block.content);
  const html = streamer.finalize(block.content);
  thinkingStreamers.delete(block.block_id);
  return html;
}

/**
 * Sort a timeline array by backend-assigned orderKey, falling back to
 * array position for items that share the same key or have no key.
 *
 * Items without an orderKey (local optimistic rows) sort after all keyed
 * items so they appear at the end until the server echo arrives and merges
 * them into the correct position.
 */
export function sortByOrderKey(items: TimelineItem[]): TimelineItem[] {
  // Fast path: if no item has a non-empty orderKey, preserve insertion order.
  if (!items.some((item) => item.orderKey)) return items;
  return [...items].sort((a, b) => {
    // Use || instead of ?? so empty string is treated as absent (sentinel).
    const ka = a.orderKey || _ORDER_KEY_MAX;
    const kb = b.orderKey || _ORDER_KEY_MAX;
    if (ka < kb) return -1;
    if (ka > kb) return 1;
    return 0;
  });
}

export function timelineItemKey(item: TimelineItem): string {
  if (item.kind === 'message') {
    const sessionId = item.sessionId ?? 'unknown-session';
    if (item.role === 'assistant') {
      const turnId = item.turnId ?? item.messageId ?? null;
      // Use (sessionId, turnId, messageId, assistantPhaseIndex?) as the render
      // key. The phase suffix is included only when assistantPhaseIndex is an
      // explicit number — i.e. a real persisted value, not derived from orderKey.
      //
      // Previously the key included `assistantStablePhase(assistantPhaseIndex,
      // orderKey)` which derives phase from orderKey when assistantPhaseIndex is
      // absent. orderKey arrives asynchronously during streaming (runtime sentinel
      // → real persisted key), so the derived phase changed mid-stream, forcing
      // Svelte to tear down and recreate the DOM node (height shift → scroll jump).
      //
      // With this fix: during streaming assistantPhaseIndex is undefined → key has
      // no phase suffix (stable). Once the real assistantPhaseIndex arrives from a
      // timeline_patch the key gains the suffix — a single, predictable transition
      // at finalization time, not on every orderKey update.
      if (turnId && item.messageId) {
        // Use assistantPhaseIndex ?? 0 so streaming items (no phase yet) and
        // phase-0 items produce the same key. This is stable across the
        // streaming lifecycle: the key does NOT change when assistantPhaseIndex
        // arrives (it was already 0 by default). Phase 1+ items get a distinct
        // suffix. Previously the key included orderKeyPhase(orderKey) which
        // derived phase from the runtime sentinel orderKey — that changed
        // mid-stream, forcing Svelte to tear down and recreate the DOM node.
        const phase = item.assistantPhaseIndex ?? 0;
        return `assistant:${sessionId}:${turnId}:${item.messageId}:${phase}`;
      }
      if (item.messageId) return `message:assistant:${sessionId}:${item.messageId}`;
      if (item.seq !== null) return `message-seq:assistant:${sessionId}:${item.seq}`;
      return `message:assistant:${item.id}`;
    }
    if (item.clientMessageId) return `client-message:${item.clientMessageId}`;
    if (item.queueId) return `queue-message:${item.queueId}`;
    if (item.seq !== null) return `message-seq:${item.role}:${sessionId}:${item.seq}`;
    if (item.messageId) return `message:${item.role}:${sessionId}:${item.messageId}`;
  }
  if (item.kind === 'thinking') {
    const sessionId = item.sessionId ?? 'unknown-session';
    const turnId = item.turnId ?? item.messageId;
    // Key on (sessionId, turnId, messageId, id) — stable from first creation.
    // Previously keyed on blocks[0].block_id which can change as blocks
    // merge/reorder during streaming, causing unnecessary DOM teardown.
    // The item id is included as a tiebreaker to distinguish separate thinking
    // segments within the same turn that share the same messageId.
    if (turnId && item.messageId) return `thinking:${sessionId}:${turnId}:${item.messageId}:${item.id}`;
    if (item.messageId) return `thinking:${sessionId}:${item.messageId}:${item.id}`;
  }
  return `${item.kind}:${item.id}`;
}

function projectionString(item: Record<string, unknown>, key: string): string | null {
  const value = item[key];
  return typeof value === 'string' ? value : null;
}

function projectionNumber(item: Record<string, unknown>, key: string): number | null {
  const value = item[key];
  return typeof value === 'number' ? value : null;
}

function projectionBoolean(item: Record<string, unknown>, key: string): boolean | undefined {
  const value = item[key];
  return typeof value === 'boolean' ? value : undefined;
}

function projectionArray<T>(item: Record<string, unknown>, key: string): T[] {
  const value = item[key];
  return Array.isArray(value) ? value as T[] : [];
}

function projectionObject(item: Record<string, unknown>, key: string): Record<string, unknown> | undefined {
  const value = item[key];
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

export function timelineFromProjection(
  projectedItems: TimelineProjectionItem[],
  options: { normalize?: boolean } = {},
): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const projected of projectedItems) {
    const kind = projectionString(projected, 'kind');
    const id = projectionString(projected, 'id');
    if (!kind || !id) continue;

    if (kind === 'message') {
      const role = projectionString(projected, 'role');
      if (role !== 'user' && role !== 'assistant' && role !== 'system') continue;
      const content = projectionString(projected, 'content') ?? '';
      const displayContent = role === 'assistant' ? normalizeRepeatedAssistantContent(content) : content;
      const streaming = projectionBoolean(projected, 'streaming');
      const assistantPhaseIndex = projectionNumber(projected, 'assistantPhaseIndex') ?? undefined;
      const orderKey = projectionString(projected, 'orderKey') ?? undefined;
      const item: MessageTimelineItem = {
        id,
        kind: 'message',
        sessionId: projectionString(projected, 'sessionId'),
        role,
        content,
        html: renderMessageHtml({
          id,
          sessionId: projectionString(projected, 'sessionId'),
          role,
          content: displayContent,
          turnId: projectionString(projected, 'turnId'),
          messageId: projectionString(projected, 'messageId') ?? undefined,
          assistantPhaseIndex,
          orderKey,
          streaming,
        }),
        seq: projectionNumber(projected, 'seq'),
        timestamp: projectionString(projected, 'timestamp'),
        turnId: projectionString(projected, 'turnId'),
        messageId: projectionString(projected, 'messageId') ?? undefined,
        streaming,
        attachments: projectionArray<AttachmentRef>(projected, 'attachments'),
        optimistic: projectionBoolean(projected, 'optimistic'),
        clientMessageId: projectionString(projected, 'clientMessageId'),
        queueId: projectionString(projected, 'queueId'),
        streamChunkCount: projectionNumber(projected, 'streamChunkCount') ?? undefined,
        streamContentOffset: projectionNumber(projected, 'streamContentOffset') ?? content.length,
        chatMode: normalizeChatMode(projected.chatMode),
        chatModeSource: normalizeChatModeSource(projected.chatModeSource),
        partial: projectionBoolean(projected, 'partial'),
        finishReason: projectionString(projected, 'finishReason'),
        runtime: normalizeMessageRuntime(projected.runtime),
        assistantPhaseIndex,
        orderKey,
      };
      const deliveryStatus = projectionString(projected, 'deliveryStatus');
      if (deliveryStatus === 'sending' || deliveryStatus === 'queued' || deliveryStatus === 'failed') {
        item.deliveryStatus = deliveryStatus;
      }
      items.push(item);
      continue;
    }

    if (kind === 'tool_call') {
      const callId = projectionString(projected, 'callId');
      const toolName = projectionString(projected, 'toolName');
      if (!callId || !toolName) continue;
      const toolItem: ToolCallTimelineItem = {
        id,
        kind: 'tool_call',
        callId,
        toolName,
        status: projectionString(projected, 'status') ?? 'started',
        timestamp: projectionString(projected, 'timestamp'),
        sessionId: projectionString(projected, 'sessionId'),
        turnId: projectionString(projected, 'turnId'),
        arguments: projectionObject(projected, 'arguments'),
        result: projectionString(projected, 'result') ?? undefined,
        streamedOutput: projectionString(projected, 'streamedOutput') ?? undefined,
        streamChunkCount: projectionNumber(projected, 'streamChunkCount') ?? undefined,
        streamContentOffset: projectionNumber(projected, 'streamContentOffset') ?? undefined,
        isError: projectionBoolean(projected, 'isError'),
        durationMs: projectionNumber(projected, 'durationMs') ?? undefined,
        attachments: projectionArray<AttachmentRef>(projected, 'attachments'),
        fileDiffs: normalizeFileDiffs(projected.fileDiffs),
        outputSize: projectionNumber(projected, 'outputSize') ?? projectionNumber(projected, 'output_size') ?? undefined,
        truncated: projectionBoolean(projected, 'truncated'),
        agentVisibleTruncated: projectionBoolean(projected, 'agentVisibleTruncated'),
        transportTruncated: projectionBoolean(projected, 'transportTruncated'),
        hasFullOutput: projectionBoolean(projected, 'hasFullOutput'),
        recoveryCallId: projectionString(projected, 'recoveryCallId'),
        toolOutputArtifactId: projectionString(projected, 'toolOutputArtifactId'),
        anchorsAvailable: projectionBoolean(projected, 'anchorsAvailable'),
        anchorCount: projectionNumber(projected, 'anchorCount') ?? undefined,
        reconstructed: projectionBoolean(projected, 'reconstructed'),
        liveOutputAvailable: projectionBoolean(projected, 'liveOutputAvailable'),
        progressPhase: projectionString(projected, 'progressPhase') ?? undefined,
        progressInputChars: projectionNumber(projected, 'progressInputChars') ?? undefined,
        progressInputLines: projectionNumber(projected, 'progressInputLines') ?? undefined,
        progressComplete: projectionBoolean(projected, 'progressComplete'),
        assistantPhaseIndex: projectionNumber(projected, 'assistantPhaseIndex') ?? undefined,
        evaluation: projectionObject(projected, 'evaluation') as ToolCallTimelineItem['evaluation'],
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      };
      const presentation = projectionObject(projected, 'tool_output_presentation');
      items.push(presentation ? mergeToolPresentation(toolItem, presentation) : toolItem);
      continue;
    }

    if (kind === 'system_message') {
      if (isTransientCompactionStartSystemMessage(projected)) continue;
      items.push({
        id,
        kind: 'system_message',
        text: projectionString(projected, 'text') ?? '',
        noticeId: projectionString(projected, 'noticeId'),
        noticeKind: projectionString(projected, 'noticeKind'),
        noticeScope: projectionString(projected, 'noticeScope'),
        followUpConversationId: projectionString(projected, 'followUpConversationId'),
        followUpSessionId: projectionString(projected, 'followUpSessionId'),
        timestamp: projectionString(projected, 'timestamp'),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
      continue;
    }

    if (kind === 'notice') {
      const tone = projectionString(projected, 'tone');
      items.push({
        id,
        kind: 'notice',
        title: projectionString(projected, 'title') ?? 'Notice',
        description: projectionString(projected, 'description') ?? '',
        tone: tone === 'warning' || tone === 'error' ? tone : 'info',
        timestamp: projectionString(projected, 'timestamp'),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
      continue;
    }

    if (kind === 'workflow_composed') {
      items.push({
        id,
        kind: 'workflow_composed',
        workflowId: projectionString(projected, 'workflowId') ?? '',
        workflowName: projectionString(projected, 'workflowName') ?? 'Workflow',
        lifecycle: projectionString(projected, 'lifecycle') ?? 'ephemeral',
        taskId: projectionString(projected, 'taskId'),
        scheduleId: projectionString(projected, 'scheduleId'),
        steps: projectionArray<string>(projected, 'steps'),
        timestamp: projectionString(projected, 'timestamp'),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
      continue;
    }

    if (kind === 'thinking') {
      const messageId = projectionString(projected, 'messageId');
      if (!messageId) continue;
      const thinkingStreaming = projectionBoolean(projected, 'streaming') ?? false;
      const blocks = projectionArray<Record<string, unknown>>(projected, 'blocks')
        .map((block): ThinkingBlock | null => {
          const blockId = projectionString(block, 'block_id');
          if (!blockId) return null;
          const title = projectionString(block, 'title') ?? 'Thinking';
          const content = normalizeThinkingContent(projectionString(block, 'content') ?? '', title);
          const complete = projectionBoolean(block, 'complete') ?? true;
          return {
            block_id: blockId,
            title,
            content,
            html: renderThinkingBlockHtml({ block_id: blockId, content, complete }),
            source: projectionString(block, 'source') ?? 'summary',
            complete,
            startedAt: projectionString(block, 'startedAt'),
            completedAt: projectionString(block, 'completedAt'),
            durationMs: projectionNumber(block, 'durationMs'),
            providerBlockIndex: projectionNumber(block, 'providerBlockIndex'),
          };
        })
        .filter((block): block is ThinkingBlock => block !== null);
      items.push({
        id,
        kind: 'thinking',
        sessionId: projectionString(projected, 'sessionId'),
        messageId,
        turnId: projectionString(projected, 'turnId'),
        blocks,
        assistantPhaseIndex: projectionNumber(projected, 'assistantPhaseIndex') ?? undefined,
        streaming: thinkingStreaming,
        activeTitle: projectionString(projected, 'activeTitle'),
        timestamp: projectionString(projected, 'timestamp'),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
      continue;
    }

    if (kind === 'delegation') {
      const taskId = projectionString(projected, 'taskId');
      if (!taskId) continue;
      items.push({
        id,
        kind: 'delegation',
        taskId,
        taskLabel: projectionString(projected, 'taskLabel') ?? 'Sub-session',
        agentId: projectionString(projected, 'agentId'),
        usedAgentId: projectionString(projected, 'usedAgentId'),
        status: normalizeDelegationStatus(projectionString(projected, 'status')),
        result: projectionString(projected, 'result'),
        timestamp: projectionString(projected, 'timestamp'),
        toolCallCount: projectionNumber(projected, 'toolCallCount') ?? undefined,
        maxToolCalls: projectionNumber(projected, 'maxToolCalls') ?? undefined,
        lastTool: projectionString(projected, 'lastTool') ?? undefined,
        todos: parseTodoSnapshot(projectionArray<Record<string, unknown>>(projected, 'todos')),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
      continue;
    }

    if (kind === 'compaction') {
      const status = projectionString(projected, 'status');
      items.push({
        id,
        kind: 'compaction',
        status: status === 'running' || status === 'failed' || status === 'skipped'
          ? status
          : 'compacted',
        sessionId: projectionString(projected, 'sessionId') ?? undefined,
        previousSessionId: projectionString(projected, 'previousSessionId') ?? undefined,
        summaryPreview: projectionString(projected, 'summaryPreview') ?? '',
        summary: projectionString(projected, 'summary') ?? undefined,
        method: projectionString(projected, 'method') ?? 'unknown',
        turnsCompacted: projectionNumber(projected, 'turnsCompacted') ?? 0,
        trigger: projectionString(projected, 'trigger') ?? undefined,
        reason: projectionString(projected, 'reason') ?? undefined,
        previousUsagePercentage: projectionNumber(projected, 'previousUsagePercentage'),
        effectiveUsagePercentage: projectionNumber(projected, 'effectiveUsagePercentage'),
        hardPressureExceeded: projectionBoolean(projected, 'hardPressureExceeded'),
        usedTimeoutFallback: projectionBoolean(projected, 'usedTimeoutFallback'),
        timestamp: projectionString(projected, 'timestamp'),
        orderKey: projectionString(projected, 'orderKey') ?? undefined,
      });
    }
  }
  return options.normalize === false ? items : normalizeTimelineItems(items);
}

/**
 * Return true when an orderKey belongs to the runtime-only sentinel band
 * (lineage 9999). Runtime items have not yet been persisted and carry a
 * sentinel key that sorts after all real history.
 */
function isRuntimeSentinelOrderKey(orderKey: string | undefined): boolean {
  return typeof orderKey === 'string' && orderKey.startsWith('9999:');
}

/**
 * Return true when an orderKey carries the no-seq sentinel (the 15-digit seq
 * component equals 999999999999999). Such an item has NOT yet been assigned a
 * durable Intaris event seq — it is a live/runtime-only item that may be
 * legitimately absent from a freshly-fetched history projection.
 *
 * Note: the *lineage* band (9998 active vs 9999 no-lineage) is NOT a reliable
 * signal — incremental reads of the active session legitimately use lineage
 * 9998 with a REAL seq. Only the seq sentinel reliably marks an item as
 * unpersisted. This mirrors the backend's `_is_sentinel_orderkey`
 * (tests/e2e/invariants.py) which keys on `parts[1] == "999999999999999"`.
 *
 * Format: ``{lineage:04d}:{seq:015d}:{phase:06d}:{kind_rank:02d}:{local:09d}``.
 *
 * Used by ChatTimeline.replaceAll to avoid evicting unconfirmed live items
 * when a refresh lands in the window after the turn produced them but before
 * they are durably queryable (the "message disappears after refresh" bug).
 */
const _ORDER_KEY_NO_SEQ_SENTINEL = '999999999999999';

export function isUnpersistedRuntimeOrderKey(orderKey: string | undefined): boolean {
  if (typeof orderKey !== 'string') return false;
  const parts = orderKey.split(':');
  return parts.length === 5 && parts[1] === _ORDER_KEY_NO_SEQ_SENTINEL;
}

function timelinePatchMergeIndex(items: TimelineItem[], patch: TimelineItem): number {
  const byId = items.findIndex((item) => (
    item.id === patch.id
    && item.kind === patch.kind
    && (
      item.kind !== 'message'
      || (patch.kind === 'message' && item.role === patch.role)
    )
  ));
  if (byId >= 0) return byId;
  if (patch.kind === 'tool_call') {
    // Primary: match by callId (the real tool call id).
    const byCallId = items.findIndex((item) => item.kind === 'tool_call' && item.callId === patch.callId);
    if (byCallId >= 0) return byCallId;
    // Secondary: match by id (handles cases where callId differs but id agrees,
    // e.g. a result patch keyed by a different id variant than the started item).
    const byId = items.findIndex((item) => item.kind === 'tool_call' && item.id === patch.id);
    if (byId >= 0) return byId;
    if (_timelineDebugEnabled()) {
      const existingTools = items.filter((i) => i.kind === 'tool_call');
      if (existingTools.length > 0) {
        _dbg('tool_call merge-miss patch:', _itemSummary(patch));
        _dbg('tool_call merge-miss existing:', existingTools.map((t) => _itemSummary(t)).join(' | '));
      }
    }
    return -1;
  }
  if (patch.kind === 'message') {
    if (patch.role === 'user') {
      const correlatedIndex = findUserMessageByCorrelationIndex(items, patch.clientMessageId, patch.queueId);
      if (correlatedIndex >= 0) return correlatedIndex;
      if (patch.optimistic !== true && patch.deliveryStatus === undefined) {
        const optimisticIndex = findOptimisticUserMessageIndex(items, patch.content, patch.attachments ?? []);
        if (optimisticIndex >= 0) return optimisticIndex;
      }
    }
    // Primary assistant fallback: match by (messageId, turnId, phase).
    // Treat absent phase as 0 — a streaming item that hasn't received its
    // explicit assistantPhaseIndex yet should merge with a persisted phase-0
    // item rather than creating a duplicate.
    const patchPhase = patch.assistantPhaseIndex ?? 0;
    const primaryIndex = items.findIndex((item) => (
      item.kind === 'message'
      && item.role === patch.role
      && Boolean(item.messageId)
      && item.messageId === patch.messageId
      && (item.turnId ?? null) === (patch.turnId ?? null)
      && (item.assistantPhaseIndex ?? 0) === patchPhase
    ));
    if (primaryIndex >= 0) return primaryIndex;
    // Safety-net fallback: if one side carries a runtime sentinel orderKey and
    // both sides have the same (messageId, turnId), collapse them regardless of
    // phase. This handles residual phase disagreements from events recorded
    // before assistant_phase_index was always persisted, preventing a duplicate
    // render that resolves only on the next reconciling patch.
    if (patch.role === 'assistant' && patch.messageId && patch.turnId) {
      const patchIsRuntime = isRuntimeSentinelOrderKey(patch.orderKey);
      const safetyIndex = items.findIndex((item) => (
        item.kind === 'message'
        && item.role === 'assistant'
        && item.messageId === patch.messageId
        && (item.turnId ?? null) === (patch.turnId ?? null)
        && (patchIsRuntime || isRuntimeSentinelOrderKey(item.orderKey))
      ));
      if (safetyIndex < 0 && _timelineDebugEnabled()) {
        // Only log when there are same-turn candidates — a miss with candidates
        // is a real identity mismatch worth investigating. A miss with no
        // candidates (brand-new item) is benign and would flood the console.
        const candidates = items.filter((i) => i.kind === 'message' && (i as MessageTimelineItem).role === 'assistant' && (i as MessageTimelineItem).turnId === (patch as MessageTimelineItem).turnId);
        if (candidates.length > 0) {
          _dbg('merge-miss assistant patch:', _itemSummary(patch));
          _dbg('merge-miss candidates:', candidates.map((c) => _itemSummary(c)).join(' | '));
        }
      }
      return safetyIndex;
    }
    return -1;
  }
  if (patch.kind === 'delegation') {
    return items.findIndex((item) => item.kind === 'delegation' && item.taskId === patch.taskId);
  }
  if (patch.kind === 'thinking') {
    // Thinking items are matched by stable id only. The backend emits one
    // thinking item per segment with a stable id (thinking:{msg}:phase:{p}:{anchor})
    // and the full cumulative blocks[] in every snapshot — no client-side
    // block accumulation is needed. The id-match at the top of this function
    // already handles the common case; this branch is now a no-op fallback.
    return -1;
  }
  if (patch.kind === 'system_message' && patch.noticeId) {
    return items.findIndex((item) => item.kind === 'system_message' && item.noticeId === patch.noticeId);
  }
  return -1;
}

// thinkingItemsMatch, mergeThinkingTimelineItem removed.
// The backend emits one thinking item per segment with a stable id and the
// full cumulative blocks[] in every snapshot. The client store is id-keyed
// and replaces verbatim — no block-array accumulation is needed or correct.
// Merging two layers (backend already consolidates; client re-merged) was
// the source of duplicate blocks, stale content, and ordering mess.

/**
 * Pick the better orderKey when merging two items.
 *
 * "Better" means the lexicographically smaller key, which corresponds to a
 * real persisted seq (e.g. "0000:000000000000003:…") over a runtime sentinel
 * (e.g. "9999:999999999999999:…").  An absent or empty orderKey is treated as
 * the LARGEST possible value so it never displaces a real key.
 */
function mergeOrderKey(existing: TimelineItem, patch: TimelineItem): string | undefined {
  const ek = existing.orderKey || _ORDER_KEY_MAX;
  const pk = patch.orderKey || _ORDER_KEY_MAX;
  const best = ek <= pk ? ek : pk;
  return best === _ORDER_KEY_MAX ? undefined : best;
}

/** @internal Used by ChatTimeline store and tests. Not part of the public API. */
export function mergeTimelinePatchItem(existing: TimelineItem, patch: TimelineItem): TimelineItem {
  const orderKey = mergeOrderKey(existing, patch);
  if (existing.kind === 'message' && patch.kind === 'message') {
    // A finalized assistant message must not be reopened by a stale streaming
    // patch. The server may replay a runtime snapshot after the completion patch
    // has already landed; keep the finalized state.
    if (
      existing.role === 'assistant'
      && existing.streaming === false
      && patch.streaming === true
    ) {
      return existing;
    }
    const content = patch.role === 'assistant' && !patch.content && existing.content
      ? existing.content
      : patch.content;
    const assistantPhaseIndex = patch.assistantPhaseIndex ?? existing.assistantPhaseIndex;
    const merged: MessageTimelineItem = {
      ...existing,
      ...patch,
      content,
      html: renderMessageHtml({
        id: existing.id,
        sessionId: patch.sessionId ?? existing.sessionId,
        role: patch.role,
        content,
        turnId: patch.turnId ?? existing.turnId,
        messageId: patch.messageId ?? existing.messageId,
        assistantPhaseIndex,
        orderKey,
        streaming: patch.streaming ?? false,
        existingHtml: existing.html,
      }),
      streaming: patch.streaming ?? false,
      assistantPhaseIndex,
      orderKey,
    };
    if (existing.role === 'user' && patch.role === 'user' && patch.deliveryStatus === undefined && patch.optimistic !== true) {
      merged.optimistic = false;
      delete merged.deliveryStatus;
    }
    return normalizeTimelineItem(merged);
  }
  if (existing.kind === 'tool_call' && patch.kind === 'tool_call') {
    const toolOrderKey = typeof existing.assistantPhaseIndex === 'number'
      && typeof patch.assistantPhaseIndex !== 'number'
      ? existing.orderKey ?? orderKey
      : orderKey;
    const assistantPhaseIndex = patch.assistantPhaseIndex ?? existing.assistantPhaseIndex;
    if (isTerminalToolStatus(existing.status) && !isTerminalToolStatus(patch.status)) {
      return normalizeTimelineItem({
        ...existing,
        sessionId: existing.sessionId ?? patch.sessionId,
        timestamp: existing.timestamp ?? patch.timestamp,
        arguments: existing.arguments ?? patch.arguments,
        attachments: existing.attachments ?? patch.attachments,
        fileDiffs: existing.fileDiffs ?? patch.fileDiffs,
        assistantPhaseIndex,
        orderKey: toolOrderKey,
      });
    }
    const keepExistingStreamedResult = Boolean(existing.liveOutputAvailable)
      && Boolean(existing.streamedOutput)
      && typeof patch.result === 'string'
      && (patch.transportTruncated === true || patch.truncated === true || patch.agentVisibleTruncated === true)
      && (existing.streamedOutput?.length ?? 0) > patch.result.length;
    return normalizeTimelineItem({
      ...existing,
      ...patch,
      id: existing.id,
      sessionId: existing.sessionId ?? patch.sessionId,
      timestamp: existing.timestamp ?? patch.timestamp,
      arguments: patch.arguments ?? existing.arguments,
      result: keepExistingStreamedResult ? existing.streamedOutput : patch.result ?? existing.result,
      streamedOutput: existing.streamedOutput ?? patch.streamedOutput,
      liveOutputAvailable: existing.liveOutputAvailable ?? patch.liveOutputAvailable,
      attachments: patch.attachments?.length ? patch.attachments : existing.attachments,
      fileDiffs: patch.fileDiffs?.length ? patch.fileDiffs : existing.fileDiffs,
      // Preserve evaluation from the existing item when the patch omits it.
      // Escalation events set evaluation on the tool call item; follow-up
      // patches (tool_result, tool_progress) do not carry evaluation.
      evaluation: patch.evaluation ?? existing.evaluation,
      assistantPhaseIndex,
      orderKey: toolOrderKey,
    });
  }
  if (existing.kind === 'delegation' && patch.kind === 'delegation') {
    const merged = mergeDelegationItem(existing, patch);
    return { ...merged, orderKey };
  }
  if (existing.kind === 'thinking' && patch.kind === 'thinking') {
    // Verbatim replace: the backend sends the full cumulative blocks[] in every
    // snapshot with a stable id. No client-side block accumulation needed.
    // Keep the min-orderKey so position never jumps (same as other kinds).
    return { ...patch, orderKey };
  }
  return normalizeTimelineItem({ ...patch, orderKey });
}

function dedupeAttachments(attachments: AttachmentRef[] | undefined): AttachmentRef[] | undefined {
  if (!attachments || attachments.length < 2) return attachments;
  const byArtifactId = new Map<string, AttachmentRef>();
  for (const attachment of attachments) {
    const existing = byArtifactId.get(attachment.artifact_id);
    byArtifactId.set(attachment.artifact_id, existing ? { ...existing, ...attachment } : attachment);
  }
  return byArtifactId.size === attachments.length ? attachments : Array.from(byArtifactId.values());
}

// dedupeThinkingBlocks removed — see mergeTimelinePatchItem thinking branch.

function normalizeTimelineItem(item: TimelineItem): TimelineItem {
  if (item.kind === 'message') {
    const attachments = dedupeAttachments(item.attachments);
    return attachments === item.attachments ? item : { ...item, attachments };
  }
  if (item.kind === 'tool_call') {
    const attachments = dedupeAttachments(item.attachments);
    return attachments === item.attachments ? item : { ...item, attachments };
  }
  return item;
}

function normalizeTimelineItems(items: TimelineItem[]): TimelineItem[] {
  const next: TimelineItem[] = [];
  for (const rawItem of items) {
    const item = normalizeTimelineItem(rawItem);
    const index = timelinePatchMergeIndex(next, item);
    if (index >= 0) {
      next[index] = mergeTimelinePatchItem(next[index], item);
      continue;
    }

    const key = timelineItemKey(item);
    const duplicateKeyIndex = next.findIndex((existing) => timelineItemKey(existing) === key);
    if (duplicateKeyIndex >= 0) {
      const existing = next[duplicateKeyIndex];
      if (existing.kind === item.kind) {
        next[duplicateKeyIndex] = mergeTimelinePatchItem(existing, item);
      }
      continue;
    }

    next.push(item);
  }
  return sortByOrderKey(next);
}

function isTerminalCompactionNotice(text: string | null | undefined): boolean {
  const normalized = (text ?? '').trim().toLowerCase();
  return normalized.startsWith('automatic compaction completed.')
    || normalized.startsWith('automatic compaction found no older history to compact.')
    || normalized.startsWith('automatic compaction was requested, but there was not enough')
    || normalized.startsWith('automatic compaction was recommended, but there is no older')
    || normalized.startsWith('context window is critically full, but there is no older');
}

function removeRunningCompactionItems(items: TimelineItem[], sessionId?: string | null): TimelineItem[] {
  if (!sessionId) {
    const runningCount = items.filter((item) => item.kind === 'compaction' && item.status === 'running').length;
    if (runningCount !== 1) return items;
  }
  return items.filter((item) => {
    if (item.kind !== 'compaction' || item.status !== 'running') return true;
    if (!sessionId) return false;
    return item.sessionId !== sessionId && item.previousSessionId !== sessionId;
  });
}

export function timelinePatchContainsActiveWork(projectedItems: TimelineProjectionItem[]): boolean {
  return projectedItems.some((item) => {
    if (item.kind === 'message') {
      return item.role === 'assistant' && item.partial === true;
    }
    if (item.kind === 'tool_call' || item.kind === 'delegation') {
      const status = typeof item.status === 'string' ? item.status : '';
      return status === 'started' || status === 'running' || status === 'pending' || status === 'paused';
    }
    if (item.kind === 'thinking') {
      return item.streaming === true;
    }
    return false;
  });
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
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind !== 'message') continue;
    if (!isRecentOptimisticUserMessage(item)) continue;

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
    timestamp: new Date().toISOString(),
    orderKey: mintClientOrderKey('notice'),
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
        const key = assistantRenderKey(item);
        const streamer = getStreamer(key);
        const finalHtml = streamer.finalize(item.content);
        releaseStreamer(key);
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

function itemMatchesBoundaryPhase(
  itemPhase: number | undefined,
  boundaryPhase: number | undefined,
): boolean {
  // When the tool-call boundary has no explicit phase, we cannot determine
  // which assistant phase it closes — allow any item (original behaviour).
  if (boundaryPhase === undefined) return true;
  // When the assistant item has no explicit phase yet (still streaming, phase
  // not yet received from the server), only match if the boundary is phase 0.
  // Previously this returned true for any boundaryPhase, which caused a
  // tool_call at phase N to prematurely finalize an assistant message whose
  // phase was still undefined — producing an orderKey jump and content freeze
  // for messages taller than the viewport.
  if (itemPhase === undefined) return boundaryPhase === 0;
  return itemPhase === boundaryPhase;
}

function findOpenPhaseAssistantIndex(
  items: TimelineItem[],
  turnId: string | null,
  boundaryPhase?: number,
): number {
  if (!turnId) return -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (
      boundaryPhase === undefined
      && item.kind === 'tool_call'
      && item.turnId === turnId
    ) {
      return -1;
    }
    if (
      item.kind === 'message'
      && item.role === 'assistant'
      && item.turnId === turnId
      && item.streaming === true
      && itemMatchesBoundaryPhase(item.assistantPhaseIndex, boundaryPhase)
    ) {
      return index;
    }
  }
  return -1;
}

function findMergeableAssistantIndex(
  items: TimelineItem[],
  turnId: string | null,
  messageId: string | undefined,
  assistantPhaseIndex?: number,
): number {
  const existingIndex = findOpenPhaseAssistantIndex(items, turnId, assistantPhaseIndex);
  if (existingIndex >= 0 && items[existingIndex]?.kind === 'message') return existingIndex;
  if (!messageId) return -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (
      assistantPhaseIndex === undefined
      && item.kind === 'tool_call'
      && item.turnId === turnId
    ) {
      return -1;
    }
    if (
      item.kind === 'message'
      && item.role === 'assistant'
      && item.turnId === turnId
      && item.messageId === messageId
      && itemMatchesBoundaryPhase(item.assistantPhaseIndex, assistantPhaseIndex)
    ) {
      return index;
    }
  }
  return -1;
}

// hasDuplicateThinkingBlock removed — see mergeTimelinePatchItem thinking branch.

function normalizeThinkingContent(content: string, title?: string | null): string {
  const raw = content;
  const value = raw.trim();
  if (value.length < 64) return content;
  const normalizedTitle = title?.trim().toLowerCase();
  const normalizedTitlePrefix = normalizedTitle?.replace(/[.…]+$/u, '').trim();
  for (let repetitions = 5; repetitions >= 2; repetitions -= 1) {
    if (raw.length % repetitions !== 0) continue;
    const unit = raw.slice(0, raw.length / repetitions);
    if (
      unit.trim().length >= 32
      && unit.repeat(repetitions) === raw
      && (
        !normalizedTitle
        || unit.trimStart().toLowerCase().startsWith(normalizedTitle)
        || (
          Boolean(normalizedTitlePrefix)
          && unit.trimStart().toLowerCase().startsWith(normalizedTitlePrefix!)
        )
      )
    ) {
      return unit.trim();
    }
  }
  return content;
}

// mergeThinkingBlock and upsertThinkingBlockInSegment removed.
// The backend emits full cumulative blocks[] per snapshot; client replaces verbatim by id.

function findOpenPhaseThinkingIndex(items: TimelineItem[], turnId: string | null): number {
  const candidateIndex = items.length - 1;
  if (candidateIndex < 0) return -1;
  const candidate = items[candidateIndex];
  if (candidate?.kind !== 'thinking') return -1;
  return candidate.turnId === turnId ? candidateIndex : -1;
}

function currentAssistantPhaseIndex(items: TimelineItem[], turnId: string | null): number | undefined {
  const assistantIndex = findOpenPhaseAssistantIndex(items, turnId);
  if (assistantIndex >= 0) {
    const assistant = items[assistantIndex];
    return assistant.kind === 'message' && assistant.role === 'assistant'
      ? assistant.assistantPhaseIndex
      : undefined;
  }
  const thinkingIndex = findOpenPhaseThinkingIndex(items, turnId);
  if (thinkingIndex >= 0) {
    const thinking = items[thinkingIndex];
    return thinking.kind === 'thinking' ? thinking.assistantPhaseIndex : undefined;
  }
  return nextAssistantPhaseIndex(items, turnId);
}

function nextThinkingSegmentPhaseIndex(items: TimelineItem[], turnId: string | null): number | undefined {
  if (!turnId) return undefined;
  const previous = items.at(-1);
  if (previous?.kind === 'thinking' && previous.turnId === turnId) {
    return previous.assistantPhaseIndex ?? nextAssistantPhaseIndex(items, turnId);
  }
  if (previous?.kind === 'message' && previous.role === 'assistant' && previous.turnId === turnId) {
    if (typeof previous.assistantPhaseIndex === 'number') {
      return previous.assistantPhaseIndex + 1;
    }
    return nextAssistantPhaseIndex(items, turnId);
  }
  return nextAssistantPhaseIndex(items, turnId);
}

function findOpenPhaseThinkingIndexByBlockId(
  items: TimelineItem[],
  blockId: string,
  turnId: string | null,
  messageId?: string,
): number {
  const index = findOpenPhaseThinkingIndex(items, turnId);
  if (index < 0) return -1;
  const item = items[index];
  if (item.kind !== 'thinking') return -1;
  if (messageId && item.messageId !== messageId) return -1;
  return item.blocks.some((block) => block.block_id === blockId && !block.complete) ? index : -1;
}

function findIncompleteThinkingIndexByBlockId(
  items: TimelineItem[],
  blockId: string,
  turnId: string | null,
  messageId?: string,
): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind !== 'thinking' || item.turnId !== turnId) continue;
    if (messageId && item.messageId !== messageId) continue;
    if (item.blocks.some((block) => block.block_id === blockId && !block.complete)) {
      return index;
    }
  }
  return -1;
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
    assistantPhaseIndex: existing.assistantPhaseIndex ?? currentAssistantPhaseIndex(items, turnId),
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

function normalizeRepeatedAssistantContent(content: string): string {
  const raw = content.trim();
  if (raw.length < 512) return content;
  for (let repetitions = 5; repetitions >= 2; repetitions -= 1) {
    if (raw.length % repetitions !== 0) continue;
    const unit = raw.slice(0, raw.length / repetitions);
    if (unit.trim().length >= 256 && unit.repeat(repetitions) === raw) {
      return unit.trim();
    }
  }
  return content;
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

function reconcilePhaseCompletedAssistantContent(
  items: TimelineItem[],
  message: MessageTimelineItem,
  completed: string | null,
): string {
  if (!completed) return message.content;
  const phaseIndex = message.assistantPhaseIndex;
  if (typeof phaseIndex !== 'number') {
    return reconcileCompletedAssistantContent(message.content, completed);
  }
  const priorPhasePrefix = items
    .filter((item): item is MessageTimelineItem => (
      item.kind === 'message'
      && item.role === 'assistant'
      && item.messageId === message.messageId
      && (item.turnId ?? null) === (message.turnId ?? null)
      && typeof item.assistantPhaseIndex === 'number'
      && item.assistantPhaseIndex < phaseIndex
    ))
    .sort((a, b) => (a.assistantPhaseIndex ?? 0) - (b.assistantPhaseIndex ?? 0))
    .map((item) => item.content)
    .join('');
  if (priorPhasePrefix && completed.startsWith(priorPhasePrefix)) {
    return reconcileCompletedAssistantContent(message.content, completed.slice(priorPhasePrefix.length));
  }
  return reconcileCompletedAssistantContent(message.content, completed);
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
    runtime = null,
    orderKey,
    assistantPhaseIndex,
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
    runtime?: MessageRuntimeMetadata | null;
    orderKey?: string;
    assistantPhaseIndex?: number;
  },
): void {
  const existingIndex = findMergeableAssistantIndex(
    items,
    turnId,
    messageId,
    assistantPhaseIndex,
  );
  if (existingIndex >= 0) {
    const existing = items[existingIndex] as MessageTimelineItem;
    const nextContent = mergeAssistantContent(existing.content, content);
    const nextAttachments = attachments.length > 0 ? [...(existing.attachments ?? []), ...attachments] : existing.attachments ?? [];
    const nextAssistantPhaseIndex = existing.assistantPhaseIndex ?? assistantPhaseIndex;
    items[existingIndex] = {
      ...existing,
      content: nextContent,
      html: renderAssistantHtml({
        id: existing.id,
        sessionId: sessionId ?? existing.sessionId,
        turnId,
        messageId: messageId ?? existing.messageId,
        assistantPhaseIndex: nextAssistantPhaseIndex,
        orderKey: orderKey ?? existing.orderKey,
        content: nextContent,
        streaming,
        existingHtml: existing.html,
      }),
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
      runtime: runtime ?? existing.runtime,
      assistantPhaseIndex: nextAssistantPhaseIndex,
      orderKey: orderKey ?? existing.orderKey,
    } satisfies MessageTimelineItem;
    return;
  }

  const item = createMessageItem(
    id,
    sessionId ?? null,
    'assistant',
    content,
    timestamp,
    seq,
    messageId,
    streaming,
    attachments,
    false,
    turnId,
    undefined,
    null,
    null,
    assistantPhaseIndex ?? nextAssistantPhaseIndex(items, turnId),
  );
  item.chatMode = chatMode;
  item.chatModeSource = chatModeSource;
  item.partial = partial;
  item.finishReason = finishReason;
  item.runtime = runtime;
  if (orderKey) {
    item.orderKey = orderKey;
    item.html = renderAssistantHtml({
      id: item.id,
      sessionId: item.sessionId,
      turnId: item.turnId,
      messageId: item.messageId,
      assistantPhaseIndex: item.assistantPhaseIndex,
      orderKey,
      content: item.content,
      streaming,
      existingHtml: item.html,
    });
  }
  items.push(item);
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
      item.orderKey = mintClientOrderKey('message:user');
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
          runtime: normalizeMessageRuntime(event.data.runtime),
          orderKey: mintClientOrderKey('message:assistant'),
          assistantPhaseIndex: typeof event.data.assistant_phase_index === 'number'
            ? event.data.assistant_phase_index
            : undefined,
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
          typeof event.data.scope === 'string' ? event.data.scope : null,
          typeof event.data.follow_up_conversation_id === 'string'
            ? event.data.follow_up_conversation_id
            : null,
          typeof event.data.follow_up_session_id === 'string'
            ? event.data.follow_up_session_id
            : null,
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
      const blockContent = normalizeThinkingContent(
        typeof event.data.content === 'string' ? event.data.content : '',
        typeof event.data.title === 'string' ? event.data.title : null,
      );
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
      items.push({
        id: `thinking:${eid}:${blockId}`,
        kind: 'thinking',
        sessionId: typeof event.data.session_id === 'string' ? event.data.session_id : null,
        messageId,
        turnId,
        blocks: [block],
        assistantPhaseIndex: nextThinkingSegmentPhaseIndex(items, turnId),
        streaming: false,
        activeTitle: null,
        timestamp: event.timestamp,
      } satisfies ThinkingTimelineItem);
      continue;
    }

    if (event.type === 'tool_call') {
      const toolName = String(event.data.name ?? event.data.tool_name ?? 'unknown');
      const visibleToolName = typeof event.data.visible_name === 'string' ? event.data.visible_name : null;
      const canonicalToolName = typeof event.data.canonical_name === 'string' ? event.data.canonical_name : toolName;
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
        displayToolName: visibleToolName ?? toolName,
        canonicalToolName,
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
      // Skip only markers that are explicitly flagged as non-visible (e.g.
      // internal context-injection seeds with no user-facing content).
      // Previously we also skipped method==='rotation' and
      // markerRole==='context_seed', but those are exactly the markers
      // produced by the deferred-rotation compaction path — the most common
      // path. Skipping them meant the backend history never contained a
      // compaction card, so any history refresh after compaction silently
      // dropped the live-streamed compaction box. We now emit a card for all
      // compaction_summary events unless timeline_visible is explicitly false.
      if (event.data.timeline_visible === false) {
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
        if (isTransientCompactionStartSystemMessage(event.data ?? {})) {
          continue;
        }
        if (message) {
          const noticeId = typeof event.data?.notice_id === 'string' ? event.data.notice_id : null;
          const itemId = noticeId ? `system:${noticeId}` : `system:${eid}`;
          const systemMessage = createSystemMessageItem(
            itemId,
            message,
            event.timestamp,
            noticeId,
            typeof event.data?.kind === 'string' ? event.data.kind : null,
            typeof event.data?.scope === 'string' ? event.data.scope : null,
            typeof event.data?.follow_up_conversation_id === 'string'
              ? event.data.follow_up_conversation_id
              : null,
            typeof event.data?.follow_up_session_id === 'string'
              ? event.data.follow_up_session_id
              : null,
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
      } else if (lifecycleEvent === 'tool_call_context_pressure') {
        items.push(createToolCallContextPressureNotice(
          `notice:tool-call-context-pressure:${eid}`,
          event.data,
          event.timestamp
        ));
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
  chatMode?: string,
  chatModeSource?: string,
): TimelineItem[] {
  if (
    clientMessageId
    && items.some((item) => item.kind === 'message'
      && item.role === 'user'
      && item.clientMessageId === clientMessageId)
  ) {
    return items;
  }
  const localId = clientMessageId
    ? `local-user:${clientMessageId}`
    : `local-user:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
  const item = createMessageItem(
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
  );
  // Mint a tail orderKey so this optimistic row sorts AFTER all current
  // timeline items, including in-flight runtime/streaming items that carry
  // the sentinel seq (999999999999999). Using mintClientOrderKey() here
  // placed the message BEFORE runtime items (counter ~1e9 < sentinel ~1e15),
  // causing it to appear above the assistant's in-flight tool calls.
  item.orderKey = mintTailOrderKey(items, 'message:user');
  // Stamp the current chat_mode so the plan-mode marker is visible immediately
  // on the optimistic bubble without waiting for the server echo (Issue D).
  if (chatMode) {
    (item as MessageTimelineItem).chatMode = chatMode as import('$lib/types/api').ChatMode;
  }
  if (chatModeSource) {
    (item as MessageTimelineItem).chatModeSource = chatModeSource as import('$lib/types/api').ChatModeSource;
  }
  return [...items, item];
}

function normalizeOptimisticContent(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function attachmentFingerprint(attachments: AttachmentRef[] | undefined): string {
  return (attachments ?? [])
    .map((attachment) => attachment.artifact_id)
    .filter((artifactId): artifactId is string => typeof artifactId === 'string' && artifactId.length > 0)
    .sort()
    .join('\u0000');
}

function timelineItemTimestampMs(item: TimelineItem): number | null {
  if (!('timestamp' in item) || typeof item.timestamp !== 'string') return null;
  const timestampMs = Date.parse(item.timestamp);
  return Number.isFinite(timestampMs) ? timestampMs : null;
}

function canonicalUserEchoMatchesDraft(item: TimelineItem, draft: OptimisticUserMessageDraft): boolean {
  if (item.kind !== 'message' || item.role !== 'user') return false;
  if (item.id.startsWith('local-user:')) return false;
  if (item.optimistic === true) return false;
  if (item.clientMessageId && item.clientMessageId !== draft.clientMessageId) return false;
  if (item.clientMessageId === draft.clientMessageId) return true;
  const timestampMs = timelineItemTimestampMs(item);
  if (timestampMs === null || timestampMs < draft.createdAt) return false;
  return normalizeOptimisticContent(item.content) === normalizeOptimisticContent(draft.content)
    && attachmentFingerprint(item.attachments) === attachmentFingerprint(draft.attachments);
}

export function reconcileOptimisticUserMessageDraftItems(
  items: TimelineItem[],
  drafts: OptimisticUserMessageDraft[],
): { items: TimelineItem[]; settledClientMessageIds: string[] } {
  let next = items;
  const settledClientMessageIds: string[] = [];
  const consumedCanonicalIndexes = new Set<number>();

  for (const draft of drafts) {
    const canonicalIndex = next.findIndex((item, index) => (
      !consumedCanonicalIndexes.has(index)
      && canonicalUserEchoMatchesDraft(item, draft)
    ));
    if (canonicalIndex >= 0) {
      consumedCanonicalIndexes.add(canonicalIndex);
      settledClientMessageIds.push(draft.clientMessageId);
      continue;
    }
    next = appendOptimisticUserMessage(
      next,
      draft.content,
      draft.attachments,
      draft.clientMessageId,
    );
  }

  return { items: next, settledClientMessageIds };
}



export function removeQueuedUserMessageTimelineItems(items: TimelineItem[], queuedMessages: QueuedMessage[]): TimelineItem[] {
  const queueIds = new Set(
    queuedMessages
      .map((message) => normalizeIdentifier(message.queue_id))
      .filter((id): id is string => id !== null)
  );
  const clientMessageIds = new Set(
    queuedMessages
      .map((message) => normalizeIdentifier(message.client_message_id))
      .filter((id): id is string => id !== null)
  );

  return items.filter((item) => {
    if (item.kind !== 'message' || item.role !== 'user') return true;
    if (item.deliveryStatus === 'queued') return false;
    if (item.id.startsWith('queued-user:')) return false;

    const matchesQueuedMessage = Boolean(
      (item.queueId && queueIds.has(item.queueId))
      || (item.clientMessageId && clientMessageIds.has(item.clientMessageId))
    );
    if (!matchesQueuedMessage) return true;

    // A queued message has not entered conversation order yet. Remove only
    // local/pending placeholders; canonical user_message events keep their
    // final timeline position once the scheduler drains the queue.
    return !(item.optimistic || item.deliveryStatus === 'sending');
  });
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

export function isAuthChallengeInputToolCall(tool: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(tool.toolName);
  if (name === 'requestauthchallenge') return true;
  return (name === 'browserfill' || name === 'browsereval') && hasDeferredAuthChallenge(tool.arguments);
}

function isPendingInputToolCall(tool: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(tool.toolName);
  if (name === 'steprequestquestions' || isAuthChallengeInputToolCall(tool)) return true;
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
  response: string | QuestionSetReply,
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
          ? typeof response === 'string'
            ? { mode: 'plain_text', answers: [{ question_id: 'q1', selected_option_ids: [], custom_answer: response }] }
            : response
          : { response: '<redacted>' },
      ),
    } satisfies ToolCallTimelineItem;
  });
}

export function optimisticallyCancelStepRequestInput(
  items: TimelineItem[],
  toolId: string,
): TimelineItem[] {
  return items.map((item) => {
    if (item.id !== toolId || item.kind !== 'tool_call') return item;
    const tool = item as ToolCallTimelineItem;
    if (!isPendingInputToolCall(tool)) return item;
    return {
      ...tool,
      status: 'completed',
      isError: false,
      result: JSON.stringify({ decision: 'cancel', state: 'cancelled' }),
    } satisfies ToolCallTimelineItem;
  });
}

function _applyWebSocketEventInner(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  let next = [...items];

  if (event.type === 'escalation') {
    // The escalation event carries two different ids:
    //   call_id      = Intaris's internal evaluation id (NOT the tool call id)
    //   tool_call_id = tc.call_id, the real LLM tool call id used by on_tool_call
    //
    // The real tool_call item is keyed by tc.call_id (= tool_call_id).
    // We must NEVER create a ghost tool_call item from an escalation — an
    // escalation is metadata about an existing/imminent tool call, not a tool
    // call itself. Creating a ghost (previously done when no match was found)
    // produced two in-progress bash bubbles: one real (arguments, no eval) and
    // one ghost (eval, no arguments), both stuck in-progress.
    //
    // Fix: use tool_call_id as the canonical correlation key. If the real
    // tool_call item already exists, annotate it. If not, defer the evaluation
    // in the tool_call item when it arrives.
    const realCallId = event.tool_call_id ?? null;
    const evaluation: ToolCallEvaluation = {
      decision: 'escalate',
      reasoning: event.reasoning ?? undefined,
      risk: event.risk ?? undefined,
    };
    const toolName = event.tool_name ?? 'unknown';
    const sessionId = event.session_id ?? null;

    // Try to find the existing tool_call item by the real call id.
    // Also accept a match by the Intaris call_id as a secondary fallback
    // (covers the case where tool_call_id is absent on older server versions).
    const index = next.findIndex((item) => {
      if (item.kind !== 'tool_call') return false;
      if (realCallId && item.callId === realCallId) return true;
      if (realCallId && item.id === `tool:${realCallId}`) return true;
      if (event.call_id && item.callId === event.call_id) return true;
      return false;
    });

    if (index >= 0) {
      // Annotate the existing item — do not replace it.
      const existing = next[index] as ToolCallTimelineItem;
      next[index] = {
        ...existing,
        evaluation: existing.evaluation ?? evaluation,
        sessionId: existing.sessionId ?? sessionId,
        toolName: existing.toolName || toolName,
        status: isTerminalToolStatus(existing.status) ? existing.status : existing.status || 'started',
      };
      _dbg('escalation annotated existing tool_call', realCallId ?? event.call_id);
      return next;
    }

    // No matching tool_call item — do nothing. Production ordering guarantees
    // on_tool_call always precedes the escalation signal, so this path is only
    // reached in degenerate cases (e.g. very old server versions without
    // tool_call_id). Do NOT push a ghost item.
    return next;
  }

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
        optimistic: false,
        deliveryStatus: undefined,
        // Preserve the synthetic orderKey from the optimistic row so the item
        // keeps its position until the next full projection assigns a real key.
        orderKey: existing.orderKey,
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
    // Mint a tail orderKey so this live user_message sorts after all current
    // items including in-flight runtime/streaming items at sentinel seq.
    item.orderKey = mintTailOrderKey(next, 'message:user');
    next.push(item);
    return applyChatModeToTurnMessages(next, turnId, chatMode, chatModeSource);
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
      timestamp: new Date().toISOString(),
      orderKey: mintClientOrderKey('workflow_composed'),
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
      timestamp: new Date().toISOString(),
      orderKey: mintClientOrderKey('notice'),
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
      orderKey: mintClientOrderKey('delegation'),
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
      orderKey: existing?.orderKey ?? mintClientOrderKey('delegation'),
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
      timestamp: new Date().toISOString(),
      orderKey: existing?.orderKey ?? mintClientOrderKey('delegation'),
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
      timestamp: new Date().toISOString(),
      orderKey: mintClientOrderKey('compaction'),
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
    const previousSessionId = event.previous_session_id ?? undefined;
    const itemId = previousSessionId
      ? `compaction:${previousSessionId}:${event.session_id}`
      : `compaction:${event.session_id}`;
    const runningId = `compaction:running:${previousSessionId}`;
    const item: CompactionTimelineItem = {
      id: itemId,
      kind: 'compaction',
      status: 'compacted',
      sessionId: event.session_id,
      previousSessionId,
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
      timestamp: new Date().toISOString(),
      orderKey: mintClientOrderKey('compaction'),
    };
    const runningIndex = next.findIndex((existing) => existing.id === runningId && existing.kind === 'compaction');
    if (runningIndex >= 0) {
      next[runningIndex] = item;
      return next;
    }
    const fallbackRunningIndex = next.findIndex((existing) => (
      existing.kind === 'compaction'
      && existing.status === 'running'
      && (
        existing.sessionId === previousSessionId
        || existing.previousSessionId === previousSessionId
      )
    ));
    if (fallbackRunningIndex >= 0) {
      next[fallbackRunningIndex] = item;
      return next;
    }
    const runningCompactionIndexes = next
      .map((existing, index) => ({ existing, index }))
      .filter(({ existing }) => existing.kind === 'compaction' && existing.status === 'running')
      .map(({ index }) => index);
    if (runningCompactionIndexes.length === 1) {
      next[runningCompactionIndexes[0]] = item;
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
    if (isTransientCompactionStartSystemMessage({
      message: event.text,
      text: event.text,
      kind: event.kind,
      status: (event as { status?: unknown }).status,
    })) {
      return next;
    }
    if (isTerminalCompactionNotice(event.text)) {
      next = removeRunningCompactionItems(next, event.session_id);
    }
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
      followUpConversationId: event.follow_up_conversation_id ?? null,
      followUpSessionId: event.follow_up_session_id ?? null,
      timestamp: new Date().toISOString(),
      orderKey: mintClientOrderKey('system_message'),
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

/**
 * Apply a single low-frequency WebSocket event to a timeline array and return
 * a new sorted, deduped array.
 *
 * Called by ChatTimeline.applyEvent() for all events except `timeline_patch`
 * (rAF-batched via enqueuePatch) and `message_complete` (handled by
 * _finalizeStreamingAssistant in the store). The result is reconciled into the
 * store's SvelteMap via _reconcileMap (surgical diff, no clear).
 */
export function applyWebSocketEvent(items: TimelineItem[], event: CognisWebSocketEvent): TimelineItem[] {
  const result = _applyWebSocketEventInner(items, event);
  // Normalise so any id or render-key collision is collapsed before the result
  // reaches the renderer. normalizeTimelineItems also sorts by orderKey.
  const normalized = normalizeTimelineItems(result);
  if (_timelineDebugEnabled()) {
    const beforeAssistants = result.filter((i) => i.kind === 'message' && (i as MessageTimelineItem).role === 'assistant').length;
    const afterAssistants = normalized.filter((i) => i.kind === 'message' && (i as MessageTimelineItem).role === 'assistant').length;
    if (beforeAssistants !== afterAssistants) {
      _dbg(`normalize collapsed ${beforeAssistants - afterAssistants} assistant duplicate(s) on ${event.type}`);
    }
  }
  return normalized;
}
