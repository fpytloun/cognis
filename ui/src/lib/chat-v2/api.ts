import { apiUrl } from '$lib/config';
import { fetchWithTimeout, withRequestDeadline, readJsonWithSignal, cancelResponseOnAbort, isFetchTimeoutError, isReplayableBody } from '$lib/api/fetch';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { reportRequestFailure, reportRequestSuccess } from '$lib/server-reachability';
import type {
  BackgroundToolCallV2Response,
  CancelTurnV2Response,
  ChatSnapshot,
  ChatSyncResponse,
  CommandV2Response,
  ControlMutationV2Request,
  ForkAssistantMessageV2Response,
  QueueMutationResponse,
  QueueUpdateV2Request,
  RetryTurnV2Response,
  RetryProviderOptionsResponse,
  SendMessageV2Request,
  SendMessageV2Response,
  MessageAdmissionV2Response,
  TimelineBackfillResponse,
  WorkProjectionResponse,
  ActivityOverviewResponse,
  WorkActivityListResponse
} from './types';
import type { TimelineScope } from './types';

export class ChatV2ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown> | null;

  constructor(message: string, options: { code?: string; status: number; details?: Record<string, unknown> | null }) {
    super(message);
    this.name = 'ChatV2ApiError';
    this.code = options.code ?? 'request_error';
    this.status = options.status;
    this.details = options.details ?? null;
  }
}

export interface ChatV2ApiClientOptions {
  fetch?: typeof fetch;
}

export interface ChatV2TimelineRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

export type ClientPerformanceMetric =
  | 'cached_restore_ms'
  | 'timeline_fresh_ms'
  | 'activity_overview_cache_fresh_ms'
  | 'activity_overview_cache_stale_ms'
  | 'activity_overview_cache_miss_ms'
  | 'activity_overview_request_success_ms'
  | 'activity_overview_request_error_ms'
  | 'activity_overview_request_aborted_ms'
  | 'activity_overview_request_deduplicated_ms';

type RequestOptions = RequestInit & {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
};

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function readError(response: Response, signal?: AbortSignal | null): Promise<ChatV2ApiError> {
  let payload: unknown = null;
  try {
    payload = await readJsonWithSignal(response, signal);
  } catch {
    payload = null;
  }

  if (isRecord(payload)) {
    const error = payload.error;
    if (isRecord(error)) {
      return new ChatV2ApiError(String(error.message ?? 'Request failed'), {
        code: typeof error.code === 'string' ? error.code : undefined,
        details: isRecord(error.details) ? error.details : null,
        status: response.status
      });
    }
    const detail = payload.detail;
    if (typeof detail === 'string') {
      return new ChatV2ApiError(detail, { status: response.status });
    }
    if (isRecord(detail)) {
      return new ChatV2ApiError(String(detail.message ?? 'Request failed'), {
        code: typeof detail.code === 'string' ? detail.code : undefined,
        details: isRecord(detail.details) ? detail.details : null,
        status: response.status
      });
    }
  }

  return new ChatV2ApiError(`Request failed with status ${response.status}`, { status: response.status });
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const lease = documentCoordination().capture();
  const signal = options.signal ? AbortSignal.any([options.signal, lease.signal]) : lease.signal;
  try {
    return await withRequestDeadline(
      (operationSignal) => requestWithinDeadline<T>(path, { ...options, signal: operationSignal, timeoutMs: 0 }, lease),
      { signal, timeoutMs: options.timeoutMs },
    );
  } catch (error) {
    const normalized = isFetchTimeoutError(error)
      ? new ChatV2ApiError(error.message, { code: 'request_timeout', status: 0 })
      : error;
    reportRequestFailure(normalized);
    throw normalized;
  }
}

