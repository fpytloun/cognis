import { describe, expect, it } from 'vitest';
import { applyMessageAdmission, emptyChatV2State, visibleTimelineItems, type ChatV2ClientState } from './sync-engine';
import { ChatV2Store } from './store.svelte';
import { conversationTimelineScope, type MessageAdmissionV2Response, type MessageTimelineItem } from './types';

function fixture() {
  const scope = conversationTimelineScope('admissions-conversation');
  const state = {
    ...emptyChatV2State(), conversationId: 'admissions-conversation', scope, scopeKey: scope.key,
  };
  const local: MessageTimelineItem = {
    id: 'user:failed-input', kind: 'message', role: 'user',
    message_id: 'client:failed-input', client_message_id: 'failed-input',
    client_txn_id: 'failed-transaction', turn_id: 'failed-turn',
    content: 'continue', attachments: [], partial: false, status: 'complete', stable: false,
    source_refs: [], sort_key: '9996:999999999999999:000000:00:000000000',
  };
  const next: MessageTimelineItem = {
    ...local, id: 'user:next-input', message_id: 'client:next-input',
    client_message_id: 'next-input', client_txn_id: 'next-transaction',
    turn_id: 'next-turn', stable: true,
    sort_key: '0001:000000000000510:000000:00:000000000',
    source_refs: [
      { store: 'intaris', session_id: 'previous-session', seq: 510, event_type: 'user_message' },
      { store: 'intaris', session_id: 'rotated-session', seq: 9, event_type: 'user_message' },
    ],
  };
  const latest: MessageTimelineItem = {
    ...next, id: 'message:next-turn:phase:4', role: 'assistant',
    client_message_id: null, client_txn_id: null, message_id: 'next-turn',
    content: 'Newer canonical response', sort_key: '0002:000000000000100:000004:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 100, event_type: 'assistant_message' }],
  };
  state.timelineItems = [next, latest];
  state.localItems = [local];
  state.admissionPlacements['failed-input'] = {
    placement: 'timeline', status: 'complete', clientTxnId: 'failed-transaction',
  };
  const response: MessageAdmissionV2Response = {
    conversation_id: 'admissions-conversation', client_txn_id: 'failed-transaction',
    client_message_id: 'failed-input', turn_id: 'failed-turn',
    status: 'failed', started: false, admission_order: 22124,
    before_client_message_id: 'next-input',
  };
  return { state: { ...state }, local, next, response };
}

