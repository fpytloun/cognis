import { describe, expect, it } from 'vitest';
import { ChatV2Store } from './store.svelte';
import { applyBackfill, applySnapshot, applySyncResponse, emptyChatV2State, maybeApplyRuntime, visibleTimelineItems, type ChatV2ClientState } from './sync-engine';
import { conversationTimelineScope, sessionTimelineScope, type ChatSnapshot, type MessageTimelineItem, type TimelineItem, type ToolCallTimelineItem } from './types';

function fixture(): ChatV2ClientState {
  const scope = conversationTimelineScope('history-placement');
  const head: TimelineItem = {
    id: 'message:head', kind: 'message', role: 'assistant', message_id: 'head',
    content: 'Earlier execution', attachments: [], partial: false,
    stable: true, status: 'complete', turn_id: 'earlier-turn',
    sort_key: '0002:000000000000274:000188:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 274, event_type: 'assistant_message' }],
  };
  return {
    ...emptyChatV2State(), scope, scopeKey: scope.key, conversationId: 'history-placement',
    projectionVersion: 'chat-v2-test', hasMoreBefore: true, beforeCursor: 'history-window-before', syncStatus: 'ready',
    timelineItems: [head, {
      ...head, id: 'message:latest', message_id: 'latest', turn_id: 'current-turn',
      sort_key: '0003:000000000000030:000001:02:000000000', content: 'Latest execution',
      source_refs: [{ store: 'intaris', session_id: 'current-session', seq: 30, event_type: 'assistant_message' }],
    }],
    localItems: [{
      id: 'user:earlier-admission', kind: 'message', role: 'user',
      message_id: 'client:earlier-admission', client_message_id: 'earlier-admission',
      content: 'Earlier admission', attachments: [], partial: false,
      stable: false, status: 'complete', turn_id: 'earlier-turn', source_refs: [],
      sort_key: '9996:999999999999999:000000:00:000000000',
    }, {
      id: 'compaction:earlier', kind: 'compaction', status: 'compacted', stable: false,
      session_id: 'rotated-session', previous_session_id: 'previous-session', method: 'llm',
      turns_compacted: 2, summary_preview: '',
      sort_key: '9996:999999999999999:000158:10:000000000',
      source_refs: [{ store: 'runtime', session_id: 'rotated-session', seq: 0, event_type: 'compaction' }],
    }],
    runtime: {
      runtime_epoch: 'current-epoch', runtime_revision: 1, generated_at: '2026-01-02T00:00:00Z',
      has_active_turn: true, active_turn: { turn_id: 'current-turn', session_id: 'current-session', status: 'running' },
      volatile_items: [],
    },
  };
}

