export type TaskPrimaryAction = 'answer' | 'resume' | 'revise' | 'rerun' | 'configure' | 'actions';

export function taskPrimaryAction(
  status: string,
  options: { hasAttention: boolean; hasWorkflow: boolean; rerunnable: boolean; editable: boolean }
): TaskPrimaryAction {
  if (status === 'paused') return options.hasAttention ? 'answer' : 'resume';
  if (['completed', 'failed', 'cancelled'].includes(status) && options.hasWorkflow) return 'revise';
  if (options.rerunnable) return 'rerun';
  if (options.editable) return 'configure';
  return 'actions';
}

export function taskPrimaryActionLabel(action: TaskPrimaryAction): string {
  return {
    answer: 'Review decision',
    resume: 'Resume task',
    revise: 'Revise result',
    rerun: 'Re-run task',
    configure: 'Configure',
    actions: 'Actions'
  }[action];
}
