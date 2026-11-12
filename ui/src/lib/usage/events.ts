/** Existing authenticated events are hints, not authorization or accounting facts. */
export function usageEventAction(event: { type: string; reason?: string | null; metadata_only?: boolean }): 'clear' | 'refresh' | null {
  if (event.type === 'scope_invalidated') {
    if (!event.metadata_only) return 'clear';
    return ['chat_scope_changed', 'sidebar_changed', 'task_progress_changed'].includes(event.reason ?? '') ? 'refresh' : null;
  }
  return ['turn_settled', 'workflow_step_completed', 'workflow_step_failed', 'workflow_completed', 'workflow_failed', 'workflow_cancelled', 'reconnected'].includes(event.type) ? 'refresh' : null;
}
