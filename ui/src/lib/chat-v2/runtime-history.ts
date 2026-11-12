import type { MessageTimelineItem, TimelineItem } from './types';

export const CARRIED_LINEAGE_PREFIX = '9996:';

export function isCarriedAssistant(item: TimelineItem): item is MessageTimelineItem & {
  stable: false;
  role: 'assistant';
  sort_key: `${typeof CARRIED_LINEAGE_PREFIX}${string}`;
} {
  return item.stable === false && item.kind === 'message' && item.role === 'assistant'
    && item.sort_key.startsWith(CARRIED_LINEAGE_PREFIX);
}

interface TurnProgress {
  turnId: string;
  cycle: number;
  phase: number;
}

function progressOf(item: TimelineItem): TurnProgress | null {
  if (
    item.kind !== 'tool_call'
    && item.kind !== 'thinking'
    && !(item.kind === 'message' && item.role === 'assistant')
  ) return null;
  const { turn_id: turnId, turn_cycle_index: cycle, assistant_phase_index: phase } = item;
  if (
    !turnId || !Number.isInteger(cycle) || !Number.isInteger(phase)
    || cycle === null || cycle === undefined || cycle < 0
    || phase === null || phase === undefined || phase < 0
  ) return null;
  return { turnId, cycle, phase };
}

/** Keep live/uncertain overlays, but let canonical backfill own older completed history. */
export function runtimeHistoryVisibility(canonicalItems: TimelineItem[]): (item: TimelineItem) => boolean {
  const canonicalIds = new Set(canonicalItems.map(item => item.id));
  const witnesses = new Map<string, TurnProgress[]>();
  for (const item of canonicalItems) {
    const progress = progressOf(item);
    if (!progress) continue;
    const turn = witnesses.get(progress.turnId);
    if (turn) turn.push(progress);
    else witnesses.set(progress.turnId, [progress]);
  }
  return item => {
    if (canonicalIds.has(item.id)) return true;
    // Advancing coordinates do not confirm an unsaved assistant phase.
    if (isCarriedAssistant(item)) return true;
    if (
      item.status !== 'complete' && item.status !== 'failed'
      && item.status !== 'cancelled' && item.status !== 'denied'
    ) return true;
    const progress = progressOf(item);
    if (!progress) return true;
    // A bounded recovery snapshot may omit earlier persisted cycles. Their
    // retained runtime copies have tail sort keys, not canonical positions.
    // Require one same-turn witness to advance BOTH explicit coordinates;
    // never infer order from missing metadata or independent maxima.
    return !witnesses.get(progress.turnId)?.some(
      witness => witness.cycle > progress.cycle && witness.phase > progress.phase
    );
  };
}