describe('accepted admission outcomes', () => {
  it.each(['cancelled', 'failed'] as const)('hides only the persisted off-window input, not its retained recovery bytes (%s)', status => {
    const { state, local, next, response } = fixture();
    const store = new ChatV2Store();
    store.restoreState(state);
    const retained = structuredClone(store.snapshot.localItems);
    store.applyAdmissionOutcome({ ...response, status, started: true, input_persisted: true });
    expect(store.visibleItems.map(item => item.id)).toEqual(['user:next-input', 'message:next-turn:phase:4']);
    expect(store.snapshot.localItems).toEqual(retained);
    expect(store.snapshot.timelineItems).toEqual(state.timelineItems);
    expect(store.snapshot.runtime).toBe(state.runtime);
    expect(store.snapshot.queue).toBe(state.queue);
    store.restoreState(store.serializeState());
    expect(store.visibleItems.some(item => item.id === local.id)).toBe(false);
    store.applyAdmissionOutcome({ ...response, status, started: true, input_persisted: false });
    expect(store.visibleItems.some(item => item.id === local.id)).toBe(false);
    store.restoreState({ ...store.snapshot, timelineItems: [
      { ...local, stable: true, source_refs: next.source_refs.map(ref => ({ ...ref, seq: 90 })),
        sort_key: '0000:000000000000090:000000:00:000000000' },
      ...state.timelineItems,
    ] });
    expect(store.visibleItems.filter(item => item.id === local.id)).toHaveLength(1);
    expect(store.visibleItems[0]).toMatchObject({ stable: true });
    expect(store.snapshot.localItems).toEqual(retained);
  });

  it.each(['completed', 'absorbed'] as const)('does not replay a processed legacy input at the tail (%s)', status => {
    const { state, local, next, response } = fixture();
    state.admissionPlacements = {};
    state.localItems = [{ ...local, client_txn_id: null, turn_id: undefined }];
    const store = new ChatV2Store();
    store.restoreState(state);
    expect(store.visibleItems.at(-1)?.id).toBe(local.id);
    store.applyAdmissionOutcome({ ...response, status, started: status === 'completed' });
    expect(store.visibleItems.map(item => item.id)).toEqual(state.timelineItems.map(item => item.id));
    expect(store.snapshot.localItems[0]).toMatchObject({ id: local.id, content: 'continue' });
    store.restoreState(store.serializeState());
    expect(store.visibleItems.map(item => item.id)).toEqual(state.timelineItems.map(item => item.id));
    store.restoreState({ ...store.snapshot, timelineItems: [
      { ...local, stable: true, source_refs: next.source_refs, sort_key: '0000:000000000000100:000000:00:000000000' },
      ...state.timelineItems,
    ] });
    expect(store.visibleItems.filter(item => item.id === local.id)).toHaveLength(1);
    expect(store.visibleItems[0]).toMatchObject({ stable: true, source_refs: next.source_refs });
  });

  it.each([true, false])('settles legacy completion without a recorded start (transaction metadata: %s)', linked => {
    const { state, local, response } = fixture();
    if (!linked) {
      state.admissionPlacements = {};
      state.localItems = [{ ...local, client_txn_id: null }];
    }
    const retained = structuredClone(state.localItems);
    const store = new ChatV2Store();
    store.restoreState(state);
    expect(store.visibleItems.at(-1)?.id).toBe(local.id);
    store.applyAdmissionOutcome({
      ...response, status: 'completed', started: false, before_client_message_id: null,
    });
    expect(store.visibleItems.map(item => item.id)).toEqual(['user:next-input', 'message:next-turn:phase:4']);
    expect(store.snapshot.localItems).toEqual(retained);
    expect(store.snapshot.timelineItems).toEqual(state.timelineItems);
    expect(store.snapshot.runtime).toBe(state.runtime);
    expect(store.snapshot.queue).toBe(state.queue);
    store.restoreState(store.serializeState());
    expect(store.visibleItems.map(item => item.id)).toEqual(['user:next-input', 'message:next-turn:phase:4']);
    expect(store.snapshot.localItems).toEqual(retained);
  });

  it('does not resurrect a confirmed copy when Force refresh narrows history', () => {
    const { state, local, next } = fixture();
    const canonical = { ...local, stable: true, source_refs: next.source_refs,
      sort_key: '0000:000000000000100:000000:00:000000000' };
    const unconfirmed = { ...local, id: 'user:unconfirmed', message_id: 'client:unconfirmed',
      client_message_id: 'unconfirmed', client_txn_id: null };
    const store = new ChatV2Store();
    store.restoreState({ ...state, cursor: 'old', projectionVersion: 'test',
      timelineItems: [canonical, ...state.timelineItems], localItems: [local, unconfirmed] });
    const retained = structuredClone(store.snapshot.localItems);
    store.replaceFromSnapshot({
      schema_version: 2, projection_version: 'test', scope: state.scope!,
      conversation: { conversation_id: state.conversationId!, agent_id: 'fixture', status: 'active' },
      cursor: 'new', server_time: '',
      timeline: { items: state.timelineItems, has_more_before: true, before_cursor: 'before' },
      state: { state_version: 1, snapshot_generated_at: '', capabilities: [],
        active_turn: {}, pending: {}, active_session: {}, task: null },
      queue: { messages: [], queued_count: 0 },
      runtime: { runtime_epoch: 'test', runtime_revision: 0, generated_at: '',
        has_active_turn: false, active_turn: null, volatile_items: [] },
    }, true);
    expect(store.visibleItems.map(item => item.id)).toEqual([...state.timelineItems.map(item => item.id), unconfirmed.id]);
    expect(store.snapshot.localItems).toEqual(retained);
    store.restoreState(store.serializeState());
    expect(store.visibleItems.some(item => item.id === local.id)).toBe(false);
    expect(store.visibleItems.some(item => item.id === unconfirmed.id)).toBe(true);
  });

  it.each(['failed', 'cancelled'] as const)('recovers an ordinary queued optimistic input without a turn (%s)', status => {
    const { state, response } = fixture();
    const store = new ChatV2Store();
    store.restoreState({ ...state, localItems: [], admissionPlacements: {} });
    store.addOptimisticUser({ clientMessageId: 'failed-input', content: 'Ordinary queued input' });
    store.applySend({
      status: 'queued', conversation_id: state.conversationId!, client_message_id: 'failed-input',
      client_txn_id: response.client_txn_id, queue_id: 'processing-queue', server_time: '',
    });
    expect((store.snapshot.localItems[0] as MessageTimelineItem).turn_id).toBeUndefined();
    expect(store.visibleItems.some(item => item.id === 'user:failed-input')).toBe(false);
    expect(store.visibleQueue.queued_count).toBe(1);
    store.restoreState(applyMessageAdmission(store.snapshot, { ...response, status }));
    expect(store.snapshot.admissionPlacements['failed-input'].processingOutcome).toMatchObject({ status });
    expect(store.visibleQueue.queued_count).toBe(0);
    expect(store.visibleItems[0]).toMatchObject({ id: 'user:failed-input', status, content: 'Ordinary queued input' });
    expect((store.snapshot.localItems[0] as MessageTimelineItem).turn_id).toBeUndefined();
    store.restoreState(store.serializeState());
    expect(store.visibleItems[0]).toMatchObject({ id: 'user:failed-input', status });
    expect(store.visibleQueue.queued_count).toBe(0);
  });

  it('recovers consecutive queued failures and stops in durable admission order after rotation', () => {
    const { state, local, response } = fixture();
    const inputs = ['queued-first', 'queued-second', 'queued-third', 'queued-fourth'];
    let resolved: ChatV2ClientState = {
      ...state,
      localItems: inputs.map(id => ({
        ...local, id: `user:${id}`, message_id: `client:${id}`, client_message_id: id,
        client_txn_id: `transaction:${id}`, turn_id: `turn:${id}`,
      })),
      admissionPlacements: Object.fromEntries(inputs.map(id => [id, {
        placement: 'queue', status: 'complete', clientTxnId: `transaction:${id}`, queueId: `queue:${id}`,
      }])),
    };
    const retained = structuredClone(resolved.localItems);
    for (const id of [...inputs].reverse()) {
      const index = inputs.indexOf(id);
      resolved = applyMessageAdmission(resolved, {
        ...response, client_message_id: id, client_txn_id: `transaction:${id}`, turn_id: `turn:${id}`,
        status: index % 2 ? 'cancelled' : 'failed', admission_order: 22124 + index,
      });
    }
    const visible = visibleTimelineItems(resolved);
    expect(visible.map(item => item.id)).toEqual([
      ...inputs.map(id => `user:${id}`), 'user:next-input', 'message:next-turn:phase:4',
    ]);
    expect(visible.slice(0, 4).map(item => item.status)).toEqual(['failed', 'cancelled', 'failed', 'cancelled']);
    expect(visible.slice(0, 4).every(item => item.kind === 'message' && item.content === local.content
      && !item.stable && item.source_refs.length === 0)).toBe(true);
    expect(resolved.localItems).toEqual(retained);
    expect(resolved.timelineItems).toEqual(state.timelineItems);
    const store = new ChatV2Store();
    store.restoreState(resolved);
    store.restoreState(store.serializeState());
    expect(store.visibleItems.map(item => item.id)).toEqual(visible.map(item => item.id));
    expect(store.snapshot.localItems).toEqual(retained);
    expect(store.snapshot.admissionPlacements[inputs[0]].placement).toBe('queue');
  });

  it('never resurrects a deleted admission from a delayed processing receipt', () => {
    const { state, response } = fixture();
    const deleted: ChatV2ClientState = {
      ...state, admissionPlacements: {
        'failed-input': { ...state.admissionPlacements['failed-input'], placement: 'deleted' },
      },
    };
    expect(applyMessageAdmission(deleted, response)).toBe(deleted);
  });

  it('places the captured unstarted failure before its proven later admission across rotation', () => {
    const { state, local, response } = fixture();
    const resolved = applyMessageAdmission(state, response);
    const visible = visibleTimelineItems(resolved);
    expect(visible.map(item => item.id)).toEqual([
      'user:failed-input', 'user:next-input', 'message:next-turn:phase:4',
    ]);
    expect(visible[0]).toMatchObject({
      status: 'failed', content: 'continue', stable: false, source_refs: [],
    });
    expect(resolved.localItems[0]).toEqual(local);
    expect(resolved.timelineItems).toEqual(state.timelineItems);
    expect(resolved.runtime).toBe(state.runtime);
    expect(resolved.queue).toBe(state.queue);
  });

  it('restores the disposition without turning an accepted failure into an unsent retry', () => {
    const { state, response } = fixture();
    const store = new ChatV2Store();
    store.restoreState(applyMessageAdmission(state, response));
    expect(visibleTimelineItems(store.snapshot)[0]).toMatchObject({ status: 'failed' });
    expect(store.snapshot.admissionPlacements['failed-input'].status).toBe('complete');
    expect(store.snapshot.queue).toBeNull();
  });

  it('uses the exact transaction receipt when no runtime turn ID was delivered', () => {
    const { state, local, response } = fixture();
    state.localItems = [{ ...local, turn_id: null }];
    expect(visibleTimelineItems(applyMessageAdmission(state, response))[0]).toMatchObject({
      id: local.id, status: 'failed', turn_id: null,
    });
    state.localItems = [{ ...local, client_txn_id: 'newer-transaction' }];
    expect(applyMessageAdmission(state, response)).toBe(state);
  });

  it('marks the failure truthfully but never guesses a position without a canonical successor', () => {
    const { state, local, response } = fixture();
    const resolved = applyMessageAdmission(state, { ...response, before_client_message_id: 'missing-input' });
    const item = visibleTimelineItems(resolved).find(item => item.id === local.id);
    expect(item).toMatchObject({ status: 'failed', sort_key: local.sort_key, content: local.content });
  });

  it('does not deduplicate two equal message bodies or invent a boundary for an already started turn', () => {
    const { state, local, response } = fixture();
    const resolved = applyMessageAdmission(state, { ...response, started: true });
    expect(visibleTimelineItems(resolved)).toHaveLength(3);
    expect(visibleTimelineItems(resolved).find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
  });

  it.each([
    { conversation_id: 'different-conversation' },
    { client_txn_id: 'different-transaction' },
    { turn_id: 'different-turn' },
    { status: 'running' as const },
  ])('rejects stale or unrelated outcomes: %j', change => {
    const { state, response } = fixture();
    expect(applyMessageAdmission(state, { ...response, input_persisted: true, ...change })).toBe(state);
  });

  it('always yields to exact canonical confirmation, even after a failed receipt', () => {
    const { state, local, response } = fixture();
    const resolved = applyMessageAdmission(state, response);
    resolved.timelineItems = [{
      ...local, stable: true, status: 'complete',
      sort_key: '0001:000000000000509:000000:00:000000000',
      source_refs: [{ store: 'intaris', session_id: 'previous-session', seq: 509, event_type: 'user_message' }],
    }, ...resolved.timelineItems];
    const visible = visibleTimelineItems(resolved).filter(item => item.id === local.id);
    expect(visible).toHaveLength(1);
    expect(visible[0]).toMatchObject({ stable: true, status: 'complete' });
  });
});
