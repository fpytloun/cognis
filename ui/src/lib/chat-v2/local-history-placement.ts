import type { ChatV2ClientState } from './sync-engine';
import type { TimelineItem } from './types';
import { CARRIED_LINEAGE_PREFIX, isCarriedAssistant } from './runtime-history';

const sortKeyPattern = /^(\d{4}):(\d{15}):(\d{6}):(\d{2}):(\d{9})$/;
function turnId(item: TimelineItem): string | null {
  return 'turn_id' in item ? item.turn_id ?? null : null;
}

function isExecution(item: TimelineItem): item is Extract<TimelineItem, { kind: 'tool_call' | 'thinking' | 'message' }> {
  return item.kind === 'tool_call' || item.kind === 'thinking'
    || (item.kind === 'message' && item.role === 'assistant');
}

function isDurable(item: TimelineItem): boolean {
  return item.stable && item.source_refs.length > 0
    && item.source_refs.every(ref => ref.store === 'intaris' && Number.isSafeInteger(ref.seq) && ref.seq > 0);
}

function failedAdmissionPlacement(
  state: ChatV2ClientState,
  canonicalIds: ReadonlySet<string>,
  canonicalClientIds: ReadonlySet<string>,
): (item: TimelineItem) => TimelineItem {
  const anchors = new Map<string, TimelineItem | null>();
  for (const item of state.timelineItems) {
    if (item.kind !== 'message' || item.role !== 'user' || !item.client_message_id || !isDurable(item)) continue;
    anchors.set(item.client_message_id, anchors.has(item.client_message_id) ? null : item);
  }
  return item => {
    if (state.scope?.kind !== 'conversation' || item.kind !== 'message' || item.role !== 'user'
      || item.stable !== false || !item.client_message_id || canonicalIds.has(item.id)
      || canonicalClientIds.has(item.client_message_id)) return item;
    const outcome = state.admissionPlacements[item.client_message_id]?.processingOutcome;
    if (!outcome || (item.turn_id && outcome.turnId !== item.turn_id)
      || (outcome.status !== 'failed' && outcome.status !== 'cancelled')) return item;
    const failed = { ...item, status: outcome.status };
    const anchor = outcome.beforeClientMessageId ? anchors.get(outcome.beforeClientMessageId) : null;
    if (!anchor) return failed;
    const key = sortKeyPattern.exec(anchor.sort_key);
    if (!key || Number(key[2]) <= 0 || !anchor.source_refs.some(ref => ref.seq === Number(key[2]))
      || !Number.isSafeInteger(outcome.admissionOrder) || outcome.admissionOrder < 0) return failed;
    // This is a view-only slot after all events preceding the proven successor,
    // not a fabricated canonical sequence, source reference or timestamp order.
    const prefix = `${key[1]}:${String(Number(key[2]) - 1).padStart(15, '0')}`;
    return {
      ...failed,
      sort_key: `${prefix}:999999:99:${String(outcome.admissionOrder).padStart(20, '0')}`,
    };
  };
}

function carriedExecutionPlacement(
  state: ChatV2ClientState,
  canonicalIds: ReadonlySet<string>,
): (item: TimelineItem) => TimelineItem {
  const canonical = state.timelineItems;
  const activeTurn = state.runtime?.has_active_turn ? state.runtime.active_turn?.turn_id : null;
  const runs = new Map<string, { run: number; interleaved: boolean }>();
  const tails = new Map<string, { index: number; run: number }>();
  const executionPoints = new Map<string, { index: number; phase: number; cycle: number }[]>();
  const sessions = new Map<string, Set<string>>();
  let run = 0;
  let previous: string | null = null;
  canonical.forEach((item, index) => {
    const turn = turnId(item);
    if (!turn || !isDurable(item)) return;
    const turnSessions = sessions.get(turn) ?? new Set<string>();
    for (const ref of item.source_refs) turnSessions.add(ref.session_id);
    sessions.set(turn, turnSessions);
    if (isExecution(item)) {
      if (turn !== previous) { run += 1; previous = turn; }
      const old = runs.get(turn);
      runs.set(turn, { run, interleaved: Boolean(old && (old.interleaved || old.run !== run)) });
      const phase = item.assistant_phase_index;
      const cycle = item.turn_cycle_index;
      if (phase != null && cycle != null && Number.isSafeInteger(phase) && Number.isSafeInteger(cycle)
          && phase >= 0 && cycle >= 0) {
        const points = executionPoints.get(turn) ?? [];
        points.push({ index, phase, cycle });
        executionPoints.set(turn, points);
      }
    }
    tails.set(turn, { index, run });
  });
  const placements = new Map<string, { key: string; tail: string; ceiling: string; sessions: Set<string> }>();
  for (const [turn, tail] of tails) {
    const execution = runs.get(turn);
    if (!execution || execution.interleaved || execution.run >= run
        || tail.run !== execution.run || turn === activeTurn) continue;
    const item = canonical[tail.index];
    const key = sortKeyPattern.exec(item.sort_key);
    const ceiling = canonical[tail.index + 1]?.sort_key;
    if (key && ceiling) placements.set(turn, {
      key: `${key[1]}:${key[2]}:999999:99`,
      tail: item.sort_key,
      ceiling,
      sessions: sessions.get(turn)!,
    });
  }
  return item => {
    if (!isExecution(item) || item.stable !== false || !item.sort_key.startsWith(CARRIED_LINEAGE_PREFIX)
      || canonicalIds.has(item.id)) return item;
    // Preserve unconfirmed text, but never present it as a durable completion.
    const retained = isCarriedAssistant(item) ? { ...item, partial: true } : item;
    const carriedKey = sortKeyPattern.exec(item.sort_key);
    let placement = item.turn_id ? placements.get(item.turn_id) : undefined;
    const phase = item.assistant_phase_index;
    if (!placement || !carriedKey || phase == null || !Number.isSafeInteger(phase) || phase < 0) return retained;
    const cycle = item.turn_cycle_index;
    const successor = cycle == null ? undefined : executionPoints.get(item.turn_id!)?.find(
      point => point.phase > phase && point.cycle >= cycle,
    );
    if (successor) {
      const before = canonical[successor.index - 1];
      const next = canonical[successor.index];
      const key = sortKeyPattern.exec((before ?? next).sort_key);
      if (!key || (before && !isDurable(before)) || (!before && Number(key[2]) <= 0)) return retained;
      const prefix = before ? `${key[1]}:${key[2]}`
        : `${key[1]}:${String(Number(key[2]) - 1).padStart(15, '0')}`;
      placement = {
        key: `${prefix}:999999:99`,
        tail: before?.sort_key ?? '',
        ceiling: next.sort_key,
        sessions: sessions.get(item.turn_id!)!,
      };
    }
    if (!item.source_refs.length
        || !item.source_refs.every(ref => ref.store === 'runtime' && placement.sessions.has(ref.session_id))) return retained;
    const sort_key = `${placement.key}:${String(phase).padStart(9, '0')}:${carriedKey[4]}:${carriedKey[5]}`;
    // A turn-tail presentation bucket does not invent canonical provenance or
    // move queued admissions. Interleaved or unproven histories stay untouched.
    if (sort_key <= placement.tail || sort_key >= placement.ceiling) return retained;
    return { ...retained, sort_key };
  };
}

