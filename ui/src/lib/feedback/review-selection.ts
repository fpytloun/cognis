import type { FeedbackSourceRef } from './types';

/** Immutable canonical thread-source proof retained with a checked entry version. */
export interface FeedbackReviewSource {
  entry_version_id: string;
  version: number;
  thread_id: string;
  anchor_id: string;
  source: FeedbackSourceRef;
}
