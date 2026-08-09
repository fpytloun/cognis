import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import TaskProgressPanel from './TaskProgressPanel.svelte';

afterEach(cleanup);

describe('TaskProgressPanel', () => {
  it('renders current todos and nested work without opening logs', () => {
    render(TaskProgressPanel, {
      projection: {
        todos: [
          { content: 'Collect evidence', status: 'completed' },
          { content: 'Validate mitigation', status: 'in_progress' }
        ],
        work_items: [{
          kind: 'managed_conversation',
          work_id: 'work-39',
          step_name: 'investigate',
          step_run_id: 'run-39',
          title: 'Independent verification',
          agent_id: 'lumi',
          status: 'running',
          result_summary: null,
          error: null,
          todos: [{ content: 'Check metrics', status: 'completed' }],
          conversation_id: 'conversation-39'
        }],
        active_count: 1,
        completed_count: 2,
        truncated: false
      }
    });

    expect(screen.getByTestId('task-progress')).toHaveTextContent('Validate mitigation');
    expect(screen.getByTestId('task-progress')).toHaveTextContent('Independent verification');
    expect(screen.getByText('1 active')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open Independent verification' })).toHaveAttribute('href', '/chat/conversation-39');
  });

  it('gracefully explains an absent backend projection', () => {
    render(TaskProgressPanel, { projection: undefined });
    expect(screen.getByText(/Live progress is not available/)).toBeTruthy();
  });
});