/** Place proven historical fallbacks before a partial window, not after live work.
 * This is a presentation bucket, not recovered canonical provenance or chronology.
 * Never pass these copies back into reconciliation or persistence.
 */
export function localHistoryPlacement(
  state: ChatV2ClientState,
  canonicalIds: ReadonlySet<string>,
  canonicalClientIds: ReadonlySet<string>,
): (item: TimelineItem) => TimelineItem {
  const placeExecution = carriedExecutionPlacement(state, canonicalIds);
  const placeFailed = failedAdmissionPlacement(state, canonicalIds, canonicalClientIds);
  const placeCarried = (item: TimelineItem): TimelineItem => placeFailed(placeExecution(item));
  const head = state.timelineItems[0];
  if (state.scope?.kind !== 'conversation' || !state.hasMoreBefore || !head || !isDurable(head)) return placeCarried;
  const headKey = sortKeyPattern.exec(head.sort_key);
  if (!headKey || Number(headKey[2]) <= 0) return placeCarried;

  const headTurn = turnId(head);
  const activeTurn = state.runtime?.has_active_turn ? state.runtime.active_turn?.turn_id : null;
  const headTurnIsClosed = Boolean(headTurn && headTurn !== activeTurn && isExecution(head)
    && state.timelineItems.slice(1).some(item =>
      isDurable(item) && isExecution(item) && turnId(item) && turnId(item) !== headTurn));
  const headSessions = new Set(head.source_refs.map(ref => ref.session_id));
  const headSession = headSessions.size === 1 ? head.source_refs[0].session_id : null;
  const runtimeIds = new Set(state.runtime?.volatile_items.map(item => item.id));
  const queuedClientIds = new Set(state.queue?.messages.map(message => message.client_message_id));
  const prefix = `${headKey[1]}:${String(Number(headKey[2]) - 1).padStart(15, '0')}`;

  return item => {
    const carried = placeCarried(item);
    if (carried !== item) return carried;
    if (item.stable !== false || canonicalIds.has(item.id)) return item;
    const key = sortKeyPattern.exec(item.sort_key);
    if (!key || !/^999[5-9]$/.test(key[1]) || key[2] !== '999999999999999') return item;

    let beforeWindow = false;
    if (item.kind === 'message' && item.role === 'user') {
      const clientId = item.client_message_id;
      // Follow-ups can share the running turn ID; only the durable request's
      // primary admission is known to precede that turn's execution.
      const primaryAdmission = activeTurn === headTurn && isExecution(head)
        && clientId === state.runtime?.active_turn?.primary_client_message_id;
      beforeWindow = Boolean(
        clientId && item.id === `user:${clientId}` && item.message_id === `client:${clientId}`
        && item.status === 'complete' && item.turn_id === headTurn
        && (primaryAdmission || (headTurnIsClosed && !runtimeIds.has(item.id)))
        && !canonicalClientIds.has(clientId) && !queuedClientIds.has(clientId)
        && (!state.admissionPlacements[clientId] || state.admissionPlacements[clientId].placement === 'timeline'),
      );
    } else if (item.kind === 'compaction') {
      beforeWindow = !runtimeIds.has(item.id) && item.status === 'compacted' && Boolean(item.previous_session_id)
        && item.previous_session_id !== item.session_id && Boolean(headSession) && item.session_id === headSession;
    }
    if (!beforeWindow) return item;

    // Suffixes retain deterministic fallback ordering, not inferred event order.
    const sort_key = `${prefix}:${key[3]}:${key[4]}:${key[5]}`;
    return sort_key < head.sort_key ? { ...item, sort_key } : item;
  };
}
