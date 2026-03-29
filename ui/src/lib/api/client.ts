import { apiUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import { auth } from '$lib/stores/auth';
import type {
  ApiKeyCreateResponse,
  Agent,
  ApiErrorResponse,
  ApiKeyMetadata,
  BootstrapStatusResponse,
  Conversation,
  CursorPage,
  Escalation,
  ExchangeTokenResponse,
  HealthResponse,
  LLMProvider,
  MCPServerTestResponse,
  MCPServer,
  MessageHistoryResponse,
  ModelRouting,
  ProviderTestResult,
  SecretMetadata,
  Session,
  SessionEventsResponse,
  Setting,
  SettingsCategory,
  SystemDiagnostics,
  StepRun,
  Task,
  TaskDetail,
  ToolDefinitionSummary,
  UserSummary,
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
  retryOnUnauthorized?: boolean;
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
  const { auth: requiresAuth = true, retryOnUnauthorized = true, headers, body, ...rest } = options;
  const nextHeaders = new Headers(headers ?? {});

  if (body && !nextHeaders.has('Content-Type') && !(body instanceof FormData)) {
    nextHeaders.set('Content-Type', 'application/json');
  }

  if (requiresAuth) {
    const token = auth.getAccessToken();
    if (!token) {
      throw new ApiError('Authentication required', { code: 'unauthorized', status: 401 });
    }

    nextHeaders.set('Authorization', `Bearer ${token}`);
  }

  const response = await fetch(apiUrl(path), {
    ...rest,
    headers: nextHeaders,
    body
  });

  if (response.status === 401 && requiresAuth && retryOnUnauthorized) {
    const refreshedToken = await auth.refreshSession();
    if (!refreshedToken) {
      throw new ApiError('Your session has expired. Please log in again.', {
        code: 'unauthorized',
        status: 401
      });
    }

    return request<T>(path, { ...options, retryOnUnauthorized: false });
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

  conversations: {
    list(cursor: string | null = null): Promise<CursorPage<Conversation>> {
      return request<CursorPage<Conversation>>(`/api/v1/conversations${encodeQuery({ cursor, limit: 50 })}`);
    },

    async listAll(): Promise<Conversation[]> {
      return collectCursorPages((cursor) => this.list(cursor));
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
    }
  },

  agents: {
    list(cursor: string | null = null): Promise<CursorPage<Agent>> {
      return request<CursorPage<Agent>>(`/api/v1/agents${encodeQuery({ cursor, limit: 100 })}`);
    },

    async listAll(): Promise<Agent[]> {
      return collectCursorPages((cursor) => this.list(cursor));
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
    }
  },

  tools: {
    list(): Promise<ToolDefinitionSummary[]> {
      return request<ToolDefinitionSummary[]>('/api/v1/tools');
    },

    mcpServers(): Promise<MCPServer[]> {
      return request<MCPServer[]>('/api/v1/mcp/servers');
    },

    testAgentMcp(agentId: string): Promise<MCPServerTestResponse> {
      return request<MCPServerTestResponse>(`/api/v1/agents/${agentId}/mcp/test`, {
        method: 'POST'
      });
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

  workflows: {
    list(cursor: string | null = null): Promise<CursorPage<Workflow>> {
      return request<CursorPage<Workflow>>(`/api/v1/workflows${encodeQuery({ cursor, limit: 100 })}`);
    },

    async listAll(): Promise<Workflow[]> {
      return collectCursorPages((cursor) => this.list(cursor));
    },

    detail(workflowId: string): Promise<Workflow> {
      return request<Workflow>(`/api/v1/workflows/${workflowId}`);
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

    discoverModels(providerId: string): Promise<{ provider_id: string; models: Array<{ model_id: string; name: string }> }> {
      return request<{ provider_id: string; models: Array<{ model_id: string; name: string }> }>(`/api/v1/llm-providers/${providerId}/discover-models`, {
        method: 'POST'
      });
    },

    discoverModelsPreview(payload: {
      preset: string;
      base_url: string;
      api_key?: string;
      secret_name?: string;
    }): Promise<{ models: Array<{ model_id: string; name: string }> }> {
      return request<{ models: Array<{ model_id: string; name: string }> }>('/api/v1/llm-providers/discover-models-preview', {
        method: 'POST',
        body: JSON.stringify(payload)
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

  sessions: {
    detail(sessionId: string): Promise<Session> {
      return request<Session>(`/api/v1/sessions/${sessionId}`);
    },

    events(sessionId: string, afterSeq = 0, limit = 50): Promise<SessionEventsResponse> {
      return request<SessionEventsResponse>(`/api/v1/sessions/${sessionId}/events${encodeQuery({ after_seq: afterSeq, limit })}`);
    },

    cancel(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
      return request<{ ok: boolean; session_id: string }>(`/api/v1/sessions/${sessionId}/cancel`, { method: 'POST' });
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