async function requestWithinDeadline<T>(
  path: string,
  options: RequestOptions,
  lease: ReturnType<ReturnType<typeof documentCoordination>['capture']>,
): Promise<T> {
  const { fetchImpl = fetch, headers, body, timeoutMs, ...rest } = options;
  const signal = rest.signal;
  const responseOwners: Array<() => void> = [];
  try {
  const nextHeaders = new Headers(headers ?? {});

  if (body && !nextHeaders.has('Content-Type') && !(body instanceof FormData)) {
    nextHeaders.set('Content-Type', 'application/json');
  }

  const send = async (): Promise<Response> => {
    try {
      await lease.validate();
      signal?.throwIfAborted();
      const response = await fetchWithTimeout(apiUrl(path), {
        ...rest,
        signal,
        credentials: 'include',
        headers: nextHeaders,
        body
      }, { fetchImpl, timeoutMs });
      responseOwners.push(cancelResponseOnAbort(response, signal));
      await lease.validate();
      // Feed the global connectivity status (see $lib/server-reachability).
      // Gateway/unavailable responses are reported once parsed (below), so
      // critical application 503s (event_store_*) are not mistaken for an
      // unreachable server.
      if (response.status !== 502 && response.status !== 503 && response.status !== 504) {
        reportRequestSuccess();
      }
      return response;
    } catch (error) {
      const normalized = isFetchTimeoutError(error)
        ? new ChatV2ApiError(error.message, { code: 'request_timeout', status: 0 })
        : error;
      throw normalized;
    }
  };

  let response = await send();

  // A 401 is rejected before the request is handled. After one shared
  // session refresh (mobile resume bursts), retry once with the same
  // idempotency identifiers.
  if (response.status === 401 && !rest.signal?.aborted && isReplayableBody(body)) {
    if ((await auth.recoverSession()) === 'valid') {
      signal?.throwIfAborted();
      await lease.validate();
      response = await send();
    }
  }

  if (!response.ok) {
    const error = await readError(response, signal);
    throw error;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    return undefined as T;
  }

  const result = (await readJsonWithSignal(response, signal)) as T;
  await lease.validate();
  signal?.throwIfAborted();
  return result;
  } finally {
    for (const release of responseOwners) release();
  }
}

export class ChatV2ApiClient {
  private readonly fetchImpl: typeof fetch;

  constructor(options: ChatV2ApiClientOptions = {}) {
    this.fetchImpl = options.fetch ?? fetch;
  }

  private scopePath(scope: TimelineScope | string, operation: 'snapshot' | 'sync' | 'timeline' | 'work' | 'activity-overview' | `tool-outputs/${string}`): string {
    const resolved = typeof scope === 'string'
      ? { kind: 'conversation' as const, conversation_id: scope }
      : scope;
    if (resolved.kind === 'session' && resolved.session_id) {
      return `/api/v1/chat/v2/sessions/${encodeURIComponent(resolved.session_id)}/${operation}`;
    }
    if (resolved.kind === 'task_step' && resolved.step_run_id) {
      return `/api/v1/chat/v2/task-steps/${encodeURIComponent(resolved.step_run_id)}/${operation}`;
    }
    if (resolved.conversation_id) {
      return `/api/v1/chat/v2/conversations/${encodeURIComponent(resolved.conversation_id)}/${operation}`;
    }
    throw new Error('Chat v2 scope has no addressable identifier');
  }

  toolOutputPage(
    scope: TimelineScope | string,
    callId: string,
    options: { offset?: number; limit?: number; latest?: boolean; signal?: AbortSignal } = {}
  ): Promise<import('$lib/types/api').ToolOutputPageResponse> {
    return request(
      `${this.scopePath(scope, `tool-outputs/${encodeURIComponent(callId)}`)}${encodeQuery({
        offset: options.offset,
        limit: options.limit,
        latest: options.latest,
      })}`,
      { fetchImpl: this.fetchImpl, signal: options.signal }
    );
  }

  snapshot(
    scope: TimelineScope | string,
    options: ChatV2TimelineRequestOptions = {}
  ): Promise<ChatSnapshot> {
    return request<ChatSnapshot>(this.scopePath(scope, 'snapshot'), {
      fetchImpl: this.fetchImpl,
      signal: options.signal,
      timeoutMs: options.timeoutMs
    });
  }

  async snapshotCacheOnly(conversationId: string): Promise<ChatSnapshot | null> {
    try {
      const snapshot = await request<ChatSnapshot | undefined>(
        `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/snapshot/cache-only`,
        {
          fetchImpl: this.fetchImpl,
          timeoutMs: 3_000
        }
      );
      return snapshot ?? null;
    } catch (error) {
      if (error instanceof ChatV2ApiError && (error.status === 401 || error.status === 403)) {
        throw error;
      }
      return null;
    }
  }

  async clientPerformance(
    metric: ClientPerformanceMetric,
    durationMs: number
  ): Promise<void> {
    try {
      await request<void>('/api/v1/chat/v2/client-performance', {
        fetchImpl: this.fetchImpl,
        method: 'POST',
        keepalive: true,
        body: JSON.stringify({ metric, duration_ms: durationMs })
      });
    } catch {
      // Observability must never affect chat navigation or rendering.
    }
  }

