export const TERMINAL_SESSION_STATUSES = new Set([
  'completed',
  'failed',
  'cancelled',
  'terminated',
]);

export function isTerminalSessionStatus(status: string | null | undefined): boolean {
  return TERMINAL_SESSION_STATUSES.has(status ?? '');
}

/**
 * Additive, server-owned execution lifecycle state for a workstream node.
 * Optional: when absent, callers fall back to the existing
 * status/activity_state/runtime overlay heuristics.
 */
export type ExecutionState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'waiting'
  | 'recovering'
  | 'completed'
  | 'failed'
  | 'cancelled';

const ACTIVE_EXECUTION_STATES = new Set<ExecutionState>(['queued', 'running', 'waiting', 'recovering']);
const TERMINAL_EXECUTION_STATES = new Set<ExecutionState>(['completed', 'failed', 'cancelled']);

export function isActiveExecutionState(state: ExecutionState | null | undefined): boolean {
  return Boolean(state && ACTIVE_EXECUTION_STATES.has(state));
}

export function isTerminalExecutionState(state: ExecutionState | null | undefined): boolean {
  return Boolean(state && TERMINAL_EXECUTION_STATES.has(state));
}

export function isEffectiveSessionRunning(
  status: string | null | undefined,
  activityState: string | null | undefined,
  runtimeActive = false,
  executionState?: ExecutionState | null,
): boolean {
  if (executionState) return isActiveExecutionState(executionState);
  return activityState !== 'closed'
    && !isTerminalSessionStatus(status)
    && (activityState === 'ongoing' || runtimeActive);
}

export type DisplayStatus =
  | 'Idle'
  | 'Queued'
  | 'Running'
  | 'Waiting'
  | 'Recovering'
  | 'Active'
  | 'Closed'
  | 'Completed'
  | 'Failed'
  | 'Cancelled';

export function displaySessionStatus(
  status: string | null | undefined,
  activityState: string | null | undefined,
  runtimeActive = false,
  executionState?: ExecutionState | null,
): DisplayStatus {
  // When present, the server-owned execution_state is authoritative and
  // distinguishes queued/waiting/recovering plus distinct
  // completed/failed/cancelled terminal outcomes, superseding the coarser
  // status/activity_state pair it was derived from.
  if (executionState) {
    switch (executionState) {
      case 'idle': return 'Idle';
      case 'queued': return 'Queued';
      case 'running': return 'Running';
      case 'waiting': return 'Waiting';
      case 'recovering': return 'Recovering';
      case 'completed': return 'Completed';
      case 'failed': return 'Failed';
      case 'cancelled': return 'Cancelled';
    }
  }
  if (activityState === 'closed' || isTerminalSessionStatus(status)) {
    if (status === 'failed') return 'Failed';
    if (status === 'cancelled') return 'Cancelled';
    return 'Closed';
  }
  return isEffectiveSessionRunning(status, activityState, runtimeActive, executionState)
    ? 'Running'
    : 'Active';
}

export function updateRuntimeActiveSessions(
  current: Set<string>,
  sessionId: string,
  active: boolean,
  status: string | null | undefined,
  activityState: string | null | undefined,
): Set<string> {
  const next = new Set(current);
  if (active && !isTerminalSessionStatus(status) && activityState !== 'closed') {
    next.add(sessionId);
  } else {
    next.delete(sessionId);
  }
  return next;
}
