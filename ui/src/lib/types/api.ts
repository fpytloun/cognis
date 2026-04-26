export type UserRole = 'admin' | 'user' | 'viewer' | 'service';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

export interface UserSummary {
  email: string;
  name: string | null;
  role: UserRole;
}

export interface UserDetail extends UserSummary {
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_login_at: string | null;
  disabled_at: string | null;
  disabled_by: string | null;
}

export interface UserCreatePayload {
  email: string;
  name?: string | null;
  password: string;
  role?: UserRole;
}

export interface UserUpdatePayload {
  name?: string | null;
  role?: UserRole;
}

export interface TokenResponse {
  token: string;
  refresh_token: string | null;
  expires_in: number;
  user: UserSummary;
}

export interface AuthSessionResponse {
  user: UserSummary;
  expires_at: string;
  token?: string | null;
  refresh_token?: string | null;
  expires_in?: number | null;
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
    active_session_id: string | null;
    status: string;
    last_message_at: string | null;
    last_read_at: string | null;
    has_unread: boolean;
    created_at: string | null;
    updated_at: string | null;
}

export interface MessageEvent {
  seq: number | null;
  type: string;
  data: Record<string, unknown>;
  timestamp: string | null;
}

export interface AttachmentRef {
  artifact_id: string;
  kind: string;
  mime_type: string;
  filename: string;
  size_bytes: number;
  url?: string | null;
}

export interface MessageHistoryResponse {
  items: MessageEvent[];
  last_seq: number;
  has_more: boolean;
  active_session_id?: string | null;
  active_session_last_seq?: number;
  history_truncated?: boolean;
  truncation_reason?: string | null;
}

export interface Session {
  session_id: string;
  conversation_id: string;
  parent_session_id: string | null;
  previous_session_id: string | null;
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
  completion_reason: string | null;
  result_summary: string | null;
  updated_at: string | null;
}

export interface SessionEventsResponse {
  session_id: string;
  items: MessageEvent[];
  last_seq: number;
  has_more: boolean;
}

