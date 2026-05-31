import { describe, expect, it } from 'vitest';
import { skillLoadDisplayName, stepTodoWriteStatusSummary } from './tool-call-summary';

describe('tool call summaries', () => {
  it('keeps skill_load focused on the resolved skill name', () => {
    expect(skillLoadDisplayName({
      toolName: 'skill_load',
      status: 'completed',
      result: JSON.stringify({
        name: 'Cognis Coding',
        loaded: true,
        tool_count: 2
      })
    })).toBe('Cognis Coding');
  });

  it('summarizes step_todo_write active and pending todos from the result', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        status: 'updated',
        todos: [
          { content: 'Implement summary', status: 'in_progress' },
          { content: 'Run tests', status: 'pending' },
          { content: 'Commit', status: 'pending' }
        ]
      })
    })).toBe('1 active, 2 pending');
  });

  it('uses explicit step_todo_write status summaries from the backend when present', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        status_summary: '1 active, 2 pending'
      })
    })).toBe('1 active, 2 pending');
  });

  it('falls back to step_todo_write arguments while running', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'started',
      arguments: {
        todos: [
          { content: 'Implement summary', status: 'in_progress' },
          { content: 'Run tests', status: 'pending' }
        ]
      }
    })).toBe('1 active, 1 pending');
  });

  it('includes terminal todo states when present', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        todos: [
          { content: 'Implement summary', status: 'completed' },
          { content: 'Skip unrelated cleanup', status: 'cancelled' }
        ]
      })
    })).toBe('1 completed, 1 cancelled');
  });
});
