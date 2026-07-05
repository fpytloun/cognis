import { apiUrl } from '$lib/config';
import type {
  CancelTurnV2Response,
  ChatSnapshot,
  ChatSyncResponse,
  ControlMutationV2Request,
  QueueMutationResponse,
  QueueUpdateV2Request,
  SendMessageV2Request,
  SendMessageV2Response,
  TimelineBackfillResponse
} from './types';

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

type RequestOptions = RequestInit & {
  fetchImpl?: typeof fetch;
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

async function readError(response: Response): Promise<ChatV2ApiError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
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
  const { fetchImpl = fetch, headers, body, ...rest } = options;
  const nextHeaders = new Headers(headers ?? {});

  if (body && !nextHeaders.has('Content-Type') && !(body instanceof FormData)) {
    nextHeaders.set('Content-Type', 'application/json');
  }

  const response = await fetchImpl(apiUrl(path), {
    ...rest,
    credentials: 'include',
    headers: nextHeaders,
    body
  });

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

export class ChatV2ApiClient {
  private readonly fetchImpl: typeof fetch;

  constructor(options: ChatV2ApiClientOptions = {}) {
    this.fetchImpl = options.fetch ?? fetch;
  }

  snapshot(conversationId: string): Promise<ChatSnapshot> {
    return request<ChatSnapshot>(`/api/v1/chat/v2/conversations/${conversationId}/snapshot`, {
      fetchImpl: this.fetchImpl
    });
  }

  sync(
    conversationId: string,
    cursor: string,
    options: { limit?: number } = {}
  ): Promise<ChatSyncResponse> {
    return request<ChatSyncResponse>(
      `/api/v1/chat/v2/conversations/${conversationId}/sync${encodeQuery({
        cursor,
        limit: options.limit
      })}`,
      { fetchImpl: this.fetchImpl }
    );
  }

  timeline(
    conversationId: string,
    options: { before?: string | null; limit?: number } = {}
  ): Promise<TimelineBackfillResponse> {
    return request<TimelineBackfillResponse>(
      `/api/v1/chat/v2/conversations/${conversationId}/timeline${encodeQuery({
        before: options.before ?? null,
        limit: options.limit
      })}`,
      { fetchImpl: this.fetchImpl }
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
