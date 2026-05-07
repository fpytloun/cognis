import { apiUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import { auth } from '$lib/stores/auth';
import type {
  ApiKeyCreateResponse,
  Agent,
  AgentGrant,
  AttachmentRef,
  ChannelAccount,
  ChannelAccountStatus,
    ChannelContact,
    CredentialMetadata,
    CredentialUpsertPayload,
    ChannelMeta,
  ApiErrorResponse,
  ApiKeyMetadata,
  BootstrapStatusResponse,
  Conversation,
  ConversationFlatSearchResponse,
  ConversationSearchResponse,
  CursorPage,
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
  LLMProvider,
  LLMProviderOAuthStatus,
  ModelEntry,
  MCPServerConfigResponse,
  MCPServerCreateRequest,
  MCPServerTestResponse,
  MCPServer,
  MCPServerUpdateRequest,
  MessageHistoryResponse,
  QueuedMessage,
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
  Schedule,
  ScheduleRun,
    SecretMetadata,
  Session,
  SessionEventsResponse,
  Setting,
  SettingsCategory,
  Skill,
  SkillCreate,
  SkillDecompositionPreview,
  SkillExportResponse,
  SkillImportRequest,
  SkillUpdate,
  SkillVersion,
  SystemDiagnostics,
  StepRun,
  StepProfileDefinition,
  SttTranscribeResponse,
  Task,
  TaskChatResponse,
  TaskComment,
  TaskDetail,
  TaskRerunResponse,
  ToolDefinitionSummary,
  TtsSynthesizeRequest,
  TtsSynthesizeResponse,
  UserCreatePayload,
  UserDetail,
  UserSummary,
  UserUpdatePayload,
  VapidPublicKeyResponse,
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
};

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
  const { auth: requiresAuth = true, headers, body, ...rest } = options;
  const nextHeaders = new Headers(headers ?? {});

  if (body && !nextHeaders.has('Content-Type') && !(body instanceof FormData)) {
    nextHeaders.set('Content-Type', 'application/json');
  }

  const response = await fetch(apiUrl(path), {
    ...rest,
    credentials: 'include',
    headers: nextHeaders,
    body
  });

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

function encodeQuery(params: Record<string, string | number | boolean | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue;
    }
    query.set(key, String(value));
  }

  const serialized = query.toString();
  return serialized ? `?${serialized}` : '';
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
        project_id?: string | null;
        status?: 'active' | 'archived' | 'all';
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
      return request<QueuedMessagesResponse>(`/api/v1/conversations/${conversationId}/queue`);
    },

    updateQueuedMessage(conversationId: string, queueId: string, content: string): Promise<QueuedMessage> {
      return request<QueuedMessage>(`/api/v1/conversations/${conversationId}/queue/${queueId}`, {
        method: 'PATCH',
        body: JSON.stringify({ content })
      });
    },

    deleteQueuedMessage(conversationId: string, queueId: string): Promise<void> {
      return request<void>(`/api/v1/conversations/${conversationId}/queue/${queueId}`, {
        method: 'DELETE'
      });
    },

    list(
      cursor: string | null = null,
      filters: { contextType?: string | null; agentId?: string | null; status?: string | null } = {}
    ): Promise<CursorPage<Conversation>> {
      return request<CursorPage<Conversation>>(
        `/api/v1/conversations${encodeQuery({
          cursor,
          limit: 50,
          context_type: filters.contextType,
          agent_id: filters.agentId,
          status: filters.status
        })}`
      );
    },

    async listAll(
      filters: { contextType?: string | null; agentId?: string | null; status?: string | null } = {}
    ): Promise<Conversation[]> {
      return collectCursorPages((cursor) => this.list(cursor, filters));
    },

    create(payload: { agent_id: string; title?: string | null; context?: Record<string, unknown> }): Promise<Conversation> {
      return request<Conversation>('/api/v1/conversations', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    detail(conversationId: string): Promise<Conversation> {
      return request<Conversation>(`/api/v1/conversations/${conversationId}`);
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

    messages(conversationId: string, afterSeq = 0, limit = 200): Promise<MessageHistoryResponse> {
      return request<MessageHistoryResponse>(
        `/api/v1/conversations/${conversationId}/messages${encodeQuery({ after_seq: afterSeq, limit })}`
      );
    },

    sessions(conversationId: string): Promise<Session[]> {
      return request<Session[]>(`/api/v1/conversations/${conversationId}/sessions`);
    },

    delegations(conversationId: string): Promise<Session[]> {
      return request<Session[]>(`/api/v1/conversations/${conversationId}/delegations`);
    },

    resolve(payload: { agent_id: string; context_type?: string }): Promise<Conversation> {
      return request<Conversation>('/api/v1/conversations/resolve', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    sessionEvents(conversationId: string, sessionId: string, afterSeq = 0, limit = 50): Promise<SessionEventsResponse> {
      return request<SessionEventsResponse>(
        `/api/v1/conversations/${conversationId}/sessions/${sessionId}/events${encodeQuery({ after_seq: afterSeq, limit })}`
      );
    },

    markRead(conversationId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/conversations/${conversationId}/read`, {
        method: 'POST'
      });
    }
  },

  agents: {
    list(
      cursor: string | null = null,
      params?: { agent_type?: string; include_hidden?: boolean; include_system?: boolean; include_disabled?: boolean }
    ): Promise<CursorPage<Agent>> {
      return request<CursorPage<Agent>>(
        `/api/v1/agents${encodeQuery({ cursor, limit: 100, ...params })}`
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
        body: formData
      });
    },

    generate(prompt: string, options?: { size?: string; quality?: string }): Promise<{ image_id: string; url: string; prompt_used: string }> {
      return request<{ image_id: string; url: string; prompt_used: string }>('/api/v1/images/generate', {
        method: 'POST',
        body: JSON.stringify({ prompt, ...options })
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
        body: form
      });
    },

    signedUrl(artifactId: string, ttlSeconds = 3600): Promise<{ artifact_id: string; url: string; expires_at: string | null }> {
      return request<{ artifact_id: string; url: string; expires_at: string | null }>(`/api/v1/artifacts/${artifactId}/signed-url${encodeQuery({ ttl_seconds: ttlSeconds })}`);
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

    list(): Promise<ExecutorConfig[]> {
      return request<ExecutorConfig[]>('/api/v1/executors');
    },

    get(executorId: string): Promise<ExecutorConfig> {
      return request<ExecutorConfig>(`/api/v1/executors/${executorId}`);
    },

    create(data: ExecutorCreateRequest): Promise<ExecutorConfig> {
      return request<ExecutorConfig>('/api/v1/executors', { method: 'POST', body: JSON.stringify(data) });
    },

    update(executorId: string, data: ExecutorUpdateRequest): Promise<ExecutorConfig> {
      return request<ExecutorConfig>(`/api/v1/executors/${executorId}`, { method: 'PUT', body: JSON.stringify(data) });
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

  skills: {
    list(): Promise<Skill[]> {
      return request<Skill[]>('/api/v1/skills');
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
      return request<CursorPage<Task>>(`/api/v1/tasks${encodeQuery({ ...params, limit: 100 })}`);
    },

    async listAll(): Promise<Task[]> {
      return collectCursorPages((cursor) => this.list({ cursor }));
    },

    detail(taskId: string): Promise<TaskDetail> {
      return request<TaskDetail>(`/api/v1/tasks/${taskId}`);
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

    stepResponse(taskId: string, payload: Record<string, unknown>): Promise<{ ok: boolean; task_id: string; status: string }> {
      return request<{ ok: boolean; task_id: string; status: string }>(`/api/v1/tasks/${taskId}/step-response`, {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    },

    steps(taskId: string): Promise<StepRun[]> {
      return request<StepRun[]>(`/api/v1/tasks/${taskId}/steps`);
    },

    stepHistory(taskId: string, stepName: string): Promise<StepRun[]> {
      return request<StepRun[]>(`/api/v1/tasks/${taskId}/steps/${encodeURIComponent(stepName)}/history`);
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
      return request<Project[]>(`/api/v1/projects${encodeQuery(params)}`);
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
      return request<CursorPage<Workflow>>(`/api/v1/workflows${encodeQuery({ cursor, limit: 100, ...params })}`);
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

    trigger(scheduleId: string): Promise<Schedule> {
      return request<Schedule>(`/api/v1/schedules/${scheduleId}/trigger`, { method: 'POST' });
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
      preset: string;
      base_url: string;
      api_key?: string;
      secret_name?: string;
      env_var?: string;
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

    clearChatgptOAuth(providerId: string): Promise<{ ok: boolean }> {
      return request<{ ok: boolean }>(`/api/v1/llm-providers/${providerId}/oauth/chatgpt`, {
        method: 'DELETE'
      });
    }
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
        signal: opts.signal
      });
    }
  },

  stt: {
    async transcribe(file: Blob, opts: { filename?: string; language?: string; prompt?: string } = {}): Promise<SttTranscribeResponse> {
      const form = new FormData();
      form.append('file', file, opts.filename ?? 'voice-input.webm');
      if (opts.language) form.append('language', opts.language);
      if (opts.prompt) form.append('prompt', opts.prompt);
      const response = await fetch('/api/v1/stt/transcribe', {
        method: 'POST',
        body: form,
        credentials: 'include'
      });
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
      const response = await fetch('/api/v1/stt/transcribe', {
        method: 'POST',
        body: form,
        credentials: 'include'
      });
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

    events(sessionId: string, afterSeq = 0, limit = 50): Promise<SessionEventsResponse> {
      return request<SessionEventsResponse>(`/api/v1/sessions/${sessionId}/events${encodeQuery({ after_seq: afterSeq, limit })}`);
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
