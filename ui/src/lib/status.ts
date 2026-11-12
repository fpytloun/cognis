/** Product status vocabulary. Unknown/stale are neutral, never healthy. */
type StatusTone = 'danger' | 'warning' | 'success' | 'info' | 'unknown';
const styles: Record<StatusTone, { textClass: string; fillClass: string; className: string }> = {
  danger: { textClass: 'text-danger', fillClass: 'bg-danger', className: 'text-danger bg-danger-subtle border-danger-border' },
  warning: { textClass: 'text-warning', fillClass: 'bg-warning', className: 'text-warning bg-warning-subtle border-warning-border' },
  success: { textClass: 'text-success', fillClass: 'bg-success', className: 'text-success bg-success-subtle border-success-border' },
  info: { textClass: 'text-info', fillClass: 'bg-info', className: 'text-info bg-info-subtle border-info-border' },
  unknown: { textClass: 'text-secondary', fillClass: 'bg-muted', className: 'text-secondary' }
};
function presentation(label: string, icon: string, tone: StatusTone) {
  const statTone: 'danger' | 'warning' | 'unknown' | 'neutral' = tone === 'danger' ? 'danger' : tone === 'warning' ? 'warning' : tone === 'unknown' ? 'unknown' : 'neutral';
  return { label, icon, tone, statTone, ...styles[tone] };
}
export type StatusPresentation = ReturnType<typeof presentation>;

export function statusPresentation(status: string): StatusPresentation {
  switch (status) {
    case 'failed': case 'error': case 'denied':
      return presentation('Failed', '×', 'danger');
    case 'degraded': case 'partial': case 'warning':
      return presentation(status === 'partial' ? 'Partial data' : 'Degraded', '!', 'warning');
    case 'at_risk':
      return presentation('Near limit', '!', 'warning');
    case 'ready': case 'completed': case 'succeeded':
      return presentation(status === 'ready' ? 'Ready' : 'Completed', '✓', 'success');
    case 'observed_start':
      return presentation('Outcome unknown', '?', 'unknown');
    case 'inflight': case 'running': case 'pending': case 'active':
      return presentation('In progress', '↻', 'info');
    case 'evaluating':
      return presentation('Evaluating', '↻', 'info');
    case 'approved':
      return presentation('Approved', '✓', 'success');
    case 'rejected':
      return presentation('Sent back', '↺', 'warning');
    case 'paused':
      return presentation('Paused', '‖', 'warning');
    case 'waiting':
      return presentation('Waiting', '‖', 'warning');
    case 'queued':
      return presentation('Queued', '·', 'unknown');
    case 'draft':
      return presentation('Draft', '·', 'unknown');
    case 'skipped':
      return presentation('Skipped', '–', 'unknown');
    // Document comments: what the agent has done with a comment.
    case 'comment_not_sent': return presentation('Not sent', '•', 'unknown');
    case 'comment_sent': return presentation('Sent', '↻', 'info');
    case 'comment_answered': return presentation('Answered', '✓', 'success');
    case 'comment_resolved': return presentation('Resolved', '✓', 'unknown');
    case 'comment_delivery_unknown': return presentation('Checking delivery', '?', 'unknown');
    case 'comment_not_delivered': return presentation('Not delivered', '×', 'danger');
    case 'stale': return presentation('Stale', '–', 'unknown');
    case 'disabled': return presentation('Disabled', '–', 'unknown');
    case 'cancelled': return presentation('Cancelled', '–', 'unknown');
    default: return presentation('Unknown', '?', 'unknown');
  }
}

const OPEN_STEP_STATUSES = ['active', 'running', 'waiting', 'evaluating'];

/**
 * Workflow step or phase status within its task. A step that has not started
 * is "Not started"; an open step of a paused or cancelled task shows the
 * task's state instead of a live one.
 */
export function stepStatusPresentation(stepStatus: string, taskStatus: string): StatusPresentation {
  if (stepStatus === 'pending') return presentation('Not started', '·', 'unknown');
  if (OPEN_STEP_STATUSES.includes(stepStatus) && (taskStatus === 'paused' || taskStatus === 'cancelled' || taskStatus === 'failed')) {
    return statusPresentation(taskStatus);
  }
  return statusPresentation(stepStatus);
}

/** Elapsed time is meaningful for finished steps and steps that are still running. */
export function stepShowsDuration(stepStatus: string, taskStatus: string): boolean {
  if (!OPEN_STEP_STATUSES.includes(stepStatus)) return stepStatus !== 'pending';
  return taskStatus === 'running' || taskStatus === 'evaluating';
}
