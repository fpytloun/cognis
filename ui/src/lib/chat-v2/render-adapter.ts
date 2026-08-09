/**
 * Pure, per-item adapters that convert a single canonical Chat v2
 * timeline item into the leaf renderer's prop shape ($lib/chat types).
 *
 * This is a render-boundary conversion only: it is applied per item at
 * render time and carries NO mutable state, no merge/reconcile, and no
 * identity churn. Item identity and ordering are owned entirely by the
 * Chat v2 store (`chatV2Store.visibleItems`). This is only a render-boundary
 * adapter; state is never routed through a second mutable timeline store.
 */
import { renderMarkdown } from '$lib/markdown';
import { normalizeFileDiffs } from '$lib/diff';
import { parseTodoSnapshot } from '$lib/todos';
import type {
  AssistantDeliverableTimelineItem,
  CompactionTimelineItem,
  DelegationRuntime,
  DelegationTimelineItem,
  MessageTimelineItem,
  NoticeTimelineItem,
  SystemMessageTimelineItem,
  ThinkingBlock,
  ThinkingTimelineItem,
  TimelineItem as RenderTimelineItem,
  ToolCallTimelineItem,
  UserInteractionTimelineItem
} from '$lib/timeline-render-model';
import type {
  CompactionTimelineItem as ChatV2CompactionTimelineItem,
  DelegationTimelineItem as ChatV2DelegationTimelineItem,
  ErrorTimelineItem,
  ManagedConversationTimelineItem,
  NoticeTimelineItem as ChatV2NoticeTimelineItem,
  TaskTimelineItem,
  ThinkingTimelineItem as ChatV2ThinkingTimelineItem,
  TimelineItem as ChatV2TimelineItem,
  ToolCallTimelineItem as ChatV2ToolCallTimelineItem
} from './types';

export type RenderMarkdownContent = (content: string, key: string, streaming: boolean) => string;

// Kinds that produce a visible timeline row. Anything else (e.g. todo_state,
// or an unknown future kind) renders nothing and must be excluded BEFORE
// windowing so the visible window counts real rows only — otherwise a tail of
// non-row items could push real messages out of the window.
const RENDERABLE_KINDS: ReadonlySet<string> = new Set([
  'message',
  'thinking',
  'tool_call',
  'delegation',
  'managed_conversation',
  'task',
  'notice',
  'error',
  'artifact',
  'assistant_deliverable',
  'file_diff',
  'compaction',
  'question_set',
  'auth_challenge',
  'credential_request',
  'user_interaction'
]);

/**
 * Whether a Chat v2 item produces a visible timeline row. Cheap (kind check
 * only) so it can be used to filter before windowing without re-running the
 * markdown-heavy conversion.
 */
export function isRenderableChatV2Item(item: ChatV2TimelineItem): boolean {
  return RENDERABLE_KINDS.has(item.kind);
}

/**
 * Convert a single Chat v2 timeline item to its legacy leaf prop shape.
 * Returns null for items that have no visible row (e.g. todo_state).
 */
export function toRenderItem(
  item: ChatV2TimelineItem,
  renderMarkdownContent: RenderMarkdownContent = statelessMarkdown
): RenderTimelineItem | null {
  return convertToRenderItem(item, renderMarkdownContent);
}

