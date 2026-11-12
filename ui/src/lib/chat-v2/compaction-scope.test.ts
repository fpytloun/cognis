import { describe, expect, it } from 'vitest';
import { emptyChatV2State } from './sync-engine';
import { reconcileCompactionScope, unconfirmedCompactionIds } from './compaction-scope';
import {
  conversationTimelineScope, sessionTimelineScope,
  type ActivityOverviewResponse, type CompactionTimelineItem, type WorkstreamRef,
} from './types';

const scope = conversationTimelineScope('compaction-scope-fixture');
const card = (id: string, sessionId: string): CompactionTimelineItem => ({
  id, kind: 'compaction', session_id: sessionId, previous_session_id: `${sessionId}-previous`,
  sort_key: `9997:${id}`, stable: false, source_refs: [], status: 'compacted',
  summary_preview: '', method: 'llm', turns_compacted: 3,
});
const delegate: WorkstreamRef = {
  key: 'delegate:child', parent_key: scope.key, root_key: scope.key, kind: 'delegate', edge_kind: 'delegate',
  ordinal: 1, conversation_id: scope.conversation_id, session_id: 'child',
  backing_session_ids: ['child', 'child-continued'], event_store_session_id: 'child-intaris',
  title: 'Delegate', agent_id: 'agent', status: 'completed', current: true, superseded: false,
};
const graph = { scope, workstreams: [delegate] } as ActivityOverviewResponse;
function state() {
  return {
    ...emptyChatV2State(), scope, scopeKey: scope.key, conversationId: scope.conversation_id!,
    localItems: [card('leaked-child', 'child-continued'), card('own', 'root'), card('unknown', 'unobserved')],
  };
}

describe('compaction scope reconciliation', () => {
  it('retires only positively identified child overlays, even from a partial graph', () => {
    const previous = state();
    const before = structuredClone(previous);
    const next = reconcileCompactionScope(previous, { ...graph, graph_truncated: true });
    expect(next.localItems.map(item => item.id)).toEqual(['own', 'unknown']);
    expect(next.timelineItems).toBe(previous.timelineItems);
    expect(next.localItems[0]).toBe(previous.localItems[1]);
    expect(previous).toEqual(before);
  });

  it('uses previous-session and event-store aliases without changing runtime authority', () => {
    const previous = state();
    const leaked = { ...card('running-child', 'not-observed-yet'), previous_session_id: 'child-intaris' };
    const runtime = {
      runtime_epoch: 'epoch', runtime_revision: 7, generated_at: '', has_active_turn: true,
      volatile_items: [leaked, card('running-root', 'root')],
      authority: { direct_request_id: 'request', turn_id: 'turn', fencing_token: 7, lifecycle: 'active' as const },
    };
    const next = reconcileCompactionScope({ ...previous, runtime }, graph);
    expect(next.runtime?.volatile_items.map(item => item.id)).toEqual(['running-root']);
    expect(next.runtime?.authority).toBe(runtime.authority);
    expect(next.runtime?.runtime_revision).toBe(7);
  });

  it('never removes canonical or sourced records or an unrelated local admission', () => {
    const previous = state();
    const canonical = { ...previous.localItems[0], stable: true };
    const sourced = { ...card('sourced', 'child'), source_refs: [{ store: 'intaris', session_id: 'child', seq: 1, event_type: 'lifecycle' }] };
    const input = {
      ...previous, timelineItems: [canonical], localItems: [...previous.localItems, sourced],
    };
    expect(reconcileCompactionScope(input, graph)).toBe(input);
  });

  it('retains unknown ownership, other conversations and a delegate own view', () => {
    const previous = state();
    expect(reconcileCompactionScope(previous, { ...graph, workstreams: [] })).toBe(previous);
    expect(reconcileCompactionScope(previous, {
      ...graph, scope: conversationTimelineScope('another-conversation'),
    })).toBe(previous);
    expect(reconcileCompactionScope(previous, {
      ...graph, workstreams: [{ ...delegate, conversation_id: 'another-conversation' }],
    })).toBe(previous);
    const child = sessionTimelineScope('child', scope.conversation_id!);
    const childState = { ...previous, scope: child, scopeKey: child.key };
    expect(reconcileCompactionScope(childState, { ...graph, scope: child })).toBe(childState);
    expect(unconfirmedCompactionIds(previous)).toEqual(['leaked-child', 'own', 'unknown']);
  });
});