  work(
    scope: TimelineScope | string,
    options: {
      before?: string;
      limit?: number;
      category?: import('./types').WorkCategory;
      from?: string;
      to?: string;
      sessionId?: string;
      detail?: 'lightweight' | 'full';
      signal?: AbortSignal;
    } = {}
  ): Promise<WorkProjectionResponse> {
    return request<WorkProjectionResponse>(
      `${this.scopePath(scope, 'work')}${encodeQuery({
        before: options.before,
        limit: options.limit,
        category: options.category,
        from: options.from,
        to: options.to,
        session_id: options.sessionId,
        detail: options.detail,
      })}`,
      {
        fetchImpl: this.fetchImpl,
        signal: options.signal
      }
    );
  }

  refreshWork(
    scope: TimelineScope,
    options: { signal?: AbortSignal } = {}
  ): Promise<import('./types').WorkRefreshResponse> {
    return request<import('./types').WorkRefreshResponse>('/api/v1/work/refresh', {
      fetchImpl: this.fetchImpl,
      method: 'POST',
      signal: options.signal,
      body: JSON.stringify({ scope })
    });
  }

  fileHistory(
    payload: import('./types').WorkFileHistoryRequest,
    options: { signal?: AbortSignal } = {}
  ): Promise<import('./types').WorkFileHistoryResponse> {
    return request<import('./types').WorkFileHistoryResponse>('/api/v1/work/file-history', {
      fetchImpl: this.fetchImpl,
      method: 'POST',
      signal: options.signal,
      body: JSON.stringify(payload)
    });
  }

  workActivities(
    options: { limit?: number; cursor?: string | null; signal?: AbortSignal } = {}
  ): Promise<WorkActivityListResponse> {
    return request<WorkActivityListResponse>(
      `/api/v1/work/activities${encodeQuery({
        limit: options.limit ?? 20,
        cursor: options.cursor,
      })}`,
      {
        fetchImpl: this.fetchImpl,
        signal: options.signal,
      }
    );
  }

  activityOverview(
    scope: TimelineScope | string,
    options: { detail?: 'lightweight' | 'full'; signal?: AbortSignal } = {}
  ): Promise<ActivityOverviewResponse> {
    return request<ActivityOverviewResponse>(
      `${this.scopePath(scope, 'activity-overview')}${encodeQuery({ detail: options.detail })}`,
      {
      fetchImpl: this.fetchImpl,
      signal: options.signal
      }
    );
  }

  sync(
    scope: TimelineScope | string,
    cursor: string,
    options: { limit?: number } & ChatV2TimelineRequestOptions = {}
  ): Promise<ChatSyncResponse> {
    return request<ChatSyncResponse>(
      `${this.scopePath(scope, 'sync')}${encodeQuery({
        cursor,
        limit: options.limit
      })}`,
      {
        fetchImpl: this.fetchImpl,
        signal: options.signal,
        timeoutMs: options.timeoutMs
      }
    );
  }

  timeline(
    scope: TimelineScope | string,
    options: { before?: string | null; limit?: number } & ChatV2TimelineRequestOptions = {}
  ): Promise<TimelineBackfillResponse> {
    return request<TimelineBackfillResponse>(
      `${this.scopePath(scope, 'timeline')}${encodeQuery({
        before: options.before ?? null,
        limit: options.limit
      })}`,
      {
        fetchImpl: this.fetchImpl,
        signal: options.signal,
        timeoutMs: options.timeoutMs
      }
    );
  }

  messageAdmission(
    conversationId: string, clientTxnId: string, options: ChatV2TimelineRequestOptions = {},
    canonicalClientMessageIds: readonly string[] = [],
  ): Promise<MessageAdmissionV2Response> {
    const query = new URLSearchParams(canonicalClientMessageIds.slice(0, 100)
      .map(id => ['canonical_client_message_id', id]));
    return request<MessageAdmissionV2Response>(
      `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(clientTxnId)}/admission${query.size ? `?${query}` : ''}`,
      { fetchImpl: this.fetchImpl, ...options },
    );
  }