function convertToRenderItem(
  item: ChatV2TimelineItem,
  renderMarkdownContent: RenderMarkdownContent
): RenderTimelineItem | null {
  switch (item.kind) {
    case 'message':
      return toRenderMessage(item, renderMarkdownContent);
    case 'thinking':
      return toRenderThinking(item, renderMarkdownContent);
    case 'tool_call':
      return toRenderToolCall(item);
    case 'delegation':
      return toRenderDelegation(item);
    case 'managed_conversation':
      return toRenderManagedConversation(item);
    case 'task':
      return toRenderTask(item);
    case 'notice':
      return toRenderNotice(item);
    case 'error':
      return toRenderError(item);
    case 'compaction':
      return toRenderCompaction(item);
    case 'artifact':
      return notice(
        item.id,
        'Artifact',
        item.title ?? item.artifact_id,
        item.sort_key,
        item.created_at ?? item.updated_at ?? null
      );
    case 'assistant_deliverable':
      return toRenderAssistantDeliverable(item);
    case 'file_diff':
      return notice(
        item.id,
        item.title ?? 'File diff',
        item.file_diffs.map((diff) => diff.path).join(', '),
        item.sort_key,
        item.created_at ?? item.updated_at ?? null
      );
    case 'todo_state':
      return null;
    case 'question_set':
      return notice(
        item.id,
        item.title ?? 'Question set',
        `${item.questions.length} question${item.questions.length === 1 ? '' : 's'} pending`,
        item.sort_key,
        item.created_at ?? item.updated_at ?? null,
        'warning'
      );
    case 'auth_challenge':
      return notice(
        item.id,
        item.label,
        item.message,
        item.sort_key,
        item.created_at ?? item.updated_at ?? null,
        'warning'
      );
    case 'credential_request':
      return notice(
        item.id,
        item.label,
        item.description ?? 'Action required',
        item.sort_key,
        item.created_at ?? item.updated_at ?? null,
        'warning'
      );
    case 'user_interaction':
      return {
        id: item.id,
        kind: 'user_interaction',
        orderKey: item.sort_key,
        timestamp: item.created_at ?? item.updated_at ?? null,
        interactionType: item.interaction_type,
        originCallId: item.origin_call_id ?? null,
        title: item.title,
        summary: item.summary ?? null,
        answers: item.answers.map((answer) => ({
          question: answer.question ?? null,
          answer: answer.answer
        })),
        status: item.status
      } satisfies UserInteractionTimelineItem;
    default:
      return null;
  }
}

/**
 * Convert a list of Chat v2 items, dropping items without a visible row.
 * Order is preserved exactly from the input (caller owns ordering).
 */
export function toRenderItems(items: ChatV2TimelineItem[]): RenderTimelineItem[] {
  return items
    .map((item) => toRenderItem(item))
    .filter((item): item is RenderTimelineItem => item !== null);
}

function statelessMarkdown(content: string): string {
  return renderMarkdown(content);
}

function toRenderAssistantDeliverable(
  item: Extract<ChatV2TimelineItem, { kind: 'assistant_deliverable' }>
): AssistantDeliverableTimelineItem {
  return {
    id: item.id,
    kind: 'assistant_deliverable',
    orderKey: item.sort_key,
    sourceRefs: item.source_refs?.map((ref) => `${ref.store}:${ref.session_id}:${ref.seq}`) ?? [],
    createdAt: item.created_at ?? undefined,
    updatedAt: item.updated_at ?? undefined,
    deliverableId: item.deliverable_id,
    format: item.format,
    title: item.title ?? undefined,
    content: item.content ?? undefined,
  };
}

function toRenderMessage(
  item: Extract<ChatV2TimelineItem, { kind: 'message' }>,
  renderMarkdownContent: RenderMarkdownContent
): MessageTimelineItem | SystemMessageTimelineItem {
  if (item.role === 'system') {
    return toRenderSystemMessage(item);
  }
  const streaming = item.stable === false && (item.status === 'running' || item.partial === true);
  return {
    id: item.id,
    kind: 'message',
    sessionId: firstSessionId(item),
    role: item.role,
    content: item.content,
    html: renderMarkdownContent(item.content, `message:${item.id}`, streaming),
    seq: firstSeq(item),
    timestamp: item.created_at ?? item.updated_at ?? null,
    turnId: item.turn_id ?? null,
    messageId: item.message_id ?? undefined,
    // Streaming is derived strictly from backend flags. Once the runtime
    // overlay clears (has_active_turn=false), the store terminalizes items
    // so this resolves to false and no message hangs "in progress".
    streaming,
    attachments: item.attachments,
    optimistic: item.role === 'user' && item.source_refs.length === 0,
    deliveryStatus: item.role === 'user' && item.source_refs.length === 0
      ? item.status === 'failed'
        ? 'failed'
        : item.status === 'pending'
          ? 'sending'
          : undefined
      : undefined,
    clientMessageId: item.client_message_id ?? null,
    partial: item.partial,
    assistantPhaseIndex: item.assistant_phase_index ?? undefined,
    turnCycleIndex: item.turn_cycle_index ?? undefined,
    chatMode: item.chat_mode ?? undefined,
    chatModeSource: item.chat_mode_source as MessageTimelineItem['chatModeSource'],
    orderKey: item.sort_key
  } satisfies MessageTimelineItem;
}

