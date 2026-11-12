/**
 * Plain-language reasons for tasks that stopped and need a person.
 * The backend reports recovery and runtime reasons in engineering terms; the
 * original text stays available as technical detail.
 */
const KNOWN_REASONS: Array<[RegExp, string]> = [
  [
    /interrupted child execution could not be attached to its owning delegation/i,
    'The agent was interrupted while a helper agent was working, and the helper could not be reconnected.'
  ],
  [/executor .*(unavailable|offline|disconnected)/i, 'The machine running this task went offline.'],
  [/(timed out|timeout)/i, 'The step ran out of time.']
];

export type TaskAttentionReason = { reason: string; detail: string | null };

export function humanTaskReason(text: string | null | undefined): TaskAttentionReason | null {
  const value = text?.trim();
  if (!value) return null;
  for (const [pattern, reason] of KNOWN_REASONS) {
    if (pattern.test(value)) return { reason, detail: value };
  }
  return { reason: value, detail: null };
}

export type TaskStoppedSummary = {
  tone: 'danger' | 'warning';
  title: string;
  reason: string;
  detail: string | null;
};

/** Headline for a task that stopped without a pending question or gate. */
export function taskStoppedSummary(task: {
  status: string;
  result_summary?: string | null;
  applied_completion_reason?: string | null;
}): TaskStoppedSummary | null {
  if (task.status !== 'failed' && task.status !== 'paused') return null;
  const human = humanTaskReason(task.result_summary || task.applied_completion_reason);
  return {
    tone: task.status === 'failed' ? 'danger' : 'warning',
    title: task.status === 'failed' ? 'Task failed' : 'Task paused',
    reason: human?.reason ?? (task.status === 'failed'
      ? 'The task could not finish. Check the latest step, then retry.'
      : 'The task stopped and is waiting for you to resume it.'),
    detail: human?.detail ?? null
  };
}
