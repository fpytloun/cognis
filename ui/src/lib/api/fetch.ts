export const DEFAULT_API_REQUEST_TIMEOUT_MS = 30_000;

export class FetchTimeoutError extends Error {
  constructor(timeoutMs: number) {
    super(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds.`);
    this.name = 'FetchTimeoutError';
  }
}

export interface FetchWithTimeoutOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

type SignalListener = {
  signal: AbortSignal;
  listener: () => void;
};

function abortController(controller: AbortController, signal: AbortSignal): void {
  if (controller.signal.aborted) return;
  controller.abort(signal.reason);
}

function mergeAbortSignals(signals: AbortSignal[]): { signal: AbortSignal; cleanup: () => void } {
  const activeSignals = signals.filter(Boolean);
  if (activeSignals.length === 1) {
    return { signal: activeSignals[0], cleanup: () => {} };
  }

  const controller = new AbortController();
  const listeners: SignalListener[] = [];

  for (const signal of activeSignals) {
    if (signal.aborted) {
      abortController(controller, signal);
      break;
    }
    const listener = () => abortController(controller, signal);
    signal.addEventListener('abort', listener, { once: true });
    listeners.push({ signal, listener });
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      for (const { signal, listener } of listeners) {
        signal.removeEventListener('abort', listener);
      }
    }
  };
}

export function isFetchTimeoutError(error: unknown): error is FetchTimeoutError {
  return error instanceof FetchTimeoutError;
}

export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: FetchWithTimeoutOptions = {}
): Promise<Response> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_API_REQUEST_TIMEOUT_MS;

  // Explicit opt-out for long-running uploads, downloads, audio synthesis, and
  // other calls that must not be aborted by the default API deadline.
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    return fetchImpl(input, init);
  }

  const timeoutController = new AbortController();
  const { signal, cleanup } = mergeAbortSignals(
    init.signal ? [init.signal, timeoutController.signal] : [timeoutController.signal]
  );
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeoutPromise = new Promise<never>((_resolve, reject) => {
    timeoutId = setTimeout(() => {
      const error = new FetchTimeoutError(timeoutMs);
      // Best-effort abort of the underlying fetch. Some WebKit/PWA fetches can
      // remain pending after abort, so the Promise.race below rejects directly
      // from this timer instead of relying on fetch to settle.
      timeoutController.abort(error);
      reject(error);
    }, timeoutMs);
  });

  try {
    return await Promise.race([fetchImpl(input, { ...init, signal }), timeoutPromise]);
  } catch (error) {
    if (timeoutController.signal.aborted && !init.signal?.aborted) {
      throw isFetchTimeoutError(error) ? error : new FetchTimeoutError(timeoutMs);
    }
    throw error;
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
    cleanup();
  }
}
