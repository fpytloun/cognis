import { describe, expect, it } from 'vitest';
import { captureClientDiagnostic, clientDiagnosticFilename } from './client-diagnostic';
import { emptyChatV2State, type ChatV2ClientState } from './sync-engine';
import { conversationTimelineScope, type TimelineItem } from './types';

const environment = { build: 'diagnostic-build', standalone: true, online: true };
const privateText = 'private-diagnostic-test-content';
function fixture(): ChatV2ClientState {
  const state = emptyChatV2State();
  const scope = conversationTimelineScope('diagnostic-conversation');
  const canonical: TimelineItem = {
    id: 'user:accepted', kind: 'message', role: 'user', message_id: 'accepted',
    client_message_id: 'accepted', content: privateText, attachments: [], partial: false,
    stable: true, sort_key: '0001:000000000000607:000000:00:000000184',
    source_refs: [{
      store: 'intaris', session_id: 'old-session', seq: 607, event_type: 'user_message',
    }],
    turn_id: 'old-turn', created_at: '2026-01-01T15:37:00Z',
  };
  return {
    ...state, scope, scopeKey: scope.key, conversationId: 'diagnostic-conversation',
    projectionVersion: 'diagnostic-projection', cursor: privateText,
    syncStatus: 'ready', hasMoreBefore: true,
    timelineItems: [canonical],
    localItems: [{
      ...canonical, stable: false, source_refs: [],
      sort_key: '9995:000000000000001:000000:00:000000000',
    }],
    admissionPlacements: {
      accepted: {
        placement: 'timeline', status: 'complete', updatedAt: '2026-01-01T15:37:00Z',
        content: privateText, attachments: [], queueId: 'queued-admission',
        clientTxnId: 'accepted-transaction', historyConfirmed: true,
        processingOutcome: {
          status: 'completed', turnId: 'old-turn', admissionOrder: 1, beforeClientMessageId: null,
        },
      },
    },
    runtime: {
      runtime_epoch: 'runtime-epoch', runtime_revision: 5, generated_at: '2026-01-01T20:00:00Z',
      has_active_turn: true,
      active_turn: { turn_id: 'current-turn', session_id: 'current-session', status: 'running' },
      authority: {
        protocol: 'runtime_authority_v1', direct_request_id: 'current-request', turn_id: 'current-turn',
        fencing_token: 4, lifecycle: 'active', source_epoch: 'source-epoch', source_revision: 2,
      },
      volatile_items: [{
        id: 'tool:running', kind: 'tool_call', call_id: 'running', tool_name: 'read',
        stable: false, status: 'running', sort_key: '9998:000000000000001:000000:03:000000000',
        source_refs: [], turn_id: 'current-turn', turn_cycle_index: 9, assistant_phase_index: 8,
        arguments: { content: privateText }, arguments_preview: privateText, result_preview: privateText,
        streamed_output: privateText, attachments: [], file_diffs: [],
        is_error: false, truncated: false, has_full_output: false,
        evaluation: { secret: privateText }, delegation: { result: privateText },
      }],
      cycle_states: [], boundary_receipts: [], volatile_items_complete: false,
    },
  };
}

