/**
 * Presentation-only DTOs consumed by the established timeline leaf components.
 *
 * This module owns no canonical timeline state, identity, synchronization,
 * reconciliation, projection, polling, or realtime behavior. ChatV2 remains
 * authoritative; render-adapter.ts performs a stateless conversion into these
 * leaf props at the component boundary.
 */
import type { FileDiff } from '$lib/diff';
import type { TodoSnapshotItem } from '$lib/todos';
import type { AttachmentRef, ChatMode, ChatModeSource, MessageRuntimeMetadata } from '$lib/types/api';

export type TimelineItem =
  | MessageTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | AssistantDeliverableTimelineItem
  | WorkflowComposedTimelineItem
  | NoticeTimelineItem
  | SystemMessageTimelineItem
  | UserInteractionTimelineItem
  | CompactionTimelineItem
  | ThinkingTimelineItem;

export interface AssistantDeliverableTimelineItem {
  id: string;
  kind: 'assistant_deliverable';
  orderKey?: string;
  sourceRefs?: string[];
  createdAt?: string;
  updatedAt?: string;
  deliverableId: string;
  format: string;
  title?: string | null;
  content?: string | null;
}

export interface UserInteractionTimelineItem {
  id: string;
  kind: 'user_interaction';
  orderKey?: string;
  timestamp: string | null;
  interactionType: string;
  originCallId?: string | null;
  title: string;
  summary?: string | null;
  answers: Array<{ question?: string | null; answer: string }>;
  status: 'complete' | 'cancelled' | 'denied' | 'failed';
}

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
  managedConversation?: Record<string, unknown>;
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

export type { TodoSnapshotItem };

export function isTerminalToolStatus(status: string | null | undefined): boolean {
  return ['completed', 'failed', 'cancelled'].includes(status ?? '');
}

export function isActiveToolStatus(status: string | null | undefined): boolean {
  return !isTerminalToolStatus(status);
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
  retryReason?: string | null;
  retrySourceTurnId?: string | null;
  reasonClass?: string | null;
  providerId?: string | null;
  model?: string | null;
  retryAfterSeconds?: number | null;
  providerRetryAfterSeconds?: number | null;
  retryAt?: string | null;
  attempt?: number | null;
  maxAttempts?: number | null;
  attempts?: number | null;
  attemptsPerCycle?: number | null;
  continuationAttempts?: number | null;
  recoverable?: boolean | null;
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
