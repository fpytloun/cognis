import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearSessionInfoCache, getSessionInfo, setSessionInfo, type SessionInfoData } from './sessionInfoCache';

function detail(id: string): SessionInfoData {
  return {
    intaris_session_id: id, intention: null, summary: null, status: 'complete',
    total_calls: 0, approved_count: 0, denied_count: 0, escalated_count: 0,
  };
}

describe('sessionInfoCache', () => {
  beforeEach(() => clearSessionInfoCache());

  it('retains details for two minutes and expires them by five minutes', () => {
    vi.useFakeTimers();
    setSessionInfo('conversation', 'session', detail('session'));
    vi.advanceTimersByTime(119_999);
    expect(getSessionInfo('conversation', 'session')?.intaris_session_id).toBe('session');
    vi.advanceTimersByTime(2);
    expect(getSessionInfo('conversation', 'session')).toBeNull();
    vi.useRealTimers();
  });

  it('keeps eight sessions per conversation and sixteen globally', () => {
    for (let index = 0; index < 10; index += 1) {
      setSessionInfo('conversation', `session-${index}`, detail(`session-${index}`));
    }
    expect(getSessionInfo('conversation', 'session-0')).toBeNull();
    expect(getSessionInfo('conversation', 'session-2')).not.toBeNull();
    for (let index = 0; index < 10; index += 1) {
      setSessionInfo(`other-${index}`, 'session', detail(`other-${index}`));
    }
    expect(getSessionInfo('conversation', 'session-3')).toBeNull();
  });
});
