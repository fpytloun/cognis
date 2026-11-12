import type { TaskDeliverableFreshness } from '$lib/types/api';

const STOPPED_STEP_STATUSES = new Set(['failed', 'cancelled', 'paused', 'waiting', 'rejected', 'superseded']);

/**
 * One line that relates a step's deliverable to the step's own state.
 *
 * Freshness comes from the backend task output presenter; this only puts it
 * into words next to the step status, so an approved deliverable on a failed
 * step no longer reads as a contradiction.
 */
export function deliverableRelationNote(stepStatus: string, freshness: TaskDeliverableFreshness | null): string | null {
  if (freshness?.state === 'historical') return `${freshness.label}. A later attempt produced a newer output.`;
  if (freshness?.state === 'stale') return `${freshness.label}. A later attempt of this step is replacing it.`;
  if (STOPPED_STEP_STATUSES.has(stepStatus)) {
    return 'The step wrote this deliverable before it stopped. It is still the latest output of this step.';
  }
  return null;
}
