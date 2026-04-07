export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
}

export const SESSION_LOG_PAGE_SIZE = 200;
export const SESSION_LOG_BOOTSTRAP_MAX_PAGES = 5;
export const SESSION_LOG_POLL_INTERVAL_MS = 3000;
export const SESSION_LOG_POLL_MAX_INTERVAL_MS = 30000;

export function nextPollDelayMs(currentDelayMs: number): number {
  const baseDelay = Math.max(currentDelayMs, SESSION_LOG_POLL_INTERVAL_MS);
  const doubled = Math.min(baseDelay * 2, SESSION_LOG_POLL_MAX_INTERVAL_MS);
  const jitter = 0.85 + Math.random() * 0.3;
  return Math.round(doubled * jitter);
}

export function nextConversationLoadId(current: number): number {
  return current + 1;
}

export function isCurrentConversationLoad(requestId: number, activeRequestId: number): boolean {
  return requestId === activeRequestId;
}

export function getConversationRetryScope(errors: {
  sessionsError: string;
  historyError: string;
}): ConversationRetryScope {
  return {
    sessions: Boolean(errors.sessionsError),
    history: Boolean(errors.historyError)
  };
}

export function getNextHistoryAfterSeq(response: {
  last_seq: number;
  items: Array<{ seq: number | null }>;
}): number {
  if (response.last_seq > 0) {
    return response.last_seq;
  }

  const lastItem = response.items[response.items.length - 1];
  return typeof lastItem?.seq === 'number' ? lastItem.seq : 0;
}
