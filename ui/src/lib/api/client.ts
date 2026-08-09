import { apiUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import { fetchWithTimeout, isFetchTimeoutError } from '$lib/api/fetch';
import { auth } from '$lib/stores/auth';
import type {
  ApiKeyCreateResponse,
  Agent,
  AgentGrant,
  MemoryBackendDescriptor,
  AttachmentRef,
  ChannelAccount,
  ChannelAccountStatus,
    ChannelContact,
    CredentialMetadata,
    CredentialUpsertPayload,
    ChannelMeta,
  ApiErrorResponse,
  ApiKeyMetadata,
  AgentDirectChat,
  BootstrapStatusResponse,
  Conversation,
  ConversationFlatSearchResponse,
  ConversationOpenRequest,
  ConversationSearchResponse,
  ConversationTitleSuggestion,
  CursorPage,
  Deliverable,
  DeliverableShareLink,
  Escalation,
  ExecutorConfig,
  ExecutorCreateRequest,
  ExecutorStatus,
  ExecutorTokenResponse,
  EffectiveToolsPreviewRequest,
  EffectiveToolsResponse,
  ExecutorUpdateRequest,
  ExchangeTokenResponse,
  HealthResponse,
  SearchHealth,
  SearchKind,
  SearchMode,
  IntarisMCPServer,
  IntarisSessionDetail,
  KnowledgebaseModel,
  KnowledgebaseFacetRequest,
  KnowledgebaseFacetResponse,
  KnowledgebaseShareCandidate,
  KnowledgebaseShareModel,
  KnowledgebaseShareRequest,
  KnowledgebaseCreateRequest,
  KnowledgebaseUpdateRequest,
   KnowledgebaseHealth,
   KnowledgebaseCapabilities,
   KnowledgebaseArtifactModel,
   KnowledgebaseAttachRequest,
    KnowledgebaseDocumentDetail,
    KnowledgebaseDocumentListResponse,
   KnowledgebaseDocumentUploadResponse,
  KnowledgebaseDocumentContentResponse,
  KnowledgebaseDocumentConflictPolicy,
  KnowledgebaseIndexJobModel,
  KnowledgebaseSearchRequest,
  KnowledgebaseSearchResponse,
  KnowledgebaseSourceContextRequest,
  KnowledgebaseSourceContextResponse,
  KnowledgebaseDiagnostics,
  KnowledgebaseAskRequest,
  KnowledgebaseAskResponse,
  LocalModelCatalogItem,
  LocalModelCatalogResponse,
  LocalModelCatalogSource,
  LocalModelDeployment,
  LocalModelDeploymentCreate,
  LocalModelDeploymentUpdate,
  LocalModelFitPlan,
  LocalModelFitPlanRequest,
  LocalModelManagedDeploymentCreate,
  LocalModelManagedDeploymentCreateResponse,
  LocalModelOperation,
  LocalModelProviderFindOrCreateResponse,
  LocalModelProviderRecommendation,
  LocalModelSelector,
  LocalModelTargetStatus,
  LLMProvider,
  LLMProviderOAuthStatus,
  CodexUsage,
  ModelEntry,
  MCPServerConfigResponse,
  MCPServerCreateRequest,
  MCPServerTestResponse,
  MCPServer,
  MCPServerUpdateRequest,
  ManagedConversationActionResponse,
  QueuedMessagesResponse,
  Notification,
  PushSubscriptionPayload,
  PushSubscriptionResponse,
  PushSubscriptionStatusResponse,
  PushSubscriptionTestResponse,
  ModelRouting,
  ProviderTestResult,
  PairingRequest,
  Project,
  ProjectGrant,
  ProjectSource,
  QuestionSetReply,
  Schedule,
  ScheduleTriggerResponse,
  ScheduleRun,
    SecretMetadata,
  Session,
  SidebarProjection,
  Setting,
  SettingsCategory,
  Skill,
  SkillCreate,
  SkillDecompositionPreview,
  SkillExportResponse,
  SkillImportRequest,
  SkillUpdate,
  SkillVersion,
  SlashCommandSuggestionsResponse,
  SystemDiagnostics,
  StepRun,
  StepProfileDefinition,
  SttTranscribeResponse,
  Task,
  TaskBoard,
  TaskBoardColumn,
  TaskBoardItem,
  TaskChatResponse,
  TaskControlChatResponse,
  TaskComment,
  TaskDetail,
  TaskRerunResponse,
  ToolDefinitionSummary,
  TtsSynthesizeRequest,
  TtsSynthesizeResponse,
  UserCreatePayload,
  UserDetail,
  UserPreferences,
  UserSummary,
  UserUpdatePayload,
  VapidPublicKeyResponse,
  WebBackendUpdatePayload,
  WebDefaultsUpdatePayload,
  WebConfigStatus,
  Workflow,
  WorkflowRun
} from '$lib/types/api';
import { isRecord, toErrorMessage } from '$lib/utils';

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown> | null;

  constructor(message: string, options: { code?: string; status: number; details?: Record<string, unknown> | null }) {
    super(message);
    this.name = 'ApiError';
    this.code = options.code ?? 'request_error';
    this.status = options.status;
    this.details = options.details ?? null;
  }
}

type RequestOptions = RequestInit & {
  auth?: boolean;
  timeoutMs?: number;
};

const UI_LOAD_REQUEST_TIMEOUT_MS = 30_000;
const DISABLE_API_REQUEST_TIMEOUT_MS = 0;

async function readError(response: Response): Promise<ApiError> {
  let payload: ApiErrorResponse | null = null;

  try {
    payload = (await response.json()) as ApiErrorResponse;
  } catch {
    payload = null;
  }

  if (payload?.error) {
    return new ApiError(payload.error.message ?? 'Request failed', {
      code: payload.error.code,
      details: payload.error.details ?? null,
      status: response.status
    });
  }

  if (typeof payload?.detail === 'string') {
    return new ApiError(payload.detail, { status: response.status });
  }

  if (isRecord(payload?.detail)) {
    return new ApiError(String(payload.detail.message ?? 'Request failed'), {
      code: typeof payload.detail.code === 'string' ? payload.detail.code : undefined,
      details: isRecord(payload.detail.details) ? payload.detail.details : null,
      status: response.status
    });
  }

  return new ApiError(`Request failed with status ${response.status}`, { status: response.status });
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { auth: requiresAuth = true, headers, body, timeoutMs, ...rest } = options;
  const nextHeaders = new Headers(headers ?? {});

  if (body && !nextHeaders.has('Content-Type') && !(body instanceof FormData)) {
    nextHeaders.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetchWithTimeout(apiUrl(path), {
      ...rest,
      credentials: 'include',
      headers: nextHeaders,
      body
    }, { timeoutMs });
  } catch (error) {
    if (isFetchTimeoutError(error)) {
      throw new ApiError(error.message, { code: 'request_timeout', status: 0 });
    }
    throw error;
  }

  if (response.status === 401 && requiresAuth) {
    const message = 'Your session has expired. Please log in again.';
    auth.clear(message);
    throw new ApiError(message, {
      code: 'unauthorized',
      status: 401
    });
  }

  if (!response.ok) {
    throw await readError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function collectCursorPages<T>(fetchPage: (cursor: string | null) => Promise<CursorPage<T>>): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;

  while (true) {
    const page = await fetchPage(cursor);
    items.push(...page.items);
    if (!page.has_more || !page.cursor) {
      return items;
    }

    cursor = page.cursor;
  }
}

function encodeQuery(params: Record<string, string | number | boolean | string[] | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item) query.append(key, item);
      }
      continue;
    }
    query.set(key, String(value));
  }

  const serialized = query.toString();
  return serialized ? `?${serialized}` : '';
}

const executorConfigVersions = new Map<string, number>();

function rememberExecutorConfigVersion(executor: ExecutorConfig): ExecutorConfig {
  executorConfigVersions.set(executor.executor_id, executor.desired_config_version);
  return executor;
}

