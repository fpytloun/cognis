import type { TimelineScope, WorkCategory } from '$lib/chat-v2/types';
import type { WorkViewTab } from '$lib/work/workViewState';

export type TaskAgentDockState = 'minimized' | 'open' | 'fullscreen';
export type TaskAgentDockTab = 'chat' | 'work';

class TaskAgentDockStore {
  state = $state<TaskAgentDockState>('minimized');
  tab = $state<TaskAgentDockTab>('chat');
  workScope = $state<TimelineScope | null>(null);
  workCategory = $state<WorkViewTab>('files');
  workSessionId = $state<string | null>(null);

  open(tab: TaskAgentDockTab = 'chat'): void {
    this.tab = tab;
    this.state = 'open';
  }

  openWork(scope: TimelineScope, category: WorkCategory = 'files', sessionId?: string): void {
    this.workScope = scope;
    this.workCategory = category === 'deliverables' ? 'results' : category;
    this.workSessionId = sessionId ?? null;
    this.open('work');
  }

  minimize(): void {
    this.state = 'minimized';
  }

  expand(): void {
    this.state = 'fullscreen';
  }

  reset(): void {
    this.state = 'minimized';
    this.tab = 'chat';
    this.workScope = null;
    this.workCategory = 'files';
    this.workSessionId = null;
  }
}

export const taskAgentDock = new TaskAgentDockStore();

export function taskDockWorkKey(
  scope: TimelineScope,
  category: WorkViewTab,
  sessionId: string | null,
): string {
  return `${scope.key}:${category}:${sessionId ?? 'all'}`;
}

export function isTextInputTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
}
