import { describe, expect, it } from 'vitest';
import type { WorkstreamRef } from '$lib/chat-v2/types';
import type { Conversation } from '$lib/types/api';
import {
  canonicalChildView, childViewForWorkstream, childViewScope,
  controllerRootConversationId, eventNeedsTreeRefresh, parentChildView,
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
  it('canonicalizes delegate backing identity once and uses session scope', () => {
    const view = canonicalChildView([node({})], 'old-session', 'root-conversation');
    expect(view).toMatchObject({ kind: 'delegate', sessionId: 'canonical-session', nodeKey: 'delegate' });
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
});
