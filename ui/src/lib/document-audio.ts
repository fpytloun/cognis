/** Portable document audio transport. No Cognis app/auth stores in this module. */
import type { AudioJob, AudioJobCreateRequest, DocumentAudioContext, PodcastLengthEstimate } from '$lib/types/api';

export interface DocumentAudioClient {
  context(ref: string, options?: { signal?: AbortSignal }): Promise<DocumentAudioContext>;
  estimates(ref: string, options?: { signal?: AbortSignal }): Promise<PodcastLengthEstimate[]>;
  create(payload: AudioJobCreateRequest, options?: { signal?: AbortSignal }): Promise<AudioJob>;
  get(id: string, options?: { signal?: AbortSignal }): Promise<AudioJob>;
  cancel(id: string): Promise<AudioJob>;
}

class AudioRequestError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

/** Cookie authentication is supported by the same-origin standalone viewer. */
export function standaloneAudioClient(): DocumentAudioClient {
  async function read<T>(url: string, options: RequestInit = {}): Promise<T> {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    if (!response.ok) throw new AudioRequestError(response.status,
      response.status === 401 ? 'Sign in with document access to generate audio.' : 'Audio request failed.');
    return response.json() as Promise<T>;
  }
  const base = '/api/v1/audio/jobs';
  return {
    estimates: (ref, options = {}) => read<PodcastLengthEstimate[]>(
      `${base}/estimates?${new URLSearchParams({ content_ref: ref })}`, options),
    async context(ref, options = {}) {
      try {
        return await read<DocumentAudioContext>(`${base}/source?${new URLSearchParams({ content_ref: ref })}`, options);
      } catch (error) {
        if (error instanceof AudioRequestError && [401, 403, 404].includes(error.status)) {
          return { supported: false, can_generate: false, can_cancel: false,
            detail: null, jobs: [] };
        }
        throw error;
      }
    },
    create: (payload, options = {}) => read<AudioJob>(base, { ...options,
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    }),
    get: (id, options = {}) => read<AudioJob>(`${base}/${encodeURIComponent(id)}`, options),
    cancel: (id) => read<AudioJob>(`${base}/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
  };
}

export const defaultDocumentAudioClient = standaloneAudioClient();

/** Read one exact revision, reusing recordings and never replaying a failed paid attempt. */
export async function readDocumentNarration(
  client: DocumentAudioClient, ref: string, signal: AbortSignal,
  onPart: (part: AudioJob['parts'][number]) => void,
): Promise<AudioJob> {
  const context = await client.context(ref, { signal });
  signal.throwIfAborted();
  if (!context.supported) throw new Error('Document narration is unavailable.');
  const recordings = context.jobs.filter(job => job.mode === 'narration');
  let job = recordings.find(job => ['queued', 'scripting', 'synthesizing', 'completed'].includes(job.status))
    ?? recordings[0];
  if (!job) {
    if (!context.can_generate) throw new Error('Document narration is not permitted.');
    job = await client.create({
      content_ref: ref, mode: 'narration', idempotency_key: `conversation-narration:${ref}`,
    }, { signal });
  }
  const played = new Set<string>();
  while (true) {
    signal.throwIfAborted();
    if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
      throw new Error('Document narration did not complete.');
    }
    for (const part of job.parts) {
      if (played.has(part.artifact_id)) continue;
      played.add(part.artifact_id);
      onPart(part);
    }
    if (job.status === 'completed') {
      if (!played.size) throw new Error('Document narration contains no audio.');
      return job;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
    signal.throwIfAborted();
    job = await client.get(job.job_id, { signal });
  }
}