describe('metadata-only client diagnostic', () => {
  it('includes the conversation identifier in a safe JSON filename', () => {
    expect(clientDiagnosticFilename('diagnostic-conversation')).toBe('cognis-chat-diagnostic-diagnostic-conversation.json');
    expect(clientDiagnosticFilename('conversation/with\nseparators')).toBe('cognis-chat-diagnostic-conversation_with_separators.json');
    expect(clientDiagnosticFilename(null)).toBe('cognis-chat-diagnostic.json');
  });

  it('separates canonical, local, runtime and actual visible ordering', () => {
    const state = fixture();
    const visible = [state.timelineItems[0], state.runtime!.volatile_items[0], state.localItems[0]];
    const capture = captureClientDiagnostic(state, visible, environment);
    expect(capture.canonical_items[0].source_refs[0].seq).toBe(607);
    expect(capture.local_items[0].sort_key.startsWith('9995')).toBe(true);
    expect(capture.runtime_items[0]).toMatchObject({
      id: 'tool:running', turn_id: 'current-turn', turn_cycle_index: 9, assistant_phase_index: 8,
    });
    expect(capture.visible_items.map(item => item.id)).toEqual(visible.map(item => item.id));
    expect(capture.projection_version).toBe('diagnostic-projection');
    expect(capture.runtime?.authority?.fencing_token).toBe(4);
    expect(capture.admission_placements[0]).toMatchObject({
      client_txn_id: 'accepted-transaction', history_confirmed: true,
      processing_outcome: { status: 'completed', turn_id: 'old-turn', admission_order: 1 },
    });
    expect(capture.environment).toEqual(environment);
  });

  it('excludes contents, tool data, admission text, cursor and unknown extensions', () => {
    const state = fixture();
    Object.assign(state, { lastError: privateText, future_extension: { secret: privateText } });
    Object.assign(state.scope!, { title: privateText });
    Object.assign(state.timelineItems[0], { metadata: { secret: privateText } });
    Object.assign(state.timelineItems[0].source_refs[0], { future_secret: privateText });
    Object.assign(state.runtime!.authority!, { token: privateText });
    Object.assign(state.runtime!.active_turn!, { waiting_reason: privateText });
    Object.assign(state.admissionPlacements.accepted.processingOutcome!, { future_secret: privateText });
    const result = JSON.stringify(captureClientDiagnostic(
      state, [...state.timelineItems, ...state.localItems, ...state.runtime!.volatile_items], environment,
    ));
    expect(result).not.toContain(privateText);
    expect(result).not.toContain('future_extension');
    expect(result).not.toContain('streamed_output');
    expect(result).not.toContain('arguments_preview');
    expect(result).not.toContain('waiting_reason');
    expect(result).not.toContain('"content"');
    expect(result).not.toContain('"cursor"');
  });

  it('includes compaction identity and lineage without its summary', () => {
    const state = fixture();
    const compaction: TimelineItem = {
      id: 'compaction:earlier', kind: 'compaction',
      stable: false, status: 'compacted', sort_key: '9995:000000000000002:000000:10:000000000',
      source_refs: [], previous_session_id: 'old-session', session_id: 'current-session',
      summary: privateText, summary_preview: privateText, method: 'llm', turns_compacted: 2,
    };
    state.localItems.push(compaction);
    const capture = captureClientDiagnostic(state, state.localItems, environment);
    expect(capture.local_items[1]).toMatchObject({
      id: compaction.id, status: 'compacted', previous_session_id: 'old-session', session_id: 'current-session',
    });
    expect(JSON.stringify(capture)).not.toContain(privateText);
  });

  it('does not mutate state or retain mutable source-reference objects', () => {
    const state = fixture();
    const before = structuredClone(state);
    const capture = captureClientDiagnostic(state, state.timelineItems, environment);
    expect(state).toEqual(before);
    state.timelineItems[0].source_refs[0].seq = 900;
    state.timelineItems[0].sort_key = 'changed-after-capture';
    expect(capture.canonical_items[0].source_refs[0].seq).toBe(607);
    expect(capture.canonical_items[0].sort_key).toBe(before.timelineItems[0].sort_key);
  });

  it('handles empty and inactive state without serializing arbitrary session metadata', () => {
    const state = emptyChatV2State();
    state.state = {
      state_version: 1, snapshot_generated_at: '2026-01-01T20:00:00Z',
      active_session: { session_id: { secret: privateText }, todos: [privateText] },
      capabilities: [], active_turn: {}, pending: {},
    };
    const capture = captureClientDiagnostic(state, [], environment);
    expect(capture.runtime).toBeNull();
    expect(capture.active_session_id).toBeNull();
    expect(capture.visible_items).toEqual([]);
    expect(JSON.stringify(capture)).not.toContain(privateText);
  });
});