  messageAdmissionByClient(
    conversationId: string, clientMessageId: string, options: ChatV2TimelineRequestOptions = {},
    canonicalClientMessageIds: readonly string[] = [],
  ): Promise<MessageAdmissionV2Response> {
    const query = new URLSearchParams(canonicalClientMessageIds.slice(0, 100)
      .map(id => ['canonical_client_message_id', id]));
    return request<MessageAdmissionV2Response>(
      `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/message-admissions/${encodeURIComponent(clientMessageId)}${query.size ? `?${query}` : ''}`,
      { fetchImpl: this.fetchImpl, ...options },
    );
  }

  sendMessage(
    conversationId: string,
    clientTxnId: string,
    payload: SendMessageV2Request
  ): Promise<SendMessageV2Response> {
    return request<SendMessageV2Response>(
      `/api/v1/chat/v2/conversations/${conversationId}/messages/${clientTxnId}`,
      {
        fetchImpl: this.fetchImpl,
        method: 'PUT',
        body: JSON.stringify(payload)
      }
    );
  }

  retryProviderOptions(
    conversationId: string,
    options: ChatV2TimelineRequestOptions & {
      turnId?: string; providerId?: string | null; model?: string | null;
    } = {}
  ): Promise<RetryProviderOptionsResponse> {
    const query = new URLSearchParams();
    if (options.turnId) query.set('turn_id', options.turnId);
    if (options.providerId) query.set('provider_id', options.providerId);
    if (options.model) query.set('model', options.model);
    return request<RetryProviderOptionsResponse>(
      `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/retry-provider-options?${query}`,
      { fetchImpl: this.fetchImpl, signal: options.signal }
    );
  }

  executeCommand(
    conversationId: string,
    clientTxnId: string,
    content: string
  ): Promise<CommandV2Response> {
    return request<CommandV2Response>(
      `/api/v1/chat/v2/conversations/${conversationId}/commands/${clientTxnId}`,
      {
        fetchImpl: this.fetchImpl,
        method: 'PUT',
        timeoutMs: 600_000,
        body: JSON.stringify({ content })
      }
    );
  }

  forkAssistantMessage(
    conversationId: string,
    payload: { source_session_id: string; source_seq: number }
  ): Promise<ForkAssistantMessageV2Response> {
    return request<ForkAssistantMessageV2Response>(
      `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/assistant-messages/fork`,
      {
        fetchImpl: this.fetchImpl,
        method: 'POST',
        body: JSON.stringify(payload)
      }
    );
  }

  cancelTurn(
    conversationId: string,
    payload: ControlMutationV2Request
  ): Promise<CancelTurnV2Response> {
    return request<CancelTurnV2Response>(`/api/v1/chat/v2/conversations/${conversationId}/cancel`, {
      fetchImpl: this.fetchImpl,
      method: 'POST',
      body: JSON.stringify(payload)
    });
  }

  /** Ask the executor to continue a running foreground tool call as a background job. */
  backgroundToolCall(conversationId: string, callId: string): Promise<BackgroundToolCallV2Response> {
    return request<BackgroundToolCallV2Response>(
      `/api/v1/chat/v2/conversations/${encodeURIComponent(conversationId)}/tool-calls/${encodeURIComponent(callId)}/background`,
      {
        fetchImpl: this.fetchImpl,
        method: 'POST'
      }
    );
  }

  retryTurn(
    conversationId: string,
    turnId: string,
    payload: ControlMutationV2Request
  ): Promise<RetryTurnV2Response> {
    return request<RetryTurnV2Response>(
      `/api/v1/chat/v2/conversations/${conversationId}/turns/${turnId}/retry`,
      {
        fetchImpl: this.fetchImpl,
        method: 'POST',
        body: JSON.stringify(payload)
      }
    );
  }

  deleteQueuedMessage(
    conversationId: string,
    queueId: string,
    payload: ControlMutationV2Request
  ): Promise<QueueMutationResponse> {
    return request<QueueMutationResponse>(
      `/api/v1/chat/v2/conversations/${conversationId}/queue/${queueId}${encodeQuery({
        client_txn_id: payload.client_txn_id
      })}`,
      {
        fetchImpl: this.fetchImpl,
        method: 'DELETE'
      }
    );
  }

  updateQueuedMessage(
    conversationId: string,
    queueId: string,
    payload: QueueUpdateV2Request
  ): Promise<QueueMutationResponse> {
    return request<QueueMutationResponse>(
      `/api/v1/chat/v2/conversations/${conversationId}/queue/${queueId}`,
      {
        fetchImpl: this.fetchImpl,
        method: 'PATCH',
        body: JSON.stringify(payload)
      }
    );
  }
}

export const chatV2Api = new ChatV2ApiClient();
