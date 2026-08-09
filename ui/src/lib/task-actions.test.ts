import { describe, expect, it } from 'vitest';
import { taskPrimaryAction, taskPrimaryActionLabel } from './task-actions';

describe('task action priority', () => {
  it('prioritizes answering a pause over re-running on mobile', () => {
    const action = taskPrimaryAction('paused', {
      hasAttention: true,
      hasWorkflow: true,
      rerunnable: true,
      editable: true
    });
    expect(action).toBe('answer');
    expect(taskPrimaryActionLabel(action)).toBe('Review decision');
  });

  it('makes revision the primary terminal workflow action', () => {
    const action = taskPrimaryAction('completed', {
      hasAttention: false,
      hasWorkflow: true,
      rerunnable: true,
      editable: false
    });
    expect(action).toBe('revise');
    expect(taskPrimaryActionLabel(action)).toBe('Revise result');
  });
});
