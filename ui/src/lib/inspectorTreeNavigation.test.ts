import { describe, expect, it } from 'vitest';
import {
  canonicalWorkstreamSessionId,
  rootOverviewForConversation,
  selectedWorkSubtreeScope,
  structuralParentSessionId,
  traverseInspectorSession,
  treeSessionNavigation,
  workstreamForSession,
} from './inspectorTreeNavigation';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';

const overview = {
  scope: { key: 'conversation:a', kind: 'conversation', conversation_id: 'a' },
} as ActivityOverviewResponse;

describe('inspector tree navigation', () => {
  it('never exposes conversation A tree while conversation B is active', () => {
    expect(rootOverviewForConversation(overview, 'a')).toBe(overview);
    expect(rootOverviewForConversation(overview, 'b')).toBeNull();
    expect(rootOverviewForConversation({
      ...overview,
      scope: { key: 'session:s', kind: 'session', conversation_id: 'a', session_id: 's' },
    }, 'a')).toBeNull();
  });

  it('uses the selected session as a subtree scope', () => {
    expect(selectedWorkSubtreeScope('conversation-a', null)).toEqual({
      key: 'conversation:conversation-a',
      kind: 'conversation',
      conversation_id: 'conversation-a',
    });
    expect(selectedWorkSubtreeScope('conversation-a', 'session-child')).toEqual({
      key: 'session:session-child',
      kind: 'session',
      session_id: 'session-child',
      conversation_id: 'conversation-a',
    });
  });

  it('resolves backing sessions to their canonical logical workstream', () => {
    const nodes = [
      {
        key: 'logical-child',
        session_id: 'canonical-child',
        backing_session_ids: ['old-child', 'canonical-child'],
      },
    ] as ActivityOverviewResponse['workstreams'];

    expect(workstreamForSession(nodes, 'old-child')).toBe(nodes[0]);
    expect(canonicalWorkstreamSessionId(nodes, 'old-child')).toBe('canonical-child');
    expect(canonicalWorkstreamSessionId(nodes, 'canonical-child')).toBe('canonical-child');
    expect(selectedWorkSubtreeScope(
      'conversation-a',
      canonicalWorkstreamSessionId(nodes, 'old-child'),
    )).toEqual({
      key: 'session:canonical-child',
      kind: 'session',
      session_id: 'canonical-child',
      conversation_id: 'conversation-a',
    });
  });

  it('opens the real middle viewer without closing pinned or overlay presentation', () => {
    expect(treeSessionNavigation('pinned')).toEqual({
      openSubSessionViewer: true,
      closeInspectorOverlay: false,
    });
    expect(treeSessionNavigation('overlay')).toEqual({
      openSubSessionViewer: true,
      closeInspectorOverlay: false,
    });
  });

  it('keeps drawer presentation and tab while synchronizing focus, middle, and Work scope', () => {
    const initial = {
      drawerOpen: true,
      activeTab: 'work' as const,
      presentation: 'overlay' as const,
      focusedSessionId: 'session-a',
      middleSessionId: 'session-a',
      workSessionId: 'session-a',
    };
    const sessionB = traverseInspectorSession(initial, 'session-b');
    expect(sessionB).toEqual({
      ...initial,
      focusedSessionId: 'session-b',
      middleSessionId: 'session-b',
      workSessionId: 'session-b',
    });
    const back = traverseInspectorSession(sessionB, 'session-a');
    expect(back).toEqual(initial);
  });

  it('navigates one structural parent and maps the canonical root to conversation scope', () => {
    const nodes = [
      { key: 'root', parent_key: null, session_id: 'root-session' },
      { key: 'parent', parent_key: 'root', session_id: 'parent-session' },
      { key: 'child', parent_key: 'parent', session_id: 'child-session' },
    ] as ActivityOverviewResponse['workstreams'];
    expect(structuralParentSessionId(nodes, 'child-session')).toBe('parent-session');
    expect(structuralParentSessionId(nodes, 'parent-session')).toBeNull();
    expect(structuralParentSessionId(nodes, 'root-session')).toBeNull();
  });

  it('uses the logical node for structural Back from an old backing session', () => {
    const nodes = [
      { key: 'root', parent_key: null, session_id: 'root-session' },
      { key: 'parent', parent_key: 'root', session_id: 'parent-session' },
      {
        key: 'child',
        parent_key: 'parent',
        session_id: 'canonical-child',
        backing_session_ids: ['old-child'],
      },
    ] as ActivityOverviewResponse['workstreams'];
    expect(structuralParentSessionId(nodes, 'old-child')).toBe('parent-session');
  });
});
