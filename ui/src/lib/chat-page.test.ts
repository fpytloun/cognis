import { describe, expect, it } from 'vitest';

import {
  getNextHistoryAfterSeq,
  getConversationRetryScope,
  isCurrentConversationLoad,
  nextConversationLoadId
} from '$lib/chat-page';

describe('chat page helpers', () => {
  it('increments and validates conversation load ids', () => {
    const first = nextConversationLoadId(0);
    const second = nextConversationLoadId(first);

    expect(first).toBe(1);
    expect(second).toBe(2);
    expect(isCurrentConversationLoad(second, second)).toBe(true);
    expect(isCurrentConversationLoad(first, second)).toBe(false);
  });

  it('scopes retries to failed subloads only', () => {
    expect(
      getConversationRetryScope({
        sessionsError: '',
        historyError: 'history failed'
      })
    ).toEqual({ sessions: false, history: true });

    expect(
      getConversationRetryScope({
        sessionsError: 'sessions failed',
        historyError: ''
      })
    ).toEqual({ sessions: true, history: false });
  });

  it('advances history pagination with response last_seq when rows were skipped', () => {
    expect(
      getNextHistoryAfterSeq({
        items: [],
        last_seq: 42
      })
    ).toBe(42);

    expect(
      getNextHistoryAfterSeq({
        items: [{ seq: 5 }],
        last_seq: 0
      })
    ).toBe(5);
  });
});