describe('carried interrupted assistant placement', () => {
  function carriedFixture(): ChatV2ClientState {
    const state = fixture();
    const old = state.timelineItems[0] as MessageTimelineItem;
    old.id = 'message:earlier-turn:phase:44';
    old.message_id = 'earlier-turn';
    old.sort_key = '0002:000000000000274:000044:02:000000000';
    old.assistant_phase_index = 44;
    old.turn_cycle_index = 18;
    state.timelineItems[1] = {
      ...old, id: 'message:current-turn:phase:2', message_id: 'current-turn', turn_id: 'current-turn',
      assistant_phase_index: 2, turn_cycle_index: 1,
      sort_key: '0002:000000000000295:000002:02:000000000',
      source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 295, event_type: 'assistant_message' }],
    };
    state.runtime!.active_turn!.session_id = 'rotated-session';
    state.localItems = [{
      ...old, id: 'message:earlier-turn:phase:47', stable: false, partial: false, status: 'complete',
      assistant_phase_index: 47, turn_cycle_index: 19, content: 'Retained interrupted response.',
      sort_key: '9996:999999999999999:000047:02:000000000',
      source_refs: [{ store: 'runtime', session_id: 'rotated-session', seq: 0, event_type: 'assistant_stream' }],
    }];
    return state;
  }

  it.each([true, false])('keeps carried execution with its closed turn across compaction (partial=%s)', hasMore => {
    const state = carriedFixture();
    state.hasMoreBefore = hasMore;
    state.timelineItems[0].sort_key = '0001:000000000000274:000044:02:000000000';
    state.timelineItems[0].source_refs[0].session_id = 'previous-session';
    const tool: ToolCallTimelineItem = {
      id: 'tool:interrupted-tool', call_id: 'interrupted-tool', kind: 'tool_call',
      tool_name: 'read', turn_id: 'earlier-turn', assistant_phase_index: 47, turn_cycle_index: 19,
      stable: false, status: 'complete', attachments: [], file_diffs: [], is_error: false,
      has_full_output: false, truncated: false,
      sort_key: '9996:999999999999999:000047:03:000000000',
      source_refs: [{ store: 'runtime', session_id: 'previous-session', seq: 0, event_type: 'tool_call' }],
    };
    const durableTool: ToolCallTimelineItem = {
      ...tool, id: 'tool:confirmed-tool', call_id: 'confirmed-tool', stable: true,
      assistant_phase_index: 46, turn_cycle_index: 18,
      sort_key: '0002:000000000000004:000046:03:000000000',
      source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 4, event_type: 'tool_result' }],
    };
    state.timelineItems.splice(1, 0, durableTool);
    const assistant = { ...state.localItems[0], source_refs: tool.source_refs };
    const thinking: TimelineItem = {
      id: 'thinking:earlier-turn:47', kind: 'thinking', blocks: [], stable: false, status: 'complete',
      turn_id: 'earlier-turn', assistant_phase_index: 47, turn_cycle_index: 19,
      sort_key: '9996:999999999999999:000047:01:000000000', source_refs: tool.source_refs,
    };
    state.localItems = [tool, assistant, thinking];
    const before = structuredClone(state);
    const visible = visibleTimelineItems(state);
    expect(visible.map(item => item.id)).toEqual([
      state.timelineItems[0].id, durableTool.id, thinking.id, assistant.id, tool.id, state.timelineItems[2].id,
    ]);
    expect(visible.find(item => item.id === assistant.id)).toMatchObject({ partial: true });
    expect(visible.find(item => item.id === tool.id)).toMatchObject({
      status: 'complete', stable: false, source_refs: tool.source_refs,
    });
    expect(state).toEqual(before);
    const store = new ChatV2Store();
    store.restoreState(state);
    store.restoreState(store.serializeState());
    expect(store.visibleItems.map(item => item.id)).toEqual(visible.map(item => item.id));
    expect(store.snapshot.localItems).toEqual(before.localItems);
  });

  it.each([true, false])('keeps the captured fallback beside its owning turn (partial history=%s)', hasMore => {
    const state = carriedFixture();
    state.hasMoreBefore = hasMore;
    const original = state.localItems[0];
    const canonical = [...state.timelineItems];
    const items = visibleTimelineItems(state);
    expect(items.map(item => item.id)).toEqual([canonical[0].id, original.id, canonical[1].id]);
    expect(items[1]).toMatchObject({ partial: true, content: 'Retained interrupted response.' });
    expect(state.localItems[0]).toBe(original);
    expect(original.sort_key.startsWith('9996:')).toBe(true);
    expect(original).toMatchObject({ partial: false });
    expect(state.timelineItems).toEqual(canonical);
  });

  it.each(['active', 'no-successor', 'wrong-session', 'interleaved'])(
    'preserves content without guessing an unsafe placement: %s',
    condition => {
      const state = carriedFixture();
      if (condition === 'active') state.runtime!.active_turn!.turn_id = 'earlier-turn';
      if (condition === 'no-successor') state.timelineItems.pop();
      if (condition === 'wrong-session') state.localItems[0].source_refs[0].session_id = 'other-session';
      if (condition === 'interleaved') state.timelineItems.push({
        ...state.timelineItems[0], id: 'message:earlier-turn:phase:45', assistant_phase_index: 45,
        sort_key: '0002:000000000000300:000045:02:000000000',
      } as MessageTimelineItem);
      const fallback = visibleTimelineItems(state).find(item => item.id === state.localItems[0].id);
      expect(fallback).toMatchObject({ partial: true, sort_key: state.localItems[0].sort_key });
    },
  );

  it('retires the fallback when its canonical assistant phase arrives', () => {
    const state = carriedFixture();
    state.timelineItems.splice(1, 0, {
      ...state.timelineItems[0], id: state.localItems[0].id,
      assistant_phase_index: 47, turn_cycle_index: 19,
      sort_key: '0002:000000000000280:000047:02:000000000',
    } as MessageTimelineItem);
    const items = visibleTimelineItems(state);
    const copies = items.filter(item => item.id === state.localItems[0].id);
    expect(copies).toHaveLength(1);
    expect(copies[0]).toMatchObject({ stable: true, assistant_phase_index: 47, turn_cycle_index: 19 });
  });

  it.each([true, false])('retains an unsaved phase before later same-turn progress (bounded=%s)', bounded => {
    const state = carriedFixture();
    const later = {
      ...state.timelineItems[0], id: 'message:earlier-turn:phase:48',
      assistant_phase_index: 48, turn_cycle_index: 20,
      sort_key: '0002:000000000000280:000048:02:000000000',
    } as MessageTimelineItem;
    if (bounded) state.timelineItems[0] = later;
    else state.timelineItems.splice(1, 0, later);
    const visible = visibleTimelineItems(state);
    const retained = visible.findIndex(item => item.id === state.localItems[0].id);
    const successor = visible.findIndex(item => item.id === later.id);
    expect(retained).toBeGreaterThanOrEqual(0);
    expect(retained).toBeLessThan(successor);
    expect(visible[retained]).toMatchObject({ partial: true, content: 'Retained interrupted response.' });
  });

  it('recomputes carried placement across repeated cache hydration without persisting fabricated provenance', () => {
    const state = carriedFixture();
    const store = new ChatV2Store();
    store.restoreState(state);
    for (let attempt = 0; attempt < 3; attempt++) {
      expect(store.visibleItems.map(item => item.id)).toEqual([
        state.timelineItems[0].id, state.localItems[0].id, state.timelineItems[1].id,
      ]);
      expect(store.visibleItems[1]).toMatchObject({ partial: true });
      const saved = store.serializeState();
      expect(saved.localItems[0].sort_key).toBe(state.localItems[0].sort_key);
      expect(saved.localItems[0].source_refs).toEqual(state.localItems[0].source_refs);
      store.restoreState(saved);
    }
  });

  it.each(['backfill', 'snapshot', 'sync'] as const)(
    'keeps an unconfirmed phase through real %s reconciliation until exact confirmation', transport => {
    const state = carriedFixture();
    state.cursor = 'carried-cursor-0';
    let revision = 0;
    const advance = (prior: ChatV2ClientState, items: TimelineItem[]): ChatV2ClientState => {
      const cursor = `carried-cursor-${++revision}`;
      const serverTime = '2026-10-25T11:00:00Z';
      if (transport === 'snapshot') {
        return applySnapshot({
          schema_version: 2, projection_version: 'chat-v2-test',
          conversation: { conversation_id: 'history-placement', agent_id: 'agent-1', status: 'active' },
          timeline: { items: [...prior.timelineItems, ...items], has_more_before: false },
          state: {
            state_version: revision, snapshot_generated_at: serverTime, capabilities: [],
            active_turn: {}, pending: {}, active_session: {}, task: null,
          },
          queue: { messages: [], queued_count: 0 },
          runtime: prior.runtime!, cursor, server_time: serverTime,
        }, prior);
      }
      if (transport === 'sync') {
        const result = applySyncResponse(prior, {
          schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'history-placement',
          cursor_before: prior.cursor!, cursor_after: cursor, server_time: serverTime,
          ops: items.map(item => ({ op: 'upsert_item', item })), runtime: null,
          reset_required: false, reset_reason: null, has_more: false,
        });
        expect(result.outcome).toBe('applied');
        return result.state;
      }
      return applyBackfill(prior, {
        schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'history-placement',
        server_time: serverTime, items, has_more_before: false,
      });
    };
    const retained = state.localItems[0];
    const later = {
      ...state.timelineItems[0], id: 'message:earlier-turn:phase:48',
      assistant_phase_index: 48, turn_cycle_index: 20,
      sort_key: '0002:000000000000280:000048:02:000000000',
      source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 280, event_type: 'assistant_message' }],
    } as MessageTimelineItem;
    const advanced = advance(state, [later]);
    expect(advanced.localItems.some(item => item.id === retained.id)).toBe(true);
    expect(advanced.localItems.find(item => item.id === retained.id)).toEqual(retained);
    expect(advanced.runtime).toEqual(state.runtime);
    expect(visibleTimelineItems(advanced).find(item => item.id === retained.id))
      .toMatchObject({ partial: true, content: 'Retained interrupted response.' });
    const canonical = {
      ...later, id: retained.id, assistant_phase_index: 47, turn_cycle_index: 19,
      sort_key: '0002:000000000000279:000047:02:000000000', content: 'Canonical recovered response.',
      source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 279, event_type: 'assistant_message' }],
    } as MessageTimelineItem;
    const confirmed = advance(advanced, [canonical]);
    expect(confirmed.localItems.some(item => item.id === retained.id)).toBe(false);
    expect(visibleTimelineItems(confirmed).filter(item => item.id === retained.id)).toHaveLength(1);
    expect(visibleTimelineItems(confirmed).find(item => item.id === retained.id)).toEqual(canonical);
  });
});

