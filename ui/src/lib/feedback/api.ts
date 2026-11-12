import type {
  FeedbackContext, FeedbackSourceRef, ResolvedFeedbackSource, FeedbackThreadPage, FeedbackThread,
  FeedbackAnchor, FeedbackEntryVersion, FeedbackStagingSnapshot, FeedbackStagedItem,
  FeedbackPreflightRequest, FeedbackPreflight, SubmissionReceipt, FeedbackDestination,
  ObservedCaptureCandidate,
  FeedbackDraftRequest, FeedbackShare,
  FeedbackTextRequest, ScopedCanonicalFeedbackText,
  FeedbackCapabilities,
  FeedbackSubmissionDetail,
} from './types';

import { request } from './transport';

const root = '/api/v1/feedback';
const id = encodeURIComponent;
export interface FeedbackDestinationOption { destination: FeedbackDestination | null; label: string; available: boolean; reason: string | null; description?: string | null; recommended?: boolean }
export type FeedbackSurface = 'task' | 'library' | 'other';
export interface FeedbackHistoryItem {
  kind: 'submission' | 'resolution' | 'reattach' | 'moderation' | 'disposition' | 'result';
  subject_id: string; created_at: string; receipt: SubmissionReceipt | null;
  entry_version_id: string | null; state: string | null; reason: string | null; result_ref: string | null;
}
export interface FeedbackHistoryPage { items: FeedbackHistoryItem[]; next_cursor: string | null }
export interface FeedbackGrant { principal_email: string; capabilities: FeedbackCapabilities; expected_version: number; version: number }
export const feedbackApi = {
  owner: () => request<{ email: string }>('/api/auth/me'),
  text: (body: FeedbackTextRequest) => request<ScopedCanonicalFeedbackText>(`${root}/sources/text`, { method: 'POST', body: JSON.stringify(body) }),
  resolve: (source: FeedbackSourceRef, context: FeedbackContext, signal?: AbortSignal) =>
    request<ResolvedFeedbackSource>(`${root}/sources/resolve`, { method: 'POST', body: JSON.stringify({ source, context }), signal }),
  threads: (subject: string, cursor?: string, signal?: AbortSignal) =>
    request<FeedbackThreadPage>(`${root}/subjects/${id(subject)}/threads${cursor ? `?cursor=${id(cursor)}` : ''}`, { signal }),
  createThread: (body: { creation_key: string; context: FeedbackContext; anchor: FeedbackAnchor; body: string; audience: 'internal' }) =>
    request<FeedbackThread>(`${root}/threads`, { method: 'POST', body: JSON.stringify(body) }),
  reply: (thread: string, body: { creation_key: string; expected_thread_version: number; reply_to_entry_id: string | null; body: string; context: FeedbackContext }) =>
    request<FeedbackEntryVersion>(`${root}/threads/${id(thread)}/entries`, { method: 'POST', body: JSON.stringify(body) }),
  edit: (entry: string, expected_version: number, body: string, context?: FeedbackContext) =>
    request<FeedbackEntryVersion>(`${root}/entries/${id(entry)}`, { method: 'PUT', body: JSON.stringify({ expected_version, body, context }) }),
  thread: (thread: string, signal?: AbortSignal) => request<FeedbackThread>(`${root}/threads/${id(thread)}`, { signal }),
  /**
   * Resolve, reopen or remove using the latest thread version. Lists can hold an older copy
   * (another window, an auto-resolve on send), so one version conflict re-reads and retries.
   */
  async changeThread(thread: FeedbackThread, change: { state: 'open' | 'resolved' } | { hidden: boolean }): Promise<FeedbackThread> {
    const apply = (current: FeedbackThread) => 'state' in change
      ? current.state === change.state ? Promise.resolve(current) : feedbackApi.resolveThread(current.thread_id, current.version, change.state)
      : current.hidden === change.hidden ? Promise.resolve(current)
        : feedbackApi.moderate(current.thread_id, current.version, change.hidden ? 'hide' : 'restore', change.hidden ? 'Removed' : 'Restored');
    try { return await apply(thread); }
    catch (caught) {
      if ((caught as { status?: number }).status !== 409) throw caught;
      return apply(await feedbackApi.thread(thread.thread_id));
    }
  },
  /** Reply to a comment; one re-read and retry absorbs a concurrent agent answer or resolve. */
  async replyTo(thread: FeedbackThread, body: string, context: FeedbackContext): Promise<FeedbackEntryVersion> {
    const creation_key = crypto.randomUUID();
    const send = (current: FeedbackThread) => feedbackApi.reply(current.thread_id, { creation_key,
      expected_thread_version: current.version, reply_to_entry_id: current.entries.at(-1)?.entry_id ?? null, body, context });
    try { return await send(thread); }
    catch (caught) {
      if ((caught as { status?: number }).status !== 409) throw caught;
      return send(await feedbackApi.thread(thread.thread_id));
    }
  },
  resolveThread: (thread: string, expected_version: number, state: 'open' | 'resolved', reason = '') =>
    request<FeedbackThread>(`${root}/threads/${id(thread)}/resolution`, { method: 'POST', body: JSON.stringify({ expected_version, state, reason }) }),
  moderate: (thread: string, expected_version: number, action: 'hide' | 'restore', reason: string) =>
    request<FeedbackThread>(`${root}/threads/${id(thread)}/moderation`, { method: 'POST', body: JSON.stringify({ expected_version, action, reason }) }),
  reattach: (thread: string, expected_version: number, anchor: FeedbackAnchor, reason: string) =>
    request<FeedbackThread>(`${root}/threads/${id(thread)}/reattach`, { method: 'POST', body: JSON.stringify({ expected_version, anchor, reason }) }),
  async capturePixels(capture: string): Promise<Blob> {
    const response = await fetch(`${root}/captures/${id(capture)}/pixels`, { credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (!response.ok) throw new Error(`Saved observation unavailable (${response.status}).`);
    return response.blob();
  },
  staging: (conversation: string) => request<FeedbackStagingSnapshot>(`${root}/conversations/${id(conversation)}/staging`),
  stage: (conversation: string, entry: string, body: { expected_version: number; entry_version_id: string; inclusion: 'prepared' | 'included' }) =>
    request<FeedbackStagedItem>(`${root}/conversations/${id(conversation)}/staging/${id(entry)}`, { method: 'PUT', body: JSON.stringify(body) }),
  unstage: (conversation: string, item: FeedbackStagedItem) =>
    request<void>(`${root}/conversations/${id(conversation)}/staging/${id(item.entry_id)}?expected_version=${item.staging_version}`, { method: 'DELETE' }),
  preflight: (body: FeedbackPreflightRequest) => request<FeedbackPreflight>(`${root}/preflights`, { method: 'POST', body: JSON.stringify(body) }),
  destinations: (context: FeedbackContext, subject_ids: string[], surface: FeedbackSurface | null = null) =>
    request<{ options: FeedbackDestinationOption[] }>(`${root}/destinations`, { method: 'POST', body: JSON.stringify({ context, subject_ids, surface }) }),
  submit: (body: { creation_key: string; preflight_id: string; manifest_hash: string; replaces_rejected_submission_id: string | null }) =>
    request<SubmissionReceipt>(`${root}/submissions`, { method: 'POST', body: JSON.stringify(body) }),
  receipt: (key: string) => request<SubmissionReceipt>(`${root}/submissions/by-creation-key/${id(key)}/receipt`),
  check: (submission: string) => request<SubmissionReceipt>(`${root}/submissions/${id(submission)}/check-delivery`, { method: 'POST' }),
  submission: (submission: string) => request<FeedbackSubmissionDetail>(`${root}/submissions/${id(submission)}`),
  results: (submission: string) => request<{ items: { content_ref: string; relation: 'revises_source' | 'separate_result' | 'answer_only' }[] }>(`${root}/submissions/${id(submission)}/results`),
  publishOutcome: (share: string, entry_version_id: string, disposition: 'addressed' | 'needs_clarification' | 'not_changed', reason: string) =>
    request(`${root}/public-shares/${id(share)}/outcomes`, { method: 'POST', body: JSON.stringify({ entry_version_id, disposition, reason }) }),
  history: (subject: string, signal?: AbortSignal) => request<FeedbackHistoryPage>(`${root}/subjects/${id(subject)}/history`, { signal }),
  draft: (key: string) => request<{ draft_key: string; version: number; draft: FeedbackDraftRequest }>(`${root}/drafts/${id(key)}`),
  saveDraft: (key: string, body: FeedbackDraftRequest) => request<{ version: number }>(`${root}/drafts/${id(key)}`, { method: 'PUT', body: JSON.stringify(body) }),
  shares: (subject: string) => request<{ items: FeedbackShare[] }>(`${root}/subjects/${id(subject)}/public-shares`),
  copyShare: (share: string) => request<FeedbackShare>(`${root}/public-shares/${id(share)}/link`),
  share: (subject: string, body: { source: Extract<FeedbackSourceRef, { kind: 'document' }>; expected_content_digest: string; allow_comments: boolean; discussion: 'own_only' | 'shared'; expires_at: string; link_mode?: 'view' | 'download' }) =>
    request<FeedbackShare>(`${root}/subjects/${id(subject)}/public-shares`, { method: 'POST', body: JSON.stringify(body) }),
  revokeShare: (share: FeedbackShare) =>
    request<FeedbackShare>(`${root}/public-shares/${id(share.share_id)}?expected_version=${share.version}`, { method: 'DELETE' }),
  updateShare: (share: FeedbackShare, allow_comments: boolean) => request<FeedbackShare>(`${root}/public-shares/${id(share.share_id)}`,
    { method: 'PUT', body: JSON.stringify({ expected_version: share.version, allow_comments }) }),
  grants: (subject: string) => request<{ items: FeedbackGrant[] }>(`${root}/subjects/${id(subject)}/grants`),
  grant: (subject: string, principal_email: string, expected_version: number, capabilities: FeedbackCapabilities) =>
    request<FeedbackGrant>(`${root}/subjects/${id(subject)}/grants`, { method: 'PUT', body: JSON.stringify({ principal_email, expected_version, capabilities }) }),
  summary: (subject: string) => request<{ preferred_followup: string | null; version: number }>(`${root}/subjects/${id(subject)}/summary`),
  followup: (subject: string, expected_version: number, conversation_id: string | null) =>
    request(`${root}/subjects/${id(subject)}/preferred-followup`, { method: 'PUT', body: JSON.stringify({ expected_version, conversation_id }) }),
  async adoptCapture(source: ResolvedFeedbackSource, capture: ObservedCaptureCandidate): Promise<string> {
    if (!['image/png', 'image/webp'].includes(capture.blob.type) || capture.blob.size > 8 * 1024 * 1024
      || capture.width * capture.height > 16_000_000 || capture.width <= 0 || capture.height <= 0) {
      throw new Error('Use a PNG or WebP screenshot up to 8 MiB and 16 megapixels.');
    }
    const created = await request<{ capture_id: string; version: number }>(`${root}/captures`, {
      method: 'POST', body: JSON.stringify({ creation_key: crypto.randomUUID(), source_token: source.source_token,
        content_digest: source.content_digest, viewport: capture.viewport, render_state_digest: capture.render_state_digest, method: 'observed_pixels' }),
    });
    await request(`${root}/captures/${id(created.capture_id)}/pixels?expected_version=${created.version}&width=${capture.width}&height=${capture.height}`,
      { method: 'PUT', body: capture.blob, headers: { 'Content-Type': capture.blob.type } });
    return created.capture_id;
  },
};
