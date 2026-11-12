import { describe, expect, it } from 'vitest';
import { humanTaskReason, taskStoppedSummary } from './task-attention';

describe('task attention reasons', () => {
  it('explains the delegation recovery stop in plain language and keeps the original as detail', () => {
    const raw = 'Interrupted child execution could not be attached to its owning delegation.';
    expect(humanTaskReason(raw)).toEqual({
      reason: 'The agent was interrupted while a helper agent was working, and the helper could not be reconnected.',
      detail: raw
    });
  });

  it('keeps an agent-written summary as the reason', () => {
    expect(humanTaskReason('Partner feed rejected the credentials.')).toEqual({ reason: 'Partner feed rejected the credentials.', detail: null });
  });

  it('summarises only stopped tasks', () => {
    expect(taskStoppedSummary({ status: 'completed', result_summary: 'done' })).toBeNull();
    expect(taskStoppedSummary({ status: 'failed', result_summary: null })).toMatchObject({ tone: 'danger', title: 'Task failed' });
    expect(taskStoppedSummary({ status: 'paused', result_summary: null })?.reason).toMatch(/waiting for you to resume/);
  });
});
