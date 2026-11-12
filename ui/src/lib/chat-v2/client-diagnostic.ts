import type { ChatV2ClientState } from './sync-engine';
import type { TimelineItem } from './types';

export function clientDiagnosticFilename(conversationId: string | null): string {
  const id = conversationId?.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 128);
  return id ? `cognis-chat-diagnostic-${id}.json` : 'cognis-chat-diagnostic.json';
}

function itemMetadata(item: TimelineItem) {
  return {
    id: item.id,
    kind: item.kind,
    role: item.kind === 'message' ? item.role : null,
    status: item.status ?? null,
    stable: item.stable,
    sort_key: item.sort_key,
    created_at: item.created_at ?? null,
    updated_at: item.updated_at ?? null,
    turn_id: 'turn_id' in item ? item.turn_id ?? null : null,
    turn_cycle_index: 'turn_cycle_index' in item ? item.turn_cycle_index ?? null : null,
    assistant_phase_index: 'assistant_phase_index' in item ? item.assistant_phase_index ?? null : null,
    message_id: item.kind === 'message' ? item.message_id ?? null : null,
    client_message_id: item.kind === 'message' ? item.client_message_id ?? null : null,
    client_txn_id: item.kind === 'message' ? item.client_txn_id ?? null : null,
    call_id: item.kind === 'tool_call' ? item.call_id : null,
    previous_session_id: item.kind === 'compaction' ? item.previous_session_id ?? null : null,
    session_id: item.kind === 'compaction' ? item.session_id ?? null : null,
    source_refs: item.source_refs.map(ref => ({
      store: ref.store,
      session_id: ref.session_id,
      seq: ref.seq ?? null,
      event_id: ref.event_id ?? null,
      event_type: ref.event_type ?? null,
    })),
  };
}

/** Explicit metadata allowlist: never serialize the client state or timeline rows wholesale. */
export function captureClientDiagnostic(
  state: ChatV2ClientState,
  visibleItems: TimelineItem[],
  environment: { build: string; standalone: boolean; online: boolean },
) {
  const runtime = state.runtime;
  const authority = runtime?.authority;
  return {
    schema: 'cognis-chat-client-diagnostic-v1',
    captured_at: new Date().toISOString(),
    environment: {
      build: environment.build,
      standalone: environment.standalone,
      online: environment.online,
    },
    scope: {
      key: state.scopeKey,
      kind: state.scope?.kind ?? null,
      conversation_id: state.conversationId,
      session_id: state.scope?.session_id ?? null,
    },
    projection_version: state.projectionVersion,
    has_cursor: Boolean(state.cursor),
    has_more_before: state.hasMoreBefore,
    sync_status: state.syncStatus,
    active_session_id: typeof state.state?.active_session?.session_id === 'string'
      ? state.state.active_session.session_id : null,
    runtime: runtime ? {
      epoch: runtime.runtime_epoch,
      revision: runtime.runtime_revision,
      generated_at: runtime.generated_at,
      has_active_turn: runtime.has_active_turn,
      active_turn: runtime.active_turn ? {
        turn_id: runtime.active_turn.turn_id,
        primary_client_message_id: runtime.active_turn.primary_client_message_id ?? null,
        session_id: runtime.active_turn.session_id,
        status: runtime.active_turn.status,
      } : null,
      authority: authority ? {
        protocol: authority.protocol,
        direct_request_id: authority.direct_request_id,
        turn_id: authority.turn_id,
        fencing_token: authority.fencing_token,
        lifecycle: authority.lifecycle,
        source_epoch: authority.source_epoch,
        source_revision: authority.source_revision,
      } : null,
      volatile_items_complete: runtime.volatile_items_complete ?? false,
    } : null,
    admission_placements: Object.entries(state.admissionPlacements).map(([id, placement]) => ({
      client_message_id: id,
      placement: placement.placement,
      status: placement.status,
      queue_id: placement.queueId ?? null,
      client_txn_id: placement.clientTxnId ?? null,
      history_confirmed: placement.historyConfirmed === true,
      processing_outcome: placement.processingOutcome ? {
        status: placement.processingOutcome.status,
        turn_id: placement.processingOutcome.turnId,
        admission_order: placement.processingOutcome.admissionOrder,
        before_client_message_id: placement.processingOutcome.beforeClientMessageId,
      } : null,
    })),
    canonical_items: state.timelineItems.map(itemMetadata),
    local_items: state.localItems.map(itemMetadata),
    runtime_items: (runtime?.volatile_items ?? []).map(itemMetadata),
    visible_items: visibleItems.map(itemMetadata),
  };
}
