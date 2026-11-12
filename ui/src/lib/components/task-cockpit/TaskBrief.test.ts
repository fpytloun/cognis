import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { TaskDetail } from '$lib/types/api';
import TaskBrief from './TaskBrief.svelte';

afterEach(cleanup);

describe('TaskBrief', () => {
  it('keeps the full input and output contract visible while collapsing technical metadata', () => {
    const task = {
      task_id: 'task-39',
      description: 'Investigate the production alert.\nPreserve all evidence.',
      expected_output: 'A verified mitigation with exact validation.',
      attempt_number: 2,
      source_type: 'conversation',
      source_ref: 'conversation-39',
      created_by: 'filip@example.com',
      created_at: '2026-07-30T09:00:00Z',
      updated_at: '2026-07-30T10:00:00Z',
      started_at: '2026-07-30T09:05:00Z',
      completed_at: null,
      queue_name: 'default'
    } as TaskDetail;

    render(TaskBrief, { task, workflowLabel: 'Production triage', projectLabel: 'Operations', agentLabel: 'Lumi' });

    expect(screen.getByTestId('task-brief')).toHaveTextContent('Investigate the production alert.');
    expect(screen.getByTestId('task-brief')).toHaveTextContent('A verified mitigation');
    expect(screen.getByTestId('task-brief')).toHaveTextContent('Production triage');
    expect(screen.getByTestId('task-brief')).toHaveTextContent('Attempt');
    expect(screen.getByText('Technical metadata').closest('details')?.open).toBe(false);
    expect(screen.queryByRole('button', { name: 'Show full brief' })).toBeNull();
  });

  it('previews a long brief and expands it on request', async () => {
    const task = {
      task_id: 'task-long',
      description: 'Release step. '.repeat(80),
      expected_output: 'Verified release.',
      attempt_number: 1,
      source_type: 'schedule',
      source_ref: null,
      created_by: 'filip@example.com',
      created_at: null,
      updated_at: null,
      started_at: null,
      completed_at: null,
      queue_name: 'default'
    } as unknown as TaskDetail;

    render(TaskBrief, { task, workflowLabel: 'General task', projectLabel: 'Cognis', agentLabel: 'LaForge' });

    expect(screen.getByTestId('task-brief-body')).toHaveAttribute('data-collapsed', 'true');
    await fireEvent.click(screen.getByRole('button', { name: 'Show full brief' }));
    expect(screen.getByTestId('task-brief-body')).toHaveAttribute('data-collapsed', 'false');
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true');
  });
});