function toRenderSystemMessage(
  item: Extract<ChatV2TimelineItem, { kind: 'message' }>
): SystemMessageTimelineItem {
  return {
    id: item.id,
    kind: 'system_message',
    text: item.content,
    noticeId: item.notice_id ?? null,
    noticeKind: item.notice_kind ?? null,
    noticeScope: item.notice_scope ?? null,
    retryReason: item.retry_reason ?? null,
    retrySourceTurnId: item.retry_source_turn_id ?? null,
    reasonClass: item.reason_class ?? null,
    providerId: item.provider_id ?? null,
    model: item.model ?? null,
    retryAfterSeconds: item.retry_after_seconds ?? null,
    providerRetryAfterSeconds: item.provider_retry_after_seconds ?? null,
    retryAt: item.retry_at ?? null,
    attempt: item.attempt ?? null,
    maxAttempts: item.max_attempts ?? null,
    attempts: item.attempts ?? null,
    attemptsPerCycle: item.attempts_per_cycle ?? null,
    continuationAttempts: item.continuation_attempts ?? null,
    recoverable: item.recoverable ?? null,
    followUpConversationId: item.follow_up_conversation_id ?? null,
    followUpSessionId: item.follow_up_session_id ?? null,
    timestamp: item.created_at ?? item.updated_at ?? null,
    orderKey: item.sort_key
  };
}

function toRenderThinking(
  item: ChatV2ThinkingTimelineItem,
  renderMarkdownContent: RenderMarkdownContent
): ThinkingTimelineItem {
  // 1:1 conversion. Each canonical thinking item already maps to a single
  // rendered thinking block; there is no merging across items here.
  const blocks = item.blocks.map((block): ThinkingBlock => {
    const content = block.content ?? '';
    const blockStatus = block.status ?? item.status;
    return {
      block_id: block.id,
      title: block.title ?? 'Thinking',
      content,
      html: renderMarkdownContent(content, `thinking:${item.id}:${block.id}`, blockStatus === 'running'),
      source: 'summary',
      complete: blockStatus !== 'running',
      startedAt: block.started_at ?? null,
      completedAt: block.completed_at ?? null,
      durationMs: block.duration_ms ?? null,
      providerBlockIndex: null
    };
  });
  return {
    id: item.id,
    kind: 'thinking',
    sessionId: firstSessionId(item),
    messageId: item.message_id ?? item.id,
    turnId: item.turn_id ?? null,
    blocks,
    assistantPhaseIndex: item.assistant_phase_index ?? undefined,
    turnCycleIndex: item.turn_cycle_index ?? undefined,
    streaming: item.status === 'running' || blocks.some((block) => !block.complete),
    activeTitle: item.active_title ?? blocks.find((block) => !block.complete)?.title ?? null,
    timestamp: item.created_at ?? item.updated_at ?? null,
    orderKey: item.sort_key
  };
}

