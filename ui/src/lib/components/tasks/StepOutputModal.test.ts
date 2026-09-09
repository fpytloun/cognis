import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { StepRun } from '$lib/types/api';
import StepOutputModal from './StepOutputModal.svelte';

vi.mock('$lib/api/client', () => ({
  api: {
    deliverables: {
      getForStepRun: vi.fn(),
    },
  },
}));

function runWithoutDeliverables(): StepRun {
  return {
    step_run_id: 'run-projection',
    task_id: 'task-1',
    step_name: 'prepare',
    step_type: 'run',
    status: 'completed',
    agent_id: 'riker',
    attempt: 1,
    is_projection: false,
    output: {
      summary: 'Hydrated output without a deliverable collection.',
      content: 'Result body',
    },
    evaluation: null,
    runtime_info: null,
    todos: [],
    started_at: null,
    completed_at: null,
    updated_at: null,
  } as unknown as StepRun;
}

describe('StepOutputModal', () => {
  it('renders an output whose partial projection omits deliverables', async () => {
    const escapedToParent = vi.fn();
    window.addEventListener('keydown', escapedToParent);
    const view = render(StepOutputModal, {
      stepRun: runWithoutDeliverables(),
      agentName: 'Riker',
      visibleStatus: 'completed',
      onclose: vi.fn(),
    });

    expect(await screen.findByRole('dialog', { name: 'Step output: prepare' })).toBeVisible();
    expect(screen.getByText('Hydrated output without a deliverable collection.')).toBeVisible();
    expect(screen.queryByText(/Deliverable v/)).not.toBeInTheDocument();
    await fireEvent.keyDown(document, { key: 'Escape' });
    expect(escapedToParent).not.toHaveBeenCalled();
    view.unmount();
    window.removeEventListener('keydown', escapedToParent);
    await Promise.resolve();
  });
});
