import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSessionInfoCache,
  getSessionInfo,
  mergeRuntimeSelection,
  setSessionInfo,
  type SessionInfoData
} from './sessionInfoCache';

function detail(id: string): SessionInfoData {
  return {
    intaris_session_id: id, intention: null, summary: null, status: 'complete',
    total_calls: 0, approved_count: 0, denied_count: 0, escalated_count: 0,
  };
}

describe('sessionInfoCache', () => {
  beforeEach(() => clearSessionInfoCache());

  it('rejects an older runtime selection', () => {
    const current = {
      ...detail('session'),
      runtime_selection: {
        revision: 3,
        profile_id: 'developer',
        profile_source: 'session',
        model: 'new-model',
        provider_id: 'codex',
        model_source: 'session_override',
        reasoning_effort: 'high',
        reasoning_effort_source: 'session_override',
        fast_mode: null,
        fast_mode_source: 'agent_profile'
      }
    };
    const merged = mergeRuntimeSelection(current, {
      ...current.runtime_selection,
      revision: 2,
      model: 'old-model'
    });
    expect(merged).toBe(current);
  });

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