function toRenderToolCall(item: ChatV2ToolCallTimelineItem): ToolCallTimelineItem {
  const status = normalizeToolStatus(item.status);
  return {
    id: item.id,
    kind: 'tool_call',
    callId: item.call_id,
    toolName: item.tool_name ?? 'tool',
    displayToolName: item.display_name ?? undefined,
    status,
    timestamp: item.created_at ?? item.updated_at ?? null,
    sessionId: firstSessionId(item),
    turnId: item.turn_id ?? null,
    // Prefer the structured, named arguments so per-tool subtitles and rich
    // bodies render correctly; fall back to the raw preview string only when no
    // structured args were projected.
    arguments: toRenderArguments(item),
    result: item.result_preview ?? undefined,
    streamedOutput: item.streamed_output ?? item.result_preview ?? undefined,
    isError: item.is_error,
    durationMs: item.duration_ms ?? undefined,
    attachments: item.attachments,
    fileDiffs: normalizeFileDiffs(item.file_diffs),
    outputSize: item.output_size ?? undefined,
    truncated: item.truncated,
    hasFullOutput: item.has_full_output,
    recoveryCallId: item.recovery_call_id ?? null,
    toolOutputArtifactId: item.tool_output_artifact_id ?? null,
    evaluation: toRenderEvaluation(item.evaluation),
    progressPhase: item.progress_phase ?? undefined,
    progressInputChars: item.progress_input_chars ?? undefined,
    progressInputLines: item.progress_input_lines ?? undefined,
    progressComplete: item.progress_complete ?? undefined,
    managedConversation: item.managed_conversation ?? undefined,
    delegation: toRenderDelegationRuntime(item.delegation),
    assistantPhaseIndex: item.assistant_phase_index ?? undefined,
    turnCycleIndex: item.turn_cycle_index ?? undefined,
    orderKey: item.sort_key
  };
}

function toRenderArguments(
  item: ChatV2ToolCallTimelineItem
): Record<string, unknown> | undefined {
  if (item.arguments && typeof item.arguments === 'object') {
    return item.arguments;
  }
  return item.arguments_preview ? { preview: item.arguments_preview } : undefined;
}

function toRenderDelegationRuntime(
  value: Record<string, unknown> | null | undefined
): DelegationRuntime | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const todos = value.todos ? parseTodoSnapshot(value.todos) : undefined;
  return {
    childSessionId: nullableString(value.child_session_id),
    status: nullableString(value.status),
    agentId: nullableString(value.agent_id),
    usedAgentId: nullableString(value.used_agent_id),
    title: nullableString(value.title),
    summary: nullableString(value.summary),
    startedAt: nullableString(value.started_at),
    durationMs: nullableNumber(value.duration_ms),
    resultSummary: nullableString(value.result_summary),
    resultContent: nullableString(value.result_content),
    resultSource: nullableString(value.result_source),
    resultTruncated: nullableBoolean(value.result_truncated),
    resultAnchors: isRecord(value.result_anchors) ? value.result_anchors : undefined,
    todos,
    toolCallCount: nullableNumber(value.tool_call_count),
    maxToolCalls: nullableNumber(value.max_tool_calls),
    lastTool: nullableString(value.last_tool),
    error: nullableString(value.error)
  };
}

