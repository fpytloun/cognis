export type UserRole = 'admin' | 'user' | 'viewer' | 'service';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

export interface UserSummary {
  email: string;
  name: string | null;
  role: UserRole;
}

export interface TokenResponse {
  token: string;
  refresh_token: string | null;
  expires_in: number;
  user: UserSummary;
}

export interface ExchangeTokenResponse {
  token: string;
  target: string;
  expires_in: number;
}

export interface BootstrapStatusResponse {
  setup_available: boolean;
  setup_complete: boolean;
}

export interface ApiKeyMetadata {
  key_id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface ApiKeyCreateResponse extends ApiKeyMetadata {
  api_key: string;
}

export interface ProviderTestResult {
  ok: boolean;
  model_resolved: string | null;
  latency_ms: number | null;
  error_type: string | null;
  error_detail: string | null;
  tested_at: string | null;
}

export interface ApiErrorPayload {
  code?: string;
  message?: string;
  details?: Record<string, unknown> | null;
}

export interface ApiErrorResponse {
  error?: ApiErrorPayload;
  detail?: string | ApiErrorPayload;
}

export interface CursorPage<T> {
  items: T[];
  cursor: string | null;
  has_more: boolean;
}

export interface ConversationContext {
  type: string;
  ref: string | null;
  platform_data: Record<string, unknown>;
  memory_labels: Record<string, string>;
}

export interface Conversation {
  conversation_id: string;
  user_email: string;
  agent_id: string;
  title: string | null;
  context: ConversationContext;
  root_session_id: string | null;
  status: string;
  last_message_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MessageEvent {
  seq: number | null;
  type: string;
  data: Record<string, unknown>;
  timestamp: string | null;
}

export interface MessageHistoryResponse {
  items: MessageEvent[];
  last_seq: number;
  has_more: boolean;
}

export interface Session {
  session_id: string;
  conversation_id: string;
  parent_session_id: string | null;
  user_email: string;
  agent_id: string;
  delegation_mode: string | null;
  delegation_task: string | null;
  status: string;
  intaris_session_id: string | null;
  mnemory_session_id: string | null;
  started_at: string | null;
  idle_since: string | null;
  completed_at: string | null;
  result_summary: string | null;
  updated_at: string | null;
}

export interface SessionEventsResponse {
  session_id: string;
  items: MessageEvent[];
  last_seq: number;
  has_more: boolean;
}

export interface Agent {
  agent_id: string;
  owner_email: string;
  name: string;
  display_name: string | null;
  description: string | null;
  system_prompt: string | null;
  personality: Record<string, unknown> | null;
  skills: Record<string, unknown> | null;
  tools: Record<string, unknown> | null;
  permissions: Record<string, unknown> | null;
  llm_config: Record<string, unknown> | null;
  execution: Record<string, unknown> | null;
  personality_synced: boolean;
  personality_sync_error: string | null;
  personality_sync_checked_at: string | null;
  avatar_url: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ToolDefinitionSummary {
  name: string;
  description: string;
  category: string;
  read_only: boolean;
  source: Record<string, unknown>;
  timeout_seconds: number;
  non_bypassable: boolean;
}

export interface MCPServer {
  name: string;
  type: string;
  details: Record<string, unknown>;
}

export interface MCPServerTestItem {
  name: string;
  ok: boolean;
  tools: string[];
  error_type: string | null;
  error_detail: string | null;
  duration_ms: number | null;
}

export interface MCPServerTestResponse {
  ok: boolean;
  items: MCPServerTestItem[];
}

export interface TaskDelivery {
  mode: string;
  target: string | null;
}

export interface WorkflowState {
  current_step_index: number;
  step_outputs: Record<string, Record<string, unknown>>;
  loop_iterations: Record<string, number>;
  status: string;
  last_evaluation_feedback?: string | null;
  pending_pause_type?: string | null;
  pending_pause_payload?: Record<string, unknown> | null;
  current_step_status?: string | null;
}

export interface Dependency {
  task_id: string;
  depends_on: string;
  required: boolean;
}

export interface PendingPause {
  pause_id: string;
  pause_type: string;
  task_id: string | null;
  step_name: string | null;
  step_run_id: string | null;
  session_id: string | null;
  question: string | null;
  options: Array<Record<string, unknown>> | null;
  context: Record<string, unknown> | null;
}

export interface StepRun {
  step_run_id: string;
  task_id: string;
  step_name: string;
  step_type: string;
  status: string;
  attempt: number;
  agent_id: string;
  session_id: string | null;
  intaris_session_id: string | null;
  output: Record<string, unknown> | null;
  evaluation: Record<string, unknown> | null;
  todos: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
}

export interface WorkflowRun {
  task_id: string;
  workflow_id: string | null;
  workflow_state: WorkflowState | null;
  current_step_name: string | null;
  pending_pause: PendingPause | null;
}

export interface Task {
  task_id: string;
  title: string;
  description: string;
  status: string;
  priority: number;
  created_by: string;
  agent_id: string;
  source_type: string;
  source_ref: string | null;
  delivery: TaskDelivery;
  workflow_id: string | null;
  workflow_state: WorkflowState | null;
  queue_name: string;
  scheduled_for: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  result_summary: string | null;
  result_data: Record<string, unknown> | null;
}

export interface TaskDetail extends Task {
  dependencies: Dependency[];
  step_runs: StepRun[];
  workflow_run: WorkflowRun | null;
  pending_pause: PendingPause | null;
}

export interface WorkflowStep {
  name: string;
  type: string;
  description?: string;
  prompt?: string;
  input?: {
    type: string;
    source?: string | string[] | null;
  } | string | string[] | null;
  allow_questions?: boolean;
  completion?: Record<string, unknown> | null;
  gate?: Record<string, unknown> | null;
  on_reject?: Record<string, unknown> | null;
}

export interface Workflow {
  workflow_id: string;
  name: string;
  description: string;
  version: number;
  criteria: string;
  tags: string[];
  interaction: Record<string, unknown>;
  defaults: Record<string, unknown>;
  steps: WorkflowStep[];
  is_system: boolean;
  owner_email: string | null;
}

export interface Setting {
  key: string;
  value: unknown;
  category: string;
  updated_by: string | null;
  updated_at: string | null;
}

export interface SettingsCategory {
  category: string;
  items: Setting[];
}

export interface LLMProvider {
  provider_id: string;
  display_name: string;
  location: string;
  backend: string;
  config: Record<string, unknown>;
  is_default: boolean;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  models: Array<Record<string, unknown>>;
  last_test: ProviderTestResult | null;
}

export interface ModelRouting {
  default: string | null;
  classifier: string | null;
  compaction: string | null;
  simple_inline: string | null;
  items: Record<string, string>;
}

export interface SecretMetadata {
  name: string;
  scope: string;
  agent_id: string | null;
  description: string | null;
}

export interface ProviderHealth {
  name: string;
  status: string;
  latency_ms?: number | null;
  circuit_state?: string | null;
  error?: string | null;
  details?: Record<string, unknown> | null;
}

export interface HealthResponse {
  status: string;
  providers: Record<string, ProviderHealth>;
  remember_queue?: {
    depth: number;
  } | null;
}

export interface SystemDiagnostics {
  readiness: Record<string, boolean>;
  ui: Record<string, unknown>;
  database: Record<string, unknown>;
  config: Record<string, unknown>;
  providers: Array<Record<string, unknown>>;
  agents: Record<string, unknown>;
  key_fingerprint: string | null;
}

export interface Escalation {
  call_id: string;
  session_id: string | null;
  tool_name: string | null;
  decision: string;
  resolved: boolean;
  reasoning: string | null;
  risk: string | null;
}

export interface WebSocketAuthenticatedEvent {
  type: 'authenticated';
}

export interface WebSocketChunkEvent {
  type: 'chunk';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  content: string;
  index: number;
}

export interface WebSocketChunkGapEvent {
  type: 'chunk_gap';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  dropped_count: number;
  recoverable: boolean;
}

export interface WebSocketMessageCompleteEvent {
  type: 'message_complete';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  seq: number;
  token_usage: Record<string, unknown> | null;
  queued_count: number;
}

export interface WebSocketToolCallEvent {
  type: 'tool_call';
  conversation_id?: string;
  session_id?: string;
  call_id: string;
  tool_name: string;
  status: string;
  arguments?: Record<string, unknown>;
}

export interface WebSocketDelegationStartedEvent {
  type: 'delegation_started';
  conversation_id?: string;
  parent_session_id?: string;
  child_session_id: string;
  mode: string;
  agent_id?: string;
  task?: string;
}

export interface WebSocketDelegationProgressEvent {
  type: 'delegation_progress';
  conversation_id?: string;
  child_session_id: string;
  step?: string;
  progress?: string;
}

export interface WebSocketWorkflowStepStartedEvent {
  type: 'workflow_step_started';
  conversation_id?: string;
  task_id: string;
  step_name: string;
  step_run_id?: string;
}

export interface WebSocketWorkflowStepCompletedEvent {
  type: 'workflow_step_completed';
  conversation_id?: string;
  task_id: string;
  step_name: string;
  attempt?: number;
}

export interface WebSocketWorkflowGateEvent {
  type: 'workflow_gate';
  conversation_id?: string;
  task_id: string;
  step_name?: string;
  message?: string;
  options?: Array<Record<string, unknown>>;
  context?: Record<string, unknown>;
}

export interface WebSocketWorkflowQuestionEvent {
  type: 'workflow_step_question';
  conversation_id?: string;
  task_id: string;
  step_name?: string;
  question?: string;
  options?: Array<Record<string, unknown>>;
  context?: Record<string, unknown>;
}

export interface WebSocketWorkflowCompletedEvent {
  type: 'workflow_completed';
  conversation_id?: string;
  task_id: string;
  result?: string;
}

export interface WebSocketWorkflowFailedEvent {
  type: 'workflow_failed';
  conversation_id?: string;
  task_id: string;
  reason?: string;
}

export interface WebSocketWorkflowCancelledEvent {
  type: 'workflow_cancelled';
  conversation_id?: string;
  task_id: string;
  reason?: string;
}

export interface WebSocketQueuedEvent {
  type: 'queued';
  conversation_id?: string;
  queued_count: number;
}

export interface WebSocketReconnectedEvent {
  type: 'reconnected';
  conversation_id?: string;
  missed_events_count: number;
}

export interface WebSocketSessionRecoveredEvent {
  type: 'session_recovered';
  conversation_id?: string;
  session_id: string;
  reason?: string;
}

export interface WebSocketConversationUpdatedEvent {
  type: 'conversation_updated';
  conversation_id?: string;
  title?: string;
}

export interface WebSocketToolResultEvent {
  type: 'tool_result';
  conversation_id?: string;
  session_id?: string;
  call_id: string;
  tool_name: string;
  result: string;
  is_error: boolean;
  duration_ms: number | null;
}

export interface WebSocketReasoningEvent {
  type: 'reasoning';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  content: string;
}

export interface WebSocketErrorEvent {
  type: 'error';
  code: string;
  message: string;
  recoverable: boolean;
  error_detail?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface WebSocketPongEvent {
  type: 'pong';
}

export type CognisWebSocketEvent =
  | WebSocketAuthenticatedEvent
  | WebSocketChunkEvent
  | WebSocketChunkGapEvent
  | WebSocketMessageCompleteEvent
  | WebSocketToolCallEvent
  | WebSocketToolResultEvent
  | WebSocketReasoningEvent
  | WebSocketConversationUpdatedEvent
  | WebSocketDelegationStartedEvent
  | WebSocketDelegationProgressEvent
  | WebSocketWorkflowStepStartedEvent
  | WebSocketWorkflowStepCompletedEvent
  | WebSocketWorkflowGateEvent
  | WebSocketWorkflowQuestionEvent
  | WebSocketWorkflowCompletedEvent
  | WebSocketWorkflowFailedEvent
  | WebSocketWorkflowCancelledEvent
  | WebSocketQueuedEvent
  | WebSocketReconnectedEvent
  | WebSocketSessionRecoveredEvent
  | WebSocketErrorEvent
  | WebSocketPongEvent;
