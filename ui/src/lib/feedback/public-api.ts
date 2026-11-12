import type { CanonicalFeedbackText, FeedbackAnchor, FeedbackEntryVersion, FeedbackLocator, FeedbackSourceRef, FeedbackTextRequest, FeedbackThread, FeedbackThreadPage, ObservedCaptureCandidate, ResolvedFeedbackSource, ScopedCanonicalFeedbackText } from './types';
import type { DocumentAudioClient } from '$lib/document-audio';

export interface PublicGuestSession { guest_session_id: string; guest_capability: string; expires_at: string }
export interface PublicFeedbackExchange {
  link_mode?: 'view' | 'download';
  share_id: string; capability: string; expires_at: string;
  source: { source_token: string; content_digest: string; content_ref: string; format: string; title: string | null; canonical_text: CanonicalFeedbackText | null };
  allow_comments: boolean; discussion: 'own_only' | 'shared'; permitted_locator_kinds: FeedbackLocator['kind'][];
  guest_session?: PublicGuestSession | null; viewer_path?: string;
}
/** Local viewer state also supports a pre-minted standalone capability. */
export interface PublicFeedbackAccess extends Omit<PublicFeedbackExchange, 'expires_at' | 'discussion'> {
  expires_at: string | null; discussion: 'own_only' | 'shared' | 'unknown';
}
export interface PublicFeedbackContent {
  format: string; mime_type: string; filename: string; content: string | null;
  rich_payload: Record<string, unknown> | null; bytes_path: '/api/v1/public/feedback/source/bytes' | null;
  theme_family?: string;
}
export class PublicFeedbackClient {
  private capability: string | null = null;
  private expiresAt: string | null = null;
  private lifetime = new AbortController();
  guest: PublicGuestSession | null = null;
  constructor(private readonly fetcher: typeof fetch = fetch) {}
  private async response(path: string, options: RequestInit = {}, exchange = false): Promise<Response> {
    const headers = new Headers(options.headers);
    if (typeof options.body === 'string') headers.set('Content-Type', 'application/json');
    if (!exchange) {
      if (!this.capability || (this.expiresAt && Date.parse(this.expiresAt) <= Date.now())) throw new Error('This feedback link has expired. Ask the owner for a new link.');
      headers.set('X-Cognis-Feedback-Capability', this.capability);
      if (this.guest && Date.parse(this.guest.expires_at) > Date.now()) headers.set('X-Cognis-Feedback-Guest', this.guest.guest_capability);
    }
    const response = await this.fetcher(`/api/v1/public/feedback/${path}`, {
      ...options, headers, signal: this.lifetime.signal, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const message = typeof payload?.detail?.message === 'string' ? payload.detail.message
        : typeof payload?.detail === 'string' ? payload.detail : `Public feedback unavailable (${response.status}).`;
      throw Object.assign(new Error(message), { status: response.status });
    }
    return response;
  }
  private async request<T>(path: string, options: RequestInit = {}, exchange = false): Promise<T> {
    const response = await this.response(path, options, exchange);
    if (response.status === 204) return undefined as T;
    return response.json();
  }
  resume(access: PublicFeedbackAccess, guest: PublicGuestSession | null = null): void {
    this.capability = access.capability; this.expiresAt = access.expires_at; this.guest = guest;
  }
  async open(share_token: string): Promise<PublicFeedbackExchange> {
    const value = await this.request<PublicFeedbackExchange>('exchange', { method: 'POST', body: JSON.stringify({ share_token }) }, true);
    this.resume(value, value.guest_session ?? null);
    return value;
  }
  async adoptCapability(capability: string, source: Extract<FeedbackSourceRef, { kind: 'document' }>): Promise<PublicFeedbackAccess> {
    this.capability = capability; this.expiresAt = null;
    const resolved = await this.resolve(source);
    if (resolved.source.kind !== 'document') throw new Error('Invalid public source.');
    return { capability, share_id: resolved.subject_id, expires_at: null, source: {
      source_token: resolved.source_token, content_digest: resolved.content_digest, content_ref: resolved.source.content_ref,
      format: resolved.format, title: resolved.title, canonical_text: resolved.canonical_text,
    }, allow_comments: resolved.capabilities.comment, discussion: 'unknown', permitted_locator_kinds: resolved.permitted_locator_kinds };
  }
  resolve(source: Extract<FeedbackSourceRef, { kind: 'document' }>): Promise<ResolvedFeedbackSource> {
    return this.request('sources/resolve', { method: 'POST', body: JSON.stringify({ source, context: { kind: 'standalone' } }) });
  }
  content(): Promise<PublicFeedbackContent> { return this.request('source'); }
  async bytes(download = false): Promise<Blob> {
    const response = await this.response(`source/bytes${download ? '?download=true' : ''}`);
    return this.readBoundedBlob(response, 32 * 1024 * 1024);
  }
  async download(format: string): Promise<Blob> {
    return this.readBoundedBlob(await this.response(`source/download?${new URLSearchParams({ format })}`), 32 * 1024 * 1024);
  }
  audio(): DocumentAudioClient {
    return {
      context: () => this.request('audio/source'),
      estimates: () => this.request('audio/estimates'),
      create: payload => this.request('audio', { method: 'POST', body: JSON.stringify(payload) }),
      get: id => this.request(`audio/${encodeURIComponent(id)}`),
      cancel: id => this.request(`audio/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
    };
  }
  private async readBoundedBlob(response: Response, maximum: number): Promise<Blob> {
    if (Number(response.headers.get('Content-Length')) > maximum) {
      await response.body?.cancel(); throw new Error('This public preview exceeds its display limit.');
    }
    const reader = response.body?.getReader();
    if (!reader) return new Blob([], { type: response.headers.get('Content-Type') ?? '' });
    const parts: ArrayBuffer[] = [];
    let total = 0;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > maximum) { await reader.cancel(); throw new Error('This public preview exceeds its display limit.'); }
        parts.push(value.slice().buffer);
      }
    } finally { reader.releaseLock(); }
    return new Blob(parts, { type: response.headers.get('Content-Type') ?? '' });
  }
  async capturePixels(captureId: string): Promise<Blob> {
    return this.readBoundedBlob(await this.response(`captures/${encodeURIComponent(captureId)}/pixels`), 8 * 1024 * 1024);
  }
  async join(display_name: string | null): Promise<PublicGuestSession> {
    this.guest = await this.request<PublicGuestSession>('guest-sessions', { method: 'POST', body: JSON.stringify({ display_name }) });
    return this.guest;
  }
  threads(cursor?: string): Promise<FeedbackThreadPage> { return this.request(`threads${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`); }
  create(creation_key: string, anchor: FeedbackAnchor, body: string) {
    return this.request<FeedbackThread>('threads', { method: 'POST', body: JSON.stringify({ creation_key, anchor, body }) });
  }
  reply(thread: string, expected_thread_version: number, reply_to_entry_id: string | null, body: string, creation_key: string) {
    return this.request(`threads/${encodeURIComponent(thread)}/entries`, { method: 'POST',
      body: JSON.stringify({ creation_key, expected_thread_version, reply_to_entry_id, body }) });
  }
  edit(entry: FeedbackEntryVersion, body: string) {
    return this.request(`entries/${encodeURIComponent(entry.entry_id)}`, { method: 'PUT', body: JSON.stringify({ expected_version: entry.version, body }) });
  }
  text(request: FeedbackTextRequest): Promise<ScopedCanonicalFeedbackText> {
    return this.request('sources/text', { method: 'POST', body: JSON.stringify(request) });
  }
  outcomes(): Promise<{ items: { entry_version_id: string; thread_id: string; disposition: 'addressed' | 'needs_clarification' | 'not_changed'; reason: string }[] }> { return this.request('outcomes'); }
  async capture(source_token: string, content_digest: string, candidate: ObservedCaptureCandidate): Promise<string> {
    if (!['image/png', 'image/webp'].includes(candidate.blob.type) || candidate.blob.size > 8 * 1024 * 1024
      || candidate.width <= 0 || candidate.height <= 0 || candidate.width > 16384 || candidate.height > 16384
      || candidate.width * candidate.height > 16_000_000) throw new Error('Use a PNG or WebP screenshot up to 8 MiB and 16 megapixels.');
    const request = { creation_key: crypto.randomUUID(), source_token, content_digest, viewport: candidate.viewport,
      render_state_digest: candidate.render_state_digest, method: 'observed_pixels' };
    const created = await this.request<{ capture_id: string; version: number }>('captures', { method: 'POST', body: JSON.stringify(request) });
    await this.request(`captures/${encodeURIComponent(created.capture_id)}/pixels?expected_version=${created.version}&width=${candidate.width}&height=${candidate.height}`,
      { method: 'PUT', body: candidate.blob, headers: { 'Content-Type': candidate.blob.type } });
    return created.capture_id;
  }
  close(): void { this.lifetime.abort(); this.capability = null; this.expiresAt = null; this.guest = null; }
}
