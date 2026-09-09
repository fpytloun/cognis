import { ChatV2ApiError } from '$lib/chat-v2/api';
import type { FileDiffRef, TimelineScope, WorkFileHistoryRequest, WorkFileHistoryResponse } from '$lib/chat-v2/types';

export type FileHistoryLoader = (
  request: WorkFileHistoryRequest,
  signal: AbortSignal,
) => Promise<WorkFileHistoryResponse>;

export type FileHistoryOutcome =
  | { status: 'ok'; items: FileDiffRef[]; hasMore: boolean; beforeCursor: string | null; restarted: boolean }
  | { status: 'not_found' }
  | { status: 'unavailable' }
  | { status: 'aborted' }
  | { status: 'error'; message: string };

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Coordinates lazy `/api/v1/work/file-history` pagination for exactly one
 * file's `path_generation_id` at a time. Each call to `loadOlder` aborts any
 * in-flight request for this session first, so selection/scope/epoch
 * changes never race a stale page into the current view.
 */
export class FileHistorySession {
  private controller: AbortController | null = null;

  constructor(private readonly loader: FileHistoryLoader) {}

  /** Abort the in-flight request, if any. Safe to call repeatedly. */
  abort(): void {
    this.controller?.abort();
    this.controller = null;
  }

  async loadOlder(params: {
    scope: TimelineScope;
    pathGenerationId: string;
    before: string | null;
  }): Promise<FileHistoryOutcome> {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;

    let before = params.before;
    let restarted = false;
    try {
      while (true) {
        try {
          const response = await this.loader(
            {
              scope: params.scope,
              path_generation_id: params.pathGenerationId,
              before: before ?? undefined,
              limit: 50,
            },
            controller.signal,
          );
          if (controller.signal.aborted) return { status: 'aborted' };
          return {
            status: 'ok',
            items: response.items,
            hasMore: response.has_more_before,
            beforeCursor: response.before_cursor ?? null,
            restarted,
          };
        } catch (error) {
          if (controller.signal.aborted || isAbort(error)) return { status: 'aborted' };
          if (error instanceof ChatV2ApiError) {
            if (error.status === 404) return { status: 'not_found' };
            if (error.status === 410) return { status: 'unavailable' };
            // A typed 409 means the cache epoch moved under us. Restart from
            // the newest page exactly once instead of surfacing a dead cursor.
            if (error.status === 409 && before !== null && !restarted) {
              before = null;
              restarted = true;
              continue;
            }
            return { status: 'error', message: error.message };
          }
          return { status: 'error', message: 'File history is temporarily unavailable.' };
        }
      }
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }
}
