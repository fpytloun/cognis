import { describe, expect, it, vi } from 'vitest';

import { ChatV2ApiError } from '$lib/chat-v2/api';
import { conversationTimelineScope, type WorkFileHistoryResponse } from '$lib/chat-v2/types';
import { FileHistorySession, type FileHistoryLoader } from './fileHistoryClient';

const scope = conversationTimelineScope('conversation-1');

function response(overrides: Partial<WorkFileHistoryResponse> = {}): WorkFileHistoryResponse {
  return {
    scope,
    path_generation_id: 'wpg_1',
    items: [{ path: 'src/file.ts', diff: '+new' }],
    before_cursor: null,
    has_more_before: false,
    ...overrides,
  };
}

describe('FileHistorySession', () => {
  it('loads a page and preserves its pagination cursor', async () => {
    const loader = vi.fn<FileHistoryLoader>().mockResolvedValue(response({
      before_cursor: 'cursor-1',
      has_more_before: true,
    }));
    const session = new FileHistorySession(loader);

    await expect(session.loadOlder({
      scope,
      pathGenerationId: 'wpg_1',
      before: null,
    })).resolves.toEqual({
      status: 'ok',
      items: [{ path: 'src/file.ts', diff: '+new' }],
      beforeCursor: 'cursor-1',
      hasMore: true,
      restarted: false,
    });
    expect(loader).toHaveBeenCalledWith(
      expect.objectContaining({ path_generation_id: 'wpg_1', limit: 50 }),
      expect.any(AbortSignal),
    );
  });

  it('aborts an obsolete request when a new page starts', async () => {
    let firstSignal: AbortSignal | undefined;
    const loader = vi.fn<FileHistoryLoader>()
      .mockImplementationOnce((_request, signal) => {
        firstSignal = signal;
        return new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        });
      })
      .mockResolvedValueOnce(response());
    const session = new FileHistorySession(loader);
    const obsolete = session.loadOlder({ scope, pathGenerationId: 'wpg_1', before: null });
    const current = session.loadOlder({ scope, pathGenerationId: 'wpg_2', before: null });

    await expect(obsolete).resolves.toEqual({ status: 'aborted' });
    await expect(current).resolves.toMatchObject({ status: 'ok' });
    expect(firstSignal?.aborted).toBe(true);
  });

  it('restarts once from newest after a typed 409 cursor invalidation', async () => {
    const loader = vi.fn<FileHistoryLoader>()
      .mockRejectedValueOnce(new ChatV2ApiError('invalid cursor', {
        code: 'cursor_invalid',
        status: 409,
      }))
      .mockResolvedValueOnce(response());
    const session = new FileHistorySession(loader);

    await expect(session.loadOlder({
      scope,
      pathGenerationId: 'wpg_1',
      before: 'old-cursor',
    })).resolves.toMatchObject({ status: 'ok', restarted: true });
    expect(loader.mock.calls[0][0].before).toBe('old-cursor');
    expect(loader.mock.calls[1][0].before).toBeUndefined();
  });

  it('keeps a restarted request abortable after a typed 409', async () => {
    let restartedSignal: AbortSignal | undefined;
    const loader = vi.fn<FileHistoryLoader>()
      .mockRejectedValueOnce(new ChatV2ApiError('invalid cursor', {
        code: 'cursor_invalid',
        status: 409,
      }))
      .mockImplementationOnce((_request, signal) => {
        restartedSignal = signal;
        return new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        });
      });
    const session = new FileHistorySession(loader);
    const pending = session.loadOlder({
      scope,
      pathGenerationId: 'wpg_1',
      before: 'old-cursor',
    });
    await vi.waitFor(() => expect(restartedSignal).toBeDefined());
    session.abort();
    await expect(pending).resolves.toEqual({ status: 'aborted' });
    expect(restartedSignal?.aborted).toBe(true);
  });

  it.each([
    [404, 'not_found'],
    [410, 'unavailable'],
  ] as const)('maps HTTP %i to %s', async (status, expected) => {
    const session = new FileHistorySession(vi.fn<FileHistoryLoader>().mockRejectedValue(
      new ChatV2ApiError('failed', { status }),
    ));
    await expect(session.loadOlder({
      scope,
      pathGenerationId: 'wpg_1',
      before: null,
    })).resolves.toEqual({ status: expected });
  });

  it('returns a retryable error for transient failures', async () => {
    const session = new FileHistorySession(vi.fn<FileHistoryLoader>().mockRejectedValue(
      new ChatV2ApiError('temporarily unavailable', {
        code: 'event_store_unavailable',
        status: 503,
      }),
    ));
    await expect(session.loadOlder({
      scope,
      pathGenerationId: 'wpg_1',
      before: null,
    })).resolves.toEqual({ status: 'error', message: 'temporarily unavailable' });
  });
});
