import type { AttachmentRef, ContextUsage } from '$lib/types/api';

export type ChatV2SchemaVersion = 2;
export type ChatMode = 'default' | 'plan' | 'build';
export type TimelineItemStatus =
  | 'pending'
  | 'running'
  | 'waiting'
  | 'complete'
  | 'failed'
  | 'cancelled'
  | 'compacted'
  | 'skipped';
export type ChatResetReason =
  | 'cursor_invalid'
  | 'cursor_expired'
  | 'projection_version_changed'
  | 'lineage_changed'
  | 'history_compacted'
  | 'range_too_large'
  | 'server_restart_lost_runtime'
  | 'unsupported_cursor';

export interface SourceRef {
  store: string;
  session_id: string;
  seq: number;
  event_id?: string | null;
  event_type: string;
}

export interface FileDiffRef {
  path: string;
  diff: string;
}

export interface ThinkingBlock {
  id: string;
  title?: string | null;
  content: string;
  status?: 'running' | 'complete' | 'failed' | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
}

export interface QuestionSpec {
  id: string;
  question: string;
  header?: string | null;
  options: Array<Record<string, unknown>>;
  multiple: boolean;
  allow_custom: boolean;
  required: boolean;
}

interface TimelineItemBase {
  id: string;
  kind: string;
  sort_key: string;
  source_refs: SourceRef[];
  created_at?: string | null;
  updated_at?: string | null;
  status?: TimelineItemStatus | null;
  stable: boolean;
}

export interface MessageTimelineItem extends TimelineItemBase {
  kind: 'message';
  role: 'user' | 'assistant' | 'system';
  content: string;
  message_id: string;
  client_message_id?: string | null;
  client_txn_id?: string | null;
  turn_id?: string | null;
  assistant_phase_index?: number | null;
  turn_cycle_index?: number | null;
  notice_id?: string | null;
  notice_kind?: string | null;
  notice_scope?: string | null;
  follow_up_conversation_id?: string | null;
  follow_up_session_id?: string | null;
  attachments: AttachmentRef[];
  partial: boolean;
  chat_mode?: ChatMode | null;
  chat_mode_source?: string | null;
}

export interface ThinkingTimelineItem extends TimelineItemBase {
  kind: 'thinking';
  message_id?: string | null;
  turn_id?: string | null;
  assistant_phase_index?: number | null;
  turn_cycle_index?: number | null;
  blocks: ThinkingBlock[];
  active_title?: string | null;
}

export interface ToolCallTimelineItem extends TimelineItemBase {
  kind: 'tool_call';
  call_id: string;
  tool_name: string;
  display_name?: string | null;
  turn_id?: string | null;
  assistant_phase_index?: number | null;
  turn_cycle_index?: number | null;
  arguments?: Record<string, unknown> | null;
  arguments_preview?: string | null;
  result_preview?: string | null;
  streamed_output?: string | null;
  is_error: boolean;
  duration_ms?: number | null;
  attachments: AttachmentRef[];
  file_diffs: FileDiffRef[];
  output_size?: number | null;
  truncated: boolean;
  has_full_output: boolean;
  recovery_call_id?: string | null;
  tool_output_artifact_id?: string | null;
  evaluation?: Record<string, unknown> | null;
  progress_phase?: string | null;
  progress_input_chars?: number | null;
  progress_input_lines?: number | null;
  progress_complete?: boolean | null;
  delegation?: Record<string, unknown> | null;
}

export interface DelegationTimelineItem extends TimelineItemBase {
  kind: 'delegation';
  child_session_id: string;
  turn_id?: string | null;
  assistant_phase_index?: number | null;
  turn_cycle_index?: number | null;
  agent_id?: string | null;
  used_agent_id?: string | null;
  title?: string | null;
  summary?: string | null;
  status: TimelineItemStatus;
  result_summary?: string | null;
  result_anchors?: Record<string, string> | null;
  todos?: Array<Record<string, unknown>> | null;
  tool_call_count?: number | null;
  max_tool_calls?: number | null;
  last_tool?: string | null;
}

