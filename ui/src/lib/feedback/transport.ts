type FeedbackTransport = <T>(path: string, options?: RequestInit) => Promise<T>;

async function sameOriginRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options,
    headers: { ...(typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = new Error(typeof payload?.detail === 'string' ? payload.detail : `Feedback request failed (${response.status}).`);
    throw Object.assign(error, { status: response.status });
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
let transport: FeedbackTransport = sameOriginRequest;
/** The SPA installs its canonical auth/retry boundary; standalone uses cookies. */
export function installFeedbackTransport(value: FeedbackTransport): void { transport = value; }
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const result = await transport<T>(path, options);
  if (options?.method === 'POST' && /\/(threads(?:\/[^/]+\/(?:resolution|moderation))?|submissions)$/.test(path)
    && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('feedback-changed'));
  }
  return result;
}
