import { afterEach, describe, expect, it } from 'vitest';

import { isTextInputTarget, taskAgentDock, taskDockWorkKey } from './taskAgentDock.svelte';

afterEach(() => taskAgentDock.reset());

describe('taskAgentDock', () => {
  it('keeps presentation state only and returns to the minimized default', () => {
    taskAgentDock.open('work');
    expect(taskAgentDock.state).toBe('open');
    expect(taskAgentDock.tab).toBe('work');
    taskAgentDock.expand();
    expect(taskAgentDock.state).toBe('fullscreen');
    taskAgentDock.reset();
    expect(taskAgentDock.state).toBe('minimized');
    expect(taskAgentDock.tab).toBe('chat');
    expect(taskAgentDock).not.toHaveProperty('taskId');
  });

  it('guards the A shortcut in editable controls', () => {
    expect(isTextInputTarget(document.createElement('textarea'))).toBe(true);
    expect(isTextInputTarget(document.createElement('button'))).toBe(false);
  });

  it('opens visible Work with the exact requested task scope and category', () => {
    const scope = {
      key: 'task_step:run-1',
      kind: 'task_step' as const,
      step_run_id: 'run-1',
      conversation_id: 'conversation-1',
      session_id: 'session-descendant-b',
    };
    taskAgentDock.openWork(scope, 'mutations', 'session-a');
    expect(taskAgentDock.state).toBe('open');
    expect(taskAgentDock.tab).toBe('work');
    expect(taskAgentDock.workScope).toEqual(scope);
    expect(taskAgentDock.workCategory).toBe('mutations');
    expect(taskAgentDock.workSessionId).toBe('session-a');
    taskAgentDock.open('chat');
    expect(taskAgentDock.workScope).toEqual(scope);
    expect(taskAgentDock.workCategory).toBe('mutations');
    taskAgentDock.openWork(scope, 'commands');
    expect(taskAgentDock.workSessionId).toBeNull();
  });

  it('changes the mounted Work key for A to B to unfiltered transitions', () => {
    const scope = {
      key: 'task_step:run-1',
      kind: 'task_step' as const,
      step_run_id: 'run-1',
      conversation_id: 'conversation-1',
      session_id: 'session-descendant-b',
    };
    const keys = [
      taskDockWorkKey(scope, 'mutations', 'session-a'),
      taskDockWorkKey(scope, 'mutations', 'session-b'),
      taskDockWorkKey(scope, 'mutations', null),
    ];
    expect(new Set(keys).size).toBe(3);
    expect(keys[0]).toContain('session-a');
    expect(keys[1]).toContain('session-b');
    expect(keys[2]).toContain(':all');
  });
});
