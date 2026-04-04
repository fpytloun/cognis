export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
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