export interface IntarisSessionDetail {
  session_id: string;
  intaris_session_id: string;
  intention: string | null;
  status: string;
  total_calls: number;
  approved_count: number;
  denied_count: number;
  escalated_count: number;
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
  avatar_image_id: string | null;
  agent_type: string;
  is_system: boolean;
  hidden: boolean;
  editable_fields: string[];
  has_overrides: boolean;
  disabled: boolean;
  disableable: boolean;
  sync_metadata: Record<string, unknown> | null;
  is_shared_with_me: boolean;
  shared_by_email: string | null;
  granted_permission: string | null;
  executor_scope: string | null;
  is_readonly_for_caller: boolean;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentGrant {
  grant_id: string;
  agent_id: string;
  grantee_type: string;
  grantee_user_email: string | null;
  grantee_group_id: string | null;
  permission: string;
  executor_scope: string;
  granted_by: string;
  granted_at: string | null;
  revoked_at: string | null;
  note: string | null;
}

export interface ToolParameterProperty {
  type: string;
  description?: string;
  enum?: string[];
  items?: Record<string, unknown>;
  properties?: Record<string, ToolParameterProperty>;
  required?: string[];
}

export interface ToolParameters {
  type?: string;
  properties?: Record<string, ToolParameterProperty>;
  required?: string[];
}

export interface ToolDefinitionSummary {
  tool_id?: string | null;
  name: string;
  description: string;
  parameters: ToolParameters;
  category: string;
  profile_group?: string | null;
  read_only: boolean;
  capabilities: string[];
  classification_status?: string | null;
  classification_source?: string | null;
  classification_confidence?: number | null;
  source: ToolSource;
  timeout_seconds: number;
  non_bypassable: boolean;
}

export interface ToolSource {
  type: string;
  server_name?: string | null;
  server_id?: string | null;
  raw_tool_name?: string | null;
  skill_id?: string | null;
  skill_version_id?: string | null;
  skill_content_hash?: string | null;
}

export interface EffectiveToolItem {
  tool_id: string;
  name: string;
  description: string;
  category: string;
  profile_group?: string | null;
  read_only: boolean;
  capabilities: string[];
  classification_status?: string | null;
  classification_source?: string | null;
  classification_confidence?: number | null;
  source: ToolSource;
  permission: string;
  enabled: boolean;
  disabled_reason?: string | null;
  timeout_seconds: number;
  non_bypassable: boolean;
}

export interface EffectiveToolsState {
  tools: EffectiveToolItem[];
  connected: boolean;
  observed_at: string | null;
  stale_after: string | null;
}

export interface EffectiveToolsExecutorSummary {
  executor_id: string | null;
  executor_type: string | null;
  selection_source: string;
}

export interface EffectiveToolsResponse {
  executor: EffectiveToolsExecutorSummary;
  configured_state: EffectiveToolsState;
  live_state: EffectiveToolsState;
  warnings: string[];
}

export interface EffectiveToolsPreviewRequest {
  tools?: Record<string, unknown>;
  permissions?: Record<string, unknown>;
  execution?: Record<string, unknown>;
  skills?: Record<string, unknown>;
  agent_id?: string | null;
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

export interface MCPServerConfigResponse {
  server_id: string;
  name: string;
  transport: string;
  command: string | null;
  url: string | null;
  args: string[];
  env: Record<string, string>;
  headers: Record<string, string>;
  timeout_seconds: number;
  description: string | null;
  shared: boolean;
  owner_email: string;
  status: string;
  invalid_reason: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MCPServerCreateRequest {
  server_id?: string | null;
  name: string;
  transport: string;
  command?: string | null;
  url?: string | null;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  timeout_seconds?: number;
  description?: string | null;
  shared?: boolean;
}

export interface MCPServerUpdateRequest {
  name?: string;
  transport?: string;
  command?: string | null;
  url?: string | null;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  timeout_seconds?: number;
  description?: string | null;
  status?: string;
  shared?: boolean;
}

export interface ChannelCapabilities {
  chat_types: string[];
  supports_threads: boolean;
  supports_reactions: boolean;
  supports_edits: boolean;
  supports_media: boolean;
  supports_typing: boolean;
  supports_read_receipts: boolean;
  supports_markdown: boolean;
  supports_buttons: boolean;
  max_message_length: number;
}

export interface ChannelCredentialField {
  name: string;
  label: string;
  description: string;
  required: boolean;
  secret: boolean;
}

export interface ChannelSettingField {
  name: string;
  label: string;
  description: string;
  field_type: string;
  default: unknown;
  options?: string[] | null;
}

export interface ChannelMeta {
  channel_type: string;
  label: string;
  description: string;
  icon: string | null;
  docs_url: string | null;
  capabilities: ChannelCapabilities;
  credential_fields: ChannelCredentialField[];
  setting_fields: ChannelSettingField[];
  connection_mode: string;
}

export interface ChannelAccountStatus {
  account_id: string;
  channel_type: string;
  status: string;
  enabled: boolean;
  connected_at: string | null;
  last_message_at: string | null;
  last_error: string | null;
  reconnect_attempts: number;
  active_chats?: number;
}

export interface ChannelAccount {
  account_id: string;
  channel_type: string;
  display_name: string;
  enabled: boolean;
  agent_id: string;
  config: Record<string, unknown>;
  credential_refs: Record<string, string>;
  default_conversation_id?: string | null;
  allow_new_conversations?: boolean;
  adapter_location?: string;
  executor_id?: string | null;
  allowed_senders: string[];
  dm_policy: string;
  group_policy: string;
  created_at: string | null;
  updated_at: string | null;
  status?: ChannelAccountStatus | { status: string };
}

export interface ChannelContact {
  contact_id: string;
  channel_type: string;
  sender_id: string;
  user_email: string;
  display_name: string | null;
  verified: boolean;
  created_at: string | null;
}

export interface PairingRequest {
  request_id: string;
  owner_email: string;
  account_id: string;
  account_display_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  channel_type: string;
  sender_id: string;
  sender_name: string | null;
  chat_id: string;
  chat_name: string | null;
  code: string;
  status: string;
  attempts: number;
  expires_at: string;
  created_at: string;
  completed_at: string | null;
}

export interface IntarisMCPServer {
  name: string;
  transport: string | null;
  enabled: boolean;
  tools_count: number;
  agent_pattern: string;
}

export interface ExecutorStatus {
  executor_type: string;
  status: string;
  active_executors: number;
  capabilities: Record<string, unknown>;
  native_tools: string[];
}

export interface ExecutorConfig {
  executor_id: string;
  name: string;
  executor_type: string;
  labels: Record<string, string>;
  enabled_tools: string[];
  enabled_tool_groups: string[];
  config: ExecutorRuntimeConfig;
  status: string;
  runtime_state: string;
  desired_config_version: number;
  applied_config_version: number;
  runtime_metadata: ExecutorRuntimeMetadata;
  last_observed_at: string | null;
  observed_tools?: ToolDefinitionSummary[];
  is_default: boolean;
  shared: boolean;
  owner_email: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ExecutorCreateRequest {
  executor_id?: string | null;
  name: string;
  executor_type?: string;
  labels?: Record<string, string>;
  enabled_tools?: string[];
  enabled_tool_groups?: string[];
  config?: ExecutorRuntimeConfig;
  is_default?: boolean;
  shared?: boolean;
}

export interface ExecutorUpdateRequest {
  name?: string;
  labels?: Record<string, string>;
  enabled_tools?: string[];
  enabled_tool_groups?: string[];
  config?: ExecutorRuntimeConfig;
  status?: string;
  is_default?: boolean;
  shared?: boolean;
}

export interface ExecutorSignalConfig {
  direct_enabled?: boolean;
  command?: string;
}

export interface ExecutorBrowserConfig {
  enabled?: boolean;
  auto_install?: boolean;
  headed_allowed?: boolean;
  engine?: string;
  runtime?: 'playwright' | 'patchright';
  channel?: string;
  max_sessions?: number;
  idle_timeout_seconds?: number;
  persistent_profiles_enabled?: boolean;
  profile_mode_default?: 'ephemeral' | 'persistent_local';
  profile_base_dir?: string;
  realistic_launch?: boolean;
  xvfb_auto?: boolean;
  locale?: string;
  timezone_id?: string;
  viewport_width?: number;
  viewport_height?: number;
  stealth_enabled?: boolean;
  stealth_evasions?: string[] | string;
  realistic_user_agent?: boolean;
  default_timezone_id?: string;
  default_accept_language?: string;
  auto_consent?: 'accept' | 'reject' | 'off';
  auto_consent_disabled_domains?: string[] | string;
  auto_consent_delay_ms?: number;
  humanize_input?: boolean;
  humanize_intensity?: 'off' | 'low' | 'medium' | 'high';
  fingerprint_hardening?: boolean;
}

export interface ExecutorRuntimeConfig {
  mcp_server_ids?: string[];
  lsp_enabled?: boolean;
  lsp_auto_install?: boolean;
  lsp_diagnostics_timeout_ms?: number;
  lsp_idle_timeout_seconds?: number;
  lsp_max_concurrent_servers?: number;
  signal?: ExecutorSignalConfig;
  browser?: ExecutorBrowserConfig;
  [key: string]: unknown;
}

export interface ExecutorMCPServerRuntimeStatus {
  server_id?: string | null;
  name: string;
  status: string;
  phase: string;
  error_class?: string | null;
  timed_out?: boolean;
  message?: string;
  stderr_summary?: string;
  tool_count?: number;
}

export interface ExecutorRuntimeMetadata {
  schema_version?: number;
  configure_capabilities?: string[];
  legacy_metadata?: boolean;
  single_controller_process?: boolean;
  warnings?: string[];
  mcp_servers?: ExecutorMCPServerRuntimeStatus[];
  environment?: Record<string, string>;
  platform?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ExecutorTokenResponse {
  executor_id: string;
  token: string;
  expires_in: number;
}

export interface SkillVersion {
  version_id: string;
  skill_id: string;
  version_number: number;
  content_hash: string;
  schema_version: number;
  instructions: string;
  tools: Record<string, unknown>[] | null;
  linked_tool_ids: string[] | null;
  prompt_templates: Record<string, unknown> | null;
  secret_placeholders: string[] | null;
  steps: Record<string, unknown>[] | null;
  decomposition_source_hash: string | null;
  decomposition_stale: boolean;
  source_url: string | null;
  resolved_url: string | null;
  commit_sha: string | null;
  import_checksum: string | null;
  imported_at: string | null;
  import_format: string | null;
  asset_manifest: SkillAsset[] | null;
  created_at: string | null;
}

export interface SkillAsset {
  filename: string;
  asset_id: string;
  artifact_namespace: string;
  artifact_object_id: string;
  content_hash: string;
  size_bytes: number;
  content_type: string;
  url: string | null;
}

export interface SkillAssetInput {
  filename: string;
  existing_asset_id?: string;
  source_artifact_id?: string;
  content?: string;
  content_b64?: string;
  content_type?: string;
}

export interface Skill {
  skill_id: string;
  name: string;
  description: string | null;
  instructions: string;
  tools: Record<string, unknown>[] | null;
  linked_tool_ids: string[] | null;
  prompt_templates: Record<string, unknown> | null;
  steps: Record<string, unknown>[] | null;
  tags: string[] | null;
  attach_to_all_agents: boolean;
  auto_load?: boolean;
  is_system: boolean;
  source: string;
  current_version_id: string | null;
  current_version: SkillVersion | null;
  owner_email: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SkillCreate {
  name: string;
  description?: string;
  instructions: string;
  tools?: Record<string, unknown>[];
  linked_tool_ids?: string[];
  prompt_templates?: Record<string, unknown>;
  steps?: Record<string, unknown>[];
  decomposition_source_hash?: string;
  tags?: string[];
  attach_to_all_agents?: boolean;
  auto_load?: boolean;
  secret_placeholders?: string[];
  assets?: SkillAssetInput[];
}

export interface SkillUpdate {
  name?: string;
  description?: string;
  instructions?: string;
  tools?: Record<string, unknown>[];
  linked_tool_ids?: string[];
  prompt_templates?: Record<string, unknown>;
  steps?: Record<string, unknown>[];
  decomposition_source_hash?: string;
  tags?: string[];
  attach_to_all_agents?: boolean;
  auto_load?: boolean;
  secret_placeholders?: string[];
  assets?: SkillAssetInput[];
}

export interface SkillImportRequest {
  url?: string;
  content?: string;
  content_b64?: string;
  filename?: string;
  format?: string;
  name?: string;
  tags?: string[];
  linked_tool_ids?: string[];
  attach_to_all_agents?: boolean;
  auto_load?: boolean;
}

export interface SkillExportResponse {
  format: string;
  content: string | null;
  content_b64: string | null;
  content_type: string | null;
  filename: string;
  warnings: string[];
}

export interface SkillDecompositionPreview {
  skill_id: string;
  source_hash: string;
  rationale: string;
  steps: Record<string, unknown>[];
}

export interface TaskDelivery {
  mode: string;
  target: string | null;
}

export interface CompletionDeliveryPolicy {
  completion_mode_family: 'default' | 'direct';
  allow_silent_completion: boolean;
}

export interface WorkflowState {
  current_step_index: number;
  step_outputs: Record<string, Record<string, unknown>>;
  loop_iterations: Record<string, number>;
  status: string;
  skipped_steps?: string[];
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

export interface Deliverable {
  deliverable_id: string;
  step_run_id: string;
  version: number;
  content: string;
  format: 'markdown' | 'plain' | 'html' | string;
  title: string | null;
  target: 'channel' | 'none' | string | null;
  outputs: Record<string, unknown>;
  status: string;
  evaluator_feedback: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface StepRun {
  step_run_id: string;
  task_id: string;
  step_name: string;
  step_type: string;
  status: string;
  attempt: number;
  agent_id: string;
  workspace_root: string | null;
  working_directory: string | null;
  conversation_id: string | null;
  session_id: string | null;
  intaris_session_id: string | null;
  deliverable_id: string | null;
  require_deliverable: boolean | null;
  output: Record<string, unknown> | null;
  evaluation: Record<string, unknown> | null;
  runtime_info: Record<string, unknown> | null;
  deliverables: Deliverable[];
  todos: Array<Record<string, unknown>>;
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
  expected_output: string | null;
  status: string;
  priority: number;
  created_by: string;
  agent_id: string;
  source_type: string;
  source_ref: string | null;
  delivery: TaskDelivery;
  completion_mode_family: 'default' | 'direct';
  allow_silent_completion: boolean;
  workflow_id: string | null;
  workspace_root: string | null;
  working_directory: string | null;
  workflow_state: WorkflowState | null;
  queue_name: string;
  scheduled_for: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  result_summary: string | null;
  result_data: Record<string, unknown> | null;
  applied_completion_mode: 'default' | 'direct' | 'silent' | null;
  applied_completion_reason: string | null;
}

export interface TaskDetail extends Task {
  dependencies: Dependency[];
  step_runs: StepRun[];
  workflow_run: WorkflowRun | null;
  pending_pause: PendingPause | null;
}

export interface TaskRerunResponse {
  ok: boolean;
  source_task_id: string;
  task_id: string;
  status: string;
  created_new: boolean;
}

export interface WorkflowStep {
  name: string;
  type: string;
  description?: string;
  prompt?: string;
  agent_override?: string | null;
  reasoning_effort?: string | null;
  input?: {
    type: string;
    source?: string | string[] | null;
  } | string | string[] | null;
  allow_questions?: boolean;
  step_profile_id?: string | null;
  step_profile_mode?: 'soft' | 'hard' | string;
  step_profile?: {
    matrix?: Record<string, string[]>;
    tool_overrides?: {
      include?: string[];
      exclude?: string[];
    } | null;
    allow_tool_search?: boolean;
  } | null;
  completion?: Record<string, unknown> | null;
  gate?: Record<string, unknown> | null;
  on_reject?: Record<string, unknown> | null;
  outcome_routes?: Array<Record<string, unknown>> | null;
  require_deliverable?: boolean;
}

export interface StepProfileDefinition {
  profile_id: string;
  name: string;
  mode: 'soft' | 'hard' | string;
  has_override?: boolean;
  is_custom?: boolean;
  config: {
    matrix?: Record<string, string[]>;
    tool_overrides?: {
      include?: string[];
      exclude?: string[];
    } | null;
    allow_tool_search?: boolean;
  };
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
  lifecycle: 'persistent' | 'ephemeral' | string;
  archived_at: string | null;
  lineage: Record<string, unknown> | null;
  editable_fields: string[];
  has_overrides: boolean;
  disabled: boolean;
  disableable: boolean;
  override_warnings: string[];
}

export interface Schedule {
  schedule_id: string;
  name: string;
  description: string | null;
  schedule_type: string;
  cron_expr: string | null;
  interval_seconds: number | null;
  one_shot_at: string | null;
  timezone: string;
  agent_id: string;
  workflow_id: string | null;
  skill_id: string | null;
  task_template: Record<string, unknown>;
  enabled: boolean;
  max_concurrent_runs: number;
  delete_after_run: boolean;
  completion_mode_family: 'default' | 'direct';
  allow_silent_completion: boolean;
  last_fired_at: string | null;
  next_fire_at: string | null;
  last_run_status: string | null;
  consecutive_errors: number;
  disabled_reason: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  human_schedule: string | null;
}

export interface ScheduleRun {
  task_id: string;
  title: string;
  status: string;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  result_summary: string | null;
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

export interface ModelEntry {
  model_id: string;
  display_name?: string;
  context_window: number;
  max_output_tokens: number;
  supports_tools: boolean;
  supports_streaming: boolean;
  supports_vision: boolean;
  supports_audio_input: boolean;
  supports_pdf_input: boolean;
  supports_file_input: boolean;
  supports_reasoning: boolean;
  reasoning_efforts: string[];
  supports_prompt_caching: boolean;
  supports_tool_search: boolean;
  supports_defer_loading: boolean;
  supports_openai_namespace_tools: boolean;
  supports_openai_allowed_tools: boolean;
  supports_openai_apply_patch: boolean;
  supports_responses_api: boolean;
  supports_extended_thinking: boolean;
  supports_image_generation: boolean;
  supported_openai_params: string[];
  max_tools?: number;
  input_cost_per_mtok?: number;
  output_cost_per_mtok?: number;
  tier: string;
}

export function defaultModelEntry(modelId: string): ModelEntry {
  return {
    model_id: modelId,
    context_window: 128000,
    max_output_tokens: 16384,
    supports_tools: true,
    supports_streaming: true,
    supports_vision: false,
    supports_audio_input: false,
    supports_pdf_input: false,
    supports_file_input: false,
    supports_reasoning: false,
    reasoning_efforts: [],
    supports_prompt_caching: false,
    supports_tool_search: false,
    supports_defer_loading: false,
    supports_openai_namespace_tools: false,
    supports_openai_allowed_tools: false,
    supports_openai_apply_patch: false,
    supports_responses_api: false,
    supports_extended_thinking: false,
    supports_image_generation: false,
    supported_openai_params: [],
    tier: 'standard'
  };
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
  models: ModelEntry[];
  last_test: ProviderTestResult | null;
}

export interface ModelRoutingEntry {
  model: string | null;
  reasoning_effort: string | null;
}

export interface ModelRouting {
  default: ModelRoutingEntry;
  classifier: ModelRoutingEntry;
  compaction: ModelRoutingEntry;
  evaluator: ModelRoutingEntry;
  speech_to_text: ModelRoutingEntry;
  image_generation: ModelRoutingEntry;
  attachment_analysis: ModelRoutingEntry;
}

export interface SecretMetadata {
  name: string;
  scope: string;
  agent_id: string | null;
  description: string | null;
}

export interface CredentialMetadata {
  credential_id: string;
  kind: string;
  label: string;
  metadata: Record<string, unknown>;
  field_names: string[];
  scope: string;
  agent_id: string | null;
  description: string | null;
  version: number;
  status: string;
  last_verified_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface WebConfigStatus {
  backend: string;
  tavily_configured: boolean;
  brave_configured: boolean;
  available_backends: string[];
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
  timeout_seconds?: number;
  received_at?: number;
}

export interface Notification {
  notification_id: string;
  notification_type: string;
  conversation_id: string;
  task_id: string | null;
  step_name: string | null;
  step_run_id: string | null;
  session_id: string | null;
  payload: Record<string, unknown>;
  status: string;
  resolution: Record<string, unknown> | null;
  created_at: string | null;
  resolved_at: string | null;
}

export interface WebSocketAuthenticatedEvent {
  type: 'authenticated';
}

export interface WebSocketChunkEvent {
  type: 'chunk';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  turn_id?: string | null;
  content: string;
  index: number;
}

export interface WebSocketChunkGapEvent {
  type: 'chunk_gap';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  turn_id?: string | null;
  dropped_count: number;
  recoverable: boolean;
}

export interface ContextUsage {
  prompt_tokens: number;
  max_context_tokens: number;
  percentage: number;
  model: string;
  reasoning_effort: string | null;
}

export interface WebSocketMessageCompleteEvent {
  type: 'message_complete';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  turn_id?: string | null;
  content?: string;
  seq: number;
  token_usage: Record<string, unknown> | null;
  context_usage: ContextUsage | null;
  queued_count: number;
  attachments?: AttachmentRef[];
}

export interface WebSocketTurnStartedEvent {
  type: 'turn_started';
  conversation_id?: string;
  session_id?: string;
  message_id?: string;
}

export interface WebSocketTurnSettledEvent {
  type: 'turn_settled';
  conversation_id?: string;
  session_id?: string;
  message_id?: string;
  queued_count?: number;
}

export interface WebSocketToolCallEvent {
  type: 'tool_call';
  conversation_id?: string;
  session_id?: string;
  seq?: number;
  turn_id?: string | null;
  call_id: string;
  tool_name: string;
  status: string;
  arguments?: Record<string, unknown>;
  timestamp?: string | null;
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

export interface WebSocketDelegationCompletedEvent {
  type: 'delegation_completed';
  conversation_id?: string;
  child_session_id: string;
  result?: string;
}

export interface WebSocketDelegationFailedEvent {
  type: 'delegation_failed';
  conversation_id?: string;
  child_session_id: string;
  reason?: string;
}

export interface WebSocketWorkflowStepStartedEvent {
  type: 'workflow_step_started';
  conversation_id?: string;
  task_id: string;
  step_name: string;
  step_run_id?: string;
}

export interface WebSocketWorkflowComposedEvent {
  type: 'workflow_composed';
  conversation_id?: string;
  task_id?: string | null;
  schedule_id?: string | null;
  workflow_id: string;
  workflow_name: string;
  lifecycle: string;
  steps: string[];
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
  notification_id?: string;
  task_id: string;
  step_name?: string;
  message?: string;
  options?: Array<Record<string, unknown>>;
  context?: Record<string, unknown>;
}

export interface WebSocketWorkflowQuestionEvent {
  type: 'workflow_step_question';
  conversation_id?: string;
  notification_id?: string;
  task_id?: string;
  step_name?: string;
  question?: string;
  options?: Array<Record<string, unknown>>;
  context?: Record<string, unknown>;
}

export interface WebSocketWorkflowGateResolvedEvent {
  type: 'workflow_gate_resolved';
  conversation_id?: string;
  notification_id?: string;
  decision?: string;
}

export interface WebSocketWorkflowQuestionResolvedEvent {
  type: 'workflow_step_question_resolved';
  conversation_id?: string;
  notification_id?: string;
  decision?: string;
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

export interface WebSocketTaskPausedEvent {
  type: 'task_paused';
  conversation_id?: string;
  task_id: string;
}

export interface WebSocketQueuedEvent {
  type: 'queued';
  conversation_id?: string;
  queued_count: number;
}

export interface WebSocketReconnectedEvent {
  type: 'reconnected';
  conversation_id?: string;
  session_id?: string;
  missed_events_count: number;
  last_seq?: number;
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
  seq?: number;
  turn_id?: string | null;
  call_id: string;
  tool_name: string;
  result: string;
  is_error: boolean;
  duration_ms: number | null;
  timestamp?: string | null;
  attachments?: AttachmentRef[];
  evaluation?: {
    decision: string;
    reasoning?: string;
    risk?: string;
    path?: string;
    latency_ms?: number;
  };
}

export interface WebSocketAssistantThinkingChunkEvent {
  type: 'assistant_thinking_chunk';
  conversation_id?: string;
  session_id?: string;
  message_id: string;
  turn_id?: string | null;
  block_id: string;
  delta: string;
  title?: string | null;
  complete: boolean;
}

export interface WebSocketAssistantThinkingBlockEvent {
  type: 'assistant_thinking_block';
  conversation_id?: string;
  session_id?: string;
  /** seq present on replay frames only */
  seq?: number;
  message_id: string;
  turn_id?: string | null;
  block_id: string;
  title?: string | null;
  /** Full content — present on replay frames */
  content?: string;
  complete: boolean;
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

export interface WebSocketSystemMessageEvent {
  type: 'system_message';
  conversation_id?: string;
  seq?: number;
  turn_id?: string | null;
  text: string;
}

export interface WebSocketNoticeEvent {
  type: 'history_notice';
  conversation_id?: string;
  seq?: number;
  title: string;
  description: string;
  tone?: 'info' | 'warning' | 'error';
}

export interface WebSocketEscalationEvent {
  type: 'escalation';
  conversation_id?: string;
  session_id?: string;
  task_id?: string;
  call_id: string;
  tool_name: string | null;
  risk: string | null;
  reasoning: string | null;
  timeout_seconds: number;
}

export interface WebSocketEscalationResolvedEvent {
  type: 'escalation_resolved';
  conversation_id?: string;
  call_id: string;
  decision: string;
  reason?: string | null;
}

export interface WebSocketSessionCompactedEvent {
  type: 'session_compacted';
  conversation_id: string;
  session_id: string;
  previous_session_id: string;
  summary_preview: string;
  method: string;
  turns_compacted: number;
}

export interface WebSocketSessionResetEvent {
  type: 'session_reset';
  conversation_id: string;
  session_id: string;
  previous_session_id: string;
}

export interface WebSocketConversationCreatedEvent {
  type: 'conversation_created';
  conversation_id: string;
  old_conversation_id: string;
}

export interface WebSocketUserMessageEvent {
  type: 'user_message';
  conversation_id?: string;
  session_id?: string;
  turn_id?: string | null;
  content: string;
  attachments?: AttachmentRef[];
}

export type CognisWebSocketEvent =
  | WebSocketAuthenticatedEvent
  | WebSocketChunkEvent
  | WebSocketChunkGapEvent
  | WebSocketTurnStartedEvent
  | WebSocketTurnSettledEvent
  | WebSocketMessageCompleteEvent
  | WebSocketToolCallEvent
  | WebSocketToolResultEvent
  | WebSocketAssistantThinkingChunkEvent
  | WebSocketAssistantThinkingBlockEvent
  | WebSocketConversationUpdatedEvent
  | WebSocketDelegationStartedEvent
  | WebSocketDelegationProgressEvent
  | WebSocketDelegationCompletedEvent
  | WebSocketDelegationFailedEvent
  | WebSocketWorkflowComposedEvent
  | WebSocketWorkflowStepStartedEvent
  | WebSocketWorkflowStepCompletedEvent
  | WebSocketWorkflowGateEvent
  | WebSocketWorkflowQuestionEvent
  | WebSocketWorkflowGateResolvedEvent
  | WebSocketWorkflowQuestionResolvedEvent
  | WebSocketWorkflowCompletedEvent
  | WebSocketWorkflowFailedEvent
  | WebSocketWorkflowCancelledEvent
  | WebSocketTaskPausedEvent
  | WebSocketSystemMessageEvent
  | WebSocketNoticeEvent
  | WebSocketEscalationEvent
  | WebSocketEscalationResolvedEvent
  | WebSocketSessionCompactedEvent
  | WebSocketSessionResetEvent
  | WebSocketConversationCreatedEvent
  | WebSocketUserMessageEvent
  | WebSocketQueuedEvent
  | WebSocketReconnectedEvent
  | WebSocketSessionRecoveredEvent
  | WebSocketErrorEvent
  | WebSocketPongEvent;