export const api = {
  auth: {
    bootstrapStatus(): Promise<BootstrapStatusResponse> {
      return request<BootstrapStatusResponse>('/api/bootstrap-status', { auth: false });
    },

    me(): Promise<UserSummary> {
      return request<UserSummary>('/api/auth/me');
    },

    changePassword(payload: { current_password: string; new_password: string }): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    listApiKeys(): Promise<ApiKeyMetadata[]> {
      return request<ApiKeyMetadata[]>('/api/v1/auth/api-keys');
    },

    createApiKey(payload: { name: string; expires_in_days?: number | null }): Promise<ApiKeyCreateResponse> {
      return request<ApiKeyCreateResponse>('/api/v1/auth/api-keys', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    revokeApiKey(keyId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/auth/api-keys/${keyId}`, { method: 'DELETE' });
    },

    exchangeToken(target: 'intaris' | 'mnemory'): Promise<ExchangeTokenResponse> {
      return request<ExchangeTokenResponse>(`/api/v1/auth/exchange-token${encodeQuery({ target })}`, {
        method: 'POST'
      });
    },

    updateProfile(payload: { name?: string | null }): Promise<UserSummary> {
      return request<UserSummary>('/api/auth/me', {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    }
  },

  users: {
    list(includeDisabled = false): Promise<CursorPage<UserDetail>> {
      return request<CursorPage<UserDetail>>(`/api/v1/admin/users${encodeQuery({ include_disabled: includeDisabled })}`);
    },

    detail(email: string): Promise<UserDetail> {
      return request<UserDetail>(`/api/v1/admin/users/${encodeURIComponent(email)}`);
    },

    create(payload: UserCreatePayload): Promise<UserDetail> {
      return request<UserDetail>('/api/v1/admin/users', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(email: string, payload: UserUpdatePayload): Promise<UserDetail> {
      return request<UserDetail>(`/api/v1/admin/users/${encodeURIComponent(email)}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    disable(email: string): Promise<UserDetail> {
      return request<UserDetail>(`/api/v1/admin/users/${encodeURIComponent(email)}/disable`, {
        method: 'POST'
      });
    },

    enable(email: string): Promise<UserDetail> {
      return request<UserDetail>(`/api/v1/admin/users/${encodeURIComponent(email)}/enable`, {
        method: 'POST'
      });
    },

    remove(email: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/admin/users/${encodeURIComponent(email)}${encodeQuery({ confirm: true })}`, {
        method: 'DELETE'
      });
    }
  },

  system: {
    bootstrapStatus(): Promise<BootstrapStatusResponse> {
      return request<BootstrapStatusResponse>('/api/bootstrap-status', { auth: false });
    },

    health(): Promise<HealthResponse> {
      return request<HealthResponse>('/api/health', { auth: false });
    },

    providers(): Promise<Record<string, unknown>> {
      return request<Record<string, unknown>>('/api/health/providers', { auth: false });
    },

    diagnostics(): Promise<SystemDiagnostics> {
      return request<SystemDiagnostics>('/api/v1/system/diagnostics');
    }
  },

  search: {
    health(): Promise<SearchHealth> {
      return request<SearchHealth>('/api/v1/search/health');
    },

    conversations(payload: {
      q: string;
      filters?: {
        agent_id?: string | null;
        agent_ids?: string[] | null;
        project_id?: string | null;
        status?: 'active' | 'starred' | 'archived' | 'all' | 'task';
        context_type?: string | null;
        context_types?: string[] | null;
        from_ts?: string | null;
        to_ts?: string | null;
      };
      kinds?: SearchKind[] | null;
      mode?: SearchMode;
      limit?: number;
      cursor?: string | null;
    }): Promise<ConversationSearchResponse> {
      return request<ConversationSearchResponse>('/api/v1/search/conversations', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    conversation(conversationId: string, payload: {
      q: string;
      kinds?: SearchKind[] | null;
      mode?: SearchMode;
      limit?: number;
      cursor?: string | null;
    }): Promise<ConversationFlatSearchResponse> {
      return request<ConversationFlatSearchResponse>(`/api/v1/search/conversation/${encodeURIComponent(conversationId)}`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    }
  },

  conversations: {
    getQueue(conversationId: string): Promise<QueuedMessagesResponse> {
      return request<QueuedMessagesResponse>(`/api/v1/conversations/${conversationId}/queue`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    list(
      cursor: string | null = null,
      filters: { contextType?: string | null; contextTypes?: string[] | null; agentId?: string | null; agentIds?: string[] | null; status?: string | null; includeAgentDirect?: boolean | null } = {}
    ): Promise<CursorPage<Conversation>> {
      return request<CursorPage<Conversation>>(
        `/api/v1/conversations${encodeQuery({
          cursor,
          limit: 50,
          context_type: filters.contextType,
          context_types: filters.contextTypes ?? undefined,
          agent_id: filters.agentId,
          agent_ids: filters.agentIds ?? undefined,
          status: filters.status,
          include_agent_direct: filters.includeAgentDirect
        })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    async listAll(
      filters: { contextType?: string | null; contextTypes?: string[] | null; agentId?: string | null; agentIds?: string[] | null; status?: string | null; includeAgentDirect?: boolean | null } = {}
    ): Promise<Conversation[]> {
      return collectCursorPages((cursor) => this.list(cursor, filters));
    },

    sidebar(
      cursor: string | null = null,
      filters: { contextType?: string | null; contextTypes?: string[] | null; agentId?: string | null; agentIds?: string[] | null; status?: string | null } = {},
      options: { changedSince?: string | null } = {}
    ): Promise<SidebarProjection> {
      return request<SidebarProjection>(
        `/api/v1/conversations/sidebar${encodeQuery({
          cursor,
          limit: 50,
          changed_since: options.changedSince,
          context_type: filters.contextType,
          context_types: filters.contextTypes ?? undefined,
          agent_id: filters.agentId,
          agent_ids: filters.agentIds ?? undefined,
          status: filters.status
        })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    contextTypes(params: { status?: string | null } = {}): Promise<string[]> {
      return request<string[]>(
        `/api/v1/conversations/context-types${encodeQuery({
          status: params.status
        })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    agentDirect(params: { agentId?: string | null; agentIds?: string[] | null; status?: string | null } = {}): Promise<AgentDirectChat[]> {
      return request<AgentDirectChat[]>(
        `/api/v1/conversations/agent-direct${encodeQuery({
          agent_id: params.agentId,
          agent_ids: params.agentIds ?? undefined,
          status: params.status
        })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    create(payload: {
      agent_id: string;
      agent_profile_id?: string | null;
      title?: string | null;
      context?: Record<string, unknown>;
    }): Promise<Conversation> {
      return request<Conversation>('/api/v1/conversations', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    detail(conversationId: string, options: { includeState?: boolean } = {}): Promise<Conversation> {
      return request<Conversation>(`/api/v1/conversations/${conversationId}${encodeQuery({
        include_state: options.includeState
      })}`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    rememberOpened(conversationId: string): Promise<Conversation> {
      return request<Conversation>(`/api/v1/conversations/${conversationId}/opened`, {
        method: 'POST'
      });
    },

    titleSuggestion(conversationId: string): Promise<ConversationTitleSuggestion> {
      return request<ConversationTitleSuggestion>(`/api/v1/conversations/${conversationId}/title-suggestion`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    slashCommandSuggestions(conversationId: string, input: string, limit = 12): Promise<SlashCommandSuggestionsResponse> {
      return request<SlashCommandSuggestionsResponse>(
        `/api/v1/conversations/${conversationId}/slash-command-suggestions${encodeQuery({ input, limit })}`
      );
    },

    update(conversationId: string, payload: Record<string, unknown>): Promise<Conversation> {
      return request<Conversation>(`/api/v1/conversations/${conversationId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    remove(conversationId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/conversations/${conversationId}`, { method: 'DELETE' });
    },

    purge(conversationId: string): Promise<{ ok: boolean; intaris_cascade: boolean; warning?: string }> {
      return request<{ ok: boolean; intaris_cascade: boolean; warning?: string }>(
        `/api/v1/conversations/${conversationId}/purge`,
        { method: 'DELETE' }
      );
    },

    sessions(
      conversationId: string,
      params: { rootOnly?: boolean; activeOnly?: boolean; limit?: number; order?: 'asc' | 'desc' } = {}
    ): Promise<Session[]> {
      return request<Session[]>(`/api/v1/conversations/${conversationId}/sessions${encodeQuery({
        root_only: params.rootOnly,
        active_only: params.activeOnly,
        limit: params.limit,
        order: params.order
      })}`, { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS });
    },

    delegations(conversationId: string): Promise<Session[]> {
      return request<Session[]>(`/api/v1/conversations/${conversationId}/delegations`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    resolve(payload: {
      agent_id: string;
      agent_profile_id?: string | null;
      context_type?: string;
      scope?: 'latest' | 'agent_direct';
    }): Promise<Conversation> {
      return request<Conversation>('/api/v1/conversations/resolve', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    open(payload: ConversationOpenRequest): Promise<Conversation> {
      return request<Conversation>('/api/v1/conversations/open', {
        method: 'POST',
        body: JSON.stringify(payload),
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    markRead(conversationId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/conversations/${conversationId}/read`, {
        method: 'POST'
      });
    },

    managedAction(
      conversationId: string,
      action: 'send' | 'wait' | 'interrupt' | 'stop' | 'retry' | 'fork' | 'close' | 'take-control',
      payload: { message?: string | null; reason?: string | null; instruction?: string | null; wait?: boolean } = {}
    ): Promise<ManagedConversationActionResponse> {
      return request<ManagedConversationActionResponse>(`/api/v1/conversations/${conversationId}/managed/${action}`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    }
  },

  agents: {
    memoryBackends(): Promise<{ items: MemoryBackendDescriptor[] }> {
      return request<{ items: MemoryBackendDescriptor[] }>('/api/v1/agents/memory-backends');
    },

    list(
      cursor: string | null = null,
      params?: { agent_type?: string; include_hidden?: boolean; include_system?: boolean; include_disabled?: boolean }
    ): Promise<CursorPage<Agent>> {
      return request<CursorPage<Agent>>(
        `/api/v1/agents${encodeQuery({ cursor, limit: 100, ...params })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    async listAll(
      params?: { agent_type?: string; include_hidden?: boolean; include_system?: boolean; include_disabled?: boolean }
    ): Promise<Agent[]> {
      return collectCursorPages((cursor) => this.list(cursor, params));
    },

    detail(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}`);
    },

    create(payload: Record<string, unknown>): Promise<Agent> {
      return request<Agent>('/api/v1/agents', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(agentId: string, payload: Record<string, unknown>): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    duplicate(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}/duplicate`, { method: 'POST' });
    },

    resetOverrides(agentId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/agents/${agentId}/reset-overrides`, { method: 'POST' });
    },

    disableSystem(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}/disable`, { method: 'POST' });
    },

    enableSystem(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}/enable`, { method: 'POST' });
    },

    archive(agentId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/agents/${agentId}`, { method: 'DELETE' });
    },

    activate(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}/activate`, { method: 'POST' });
    },

    suspend(agentId: string): Promise<Agent> {
      return request<Agent>(`/api/v1/agents/${agentId}/suspend`, { method: 'POST' });
    },

    syncPersonality(agentId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/agents/${agentId}/sync-personality`, { method: 'POST' });
    },

    tools(agentId: string): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>(`/api/v1/agents/${agentId}/tools`);
    },

    effectiveTools(agentId: string): Promise<EffectiveToolsResponse> {
      return request<EffectiveToolsResponse>(`/api/v1/agents/${agentId}/effective-tools`);
    },

    previewEffectiveTools(payload: EffectiveToolsPreviewRequest): Promise<EffectiveToolsResponse> {
      return request<EffectiveToolsResponse>(`/api/v1/agents/effective-tools/preview`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    listBindings(agentId: string): Promise<string[]> {
      return request<string[]>(`/api/v1/agents/${agentId}/bindings`);
    },

    replaceBindings(agentId: string, secondaryAgentIds: string[]): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/agents/${agentId}/bindings`, {
        method: 'PUT',
        body: JSON.stringify(secondaryAgentIds)
      });
    },

    listShares(agentId: string): Promise<AgentGrant[]> {
      return request<AgentGrant[]>(`/api/v1/agents/${agentId}/shares`);
    },

    myShare(agentId: string): Promise<AgentGrant> {
      return request<AgentGrant>(`/api/v1/agents/${agentId}/my-share`);
    },

    updateMyShare(agentId: string, payload: { execution?: Record<string, unknown> | null }): Promise<AgentGrant> {
      return request<AgentGrant>(`/api/v1/agents/${agentId}/my-share`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    createShare(
      agentId: string,
      payload: { grantee_email: string; executor_scope: 'owner_executor' | 'grantee_executor'; note?: string | null }
    ): Promise<AgentGrant> {
      return request<AgentGrant>(`/api/v1/agents/${agentId}/shares`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    updateShare(
      agentId: string,
      grantId: string,
      payload: { executor_scope?: 'owner_executor' | 'grantee_executor'; note?: string | null }
    ): Promise<AgentGrant> {
      return request<AgentGrant>(`/api/v1/agents/${agentId}/shares/${grantId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    revokeShare(agentId: string, grantId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/agents/${agentId}/shares/${grantId}`, { method: 'DELETE' });
    },

    generateField(field: string, currentValue: string, context: Record<string, string>): Promise<{ value: string }> {
      return request<{ value: string }>('/api/v1/agents/generate-field', {
        method: 'POST',
        body: JSON.stringify({ field, current_value: currentValue, context })
      });
    }
  },

  images: {
    upload(file: File): Promise<{ image_id: string; url: string }> {
      const formData = new FormData();
      formData.append('file', file);
      return request<{ image_id: string; url: string }>('/api/v1/images/upload', {
        method: 'POST',
        body: formData,
        timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS
      });
    },

    generate(prompt: string, options?: { size?: string; quality?: string }): Promise<{ image_id: string; url: string; prompt_used: string }> {
      return request<{ image_id: string; url: string; prompt_used: string }>('/api/v1/images/generate', {
        method: 'POST',
        body: JSON.stringify({ prompt, ...options }),
        timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS
      });
    },

    generatePrompt(details: { name: string; description?: string; personality?: Record<string, unknown> }): Promise<{ prompt: string }> {
      return request<{ prompt: string }>('/api/v1/images/generate-prompt', {
        method: 'POST',
        body: JSON.stringify(details)
      });
    }
  },

  artifacts: {
    upload(file: File, purpose = 'chat_input'): Promise<AttachmentRef> {
      const form = new FormData();
      form.set('file', file);
      form.set('purpose', purpose);
      return request<AttachmentRef>('/api/v1/artifacts/upload', {
        method: 'POST',
        body: form,
        timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS
      });
    },

    signedUrl(artifactId: string, ttlSeconds = 3600, mode: 'download' | 'view' = 'download'): Promise<{ artifact_id: string; url: string; mode?: string; expires_at: string | null }> {
      return request<{ artifact_id: string; url: string; mode?: string; expires_at: string | null }>(`/api/v1/artifacts/${artifactId}/signed-url${encodeQuery({ ttl_seconds: ttlSeconds, mode: mode === 'download' ? undefined : mode })}`);
    }
  },

  deliverables: {
    get(deliverableId: string, options: { accessorConversationId?: string } = {}): Promise<Deliverable> {
      const query = options.accessorConversationId
        ? `?accessor_conversation_id=${encodeURIComponent(options.accessorConversationId)}`
        : '';
      return request<Deliverable>(`/api/v1/deliverables/${encodeURIComponent(deliverableId)}${query}`);
    },

    getForStepRun(stepRunId: string, deliverableId: string): Promise<Deliverable> {
      return request<Deliverable>(
        `/api/v1/step-runs/${encodeURIComponent(stepRunId)}/deliverables/${encodeURIComponent(deliverableId)}`
      );
    },

    shareLink(deliverableId: string): Promise<DeliverableShareLink> {
      return request<DeliverableShareLink>(
        `/api/v1/deliverables/${encodeURIComponent(deliverableId)}/share-link`,
        { method: 'POST' }
      );
    }
  },

  knowledgebases: {
    health(): Promise<KnowledgebaseHealth> {
      return request<KnowledgebaseHealth>('/api/v1/knowledgebases/health');
    },

    capabilities(): Promise<KnowledgebaseCapabilities> {
      return request<KnowledgebaseCapabilities>('/api/v1/knowledgebases/capabilities');
    },

    list(): Promise<KnowledgebaseModel[]> {
      return request<KnowledgebaseModel[]>('/api/v1/knowledgebases/');
    },

    get(knowledgebaseId: string): Promise<KnowledgebaseModel> {
      return request<KnowledgebaseModel>(`/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}`);
    },

    create(payload: KnowledgebaseCreateRequest): Promise<KnowledgebaseModel> {
      return request<KnowledgebaseModel>('/api/v1/knowledgebases/', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(knowledgebaseId: string, payload: KnowledgebaseUpdateRequest): Promise<KnowledgebaseModel> {
      return request<KnowledgebaseModel>(`/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    remove(knowledgebaseId: string): Promise<{ deleted: boolean }> {
      return request<{ deleted: boolean }>(`/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}`, {
        method: 'DELETE'
      });
    },

    shares(knowledgebaseId: string): Promise<KnowledgebaseShareModel[]> {
      return request<KnowledgebaseShareModel[]>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/shares`
      );
    },

    shareCandidates(
      knowledgebaseId: string,
      query: string,
      options: { signal?: AbortSignal } = {}
    ): Promise<KnowledgebaseShareCandidate[]> {
      return request<KnowledgebaseShareCandidate[]>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/shares/candidates${encodeQuery({ q: query })}`,
        { signal: options.signal }
      );
    },

    grantShare(knowledgebaseId: string, payload: KnowledgebaseShareRequest): Promise<KnowledgebaseShareModel> {
      return request<KnowledgebaseShareModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/shares`,
        { method: 'PUT', body: JSON.stringify(payload) }
      );
    },

    revokeShare(knowledgebaseId: string, userEmail: string): Promise<{ revoked: boolean }> {
      return request<{ revoked: boolean }>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/shares/${encodeURIComponent(userEmail)}`,
        { method: 'DELETE' }
      );
    },

    diagnostics(knowledgebaseId: string): Promise<KnowledgebaseDiagnostics> {
      return request<KnowledgebaseDiagnostics>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/diagnostics`
      );
    },

    search(
      knowledgebaseId: string,
      payload: KnowledgebaseSearchRequest,
      options: { signal?: AbortSignal } = {}
    ): Promise<KnowledgebaseSearchResponse> {
      return request<KnowledgebaseSearchResponse>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/search`,
        { method: 'POST', body: JSON.stringify(payload), signal: options.signal }
      );
    },

    ask(
      knowledgebaseId: string,
      payload: KnowledgebaseAskRequest,
      options: { signal?: AbortSignal } = {}
    ): Promise<KnowledgebaseAskResponse> {
      return request<KnowledgebaseAskResponse>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/ask`,
        { method: 'POST', body: JSON.stringify(payload), signal: options.signal, timeoutMs: 60_000 }
      );
    },

    sourceContext(
      knowledgebaseId: string,
      payload: KnowledgebaseSourceContextRequest
    ): Promise<KnowledgebaseSourceContextResponse> {
      return request<KnowledgebaseSourceContextResponse>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/source-context`,
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    facets(
      knowledgebaseId: string,
      payload: KnowledgebaseFacetRequest,
      options: { signal?: AbortSignal } = {}
    ): Promise<KnowledgebaseFacetResponse> {
      return request<KnowledgebaseFacetResponse>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/facets`,
        { method: 'POST', body: JSON.stringify(payload), signal: options.signal }
      );
    },

    artifacts(knowledgebaseId: string): Promise<KnowledgebaseArtifactModel[]> {
      return request<KnowledgebaseArtifactModel[]>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/artifacts`
      );
    },

    attachArtifact(
      knowledgebaseId: string,
      payload: KnowledgebaseAttachRequest
    ): Promise<KnowledgebaseArtifactModel> {
      return request<KnowledgebaseArtifactModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/artifacts`,
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    detachArtifact(knowledgebaseId: string, artifactId: string): Promise<KnowledgebaseArtifactModel> {
      return request<KnowledgebaseArtifactModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/artifacts/${encodeURIComponent(artifactId)}`,
        { method: 'DELETE' }
      );
    },

    reindexArtifact(knowledgebaseId: string, artifactId: string): Promise<KnowledgebaseIndexJobModel> {
      return request<KnowledgebaseIndexJobModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/artifacts/${encodeURIComponent(artifactId)}/reindex`,
        { method: 'POST' }
      );
    },

    reindexAll(knowledgebaseId: string): Promise<KnowledgebaseIndexJobModel[]> {
      return request<KnowledgebaseIndexJobModel[]>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/reindex`,
        { method: 'POST' }
      );
    },

    jobs(knowledgebaseId: string): Promise<KnowledgebaseIndexJobModel[]> {
      return request<KnowledgebaseIndexJobModel[]>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/jobs`
      );
    },

    retryJob(knowledgebaseId: string, jobId: string): Promise<KnowledgebaseIndexJobModel> {
      return request<KnowledgebaseIndexJobModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/jobs/${encodeURIComponent(jobId)}/retry`,
        { method: 'POST' }
      );
    },

    cancelJob(knowledgebaseId: string, jobId: string): Promise<KnowledgebaseIndexJobModel> {
      return request<KnowledgebaseIndexJobModel>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: 'POST' }
      );
    },

    agentAssignments(knowledgebaseId: string): Promise<string[]> {
      return request<string[]>(`/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/agents`);
    },

    assignAgent(knowledgebaseId: string, agentId: string): Promise<{ assigned: boolean }> {
      return request<{ assigned: boolean }>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/agents/${encodeURIComponent(agentId)}`,
        { method: 'POST' }
      );
    },

    unassignAgent(knowledgebaseId: string, agentId: string): Promise<{ assigned: boolean }> {
      return request<{ assigned: boolean }>(
        `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/agents/${encodeURIComponent(agentId)}`,
        { method: 'DELETE' }
      );
    },

    documents: {
      upload(
        knowledgebaseId: string,
        files: File[],
        paths: string[],
        conflictPolicy: KnowledgebaseDocumentConflictPolicy,
        options: { signal?: AbortSignal } = {}
      ): Promise<KnowledgebaseDocumentUploadResponse> {
        const form = new FormData();
        files.forEach((file) => form.append('files[]', file, file.name));
        paths.forEach((path) => form.append('paths[]', path));
        form.set('conflict_policy', conflictPolicy);
        return request<KnowledgebaseDocumentUploadResponse>(
          `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/documents`,
          { method: 'POST', body: form, signal: options.signal, timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS }
        );
      },

      list(
        knowledgebaseId: string,
        params: { status?: string; query?: string; sort?: 'path' | 'updated_at'; direction?: 'asc' | 'desc'; cursor?: string; limit?: number } = {},
        options: { signal?: AbortSignal } = {}
      ): Promise<KnowledgebaseDocumentListResponse> {
        return request<KnowledgebaseDocumentListResponse>(
          `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/documents${encodeQuery(params)}`,
          { signal: options.signal }
        );
      },

      get(knowledgebaseId: string, kbArtifactId: string): Promise<KnowledgebaseDocumentDetail> {
        return request<KnowledgebaseDocumentDetail>(
          `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/documents/${encodeURIComponent(kbArtifactId)}`
        );
      },

      content(
        knowledgebaseId: string,
        kbArtifactId: string,
        contentMode: 'source' | 'extracted' = 'extracted',
        options: { signal?: AbortSignal } = {}
      ): Promise<KnowledgebaseDocumentContentResponse> {
        return request<KnowledgebaseDocumentContentResponse>(
          `/api/v1/knowledgebases/${encodeURIComponent(knowledgebaseId)}/documents/${encodeURIComponent(kbArtifactId)}/content${encodeQuery({ content_mode: contentMode })}`,
          { signal: options.signal }
        );
      }
    }
  },

  tools: {
    list(): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>('/api/v1/tools');
    },

    observedLocalMcpTools(): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>('/api/v1/tools/local-mcp/observed');
    },

    executorTools(): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>('/api/v1/tools/executor');
    },

    mcpServers(): Promise<MCPServer[]> {
      return request<MCPServer[]>('/api/v1/mcp/servers');
    },

    intarisMcpServers(): Promise<IntarisMCPServer[]> {
      return request<IntarisMCPServer[]>('/api/v1/intaris/mcp/servers');
    },

    intarisMcpTools(): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>('/api/v1/intaris/mcp/tools');
    },

    requeueClassification(payload: Record<string, unknown>): Promise<{ updated: number; status: string }> {
      return request<{ updated: number; status: string }>('/api/v1/tools/classification/requeue', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    overrideClassification(payload: Record<string, unknown>): Promise<{ updated: number; status: string }> {
      return request<{ updated: number; status: string }>('/api/v1/tools/classification/override', {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    resetClassificationOverride(payload: Record<string, unknown>): Promise<{ updated: number; status: string }> {
      return request<{ updated: number; status: string }>('/api/v1/tools/classification/reset-override', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    testAgentMcp(agentId: string): Promise<MCPServerTestResponse> {
      return request<MCPServerTestResponse>(`/api/v1/agents/${agentId}/mcp/test`, {
        method: 'POST'
      });
    },

    // Global MCP server management
    listMcpServerConfigs(): Promise<MCPServerConfigResponse[]> {
      return request<MCPServerConfigResponse[]>('/api/v1/mcp-servers');
    },

    getMcpServerConfig(serverId: string): Promise<MCPServerConfigResponse> {
      return request<MCPServerConfigResponse>(`/api/v1/mcp-servers/${serverId}`);
    },

    createMcpServer(data: MCPServerCreateRequest): Promise<MCPServerConfigResponse> {
      return request<MCPServerConfigResponse>('/api/v1/mcp-servers', {
        method: 'POST',
        body: JSON.stringify(data)
      });
    },

    updateMcpServer(serverId: string, data: MCPServerUpdateRequest): Promise<MCPServerConfigResponse> {
      return request<MCPServerConfigResponse>(`/api/v1/mcp-servers/${serverId}`, {
        method: 'PUT',
        body: JSON.stringify(data)
      });
    },

    deleteMcpServer(serverId: string): Promise<void> {
      return request<void>(`/api/v1/mcp-servers/${serverId}`, { method: 'DELETE' });
    },

    getDeliverable(deliverableId: string): Promise<Deliverable> {
      return request<Deliverable>(`/api/v1/deliverables/${encodeURIComponent(deliverableId)}`);
    },

    startMcpOAuth(serverId: string): Promise<{
      authorization_url: string;
      transaction_id: string;
      expires_at: string;
      issuer?: string | null;
      authorization_server?: string | null;
      scopes?: string[];
      flow?: string;
      verification_uri?: string | null;
      verification_uri_complete?: string | null;
      user_code?: string | null;
      interval?: number | null;
      callback_mode?: string;
      oauth_executor_id?: string | null;
      oauth_executor_name?: string | null;
      redirect_uri?: string | null;
      instructions?: string | null;
    }> {
      return request(`/api/v1/mcp-servers/${serverId}/oauth/start`, {
        method: 'POST'
      });
    },

    mcpOAuthStatus(serverId: string): Promise<{
      connected: boolean;
      issuer?: string | null;
      resource?: string | null;
      scopes?: string[];
      expires_at?: string | null;
      access_token_expires_at?: string | null;
      authorization_expires_at?: string | null;
      refreshable?: boolean;
      status?: string;
    }> {
      return request(`/api/v1/mcp-servers/${serverId}/oauth/status`);
    },

    disconnectMcpOAuth(serverId: string): Promise<{ status: string }> {
      return request(`/api/v1/mcp-servers/${serverId}/oauth/disconnect`, {
        method: 'POST'
      });
    }
  },

  channels: {
    listTypes(): Promise<ChannelMeta[]> {
      return request<ChannelMeta[]>('/api/v1/channels/types');
    },

    getType(channelType: string): Promise<ChannelMeta> {
      return request<ChannelMeta>(`/api/v1/channels/types/${channelType}`);
    },

    listAccounts(): Promise<ChannelAccount[]> {
      return request<ChannelAccount[]>('/api/v1/channels/accounts');
    },

    createAccount(payload: Record<string, unknown>): Promise<{ account_id: string; channel_type: string; display_name: string; webhook_secret: string | null; created_at: string | null }> {
      return request<{ account_id: string; channel_type: string; display_name: string; webhook_secret: string | null; created_at: string | null }>('/api/v1/channels/accounts', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    getAccount(accountId: string): Promise<ChannelAccount> {
      return request<ChannelAccount>(`/api/v1/channels/accounts/${accountId}`);
    },

    updateAccount(accountId: string, payload: Record<string, unknown>): Promise<ChannelAccount> {
      return request<ChannelAccount>(`/api/v1/channels/accounts/${accountId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    deleteAccount(accountId: string): Promise<{ deleted: boolean }> {
      return request<{ deleted: boolean }>(`/api/v1/channels/accounts/${accountId}`, {
        method: 'DELETE'
      });
    },

    startAccount(accountId: string): Promise<{ status: ChannelAccountStatus | string }> {
      return request<{ status: ChannelAccountStatus | string }>(`/api/v1/channels/accounts/${accountId}/start`, {
        method: 'POST'
      });
    },

    stopAccount(accountId: string): Promise<{ status: string }> {
      return request<{ status: string }>(`/api/v1/channels/accounts/${accountId}/stop`, {
        method: 'POST'
      });
    },

    getAccountStatus(accountId: string): Promise<ChannelAccountStatus> {
      return request<ChannelAccountStatus>(`/api/v1/channels/accounts/${accountId}/status`);
    },

    listContacts(): Promise<ChannelContact[]> {
      return request<ChannelContact[]>('/api/v1/channels/contacts');
    },

    createContact(payload: Record<string, unknown>): Promise<ChannelContact> {
      return request<ChannelContact>('/api/v1/channels/contacts', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    listPairingRequests(): Promise<PairingRequest[]> {
      return request<PairingRequest[]>('/api/v1/channels/pairing-requests');
    },

    redeemPairingCode(code: string): Promise<PairingRequest> {
      return request<PairingRequest>('/api/v1/channels/pair', {
        method: 'POST',
        body: JSON.stringify({ code })
      });
    },

    rejectPairingRequest(requestId: string): Promise<{ rejected: boolean }> {
      return request<{ rejected: boolean }>(`/api/v1/channels/pairing-requests/${requestId}/reject`, {
        method: 'POST'
      });
    }
  },

  executor: {
    status(): Promise<ExecutorStatus> {
      return request<ExecutorStatus>('/api/v1/executor/status');
    },

    async list(): Promise<ExecutorConfig[]> {
      const executors = await request<ExecutorConfig[]>('/api/v1/executors');
      executors.forEach(rememberExecutorConfigVersion);
      return executors;
    },

    async get(executorId: string): Promise<ExecutorConfig> {
      return rememberExecutorConfigVersion(
        await request<ExecutorConfig>(`/api/v1/executors/${executorId}`)
      );
    },

    async create(data: ExecutorCreateRequest): Promise<ExecutorConfig> {
      return rememberExecutorConfigVersion(
        await request<ExecutorConfig>('/api/v1/executors', {
          method: 'POST',
          body: JSON.stringify(data)
        })
      );
    },

    async update(executorId: string, data: ExecutorUpdateRequest): Promise<ExecutorConfig> {
      const expectedConfigVersion =
        data.expected_config_version ??
        (data.config === undefined ? undefined : executorConfigVersions.get(executorId));
      return rememberExecutorConfigVersion(
        await request<ExecutorConfig>(`/api/v1/executors/${executorId}`, {
          method: 'PUT',
          body: JSON.stringify({
            ...data,
            expected_config_version: expectedConfigVersion
          })
        })
      );
    },

    delete(executorId: string): Promise<void> {
      return request<void>(`/api/v1/executors/${executorId}`, { method: 'DELETE' });
    },

    generateToken(executorId: string): Promise<ExecutorTokenResponse> {
      return request<ExecutorTokenResponse>(`/api/v1/executors/${executorId}/token`, {
        method: 'POST'
      });
    }
  },

  localModels: {
    catalog(options: {
      source?: LocalModelCatalogSource;
      query?: string;
      cursor?: string;
      limit?: number;
      parameterRange?: string;
      downloadSizeRange?: string;
      quantization?: string;
      minContext?: number;
      includeUnknown?: boolean;
    } = {}): Promise<LocalModelCatalogResponse> {
      const params = new URLSearchParams();
      if (options.source) params.set('source', options.source);
      if (options.query) params.set('query', options.query);
      if (options.cursor) params.set('cursor', options.cursor);
      if (options.limit) params.set('limit', String(options.limit));
      if (options.parameterRange) params.set('parameter_range', options.parameterRange);
      if (options.downloadSizeRange) params.set('download_size_range', options.downloadSizeRange);
      if (options.quantization) params.set('quantization', options.quantization);
      if (options.minContext) params.set('min_context', String(options.minContext));
      if (options.includeUnknown != null) params.set('include_unknown', String(options.includeUnknown));
      const suffix = params.size ? `?${params.toString()}` : '';
      return request<LocalModelCatalogResponse>(`/api/v1/local-model-catalog${suffix}`);
    },

    resolve(reference: string): Promise<LocalModelCatalogItem> {
      return request<LocalModelCatalogItem>(
        `/api/v1/local-model-catalog/resolve?ref=${encodeURIComponent(reference)}`
      );
    },

    detail(repository: string, revisionSha?: string | null): Promise<LocalModelCatalogItem> {
      const params = new URLSearchParams({ repo: repository });
      if (revisionSha) params.set('revision_sha', revisionSha);
      return request<LocalModelCatalogItem>(
        `/api/v1/local-model-catalog/detail?${params.toString()}`
      );
    },

    plan(payload: LocalModelFitPlanRequest): Promise<LocalModelFitPlan> {
      return request<LocalModelFitPlan>('/api/v1/local-model-fit-plans', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    deployments(): Promise<LocalModelDeployment[]> {
      return request<LocalModelDeployment[]>('/api/v1/local-model-deployments');
    },

    createDeployment(payload: LocalModelDeploymentCreate): Promise<LocalModelDeployment> {
      return request<LocalModelDeployment>('/api/v1/local-model-deployments', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    createManagedDeployment(
      payload: LocalModelManagedDeploymentCreate
    ): Promise<LocalModelManagedDeploymentCreateResponse> {
      return request<LocalModelManagedDeploymentCreateResponse>(
        '/api/v1/local-model-deployments:managed',
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    recommendProvider(payload: {
      requested_ref: string;
      selector?: LocalModelSelector;
      shared?: boolean;
    }): Promise<LocalModelProviderRecommendation> {
      return request<LocalModelProviderRecommendation>(
        '/api/v1/local-model-providers/recommendations',
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    findOrCreateProvider(payload: {
      requested_ref: string;
      selector: LocalModelSelector;
      shared?: boolean;
      force_create?: boolean;
    }): Promise<LocalModelProviderFindOrCreateResponse> {
      return request<LocalModelProviderFindOrCreateResponse>(
        '/api/v1/local-model-providers:find-or-create',
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    updateDeployment(
      deploymentId: string,
      payload: LocalModelDeploymentUpdate
    ): Promise<LocalModelDeployment> {
      return request<LocalModelDeployment>(
        `/api/v1/local-model-deployments/${encodeURIComponent(deploymentId)}`,
        { method: 'PATCH', body: JSON.stringify(payload) }
      );
    },

    attachManagedProvider(
      deploymentId: string,
      payload: { force_create_provider?: boolean }
    ): Promise<LocalModelManagedDeploymentCreateResponse> {
      return request<LocalModelManagedDeploymentCreateResponse>(
        `/api/v1/local-model-deployments/${encodeURIComponent(deploymentId)}:attach-managed-provider`,
        { method: 'POST', body: JSON.stringify(payload) }
      );
    },

    targets(deploymentId: string): Promise<LocalModelTargetStatus[]> {
      return request<LocalModelTargetStatus[]>(
        `/api/v1/local-model-deployments/${encodeURIComponent(deploymentId)}/targets`
      );
    },

    operations(deploymentId: string): Promise<LocalModelOperation[]> {
      return request<LocalModelOperation[]>(
        `/api/v1/local-model-deployments/${encodeURIComponent(deploymentId)}/operations`
      );
    },

    reconcile(deploymentId: string): Promise<LocalModelDeployment> {
      return request<LocalModelDeployment>(
        `/api/v1/local-model-deployments/${encodeURIComponent(deploymentId)}/reconciliation-requests`,
        { method: 'POST' }
      );
    }
  },

  skills: {
    list(): Promise<Skill[]> {
      return request<Skill[]>('/api/v1/skills', { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS });
    },

    get(skillId: string): Promise<Skill> {
      return request<Skill>(`/api/v1/skills/${skillId}`);
    },

    create(data: SkillCreate): Promise<Skill> {
      return request<Skill>('/api/v1/skills', { method: 'POST', body: JSON.stringify(data) });
    },

    update(skillId: string, data: SkillUpdate): Promise<Skill> {
      return request<Skill>(`/api/v1/skills/${skillId}`, { method: 'PUT', body: JSON.stringify(data) });
    },

    delete(skillId: string): Promise<void> {
      return request<void>(`/api/v1/skills/${skillId}`, { method: 'DELETE' });
    },

    reset(skillId: string): Promise<Skill> {
      return request<Skill>(`/api/v1/skills/${skillId}/reset`, { method: 'POST' });
    },

    versions(skillId: string): Promise<SkillVersion[]> {
      return request<SkillVersion[]>(`/api/v1/skills/${skillId}/versions`);
    },

    restoreVersion(skillId: string, versionId: string): Promise<Skill> {
      return request<Skill>(`/api/v1/skills/${skillId}/versions/${versionId}/restore`, {
        method: 'POST'
      });
    },

    decomposePreview(skillId: string): Promise<SkillDecompositionPreview> {
      return request<SkillDecompositionPreview>(`/api/v1/skills/${skillId}/decompose-preview`, {
        method: 'POST'
      });
    },

    import(data: SkillImportRequest): Promise<Skill> {
      return request<Skill>('/api/v1/skills/import', { method: 'POST', body: JSON.stringify(data) });
    },

    export(skillId: string, format: string = 'skill_md'): Promise<SkillExportResponse> {
      return request<SkillExportResponse>(`/api/v1/skills/${skillId}/export?format=${format}`, { method: 'POST' });
    }
  },

  tasks: {
    list(params: Record<string, string | number | null | undefined> = {}): Promise<CursorPage<Task>> {
      return request<CursorPage<Task>>(`/api/v1/tasks${encodeQuery({ limit: 100, ...params })}`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    async listAll(): Promise<Task[]> {
      return collectCursorPages((cursor) => this.list({ cursor }));
    },

    board(params: Record<string, string | number | null | undefined> = {}): Promise<TaskBoard> {
      return request<TaskBoard>(`/api/v1/tasks/board${encodeQuery({ limit: 20, ...params })}`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    boardColumn(columnId: string, params: Record<string, string | number | null | undefined> = {}): Promise<TaskBoardColumn> {
      return request<TaskBoardColumn>(`/api/v1/tasks/board/${columnId}${encodeQuery({ limit: 20, ...params })}`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    doneGroupTasks(groupKey: string, params: Record<string, string | number | null | undefined> = {}): Promise<CursorPage<TaskBoardItem>> {
      return request<CursorPage<TaskBoardItem>>(
        `/api/v1/tasks/board/done/groups/${encodeURIComponent(groupKey)}/tasks${encodeQuery({ limit: 20, ...params })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    detail(taskId: string): Promise<TaskDetail> {
      return request<TaskDetail>(`/api/v1/tasks/${taskId}`, { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS });
    },

    summary(taskId: string): Promise<TaskDetail> {
      return request<TaskDetail>(`/api/v1/tasks/${taskId}/summary`, { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS });
    },

    create(payload: Record<string, unknown>): Promise<Task> {
      return request<Task>('/api/v1/tasks', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(taskId: string, payload: Record<string, unknown>): Promise<Task> {
      return request<Task>(`/api/v1/tasks/${taskId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    remove(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}`, { method: 'DELETE' });
    },

    submit(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/submit`, { method: 'POST' });
    },

    pause(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/pause`, { method: 'POST' });
    },

    resume(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/resume`, { method: 'POST' });
    },

    rerun(taskId: string): Promise<TaskRerunResponse> {
      return request<TaskRerunResponse>(`/api/v1/tasks/${taskId}/rerun`, {
        method: 'POST'
      });
    },

    chat(taskId: string): Promise<TaskChatResponse> {
      return request<TaskChatResponse>(`/api/v1/tasks/${taskId}/chat`, {
        method: 'POST'
      });
    },

    controlChat(taskId: string): Promise<TaskControlChatResponse> {
      return request<TaskControlChatResponse>(`/api/v1/tasks/${taskId}/control-chat`, {
        method: 'POST'
      });
    },

    stepChat(taskId: string, stepRunId: string): Promise<TaskChatResponse> {
      return request<TaskChatResponse>(`/api/v1/tasks/${taskId}/steps/${encodeURIComponent(stepRunId)}/chat`, {
        method: 'POST'
      });
    },

    cancel(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST' });
    },

    batchSubmit(taskIds: string[]): Promise<{ results: Array<Record<string, unknown>>; succeeded: number; failed: number }> {
      return request<{ results: Array<Record<string, unknown>>; succeeded: number; failed: number }>('/api/v1/tasks/batch-submit', {
        method: 'POST',
        body: JSON.stringify({ task_ids: taskIds })
      });
    },

    gateResponse(taskId: string, payload: Record<string, unknown>): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/gate-response`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    stepResponse(taskId: string, payload: QuestionSetReply): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/step-response`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    steps(taskId: string): Promise<StepRun[]> {
      return request<StepRun[]>(`/api/v1/tasks/${taskId}/steps`);
    },

    stepSummaries(
      taskId: string,
      params: Record<string, string | number | boolean | null | undefined> = {}
    ): Promise<CursorPage<StepRun>> {
      return request<CursorPage<StepRun>>(
        `/api/v1/tasks/${taskId}/steps/summary${encodeQuery({ limit: 100, latest_only: true, ...params })}`
      );
    },

    stepHistory(taskId: string, stepName: string): Promise<StepRun[]> {
      return request<StepRun[]>(`/api/v1/tasks/${taskId}/steps/${encodeURIComponent(stepName)}/history`);
    },

    stepHistorySummary(
      taskId: string,
      stepName: string,
      params: Record<string, string | number | boolean | null | undefined> = {}
    ): Promise<CursorPage<StepRun>> {
      return request<CursorPage<StepRun>>(
        `/api/v1/tasks/${taskId}/steps/${encodeURIComponent(stepName)}/summary${encodeQuery({ limit: 50, ...params })}`
      );
    },

    stepRunDetail(stepRunId: string): Promise<StepRun> {
      return request<StepRun>(`/api/v1/step-runs/${encodeURIComponent(stepRunId)}`);
    },

    stepRunDeliverable(stepRunId: string, deliverableId: string): Promise<Deliverable> {
      return request<Deliverable>(
        `/api/v1/step-runs/${encodeURIComponent(stepRunId)}/deliverables/${encodeURIComponent(deliverableId)}`
      );
    },

    comments(taskId: string): Promise<TaskComment[]> {
      return request<TaskComment[]>(`/api/v1/tasks/${taskId}/comments`);
    },

    addComment(taskId: string, payload: Record<string, unknown>): Promise<TaskComment> {
      return request<TaskComment>(`/api/v1/tasks/${taskId}/comments`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    workflowRun(taskId: string): Promise<WorkflowRun> {
      return request<WorkflowRun>(`/api/v1/tasks/${taskId}/workflow-run`);
    },

    addDependency(taskId: string, dependsOn: string, required = true): Promise<{ task_id: string; depends_on: string; required: boolean }> {
      return request<{ task_id: string; depends_on: string; required: boolean }>(`/api/v1/tasks/${taskId}/dependencies`, {
        method: 'POST',
        body: JSON.stringify({ depends_on: dependsOn, required })
      });
    },

    removeDependency(taskId: string, dependsOn: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/tasks/${taskId}/dependencies/${dependsOn}`, { method: 'DELETE' });
    }
  },

  projects: {
    list(params: { status?: string; q?: string } = {}): Promise<Project[]> {
      return request<Project[]>(`/api/v1/projects${encodeQuery(params)}`, {
        timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS
      });
    },

    detail(projectId: string): Promise<Project> {
      return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}`);
    },

    create(payload: Record<string, unknown>): Promise<Project> {
      return request<Project>('/api/v1/projects', { method: 'POST', body: JSON.stringify(payload) });
    },

    update(projectId: string, payload: Record<string, unknown>): Promise<Project> {
      return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    remove(projectId: string): Promise<Project> {
      return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' });
    },

    addSource(projectId: string, payload: Record<string, unknown>): Promise<ProjectSource> {
      return request<ProjectSource>(`/api/v1/projects/${encodeURIComponent(projectId)}/sources`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    updateSource(projectId: string, sourceId: string, payload: Record<string, unknown>): Promise<ProjectSource> {
      return request<ProjectSource>(`/api/v1/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(sourceId)}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      });
    },

    deleteSource(projectId: string, sourceId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE' });
    },

    attachWorkflow(projectId: string, workflowId: string): Promise<Project> {
      return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`, { method: 'POST' });
    },

    detachWorkflow(projectId: string, workflowId: string): Promise<Project> {
      return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`, { method: 'DELETE' });
    },

    grants(projectId: string): Promise<ProjectGrant[]> {
      return request<ProjectGrant[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/grants`);
    },

    createGrant(projectId: string, payload: Record<string, unknown>): Promise<ProjectGrant> {
      return request<ProjectGrant>(`/api/v1/projects/${encodeURIComponent(projectId)}/grants`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    revokeGrant(projectId: string, grantId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/projects/${encodeURIComponent(projectId)}/grants/${encodeURIComponent(grantId)}`, { method: 'DELETE' });
    },

    generateAvatar(projectId: string): Promise<{ avatar_image_id: string; avatar_url: string }> {
      return request<{ avatar_image_id: string; avatar_url: string }>(`/api/v1/projects/${encodeURIComponent(projectId)}/avatar/generate`, { method: 'POST' });
    }
  },

  workflows: {
    list(cursor: string | null = null, params?: { include_disabled?: boolean; include_ephemeral?: boolean; project_id?: string | null }): Promise<CursorPage<Workflow>> {
      return request<CursorPage<Workflow>>(
        `/api/v1/workflows${encodeQuery({ cursor, limit: 100, ...params })}`,
        { timeoutMs: UI_LOAD_REQUEST_TIMEOUT_MS }
      );
    },

    async listAll(params?: { include_disabled?: boolean; include_ephemeral?: boolean; project_id?: string | null }): Promise<Workflow[]> {
      return collectCursorPages((cursor) => this.list(cursor, params));
    },

    detail(workflowId: string): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}`);
    },

    stepProfiles(): Promise<StepProfileDefinition[]> {
      return request<StepProfileDefinition[]>('/api/v1/workflows/step-profiles');
    },

    create(payload: Record<string, unknown>): Promise<Workflow> {
      return request<Workflow>('/api/v1/workflows', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(workflowId: string, payload: Record<string, unknown>): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    remove(workflowId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/workflows/${workflowId}`, { method: 'DELETE' });
    },

    duplicate(workflowId: string): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}/duplicate`, { method: 'POST' });
    },

    resetOverrides(workflowId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/workflows/${workflowId}/reset-overrides`, { method: 'POST' });
    },

    disable(workflowId: string): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}/disable`, { method: 'POST' });
    },

    enable(workflowId: string): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}/enable`, { method: 'POST' });
    }
  },

  schedules: {
    list(params: Record<string, string | boolean | null | undefined> = {}): Promise<Schedule[]> {
      return request<Schedule[]>(`/api/v1/schedules${encodeQuery(params)}`);
    },

    detail(scheduleId: string): Promise<Schedule> {
      return request<Schedule>(`/api/v1/schedules/${scheduleId}`);
    },

    create(payload: Record<string, unknown>): Promise<Schedule> {
      return request<Schedule>('/api/v1/schedules', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(scheduleId: string, payload: Record<string, unknown>): Promise<Schedule> {
      return request<Schedule>(`/api/v1/schedules/${scheduleId}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    remove(scheduleId: string): Promise<void> {
      return request<void>(`/api/v1/schedules/${scheduleId}`, { method: 'DELETE' });
    },

    trigger(scheduleId: string): Promise<ScheduleTriggerResponse> {
      return request<ScheduleTriggerResponse>(`/api/v1/schedules/${scheduleId}/trigger`, { method: 'POST' });
    },

    enable(scheduleId: string): Promise<Schedule> {
      return request<Schedule>(`/api/v1/schedules/${scheduleId}/enable`, { method: 'POST' });
    },

    disable(scheduleId: string): Promise<Schedule> {
      return request<Schedule>(`/api/v1/schedules/${scheduleId}/disable`, { method: 'POST' });
    },

    runs(scheduleId: string, limit = 20): Promise<ScheduleRun[]> {
      return request<ScheduleRun[]>(`/api/v1/schedules/${scheduleId}/runs${encodeQuery({ limit })}`);
    }
  },

  settings: {
    list(): Promise<SettingsCategory[]> {
      return request<SettingsCategory[]>('/api/v1/settings');
    },

    detail(key: string): Promise<Setting> {
      return request<Setting>(`/api/v1/settings/${key}`);
    },

    update(key: string, value: unknown): Promise<Setting> {
      return request<Setting>(`/api/v1/settings/${key}`, {
        method: 'PUT',
        body: JSON.stringify({ value })
      });
    },

    reset(key: string): Promise<Setting> {
      return request<Setting>(`/api/v1/settings/${key}`, {
        method: 'DELETE'
      });
    },

    stepProfiles(): Promise<StepProfileDefinition[]> {
      return request<StepProfileDefinition[]>('/api/v1/settings/step-profiles');
    },

    createStepProfile(payload: Record<string, unknown>): Promise<StepProfileDefinition> {
      return request<StepProfileDefinition>('/api/v1/settings/step-profiles', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    updateStepProfile(profileId: string, payload: Record<string, unknown>): Promise<StepProfileDefinition> {
      return request<StepProfileDefinition>(`/api/v1/settings/step-profiles/${profileId}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    resetStepProfile(profileId: string): Promise<StepProfileDefinition> {
      return request<StepProfileDefinition>(`/api/v1/settings/step-profiles/${profileId}`, {
        method: 'DELETE'
      });
    }
  },

  userPreferences: {
    get(): Promise<UserPreferences> {
      return request<UserPreferences>('/api/v1/user-preferences');
    },

    update(payload: UserPreferences): Promise<UserPreferences> {
      return request<UserPreferences>('/api/v1/user-preferences', {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    }
  },

  llmProviders: {
    list(): Promise<CursorPage<LLMProvider>> {
      return request<CursorPage<LLMProvider>>('/api/v1/llm-providers');
    },

    detail(providerId: string): Promise<LLMProvider> {
      return request<LLMProvider>(`/api/v1/llm-providers/${providerId}`);
    },

    create(payload: Record<string, unknown>): Promise<LLMProvider> {
      return request<LLMProvider>('/api/v1/llm-providers', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    update(providerId: string, payload: Record<string, unknown>): Promise<LLMProvider> {
      return request<LLMProvider>(`/api/v1/llm-providers/${providerId}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    remove(providerId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/llm-providers/${providerId}`, { method: 'DELETE' });
    },

    test(providerId: string): Promise<ProviderTestResult & { provider_id: string }> {
      return request<ProviderTestResult & { provider_id: string }>(`/api/v1/llm-providers/${providerId}/test`, {
        method: 'POST'
      });
    },

    discoverModels(providerId: string): Promise<{ provider_id: string; models: ModelEntry[] }> {
      return request<{ provider_id: string; models: ModelEntry[] }>(`/api/v1/llm-providers/${providerId}/discover-models`, {
        method: 'POST'
      });
    },

    setDefault(providerId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/llm-providers/${providerId}/set-default`, { method: 'POST' });
    },

    discoverModelsPreview(payload: {
      provider_id?: string;
      preset: string;
      base_url: string;
      api_key?: string;
      secret_name?: string;
      env_var?: string;
      location?: string;
      executor_id?: string;
      executor_labels?: Record<string, string>;
    }): Promise<{ models: ModelEntry[] }> {
      return request<{ models: ModelEntry[] }>('/api/v1/llm-providers/discover-models-preview', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    enrichModels(providerId: string, modelIds: string[]): Promise<{ models: ModelEntry[] }> {
      return request<{ models: ModelEntry[] }>(`/api/v1/llm-providers/${providerId}/enrich-models`, {
        method: 'POST',
        body: JSON.stringify({ model_ids: modelIds })
      });
    },

    enrichModelsPreview(payload: {
      preset: string;
      base_url: string;
      model_ids: string[];
      api_key?: string;
      secret_name?: string;
      env_var?: string;
    }): Promise<{ models: ModelEntry[] }> {
      return request<{ models: ModelEntry[] }>('/api/v1/llm-providers/enrich-models-preview', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    startChatgptOAuth(providerId: string): Promise<LLMProviderOAuthStatus> {
      return request<LLMProviderOAuthStatus>(`/api/v1/llm-providers/${providerId}/oauth/chatgpt/start`, {
        method: 'POST'
      });
    },

    chatgptOAuthStatus(providerId: string): Promise<LLMProviderOAuthStatus> {
      return request<LLMProviderOAuthStatus>(`/api/v1/llm-providers/${providerId}/oauth/chatgpt/status`);
    },

    startAnthropicOAuth(providerId: string): Promise<LLMProviderOAuthStatus> {
      return request<LLMProviderOAuthStatus>(`/api/v1/llm-providers/${providerId}/oauth/anthropic/start`, {
        method: 'POST'
      });
    },

    completeAnthropicOAuth(providerId: string, callbackInput: string): Promise<LLMProviderOAuthStatus> {
      return request<LLMProviderOAuthStatus>(`/api/v1/llm-providers/${providerId}/oauth/anthropic/complete`, {
        method: 'POST',
        body: JSON.stringify({ callback_input: callbackInput })
      });
    },

    anthropicOAuthStatus(providerId: string): Promise<LLMProviderOAuthStatus> {
      return request<LLMProviderOAuthStatus>(`/api/v1/llm-providers/${providerId}/oauth/anthropic/status`);
    },

    codexUsage(providerId: string): Promise<CodexUsage> {
      return request<CodexUsage>(`/api/v1/llm-providers/${providerId}/codex/usage`);
    },

    clearChatgptOAuth(providerId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/llm-providers/${providerId}/oauth/chatgpt`, {
        method: 'DELETE'
      });
    },

    clearAnthropicOAuth(providerId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/llm-providers/${providerId}/oauth/anthropic`, {
        method: 'DELETE'
      });
    },

  },

  modelRouting: {
    get(): Promise<ModelRouting> {
      return request<ModelRouting>('/api/v1/model-routing');
    },

    update(payload: Record<string, unknown>): Promise<ModelRouting> {
      return request<ModelRouting>('/api/v1/model-routing', {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    }
  },

  tts: {
    synthesize(payload: TtsSynthesizeRequest, opts: { signal?: AbortSignal } = {}): Promise<TtsSynthesizeResponse> {
      return request<TtsSynthesizeResponse>('/api/v1/tts/synthesize', {
        method: 'POST',
        body: JSON.stringify(payload),
        signal: opts.signal,
        timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS
      });
    }
  },

  stt: {
    async transcribe(file: Blob, opts: { filename?: string; language?: string; prompt?: string; signal?: AbortSignal } = {}): Promise<SttTranscribeResponse> {
      const form = new FormData();
      form.append('file', file, opts.filename ?? 'voice-input.webm');
      if (opts.language) form.append('language', opts.language);
      if (opts.prompt) form.append('prompt', opts.prompt);
      const response = await fetchWithTimeout('/api/v1/stt/transcribe', {
        method: 'POST',
        body: form,
        credentials: 'include',
        signal: opts.signal
      }, { timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS });
      if (!response.ok) {
        let detail = 'Transcription failed';
        try {
          const body = await response.json();
          detail = body?.error?.message ?? detail;
        } catch {
          // ignore
        }
        throw new Error(detail);
      }
      return response.json();
    },

    async transcribeArtifact(artifactId: string, opts: { language?: string; prompt?: string } = {}): Promise<SttTranscribeResponse> {
      const form = new FormData();
      form.append('artifact_id', artifactId);
      if (opts.language) form.append('language', opts.language);
      if (opts.prompt) form.append('prompt', opts.prompt);
      const response = await fetchWithTimeout('/api/v1/stt/transcribe', {
        method: 'POST',
        body: form,
        credentials: 'include'
      }, { timeoutMs: DISABLE_API_REQUEST_TIMEOUT_MS });
      if (!response.ok) {
        let detail = 'Transcription failed';
        try {
          const body = await response.json();
          detail = body?.error?.message ?? detail;
        } catch {
          // ignore
        }
        throw new Error(detail);
      }
      return response.json();
    }
  },

  webConfig: {
    status(): Promise<WebConfigStatus> {
      return request<WebConfigStatus>('/api/v1/web-config/status');
    },

    updateBackend(backend: string, payload: WebBackendUpdatePayload): Promise<WebConfigStatus> {
      return request<WebConfigStatus>(`/api/v1/web-config/backends/${encodeURIComponent(backend)}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    },

    updateDefaults(payload: WebDefaultsUpdatePayload): Promise<WebConfigStatus> {
      return request<WebConfigStatus>('/api/v1/web-config/defaults', {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
    }
  },

  secrets: {
    list(): Promise<SecretMetadata[]> {
      return request<SecretMetadata[]>('/api/v1/secrets');
    },

    upsert(payload: Record<string, unknown>): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>('/api/v1/secrets', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    remove(name: string, scope = 'user', agentId: string | null = null): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/secrets/${name}${encodeQuery({ scope, agent_id: agentId })}`, {
        method: 'DELETE'
      });
    }
  },

  credentials: {
    list(): Promise<CredentialMetadata[]> {
      return request<CredentialMetadata[]>('/api/v1/credentials');
    },

    upsert(payload: Record<string, unknown>): Promise<CredentialMetadata> {
      return request<CredentialMetadata>('/api/v1/credentials', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    revoke(credentialId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/credentials/${encodeURIComponent(credentialId)}/revoke`, {
        method: 'POST'
      });
    },

    remove(credentialId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/credentials/${encodeURIComponent(credentialId)}`, {
        method: 'DELETE'
      });
    }
  },

  sessions: {
    detail(sessionId: string): Promise<Session> {
      return request<Session>(`/api/v1/sessions/${sessionId}`);
    },

    cancel(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
      return request<{ ok: boolean; session_id: string }>(`/api/v1/sessions/${sessionId}/cancel`, { method: 'POST' });
    },

    intarisDetail(sessionId: string): Promise<IntarisSessionDetail> {
      return request<IntarisSessionDetail>(`/api/v1/sessions/${sessionId}/intaris`);
    }
  },

  escalations: {
    list(sessionId?: string | null): Promise<Escalation[]> {
      return request<Escalation[]>(`/api/v1/escalations${encodeQuery({ session_id: sessionId })}`);
    },

    resolve(callId: string, payload: { decision: string; note?: string }): Promise<{ ok: boolean; call_id: string; decision: string }> {
      return request<{ ok: boolean; call_id: string; decision: string }>(`/api/v1/escalations/${callId}/resolve`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    }
  },

  notifications: {
    list(
      conversationId?: string | null,
      params: { taskId?: string | null; sessionId?: string | null } = {}
    ): Promise<Notification[]> {
      return request<Notification[]>(
        `/api/v1/notifications${encodeQuery({
          conversation_id: conversationId,
          task_id: params.taskId ?? null,
          session_id: params.sessionId ?? null,
        })}`
      );
    },

    get(notificationId: string): Promise<Notification> {
      return request<Notification>(`/api/v1/notifications/${encodeURIComponent(notificationId)}`);
    },

    resolve(notificationId: string, payload: { decision: string; note?: string; response?: string; feedback?: string; response_payload?: Record<string, unknown>; credential?: CredentialUpsertPayload }): Promise<{ ok: boolean; notification_id: string; decision: string }> {
      return request<{ ok: boolean; notification_id: string; decision: string }>(`/api/v1/notifications/${notificationId}/resolve`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    }
  },

  push: {
    vapidPublicKey(): Promise<VapidPublicKeyResponse> {
      return request<VapidPublicKeyResponse>('/api/v1/push/vapid-public-key');
    },

    subscribe(payload: PushSubscriptionPayload): Promise<PushSubscriptionResponse> {
      return request<PushSubscriptionResponse>('/api/v1/push/subscriptions', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    status(): Promise<PushSubscriptionStatusResponse> {
      return request<PushSubscriptionStatusResponse>('/api/v1/push/subscriptions/status');
    },

    test(): Promise<PushSubscriptionTestResponse> {
      return request<PushSubscriptionTestResponse>('/api/v1/push/subscriptions/test', {
        method: 'POST'
      });
    },

    unsubscribe(endpoint: string): Promise<{ ok: boolean; removed: boolean }> {
      return request<{ ok: boolean; removed: boolean }>('/api/v1/push/subscriptions/unsubscribe', {
        method: 'POST',
        body: JSON.stringify({ endpoint })
      });
    }
  }
};

export async function safeRequest<T>(run: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await run();
  } catch (error) {
    reportError('API request failed', error, { fallback: true });
    return fallback;
  }
}

export function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }

  return new ApiError(toErrorMessage(error), { status: 500 });
}
