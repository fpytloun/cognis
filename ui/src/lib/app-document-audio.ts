/** Authenticated application adapter, deliberately outside the standalone graph. */
import { api } from '$lib/api/client';
import type { DocumentAudioClient } from '$lib/document-audio';

export const appDocumentAudioClient: DocumentAudioClient = {
  context: (ref, options) => api.audioJobs.context(ref, options),
  estimates: (ref, options) => api.audioJobs.estimates(ref, options),
  create: (payload, options) => api.audioJobs.create(payload, options),
  get: (id, options) => api.audioJobs.get(id, options),
  cancel: (id) => api.audioJobs.cancel(id)
};
