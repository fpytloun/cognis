import { confirmAction } from '$lib/stores/confirm';

export type TaskPrimaryAction = 'answer' | 'resume' | 'retry' | 'revise' | 'rerun' | 'configure' | 'actions';

/** The one action a task's state asks for; every surface uses the same verb. */
export function taskPrimaryAction(
  status: string,
  options: { hasAttention: boolean; hasWorkflow: boolean; rerunnable: boolean; editable: boolean }
): TaskPrimaryAction {
  if (status === 'paused') return options.hasAttention ? 'answer' : 'resume';
  if (status === 'failed' && options.rerunnable) return 'retry';
  if (status === 'completed' && options.hasWorkflow) return 'revise';
  if (options.rerunnable) return 'rerun';
  if (options.editable) return 'configure';
  return 'actions';
}

export function taskPrimaryActionLabel(action: TaskPrimaryAction): string {
  return {
    answer: 'Review decision',
    resume: 'Resume task',
    retry: 'Retry from failed step',
    revise: 'Revise result',
    rerun: 'Start a new run',
    configure: 'Configure',
    actions: 'Actions'
  }[action];
}

/** Starting a new run creates a separate task; the current one keeps its state. */
export function confirmNewRun(status: string): Promise<boolean> {
  return confirmAction({
    title: 'Start a new run?',
    message: `This creates a new task with the same brief. This task stays ${status}.`,
    confirmLabel: 'Start new run',
    cancelLabel: 'Not now',
    variant: 'primary'
  });
}

export function confirmPause(): Promise<boolean> {
  return confirmAction({
    title: 'Pause task?',
    message: 'The agent stops after its current action. You can resume the task later.',
    confirmLabel: 'Pause task',
    cancelLabel: 'Keep running',
    variant: 'primary'
  });
}

export function confirmCancel(): Promise<boolean> {
  return confirmAction({
    title: 'Cancel task?',
    message: 'The task stops and is marked cancelled. This cannot be undone; completed output stays available.',
    confirmLabel: 'Cancel task',
    cancelLabel: 'Keep task',
    variant: 'danger'
  });
}