export interface ManagedConversationTimelineItem extends TimelineItemBase {
  kind: 'managed_conversation';
  managed_conversation_id: string;
  agent_id: string;
  title?: string | null;
  status: TimelineItemStatus;
  result_summary?: string | null;
}

export interface TaskTimelineItem extends TimelineItemBase {
  kind: 'task';
  task_id: string;
  title: string;
  workflow_id?: string | null;
  workflow_step?: string | null;
  status: TimelineItemStatus;
  result_summary?: string | null;
  deliverable_ids: string[];
}

export interface QuestionSetTimelineItem extends TimelineItemBase {
  kind: 'question_set';
  request_id: string;
  title?: string | null;
  questions: QuestionSpec[];
  status: 'waiting' | 'complete' | 'cancelled';
}

export interface AuthChallengeTimelineItem extends TimelineItemBase {
  kind: 'auth_challenge';
  challenge_id: string;
  challenge_kind: string;
  label: string;
  message: string;
  metadata: Record<string, unknown>;
  required_fields: string[];
  status: 'waiting' | 'complete' | 'cancelled' | 'failed';
}

export interface CredentialRequestTimelineItem extends TimelineItemBase {
  kind: 'credential_request';
  credential_request_id: string;
  credential_id: string;
  credential_kind: string;
  label: string;
  description?: string | null;
  required_fields: string[];
  status: 'waiting' | 'complete' | 'cancelled' | 'failed';
}

export interface TodoStateTimelineItem extends TimelineItemBase {
  kind: 'todo_state';
  todos: Array<Record<string, unknown>>;
}

export interface ArtifactTimelineItem extends TimelineItemBase {
  kind: 'artifact';
  artifact_id: string;
  filename: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  title?: string | null;
}

export interface FileDiffTimelineItem extends TimelineItemBase {
  kind: 'file_diff';
  file_diffs: FileDiffRef[];
  title?: string | null;
}

export interface NoticeTimelineItem extends TimelineItemBase {
  kind: 'notice';
  level: 'info' | 'warning';
  title: string;
  message?: string | null;
}

export interface CompactionTimelineItem extends TimelineItemBase {
  kind: 'compaction';
  status: 'running' | 'compacted' | 'failed' | 'skipped';
  session_id?: string | null;
  previous_session_id?: string | null;
  summary_preview: string;
  summary?: string | null;
  method: string;
  turns_compacted: number;
  trigger?: string | null;
  reason?: string | null;
  previous_usage_percentage?: number | null;
  effective_usage_percentage?: number | null;
  hard_pressure_exceeded?: boolean;
  used_timeout_fallback?: boolean;
}

export interface ErrorTimelineItem extends TimelineItemBase {
  kind: 'error';
  level: 'error';
  title: string;
  message?: string | null;
  error_code?: string | null;
  recoverable: boolean;
}

export type TimelineItem =
  | MessageTimelineItem
  | ThinkingTimelineItem
  | ToolCallTimelineItem
  | DelegationTimelineItem
  | ManagedConversationTimelineItem
  | TaskTimelineItem
  | QuestionSetTimelineItem
  | AuthChallengeTimelineItem
  | CredentialRequestTimelineItem
  | TodoStateTimelineItem
  | ArtifactTimelineItem
  | FileDiffTimelineItem
  | NoticeTimelineItem
  | CompactionTimelineItem
  | ErrorTimelineItem;

export interface TurnCycleState {
  turn_id: string;
  turn_cycle_index: number;
  lifecycle_status: 'open' | 'complete';
  has_tool_activity: boolean;
}

export interface TimelineWindow {
  items: TimelineItem[];
  cycle_states?: TurnCycleState[];
  has_more_before: boolean;
  before_cursor?: string | null;
}

export interface RuntimeActiveTurn {
  turn_id: string;
  session_id: string;
  status: 'starting' | 'running' | 'waiting' | 'cancelling';
  chat_mode?: ChatMode | null;
  chat_mode_source?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
}

