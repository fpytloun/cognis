import { describe, expect, it } from 'vitest';
import { skillLoadDisplayName, stepTodoWriteStatusSummary, workflowToolPresentation } from './tool-call-summary';

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

  it('builds rich write_deliverable presentation only for successful calls', () => {
    expect(workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'completed',
      arguments: {
        title: 'Implementation report',
        content: '# Done\n\nValidation passed.',
        format: 'markdown',
        outputs: { files: ['ui/src/lib/tool-call-summary.ts'] }
      },
      result: JSON.stringify({
        status: 'buffered',
        deliverable_id: 'dlv_123',
        version: 2,
        length: 27
      })
    })).toMatchObject({
      kind: 'write_deliverable',
      title: 'Implementation report',
      deliverableId: 'dlv_123',
      version: 2,
      outputKeys: ['files']
    });

    expect(workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'failed',
      isError: true,
      arguments: { content: '# Not rendered' },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });

  it('builds rich step_complete presentation only for successful calls', () => {
    expect(workflowToolPresentation({
      toolName: 'step_complete',
      status: 'completed',
      arguments: {
        summary: 'Implemented rich workflow tool cards.',
        outcome: { status: 'success' },
        claims: ['Successful calls are rendered richly'],
        outputs: { changed_files: ['ToolCallBlock.svelte'] },
        metadata: { reviewed: true },
        notification: { mode: 'direct' }
      },
      result: JSON.stringify({ status: 'completed' })
    })).toMatchObject({
      kind: 'step_complete',
      summary: 'Implemented rich workflow tool cards.',
      outcomeStatus: 'success',
      claims: ['Successful calls are rendered richly'],
      outputs: [
        { key: 'changed_files', value: ['ToolCallBlock.svelte'] }
      ],
      metadata: [
        { key: 'reviewed', value: true }
      ],
      outputKeys: ['changed_files'],
      metadataKeys: ['reviewed'],
      notificationMode: 'direct'
    });

    expect(workflowToolPresentation({
      toolName: 'step_complete',
      status: 'completed',
      isError: true,
      arguments: { summary: 'Rejected completion' },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });

  it('builds rich step_todo_write presentation only for successful calls', () => {
    expect(workflowToolPresentation({
      toolName: 'step_todo_write',
      status: 'completed',
      arguments: {
        todos: [
          { content: 'Inspect payloads', status: 'completed' },
          { content: 'Run validation', status: 'in_progress' }
        ]
      },
      result: JSON.stringify({
        status: 'updated',
        count: 2,
        todos: [
          { content: 'Inspect payloads', status: 'completed' },
          { content: 'Run validation', status: 'in_progress' }
        ],
        unchanged: false,
        non_terminal_count: 1,
        guidance: '1 todo remains open.'
      })
    })).toMatchObject({
      kind: 'step_todo_write',
      status: 'updated',
      count: 2,
      statusSummary: '1 active, 1 completed',
      guidance: '1 todo remains open.',
      unchanged: false,
      nonTerminalCount: 1,
      todos: [
        { content: 'Inspect payloads', status: 'completed' },
        { content: 'Run validation', status: 'in_progress' }
      ]
    });

    expect(workflowToolPresentation({
      toolName: 'step_todo_write',
      status: 'failed',
      isError: true,
      arguments: {
        todos: [
          { content: 'Not rendered richly', status: 'pending' }
        ]
      },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });
});
