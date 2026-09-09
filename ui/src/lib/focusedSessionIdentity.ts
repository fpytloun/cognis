import type { WorkstreamRef } from '$lib/chat-v2/types';
import type { Session } from '$lib/types/api';

export function sessionIdentityWorkstream(session: Session): WorkstreamRef {
  return {
    key: `session-identity:${session.session_id}`,
    root_key: `session-identity:${session.session_id}`,
    parent_key: null,
    kind: session.delegation_mode ? 'delegate' : 'session',
    edge_kind: session.delegation_mode ? 'delegate' : 'root',
    ordinal: 0,
    conversation_id: session.conversation_id,
    session_id: session.session_id,
    event_store_session_id: session.intaris_session_id ?? session.session_id,
    title: session.delegation_task ?? session.agent_id,
    agent_id: session.agent_id,
    // Session selection is not execution evidence. Runtime identity arrives
    // through the authorized Work projection and focused diagnostics stream.
    agent_profile_id: null,
    status: session.status,
    current: false,
    superseded: false,
    activity_state: session.status === 'active' ? 'ongoing' : 'closed',
    activity_scope_id: session.activity_scope_id,
    backing_session_ids: [session.session_id],
  };
}

export class FocusedSessionIdentityLoader {
  private generation = 0;
  private controller: AbortController | null = null;

  constructor(
    private readonly load: (sessionId: string, signal: AbortSignal) => Promise<Session>,
  ) {}

  async resolve(sessionId: string): Promise<WorkstreamRef | null> {
    this.controller?.abort();
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    try {
      const session = await this.load(sessionId, controller.signal);
      if (
        controller.signal.aborted
        || generation !== this.generation
        || session.session_id !== sessionId
      ) {
        return null;
      }
      return sessionIdentityWorkstream(session);
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }

  cancel(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }

  dispose(): void {
    this.cancel();
  }
}
