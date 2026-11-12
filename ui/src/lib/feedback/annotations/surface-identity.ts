import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
export function surfaceIdentity(binding: Pick<FeedbackSurfaceBinding, 'source' | 'context'> | null | undefined): string {
  return binding ? JSON.stringify([binding.source.source_token, binding.context, binding.source.source]) : '';
}