describe('local history boundary placement', () => {
  function activeFixture(): ChatV2ClientState {
    const state = fixture();
    state.timelineItems = state.timelineItems.slice(0, 1);
    state.localItems = state.localItems.slice(0, 1);
    state.localItems[0].sort_key = '9998:999999999999999:000000:00:000000000';
    state.runtime!.active_turn = {
      turn_id: 'earlier-turn', session_id: 'rotated-session', status: 'running',
      primary_client_message_id: 'earlier-admission',
    };
    state.runtime!.authority = {
      protocol: 'runtime_authority_v1', direct_request_id: 'request-earlier',
      turn_id: 'earlier-turn', fencing_token: 14, lifecycle: 'active',
      source_epoch: 'current-epoch', source_revision: 1,
    };
    state.runtime!.volatile_items_complete = true;
    state.runtime!.volatile_items = [{
      id: 'system:retry:earlier-turn', kind: 'message', role: 'system',
      message_id: 'retry:earlier-turn', turn_id: 'earlier-turn',
      content: 'Retry lineage notice', attachments: [], partial: false, stable: false, status: 'complete',
      sort_key: '9998:999999999999999:000000:09:000000000',
      source_refs: [{ store: 'runtime', session_id: 'rotated-session', seq: 0, event_type: 'system_message' }],
    }];
    return state;
  }

  it('places the running turn primary admission before execution without guessing retry notice chronology', () => {
    const state = activeFixture();
    const before = structuredClone(state);
    const visible = visibleTimelineItems(state);
    expect(visible.map(item => item.id)).toEqual([
      'user:earlier-admission', 'message:head', 'system:retry:earlier-turn',
    ]);
    expect(visible[1]).toBe(state.timelineItems[0]);
    expect(state).toEqual(before);
  });

  it('places a runtime primary admission once without changing its authoritative source', () => {
    const state = activeFixture();
    state.runtime!.volatile_items.push(structuredClone(state.localItems[0]));
    const visible = visibleTimelineItems(state);
    expect(visible.map(item => item.id)).toEqual([
      'user:earlier-admission', 'message:head', 'system:retry:earlier-turn',
    ]);
    expect(state.runtime!.volatile_items[1].sort_key).toMatch(/^9998:/);
  });

  it('does not infer primary identity from a shared active turn, nor move its queued follow-up', () => {
    const state = activeFixture();
    state.runtime!.active_turn!.primary_client_message_id = 'different-primary';
    const local = state.localItems[0];
    expect(visibleTimelineItems(state).find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
    state.runtime!.active_turn!.primary_client_message_id = 'earlier-admission';
    state.queue = { queued_count: 1, messages: [{
      queue_id: 'queued-admission', client_message_id: 'earlier-admission',
      content: '', attachments: [], position: 0,
    }] };
    expect(visibleTimelineItems(state).find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
  });

  it.each(['same-turn-retry', 'notice', 'retry-other-turn', 'pending', 'durable-source'])('preserves a %s until canonical history establishes notice chronology', kind => {
    const state = activeFixture();
    const notice = state.runtime!.volatile_items[0];
    if (kind === 'notice') notice.id = 'system:provider-retry:earlier-turn';
    if (kind === 'retry-other-turn' && notice.kind === 'message') notice.turn_id = 'other-turn';
    if (kind === 'pending') notice.status = 'pending';
    if (kind === 'durable-source') notice.source_refs[0] = {
      store: 'intaris', session_id: 'rotated-session', seq: 200, event_type: 'system_message',
    };
    expect(visibleTimelineItems(state).find(item => item.id === notice.id)?.sort_key).toBe(notice.sort_key);
  });

  it('preserves immutable primary identity across checkpoint frames but not an authority change', () => {
    const current = activeFixture().runtime!;
    const incoming = structuredClone(current);
    delete incoming.active_turn!.primary_client_message_id;
    incoming.authority!.source_revision = 2;
    expect(maybeApplyRuntime(current, incoming)?.active_turn?.primary_client_message_id).toBe('earlier-admission');
    incoming.authority!.fencing_token = 15;
    expect(maybeApplyRuntime(current, incoming)?.active_turn?.primary_client_message_id).toBeUndefined();
    expect(incoming.active_turn?.primary_client_message_id).toBeUndefined();
  });

  it('uses exact canonical admission and retry provenance when backfill reaches them', () => {
    const state = activeFixture();
    const canonical = [...state.localItems, ...state.runtime!.volatile_items].map((item, index) => ({
      ...item, stable: true,
      sort_key: `0002:00000000000027${index}:000000:00:000000000`,
      source_refs: [{ store: 'intaris' as const, session_id: 'rotated-session', seq: 270 + index,
        event_type: index ? 'system_message' : 'user_message' }],
    }));
    const recovered = applyBackfill(state, {
      schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'history-placement',
      server_time: '2026-01-02T00:00:00Z', items: canonical, has_more_before: false,
    });
    expect(visibleTimelineItems(recovered)).toEqual([...canonical, ...state.timelineItems]);
  });

  it('places captured settled admission and rotation fallbacks before the bounded window, without changing raw state', () => {
    const state = fixture();
    const before = structuredClone(state);
    const visible = visibleTimelineItems(state);
    expect(visible.map(item => item.id)).toEqual([
      ...state.localItems.map(item => item.id), ...state.timelineItems.map(item => item.id),
    ]);
    expect(visible.map(item => item.sort_key)).toEqual([...visible.map(item => item.sort_key)].sort());
    expect(visible.slice(2)).toEqual(state.timelineItems);
    expect(visible[0]).toEqual({ ...state.localItems[0], sort_key: '0002:000000000000273:000000:00:000000000' });
    expect(visible[1]).toEqual({ ...state.localItems[1], sort_key: '0002:000000000000273:000158:10:000000000' });
    expect(visibleTimelineItems(state)).toBe(visible);
    expect(state).toEqual(before);
  });

  it('recomputes after cache hydration and never serializes presentation keys', () => {
    const store = new ChatV2Store();
    store.restoreState(fixture());
    const expected = [...fixture().localItems, ...fixture().timelineItems].map(item => item.id);
    for (let i = 0; i < 3; i++) {
      expect(store.visibleItems.map(item => item.id)).toEqual(expected);
      const saved = store.serializeState();
      expect(saved.localItems.map(item => item.sort_key)).toEqual(fixture().localItems.map(item => item.sort_key));
      store.restoreState(saved);
    }
  });

  it('does not blink back to sentinel placement when a smaller fresh snapshot follows cache hydration', () => {
    const cached = fixture();
    const store = new ChatV2Store();
    const fresh: ChatSnapshot = {
      schema_version: 2, projection_version: 'chat-v2-test', scope: cached.scope!,
      conversation: { conversation_id: 'history-placement', agent_id: 'history-agent', status: 'active' },
      server_time: '2026-01-02T00:00:01Z', cursor: 'fresh-tail-cursor',
      timeline: { items: cached.timelineItems.slice(-1), has_more_before: true, before_cursor: 'before-fresh-tail' },
      state: { state_version: 1, snapshot_generated_at: '', capabilities: [], active_turn: {}, pending: {}, active_session: {} },
      runtime: cached.runtime!, queue: { messages: [], queued_count: 0 },
    };
    store.restoreState(cached);
    const expected = [...cached.localItems, ...cached.timelineItems].map(item => item.id);
    for (let i = 0; i < 3; i++) {
      expect(store.visibleItems.map(item => item.id)).toEqual(expected);
      store.replaceFromSnapshot(fresh);
      expect(store.visibleItems.map(item => item.id)).toEqual(expected);
      expect(store.snapshot.timelineItems).toEqual(cached.timelineItems);
      const saved = store.serializeState({ settleRuntimeOverlay: true });
      expect(saved.localItems.map(item => item.sort_key)).toEqual(cached.localItems.map(item => item.sort_key));
      store.restoreState(saved);
    }
  });

  it('lets incremental backfill replace the bucket with exact canonical order and provenance once', () => {
    const initial = fixture();
    visibleTimelineItems(initial);
    const canonical = initial.localItems.map((item, i) => ({
      ...item, stable: true,
      sort_key: i === 0 ? '0001:000000000000607:000000:00:000000000' : '0001:000000000001097:000158:10:000000000',
      source_refs: [{ store: 'intaris' as const, session_id: 'previous-session', seq: i === 0 ? 607 : 1097, event_type: i === 0 ? 'user_message' : 'lifecycle' }],
    }));
    const recovered = applyBackfill(initial, {
      schema_version: 2, projection_version: 'chat-v2-test', conversation_id: 'history-placement',
      server_time: '2026-01-02T00:00:00Z', items: canonical, has_more_before: false,
    });
    const visible = visibleTimelineItems(recovered);
    expect(visible.map(item => item.id)).toEqual([...canonical, ...initial.timelineItems].map(item => item.id));
    expect(visible.slice(0, 2)).toEqual(canonical);
  });

  const commonNoOps: [string, (state: ChatV2ClientState) => void][] = [
    ['exhausted history', state => { state.hasMoreBefore = false; }],
    ['session scope', state => { state.scope = sessionTimelineScope('rotated-session'); }],
    ['empty history', state => { state.timelineItems = []; }],
    ['malformed head key', state => { state.timelineItems[0].sort_key = '0002:274'; }],
    ['zero head sequence', state => { state.timelineItems[0].sort_key = '0002:000000000000000:000188:02:000000000'; }],
    ['non-durable head', state => { state.timelineItems[0].source_refs[0].store = 'runtime'; }],
    ['unstable head', state => { state.timelineItems[0].stable = false; }],
    ['stable locals', state => { state.localItems.forEach(item => { item.stable = true; }); }],
    ['non-sentinel sequence', state => { state.localItems.forEach(item => { item.sort_key = item.sort_key.replace('999999999999999', '000000000000001'); }); }],
    ['unrecognized band', state => { state.localItems.forEach(item => { item.sort_key = item.sort_key.replace('9996:', '9994:'); }); }],
    ['current runtime membership', state => { state.runtime!.volatile_items = structuredClone(state.localItems); }],
  ];
  it.each(commonNoOps)('does not infer placement from %s', (_label, modify) => {
    const state = fixture();
    modify(state);
    const visible = visibleTimelineItems(state);
    for (const local of state.localItems) {
      expect(visible.find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
    }
  });

  const admissionNoOps: [string, (state: ChatV2ClientState) => void][] = [
    ['pending', state => { state.localItems[0].status = 'pending'; }],
    ['unidentified', state => { if (state.localItems[0].kind === 'message') state.localItems[0].client_message_id = null; }],
    ['not admission-shaped', state => { if (state.localItems[0].kind === 'message') state.localItems[0].message_id = 'context-message'; }],
    ['missing turn', state => { if ('turn_id' in state.localItems[0]) state.localItems[0].turn_id = null; }],
    ['different head turn', state => { if ('turn_id' in state.localItems[0]) state.localItems[0].turn_id = 'unrelated-turn'; }],
    ['still-active head turn', state => { state.runtime!.active_turn!.turn_id = 'earlier-turn'; }],
    ['queued turn-ID admission', state => {
      state.admissionPlacements['earlier-admission'] = { placement: 'queue', status: 'complete' };
    }],
    ['explicit queue membership', state => {
      state.queue = { queued_count: 1, messages: [{ queue_id: 'queued-admission', client_message_id: 'earlier-admission', content: '', attachments: [], position: 0 }] };
    }],
    ['later user admission only', state => {
      if (state.timelineItems[1].kind === 'message') state.timelineItems[1].role = 'user';
    }],
    ['no later different-turn progress', state => { if ('turn_id' in state.timelineItems[1]) state.timelineItems[1].turn_id = 'earlier-turn'; }],
  ];
  it.each(admissionNoOps)('preserves a %s without moving it before prior execution', (_label, modify) => {
    const state = fixture();
    modify(state);
    const local = state.localItems[0];
    expect(visibleTimelineItems(state).find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
  });

  const compactionNoOps: [string, (state: ChatV2ClientState) => void][] = [
    ...(['running', 'failed', 'skipped'] as const).map(status => [status, (state: ChatV2ClientState) => { state.localItems[1].status = status; }] as [string, (state: ChatV2ClientState) => void]),
    ['same-session rotation', state => { if (state.localItems[1].kind === 'compaction') state.localItems[1].previous_session_id = 'rotated-session'; }],
    ['unknown previous session', state => { if (state.localItems[1].kind === 'compaction') state.localItems[1].previous_session_id = null; }],
    ['target session mismatch', state => { if (state.localItems[1].kind === 'compaction') state.localItems[1].session_id = 'unrelated-session'; }],
    ['ambiguous head session', state => {
      state.timelineItems[0].source_refs.push({ store: 'intaris', session_id: 'another-session', seq: 40, event_type: 'assistant_message' });
    }],
  ];
  it.each(compactionNoOps)('preserves %s compaction placement', (_label, modify) => {
    const state = fixture();
    modify(state);
    const local = state.localItems[1];
    expect(visibleTimelineItems(state).find(item => item.id === local.id)?.sort_key).toBe(local.sort_key);
  });
});