function toRenderEvaluation(value: unknown): ToolCallTimelineItem['evaluation'] {
  if (!isRecord(value) || typeof value.decision !== 'string') return undefined;
  return {
    decision: value.decision,
    reasoning: optionalString(value.reasoning),
    risk: optionalString(value.risk),
    path: optionalString(value.path),
    latency_ms: optionalNumber(value.latency_ms)
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function nullableString(value: unknown): string | null {
  return optionalString(value) ?? null;
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function nullableNumber(value: unknown): number | null {
  return optionalNumber(value) ?? null;
}

function nullableBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function toRenderDelegation(item: ChatV2DelegationTimelineItem): DelegationTimelineItem {
  return {
    id: item.id,
    kind: 'delegation',
    taskId: item.child_session_id,
    taskLabel: item.title ?? 'Sub-session',
    agentId: item.agent_id ?? null,
    usedAgentId: item.used_agent_id ?? item.agent_id ?? null,
    status: normalizeDelegationStatus(item.status),
    result: item.result_summary ?? null,
    timestamp: item.created_at ?? item.updated_at ?? null,
    todos: item.todos ? parseTodoSnapshot(item.todos) : undefined,
    toolCallCount: item.tool_call_count ?? undefined,
    maxToolCalls: item.max_tool_calls ?? undefined,
    lastTool: item.last_tool ?? undefined,
    orderKey: item.sort_key
  };
}

function toRenderManagedConversation(item: ManagedConversationTimelineItem): DelegationTimelineItem {
  return {
    id: item.id,
    kind: 'delegation',
    taskId: item.managed_conversation_id,
    taskLabel: item.title ?? 'Managed conversation',
    agentId: item.agent_id ?? null,
    usedAgentId: item.agent_id ?? null,
    status: normalizeDelegationStatus(item.status),
    result: item.result_summary ?? null,
    timestamp: item.created_at ?? item.updated_at ?? null,
    orderKey: item.sort_key
  };
}

function toRenderTask(item: TaskTimelineItem): NoticeTimelineItem {
  return notice(
    item.id,
    item.title,
    item.result_summary ?? item.status,
    item.sort_key,
    item.created_at ?? item.updated_at ?? null
  );
}

function toRenderNotice(item: ChatV2NoticeTimelineItem): NoticeTimelineItem {
  return notice(
    item.id,
    item.title,
    item.message ?? '',
    item.sort_key,
    item.created_at ?? item.updated_at ?? null,
    item.level === 'warning' ? 'warning' : 'info'
  );
}

function toRenderCompaction(item: ChatV2CompactionTimelineItem): CompactionTimelineItem {
  return {
    id: item.id,
    kind: 'compaction',
    status: item.status,
    sessionId: item.session_id ?? undefined,
    previousSessionId: item.previous_session_id ?? undefined,
    summaryPreview: item.summary_preview,
    summary: item.summary ?? undefined,
    method: item.method,
    turnsCompacted: item.turns_compacted,
    trigger: item.trigger ?? undefined,
    reason: item.reason ?? undefined,
    previousUsagePercentage: item.previous_usage_percentage ?? null,
    effectiveUsagePercentage: item.effective_usage_percentage ?? null,
    hardPressureExceeded: item.hard_pressure_exceeded ?? false,
    usedTimeoutFallback: item.used_timeout_fallback ?? false,
    timestamp: item.created_at ?? item.updated_at ?? null,
    orderKey: item.sort_key
  };
}

function toRenderError(item: ErrorTimelineItem): NoticeTimelineItem {
  return notice(
    item.id,
    item.title ?? item.error_code ?? 'Error',
    item.message ?? '',
    item.sort_key,
    item.created_at ?? item.updated_at ?? null,
    'error'
  );
}

function notice(
  id: string,
  title: string,
  description: string,
  orderKey: string,
  timestamp: string | null,
  tone: NoticeTimelineItem['tone'] = 'info'
): NoticeTimelineItem {
  return {
    id,
    kind: 'notice',
    title,
    description,
    tone,
    timestamp,
    orderKey
  };
}

function normalizeDelegationStatus(value: string | undefined): DelegationTimelineItem['status'] {
  if (value === 'complete') return 'completed';
  if (value === 'running' || value === 'failed' || value === 'cancelled' || value === 'completed') return value;
  if (value === 'waiting') return 'paused';
  return 'started';
}

function normalizeToolStatus(value: string | null | undefined): string {
  if (value === 'complete') return 'completed';
  return value ?? 'started';
}

function firstSessionId(item: ChatV2TimelineItem): string | null {
  return item.source_refs[0]?.session_id ?? null;
}

function firstSeq(item: ChatV2TimelineItem): number | null {
  return item.source_refs[0]?.seq ?? null;
}