export interface RuntimeOverlaySnapshot {
  runtime_epoch: string;
  runtime_revision: number;
  generated_at: string;
  has_active_turn: boolean;
  active_turn?: RuntimeActiveTurn | null;
  volatile_items: TimelineItem[];
  cycle_states?: TurnCycleState[];
  context_usage?: ContextUsage | null;
}

export interface ConversationSummary {
  conversation_id: string;
  title?: string | null;
  agent_id: string;
  agent_profile_id?: string | null;
  project_id?: string | null;
  status: string;
  active_session_id?: string | null;
  last_message_at?: string | null;
  last_read_at?: string | null;
}

export interface QueueMessage {
  queue_id: string;
  client_message_id?: string | null;
  client_txn_id?: string | null;
  content: string;
  attachments: AttachmentRef[];
  position: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface QueueState {
  messages: QueueMessage[];
  queued_count: number;
}

export interface ConversationStateView {
  state_version: number;
  snapshot_generated_at: string;
  capabilities: string[];
  active_turn: Record<string, unknown>;
  pending: Record<string, unknown>;
  active_session: Record<string, unknown>;
  task?: Record<string, unknown> | null;
}

export interface ChatSnapshot {
  schema_version: ChatV2SchemaVersion;
  projection_version: string;
  conversation: ConversationSummary;
  timeline: TimelineWindow;
  state: ConversationStateView;
  queue: QueueState;
  runtime: RuntimeOverlaySnapshot;
  cursor: string;
  server_time: string;
}

export type ChatViewOp =
  | { op: 'upsert_item'; item: TimelineItem }
  | { op: 'remove_item'; id: string; reason?: string | null }
  | { op: 'replace_conversation'; conversation: ConversationSummary }
  | { op: 'replace_state'; state: ConversationStateView }
  | { op: 'replace_queue'; queue: QueueState }
  | { op: 'reset'; reason: ChatResetReason };

export interface ChatSyncResponse {
  schema_version: ChatV2SchemaVersion;
  projection_version: string;
  conversation_id: string;
  cursor_before: string;
  cursor_after: string;
  ops: ChatViewOp[];
  cycle_states?: TurnCycleState[];
  runtime?: RuntimeOverlaySnapshot | null;
  reset_required: boolean;
  reset_reason?: ChatResetReason | null;
  has_more: boolean;
  server_time: string;
}

export interface ChatRealtimeFrame {
  type: 'chat_v2_frame';
  schema_version: ChatV2SchemaVersion;
  projection_version: string;
  conversation_id: string;
  cursor_before: string;
  cursor_after: string;
  ops: ChatViewOp[];
  cycle_states?: TurnCycleState[];
  runtime?: RuntimeOverlaySnapshot | null;
  server_time: string;
}

export interface SendMessageV2Request {
  client_message_id: string;
  content: string;
  attachments: AttachmentRef[];
  chat_mode?: ChatMode | null;
}

export interface SendMessageV2Response {
  status: 'accepted' | 'queued' | 'duplicate';
  client_txn_id: string;
  client_message_id: string;
  conversation_id: string;
  message_id?: string | null;
  queue_id?: string | null;
  cursor?: string | null;
  server_time: string;
}

export interface ControlMutationV2Request {
  client_txn_id: string;
}

export interface QueueUpdateV2Request {
  client_txn_id: string;
  content: string;
}

export interface QueueMutationResponse {
  conversation_id: string;
  client_txn_id: string;
  status: 'deleted' | 'updated' | 'duplicate';
  queue: QueueState;
  cursor?: string | null;
  runtime?: RuntimeOverlaySnapshot | null;
  server_time: string;
}

export interface CancelTurnV2Response {
  conversation_id: string;
  client_txn_id: string;
  status: 'cancelled' | 'idle' | 'duplicate';
  runtime?: RuntimeOverlaySnapshot | null;
  server_time: string;
}

export interface TimelineBackfillResponse {
  schema_version: ChatV2SchemaVersion;
  projection_version: string;
  conversation_id: string;
  items: TimelineItem[];
  cycle_states?: TurnCycleState[];
  has_more_before: boolean;
  before_cursor?: string | null;
  server_time: string;
}
