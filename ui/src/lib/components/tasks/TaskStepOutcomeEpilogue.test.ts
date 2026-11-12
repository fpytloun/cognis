import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '$lib/api/client';
import type { Deliverable, StepRun } from '$lib/types/api';
import TaskStepOutcomeEpilogue from './TaskStepOutcomeEpilogue.svelte';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const document: Deliverable = {
  deliverable_id: 'historical-task-document', step_run_id: 'historical-run',
  version: 1, attempt_number: 1, lineage_id: 'historical-task-document', revision: 1,
  format: 'markdown', title: 'Previous result', content: 'Historical document body.',
  target: 'none', outputs: {}, status: 'rejected', evaluator_feedback: null,
  created_at: null, updated_at: null,
};
const step: StepRun = {
  step_run_id: 'historical-run', step_name: 'publish', task_id: 'revision-task',
  status: 'running', attempt: 2, attempt_number: 1, agent_id: 'revision-agent',
  step_type: 'run', superseded_by_step_run_id: null, workspace_root: null,
  working_directory: null, conversation_id: null, session_id: null,
  intaris_session_id: null, deliverable_id: null, require_deliverable: true,
  evaluation: null, runtime_info: null, todos: [], started_at: null,
  completed_at: null, updated_at: null,
  deliverables: [document], output: { summary: 'Work continues after publication.' },
  deliverable_freshness: { [document.deliverable_id]: {
    state: 'stale', replacement_status: 'running', label: 'Stale — revision 1, new revision in progress',
  } },
};

it('retains a collapsed readable pre-change document until its canonical event is present', async () => {
  vi.spyOn(api.deliverables, 'getForStepRun').mockResolvedValue(document);
  const { rerender } = render(TaskStepOutcomeEpilogue, { stepRun: step });
  expect(await screen.findByText('Stale — revision 1, new revision in progress')).toBeVisible();
  expect(screen.queryByText('Historical document body.')).toBeNull();
  await fireEvent.click(await screen.findByRole('button', { name: 'Expand document' }));
  expect(await screen.findByText('Historical document body.')).toBeVisible();
  await rerender({ stepRun: step, timelineDeliverableIds: [document.deliverable_id] });
  expect(screen.queryByTestId('rich-deliverable')).toBeNull();
  expect(screen.getByText('Work continues after publication.')).toBeVisible();
});

it('explains a historical document even when its timeline event suppresses the fallback and no metadata remains', () => {
  render(TaskStepOutcomeEpilogue, {
    stepRun: { ...step, status: 'failed', output: null },
    timelineDeliverableIds: [document.deliverable_id],
  });
  expect(screen.getByRole('status')).toHaveTextContent('Stale — revision 1, new revision in progress');
  expect(screen.getByText('Document v1 · rejected')).toBeVisible();
  expect(screen.queryByTestId('rich-deliverable')).toBeNull();
});
