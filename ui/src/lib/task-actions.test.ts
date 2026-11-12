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

  it('offers a retry from the failed step, not a revision, for a failed task', () => {
    const action = taskPrimaryAction('failed', { hasAttention: false, hasWorkflow: true, rerunnable: true, editable: false });
    expect(taskPrimaryActionLabel(action)).toBe('Retry from failed step');
  });

  it('resumes a paused task that has no pending question', () => {
    const action = taskPrimaryAction('paused', { hasAttention: false, hasWorkflow: true, rerunnable: true, editable: true });
    expect(taskPrimaryActionLabel(action)).toBe('Resume task');
  });
});
