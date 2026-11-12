import { request } from '$lib/api/client';
import type { UsageSummaryRequest, UsageSummaryResponse } from '$lib/types/api';

/** Raw accounting transport. Selection, cancellation and rendering belong to the caller. */
export function fetchUsageSummaries(
  body: UsageSummaryRequest,
  options: { signal?: AbortSignal } = {},
): Promise<UsageSummaryResponse> {
  return request<UsageSummaryResponse>('/api/v1/usage/session-summaries', {
    method: 'POST',
    body: JSON.stringify(body),
    signal: options.signal,
  });
}
