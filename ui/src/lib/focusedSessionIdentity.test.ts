import { describe, expect, it, vi } from 'vitest';

import type { Session } from '$lib/types/api';
import { FocusedSessionIdentityLoader, sessionIdentityWorkstream } from './focusedSessionIdentity';

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    activity_scope_id: 'scope-1',
    conversation_id: 'conversation-1',
    parent_session_id: 'parent-session',
    previous_session_id: null,
    user_email: 'owner@example.com',
    agent_id: 'reviewer',
    agent_profile_id: 'developer',
    delegation_mode: 'delegate',
    delegation_task: 'Review nested work',
    status: 'active',
    intaris_session_id: `store-${sessionId}`,
    mnemory_session_id: null,
    started_at: null,
    idle_since: null,
    completed_at: null,
    completion_reason: null,
    result_summary: null,
    result_content: null,
    result_anchors: null,
    result_sections: null,
    updated_at: null,
  };
}

describe('FocusedSessionIdentityLoader', () => {
  it('hydrates owned local identity without inventing graph parentage', () => {
    expect(sessionIdentityWorkstream(session('child'))).toMatchObject({
      session_id: 'child',
      event_store_session_id: 'store-child',
      title: 'Review nested work',
      agent_id: 'reviewer',
      parent_key: null,
      kind: 'delegate',
    });
  });

  it('rejects a late identity after focus changes', async () => {
    let resolveFirst!: (value: Session) => void;
    const first = new Promise<Session>((resolve) => { resolveFirst = resolve; });
    const load = vi.fn()
      .mockImplementationOnce(() => first)
      .mockResolvedValueOnce(session('second'));
    const loader = new FocusedSessionIdentityLoader(load);

    const stale = loader.resolve('first');
    const current = loader.resolve('second');
    resolveFirst(session('first'));

    expect(await stale).toBeNull();
    expect((await current)?.session_id).toBe('second');
  });

  it('rejects an in-flight identity after disposal', async () => {
    let resolve!: (value: Session) => void;
    const pending = new Promise<Session>((done) => { resolve = done; });
    const loader = new FocusedSessionIdentityLoader(async () => pending);
    const result = loader.resolve('child');
    loader.dispose();
    resolve(session('child'));
    expect(await result).toBeNull();
  });
});
