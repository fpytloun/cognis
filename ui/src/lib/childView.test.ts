import { describe, expect, it } from 'vitest';
import type { WorkstreamRef } from '$lib/chat-v2/types';
import type { BackgroundWorkItem, Conversation } from '$lib/types/api';
import {
  canonicalChildView, childViewForWorkstream, childViewScope,
  controllerRootConversationId, enrichChildWorkstream, eventNeedsTreeRefresh,
  fallbackWorkstream, parentChildView, resolveChildWorkstreamIdentity,
} from './childView';

function node(overrides: Partial<WorkstreamRef>): WorkstreamRef {
  return {
    key: 'delegate', kind: 'delegate', parent_key: 'root', root_key: 'root',
    edge_kind: 'delegate', ordinal: 1, session_id: 'canonical-session',
    backing_session_ids: ['old-session'], event_store_session_id: 'canonical-session',
    title: 'Child', agent_id: 'worker', status: 'running', current: true,
    superseded: false, activity_state: 'ongoing', ...overrides,
  };
}

describe('child view navigation', () => {
  it('resolves a delegate missing from the root graph to its managed conversation before selecting a scope', async () => {
    const fallback = fallbackWorkstream('nested-delegate', 'main-conversation');
    let release!: (value: WorkstreamRef) => void;
    const pending = resolveChildWorkstreamIdentity(fallback, () => new Promise((resolve) => { release = resolve; }));
    release(node({
      session_id: 'nested-delegate', conversation_id: 'managed-conversation',
      agent_id: 'system:implement', title: 'Implementation',
    }));
    const resolved = await pending;
    expect(resolved).toMatchObject({
      key: fallback.key, root_key: fallback.root_key, parent_key: fallback.parent_key,
      agent_id: 'system:implement',
    });
    expect(childViewScope(childViewForWorkstream(resolved!, 'main-conversation'))).toMatchObject({
      key: 'session:nested-delegate', conversation_id: 'managed-conversation',
    });
  });

  it('does not guess ownership when session identity is unavailable or mismatched', async () => {
    const fallback = fallbackWorkstream('nested-delegate', 'main-conversation');
    expect(await resolveChildWorkstreamIdentity(fallback, async () => null)).toBeNull();
    expect(await resolveChildWorkstreamIdentity(fallback, async () => node({
      conversation_id: 'managed-conversation',
    }))).toBeNull();
  });

  it('canonicalizes delegate backing identity once and uses its owning conversation', () => {
    const view = canonicalChildView([node({
      conversation_id: 'owning-conversation',
    })], 'old-session', 'root-conversation');
    expect(view).toMatchObject({ kind: 'delegate', sessionId: 'canonical-session', nodeKey: 'delegate' });
    expect(childViewScope(view!)).toEqual({
      key: 'session:canonical-session', kind: 'session',
      session_id: 'canonical-session', conversation_id: 'owning-conversation',
    });
  });

  it('falls back to the controller root for delegates without an owning conversation', () => {
    const view = canonicalChildView([node({
      conversation_id: null,
    })], 'old-session', 'root-conversation');
    expect(childViewScope(view!)).toEqual({
      key: 'session:canonical-session', kind: 'session',
      session_id: 'canonical-session', conversation_id: 'root-conversation',
    });
  });

  it('uses target conversation scope for a managed node', () => {
    const view = childViewForWorkstream(node({
      key: 'managed', kind: 'managed', conversation_id: 'target-conversation',
      link_id: 'link-managed-target',
    }), 'root-conversation');
    expect(childViewScope(view)).toEqual({
      key: 'conversation:target-conversation', kind: 'conversation',
      conversation_id: 'target-conversation',
    });
  });

  it('prefers the root controller and safely falls back', () => {
    const payload = JSON.parse(JSON.stringify({
      conversation_id: 'target',
      root_controller_conversation_id: 'root',
      managed_agent: {
        controller_conversation_id: 'parent',
      },
    })) as Conversation;
    const conversation = payload;
    expect(controllerRootConversationId(conversation)).toBe('root');
    conversation.root_controller_conversation_id = null;
    expect(controllerRootConversationId(conversation)).toBe('target');
  });

  it('detects lifecycle, invalidation, and canonical child-creation tool refreshes', () => {
    expect(eventNeedsTreeRefresh({ type: 'work_invalidated' })).toBe(true);
    expect(eventNeedsTreeRefresh({ type: 'delegation_completed' })).toBe(true);
    expect(eventNeedsTreeRefresh({
      type: 'chat_v2_frame',
      ops: [{ item: { kind: 'tool_result', tool_name: 'agent_conversation_create' } }],
    })).toBe(true);
    expect(eventNeedsTreeRefresh({
      type: 'chat_v2_frame',
      ops: [{ item: { kind: 'tool_result', tool_name: 'bash' } }],
    })).toBe(false);
    expect(eventNeedsTreeRefresh({
      type: 'chat_v2_frame',
      ops: [{ item: { kind: 'tool_call', tool_name: 'bash' } }],
    })).toBe(false);
    expect(eventNeedsTreeRefresh({ type: 'chat_v2_frame', ops: [] })).toBe(false);
  });

  it('walks nested Back one logical parent while Close can discard the view', () => {
    const nodes = [
      node({ key: 'root', parent_key: null, session_id: 'root-session', kind: 'conversation' }),
      node({ key: 'child', parent_key: 'root', session_id: 'child-session' }),
      node({ key: 'grandchild', parent_key: 'child', session_id: 'grandchild-session' }),
    ];
    const child = childViewForWorkstream(nodes[1], 'root-conversation');
    const grandchild = childViewForWorkstream(nodes[2], 'root-conversation');
    expect(parentChildView(nodes, grandchild)?.sessionId).toBe('child-session');
    expect(parentChildView(nodes, child)).toBeNull();
    const inspector = { open: true, tab: 'work' };
    const closedView = null;
    expect(closedView).toBeNull();
    expect(inspector).toEqual({ open: true, tab: 'work' });
  });

  it('derives delegate and managed fallbacks from projected work without using ID prefixes', () => {
    const work: BackgroundWorkItem[] = [
      {
        kind: 'delegated_session', work_id: 'delegate-work', controller_conversation_id: 'root',
        session_id: 'plain-session', title: 'Delegate work', agent_id: 'worker',
        status: 'running', todos: [],
      },
      {
        kind: 'managed_conversation', work_id: 'managed-work', controller_conversation_id: 'root',
        target_conversation_id: 'target-conversation', session_id: 'another-session',
        title: 'Managed work', agent_id: 'manager', status: 'queued', todos: [],
      },
    ];
    const delegate = fallbackWorkstream('plain-session', 'root', work);
    const managed = fallbackWorkstream('another-session', 'root', work);
    const bare = fallbackWorkstream('managed-looking-id', 'root');

    expect(childViewForWorkstream(delegate, 'root').kind).toBe('delegate');
    expect(childViewForWorkstream(managed, 'root')).toMatchObject({
      kind: 'managed',
      conversationId: 'target-conversation',
    });
    expect(childViewForWorkstream(bare, 'root').kind).toBe('delegate');
  });

  it('enriches presentation from a late overview without changing the selected scope', () => {
    const selected = node({
      key: 'fallback:session',
      kind: 'managed',
      conversation_id: 'selected-conversation',
      session_id: 'selected-session',
      event_store_session_id: 'selected-session',
      title: 'Initial title',
    });
    const enriched = enrichChildWorkstream(selected, node({
      key: 'overview-key',
      kind: 'delegate',
      conversation_id: 'different-conversation',
      session_id: 'different-session',
      event_store_session_id: 'different-session',
      title: 'Enriched title',
      status: 'completed',
    }));

    expect(enriched).toMatchObject({
      key: 'fallback:session',
      kind: 'managed',
      conversation_id: 'selected-conversation',
      session_id: 'selected-session',
      event_store_session_id: 'selected-session',
      title: 'Enriched title',
      status: 'completed',
    });
  });
});
